#!/usr/bin/env python3
"""Execute the formal publisher state machine only in a sentinel-bound copy.

The real Quantification workspace is always the evidence source and is never a
publication destination.  The command is disabled unless ``--execute-isolated``
is supplied, and even then the destination must be one direct child of the
dedicated tmp isolation root with an exact sentinel and translated catalog.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TOOLS = Path(__file__).resolve().parent
CATALOG_LIB = ROOT / "data/catalog"
for value in (TOOLS, CATALOG_LIB):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

import atomic_snapshot_publish as atomic  # noqa: E402
import dataset_cli  # noqa: E402
import run_formal_publisher_preflight as preflight_module  # noqa: E402
import validate_dataset_catalog  # noqa: E402
import validate_formal_atomic_publish_execution as execution_contract  # noqa: E402
from snapshot_contract import (  # noqa: E402
    AUTHORITY_RELATIVE_PATHS,
    HASH_ALGORITHM,
    request_contract_hash,
    snapshot_contract_hash,
    verify_snapshot_contract,
)


CONTRACT_ID = "formal_atomic_publish_isolated_executor_v1"
PUBLISHER_ID = "execute_formal_atomic_publish_isolated.py/v1"
ISOLATED_ROOT = ROOT / "tmp/formal-publisher-isolated-workspaces"
SENTINEL_NAME = ".qdata-formal-publisher-isolated-workspace.json"
LOCK_NAME = ".datasets.json.formal.lock"
ALLOWED_FAILPOINTS = {
    "hard_crash_after_private_staging",
    "hard_crash_after_payload_finalize",
    "hard_crash_after_catalog_visible",
    "hard_crash_after_qdata_success",
}


class IsolatedExecutionError(RuntimeError):
    """Fail-closed isolated execution error."""


class QdataPostconditionError(IsolatedExecutionError):
    """Raised after a qdata postcondition fails and rollback is required."""


@dataclass(frozen=True)
class Paths:
    workspace: Path
    catalog: Path
    market: Path
    stage: Path
    raw_final: Path
    curated_final: Path
    snapshot_final: Path
    journal: Path
    ledger: Path
    lock: Path
    rollback: Path


def _pretty_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha(payload: bytes) -> str:
    return execution_contract.sha256_bytes(payload)


def _path_text(path: Path) -> str:
    return str(path.resolve(strict=False)).replace("\\", "/")


def _load_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        value, payload = preflight_module.load_json_object(path, label)
    except preflight_module.PreflightError as exc:
        raise IsolatedExecutionError(str(exc)) from exc
    return value, payload


def _assert_isolated_workspace(
    workspace: Path,
    source_catalog_sha256: str,
) -> dict[str, Any]:
    workspace = Path(os.path.abspath(workspace))
    isolated_root = Path(os.path.abspath(ISOLATED_ROOT))
    try:
        child = workspace.relative_to(isolated_root)
    except ValueError as exc:
        raise IsolatedExecutionError(
            "isolated workspace must be below the dedicated tmp isolation root"
        ) from exc
    if len(child.parts) != 1 or workspace == Path(os.path.abspath(ROOT)):
        raise IsolatedExecutionError("isolated workspace must be one dedicated child")
    atomic.assert_path_under_workspace(workspace, isolated_root, "isolated_workspace")
    if not workspace.is_dir() or atomic.is_reparse_point(workspace):
        raise IsolatedExecutionError("isolated workspace must be a plain directory")
    sentinel_path = workspace / SENTINEL_NAME
    if not sentinel_path.is_file() or atomic.is_reparse_point(sentinel_path):
        raise IsolatedExecutionError("isolated workspace sentinel is missing or unsafe")
    sentinel, _ = _load_object(sentinel_path, "isolated workspace sentinel")
    expected = {
        "schema_version": 1,
        "contract_id": "formal_publisher_isolated_workspace_v1",
        "workspace_id": child.name,
        "workspace_root": workspace.as_posix(),
        "source_workspace_root": ROOT.as_posix(),
        "source_catalog_sha256": source_catalog_sha256,
        "allow_isolated_formal_execution": True,
    }
    if sentinel != expected:
        raise IsolatedExecutionError("isolated workspace sentinel binding mismatch")
    catalog_path = workspace / "data/catalog/datasets.json"
    if not catalog_path.is_file() or atomic.is_reparse_point(catalog_path):
        raise IsolatedExecutionError("isolated catalog is missing or unsafe")
    catalog = atomic.load_json(catalog_path)
    if Path(str(catalog.get("workspace_root", "")).replace("/", os.sep)).resolve() != workspace.resolve():
        raise IsolatedExecutionError("isolated catalog workspace_root mismatch")
    errors = validate_dataset_catalog.validate_catalog(catalog)
    if errors:
        raise IsolatedExecutionError("isolated catalog is invalid: " + " | ".join(errors))
    return catalog


def _authorization_value(plan: dict[str, Any]) -> dict[str, Any]:
    preflight_path = ROOT.joinpath(
        *PurePosixPath(plan["preflight"]["relative_path"]).parts
    )
    return execution_contract._load_preflight_evidence_value(  # noqa: SLF001
        preflight_path,
        "user_authorization_receipt",
        "user authorization receipt",
    )


def _build_paths(
    workspace: Path,
    plan: dict[str, Any],
    authorization_id: str,
) -> Paths:
    execution_id = plan["execution_id"]
    dataset_id = plan["target"]["dataset_id"]
    snapshot_id = plan["target"]["snapshot_id"]
    market = workspace / "data/market"
    return Paths(
        workspace=workspace,
        catalog=workspace / "data/catalog/datasets.json",
        market=market,
        stage=market / ".formal-staging" / dataset_id / execution_id,
        raw_final=market / "raw" / dataset_id / snapshot_id,
        curated_final=market / "curated" / dataset_id / snapshot_id,
        snapshot_final=workspace.joinpath(
            *PurePosixPath(plan["target"]["target_relative_path"]).parts
        ),
        journal=market / "transactions" / f"formal-{execution_id}.json",
        ledger=market / "authorization-ledger" / f"{authorization_id}.json",
        lock=workspace / "data/catalog" / LOCK_NAME,
        rollback=market / "rollback" / execution_id,
    )


def _write_exclusive_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as exc:
        raise IsolatedExecutionError(f"exclusive state already exists: {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_pretty_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _write_journal(paths: Paths, plan: dict[str, Any], state: str, **extra: Any) -> None:
    current: dict[str, Any] = {}
    if paths.journal.exists():
        current = atomic.load_json(paths.journal)
        immutable = {
            "publisher_id": PUBLISHER_ID,
            "execution_id": plan["execution_id"],
            "request_sha256": plan["request"]["sha256"],
            "execution_plan_sha256": plan["execution_plan_sha256"],
            "dataset_id": plan["target"]["dataset_id"],
            "snapshot_id": plan["target"]["snapshot_id"],
        }
        if any(current.get(key) != value for key, value in immutable.items()):
            raise IsolatedExecutionError("journal identity binding mismatch")
    value = {
        "schema_version": 1,
        "contract_id": CONTRACT_ID,
        "publisher_id": PUBLISHER_ID,
        "execution_id": plan["execution_id"],
        "request_sha256": plan["request"]["sha256"],
        "execution_plan_sha256": plan["execution_plan_sha256"],
        "dataset_id": plan["target"]["dataset_id"],
        "snapshot_id": plan["target"]["snapshot_id"],
        "state": state,
        "updated_at": atomic.utc_now(),
        "history": [*current.get("history", []), state],
        **extra,
    }
    atomic.write_json_atomic(paths.journal, value)


def _existing_journal(paths: Paths, plan: dict[str, Any]) -> dict[str, Any] | None:
    if not paths.journal.exists():
        return None
    value = atomic.load_json(paths.journal)
    expected = {
        "publisher_id": PUBLISHER_ID,
        "execution_id": plan["execution_id"],
        "request_sha256": plan["request"]["sha256"],
        "execution_plan_sha256": plan["execution_plan_sha256"],
        "dataset_id": plan["target"]["dataset_id"],
        "snapshot_id": plan["target"]["snapshot_id"],
    }
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise IsolatedExecutionError("existing journal belongs to another sealed execution")
    return value


def _reserve_authorization(
    paths: Paths,
    plan: dict[str, Any],
    authorization: dict[str, Any],
) -> dict[str, Any]:
    expected = {
        "authorization_id": authorization["authorization_id"],
        "execution_id": plan["execution_id"],
        "request_sha256": plan["request"]["sha256"],
        "dataset_id": plan["target"]["dataset_id"],
        "snapshot_id": plan["target"]["snapshot_id"],
    }
    if paths.ledger.exists():
        ledger = atomic.load_json(paths.ledger)
        if any(ledger.get(key) != value for key, value in expected.items()):
            raise IsolatedExecutionError("authorization single-use replay detected")
        if ledger.get("state") not in {"RESERVED", "CONSUMED"}:
            raise IsolatedExecutionError(
                f"authorization is not reusable after state {ledger.get('state')!r}"
            )
        return ledger
    value = {
        "schema_version": 1,
        "contract_id": "formal_publish_authorization_ledger_v1",
        **expected,
        "state": "RESERVED",
        "reserved_at": atomic.utc_now(),
        "consumed_at": None,
    }
    _write_exclusive_json(paths.ledger, value)
    return value


def _update_ledger(paths: Paths, plan: dict[str, Any], state: str) -> None:
    ledger = atomic.load_json(paths.ledger)
    if ledger.get("execution_id") != plan["execution_id"]:
        raise IsolatedExecutionError("authorization ledger execution binding mismatch")
    if state == "CONSUMED" and ledger.get("state") == "CONSUMED":
        return
    if ledger.get("state") != "RESERVED":
        raise IsolatedExecutionError("authorization is not in RESERVED state")
    ledger["state"] = state
    if state == "CONSUMED":
        ledger["consumed_at"] = atomic.utc_now()
    else:
        ledger["aborted_at"] = atomic.utc_now()
    atomic.write_json_atomic(paths.ledger, ledger)


def _lock_value(paths: Paths, plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contract_id": "formal_publisher_isolated_lock_v1",
        "publisher_id": PUBLISHER_ID,
        "execution_id": plan["execution_id"],
        "request_sha256": plan["request"]["sha256"],
        "execution_plan_sha256": plan["execution_plan_sha256"],
        "pid": os.getpid(),
        "process_start_token": atomic.process_start_token(os.getpid()),
        "lock_id": uuid.uuid4().hex,
        "acquired_at": atomic.utc_now(),
        "workspace_root": _path_text(paths.workspace),
    }


def _acquire_lock(
    paths: Paths,
    plan: dict[str, Any],
    recover_hash: str | None,
) -> dict[str, Any]:
    if paths.lock.exists():
        payload = paths.lock.read_bytes()
        actual_hash = _sha(payload)
        old = atomic.load_json(paths.lock)
        if recover_hash is None:
            raise IsolatedExecutionError("isolated catalog writer lock already exists")
        if actual_hash != recover_hash:
            raise IsolatedExecutionError("stale lock confirmation hash mismatch")
        if (
            old.get("publisher_id") != PUBLISHER_ID
            or old.get("execution_id") != plan["execution_id"]
            or old.get("request_sha256") != plan["request"]["sha256"]
            or old.get("execution_plan_sha256") != plan["execution_plan_sha256"]
        ):
            raise IsolatedExecutionError("stale lock belongs to another execution")
        pid = old.get("pid")
        if isinstance(pid, int) and atomic.process_is_alive(
            pid,
            old.get("process_start_token"),
        ):
            raise IsolatedExecutionError("catalog lock owner is still alive")
        archive = paths.lock.with_name(f"{paths.lock.name}.stale.{actual_hash}")
        if archive.exists():
            raise IsolatedExecutionError("stale lock archive collision")
        os.replace(paths.lock, archive)
    elif recover_hash is not None:
        raise IsolatedExecutionError("recovery confirmation supplied but no lock exists")
    value = _lock_value(paths, plan)
    _write_exclusive_json(paths.lock, value)
    return value


def _remove_lock(paths: Paths, lock_id: str) -> None:
    if not paths.lock.exists():
        return
    value = atomic.load_json(paths.lock)
    if value.get("lock_id") != lock_id or value.get("pid") != os.getpid():
        raise IsolatedExecutionError("refusing to remove an unowned catalog lock")
    paths.lock.unlink()


def _payload_entries(plan: dict[str, Any], paths: Paths) -> list[dict[str, Any]]:
    records = []
    for item in plan["payload_layout"]:
        base = paths.raw_final if item["layer"] == "raw" else paths.curated_final
        relative = item["snapshot_relative_path"]
        records.append(
            {
                "layer": item["layer"],
                "relative_path": relative,
                "workspace_relative_path": (base / Path(relative)).relative_to(
                    paths.workspace
                ).as_posix(),
                "bytes": item["bytes"],
                "sha256": item["sha256"],
            }
        )
    return sorted(records, key=lambda item: (item["layer"], item["relative_path"]))


def _build_snapshot_contract(
    plan: dict[str, Any],
    paths: Paths,
) -> tuple[dict[str, dict[str, Any]], str, dict[str, Any]]:
    profile = plan["publication_profile"]
    target = plan["target"]
    dataset_id = target["dataset_id"]
    snapshot_id = target["snapshot_id"]
    request = {
        "schema_version": 1,
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "store_id": profile["store_id"],
        "owner_project": profile["owner_project"],
        "purpose_ceiling": profile["purpose_ceiling"],
        "license_status": profile["license_status"],
        "quality_status": profile["quality_status"],
        "consumers": profile["consumers"],
        "execution_contract_id": execution_contract.CONTRACT_ID,
        "execution_plan_sha256": plan["execution_plan_sha256"],
    }
    files = _payload_entries(plan, paths)
    payload_hash = atomic.payload_digest(
        [
            {
                "layer": item["layer"],
                "relative_path": item["relative_path"],
                "bytes": item["bytes"],
                "sha256": item["sha256"],
            }
            for item in files
        ]
    )
    manifest = {
        "schema_version": 1,
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "root_path": _path_text(paths.snapshot_final),
        "hash_algorithm": HASH_ALGORITHM,
        "payload_hash": payload_hash,
        "payload_hash_algorithm": "sha256-canonical-file-list-v1",
        "contract_id": profile["dataset_contract_id"],
        "dataset_schema_version": profile["dataset_schema_version"],
        "research_input_contract": profile["research_input_contract"],
        "created_at": profile["published_at"],
        "available_at": profile["available_at"],
        "partition_segments": profile["partition_segments"],
        "raw_root": paths.raw_final.relative_to(paths.workspace).as_posix(),
        "curated_root": paths.curated_final.relative_to(paths.workspace).as_posix(),
        "status": "SEALED_SNAPSHOT",
        "formal_execution_profile": True,
    }
    provenance = {
        "schema_version": 1,
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "mode": "FORMAL_ATOMIC_PUBLICATION",
        "source_id": profile["source_id"],
        "contract_id": profile["dataset_contract_id"],
        "request_contract_hash": request_contract_hash(request),
        "payload_hash": payload_hash,
        "available_at": profile["available_at"],
        "execution_plan_sha256": plan["execution_plan_sha256"],
    }
    partitions = {
        "schema_version": 1,
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "payload_hash": payload_hash,
        "files": files,
    }
    quality = {
        "schema_version": 1,
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "overall": "PASS",
        "catalog_quality_status": profile["quality_status"],
        "purpose_ceiling": profile["purpose_ceiling"],
        "formal_execution_profile": True,
        "execution_plan_sha256": plan["execution_plan_sha256"],
    }
    content_hash = snapshot_contract_hash(
        request,
        manifest,
        provenance,
        partitions,
        quality,
    )
    manifest["content_hash"] = content_hash
    documents = {
        "request.json": request,
        "manifest.json": manifest,
        "provenance.json": provenance,
        "partition-manifest.json": partitions,
        "quality/summary.json": quality,
    }
    item = {
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "store_id": profile["store_id"],
        "owner_project": profile["owner_project"],
        "physical_path": _path_text(paths.snapshot_final),
        "lifecycle": profile["lifecycle"],
        "mutability": profile["mutability"],
        "purpose_ceiling": profile["purpose_ceiling"],
        "license_status": profile["license_status"],
        "quality_status": profile["quality_status"],
        "authority_paths": [
            _path_text(paths.snapshot_final / relative)
            for relative in AUTHORITY_RELATIVE_PATHS
        ],
        "consumers": profile["consumers"],
        "known_limits": profile["known_limits"],
        "notes": profile["notes"],
        "content_hash": content_hash,
        "hash_algorithm": HASH_ALGORITHM,
        "available_at": profile["available_at"],
        "published_at": profile["published_at"],
    }
    return documents, content_hash, item


def _copy_one(source: Path, target: Path, expected_bytes: int, expected_sha: str) -> None:
    if target.exists():
        raise IsolatedExecutionError(f"staging destination already exists: {target}")
    if not source.is_file() or atomic.is_reparse_point(source) or source.stat().st_nlink != 1:
        raise IsolatedExecutionError(f"payload source is not a plain regular file: {source}")
    if source.stat().st_size != expected_bytes or atomic.sha256_file(source) != expected_sha:
        raise IsolatedExecutionError(f"payload source changed after plan sealing: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target, follow_symlinks=False)
    if target.stat().st_size != expected_bytes or atomic.sha256_file(target) != expected_sha:
        raise IsolatedExecutionError(f"staged payload verification failed: {target}")


def _verify_payload_root(root: Path, plan: dict[str, Any], layer: str) -> None:
    expected = {
        item["snapshot_relative_path"]: (item["bytes"], item["sha256"])
        for item in plan["payload_layout"]
        if item["layer"] == layer
    }
    actual = {
        path.relative_to(root).as_posix(): (path.stat().st_size, atomic.sha256_file(path))
        for path in root.rglob("*")
        if path.is_file()
    }
    if actual != expected:
        raise IsolatedExecutionError(f"{layer} payload tree differs from the sealed layout")


def _prepare_stage(
    paths: Paths,
    plan: dict[str, Any],
    catalog: dict[str, Any],
) -> dict[str, Any]:
    prepared_path = paths.stage / "prepared.json"
    if paths.stage.exists():
        prepared = atomic.load_json(prepared_path)
        if (
            prepared.get("execution_plan_sha256") != plan["execution_plan_sha256"]
            or prepared.get("request_sha256") != plan["request"]["sha256"]
        ):
            raise IsolatedExecutionError("existing stage belongs to another execution")
        return prepared
    temporary = paths.stage.with_name(f".{paths.stage.name}.partial.{uuid.uuid4().hex}")
    if temporary.exists():
        raise IsolatedExecutionError("temporary stage collision")
    temporary.mkdir(parents=True)
    try:
        atomic.write_json(
            temporary / "owner.json",
            {
                "schema_version": 1,
                "publisher_id": PUBLISHER_ID,
                "execution_id": plan["execution_id"],
                "execution_plan_sha256": plan["execution_plan_sha256"],
            },
        )
        for item in plan["payload_layout"]:
            source = ROOT.joinpath(*PurePosixPath(item["source_relative_path"]).parts)
            destination = temporary / item["layer"] / Path(item["snapshot_relative_path"])
            _copy_one(source, destination, item["bytes"], item["sha256"])
        documents, content_hash, catalog_item = _build_snapshot_contract(plan, paths)
        for relative, document in documents.items():
            atomic.write_json(temporary / "snapshot" / relative, document)
        catalog_candidate = copy.deepcopy(catalog)
        if any(
            item.get("dataset_id") == plan["target"]["dataset_id"]
            and item.get("snapshot_id") == plan["target"]["snapshot_id"]
            for item in catalog_candidate.get("datasets", [])
        ):
            raise IsolatedExecutionError("immutable catalog identity already exists")
        catalog_candidate["datasets"].append(catalog_item)
        catalog_candidate["updated_at"] = plan["publication_profile"]["published_at"]
        before_payload = paths.catalog.read_bytes()
        candidate_payload = _pretty_bytes(catalog_candidate)
        (temporary / "catalog-before.bin").write_bytes(before_payload)
        (temporary / "catalog-candidate.json").write_bytes(candidate_payload)
        prepared = {
            "schema_version": 1,
            "contract_id": CONTRACT_ID,
            "publisher_id": PUBLISHER_ID,
            "execution_id": plan["execution_id"],
            "request_sha256": plan["request"]["sha256"],
            "execution_plan_sha256": plan["execution_plan_sha256"],
            "catalog_hash_before": _sha(before_payload),
            "catalog_hash_after": _sha(candidate_payload),
            "content_hash": content_hash,
            "catalog_item": catalog_item,
        }
        atomic.write_json(temporary / "prepared.json", prepared)
        paths.stage.parent.mkdir(parents=True, exist_ok=True)
        atomic.assert_same_volume(paths.catalog, [temporary, paths.stage])
        os.replace(temporary, paths.stage)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return prepared


def _finalize_payload(paths: Paths, plan: dict[str, Any], prepared: dict[str, Any]) -> None:
    atomic.assert_same_volume(
        paths.catalog,
        [paths.stage, paths.raw_final, paths.curated_final, paths.snapshot_final],
    )
    for layer, final in (("raw", paths.raw_final), ("curated", paths.curated_final)):
        staged = paths.stage / layer
        if final.exists():
            _verify_payload_root(final, plan, layer)
        else:
            if not staged.is_dir():
                raise IsolatedExecutionError(f"missing staged {layer} payload")
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, final)
            _verify_payload_root(final, plan, layer)
    if paths.snapshot_final.exists():
        errors = verify_snapshot_contract(
            catalog={
                **atomic.load_json(paths.catalog),
                "datasets": [prepared["catalog_item"]],
            },
            item=prepared["catalog_item"],
            snapshot_path=paths.snapshot_final,
            deep_payload=True,
        )
        if errors:
            raise IsolatedExecutionError("existing snapshot verification failed: " + " | ".join(errors))
    else:
        staged_snapshot = paths.stage / "snapshot"
        if not staged_snapshot.is_dir():
            raise IsolatedExecutionError("missing staged snapshot contract")
        paths.snapshot_final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged_snapshot, paths.snapshot_final)


def _replace_catalog_bytes(
    path: Path,
    payload: bytes,
    *,
    expected_before: str,
    expected_after: str,
) -> None:
    if _sha(payload) != expected_after:
        raise IsolatedExecutionError("catalog replacement payload hash mismatch")
    if atomic.sha256_file(path) != expected_before:
        raise IsolatedExecutionError("catalog compare-and-swap base hash mismatch")
    temporary: Path | None = None
    try:
        descriptor, raw = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.formal.",
            suffix=".tmp",
        )
        temporary = Path(raw)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if atomic.sha256_file(temporary) != expected_after:
            raise IsolatedExecutionError("catalog temporary hash mismatch")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _publish_catalog(paths: Paths, prepared: dict[str, Any]) -> None:
    current = atomic.sha256_file(paths.catalog)
    if current == prepared["catalog_hash_after"]:
        return
    if current != prepared["catalog_hash_before"]:
        raise IsolatedExecutionError("isolated catalog drifted after staging")
    candidate_path = paths.stage / "catalog-candidate.json"
    candidate = atomic.load_json(candidate_path)
    errors = validate_dataset_catalog.validate_catalog(candidate)
    if errors:
        raise IsolatedExecutionError("candidate catalog is invalid: " + " | ".join(errors))
    _replace_catalog_bytes(
        paths.catalog,
        candidate_path.read_bytes(),
        expected_before=prepared["catalog_hash_before"],
        expected_after=prepared["catalog_hash_after"],
    )


def _qdata_postcondition(
    paths: Paths,
    plan: dict[str, Any],
    *,
    inject_failure: bool,
) -> None:
    if inject_failure:
        raise QdataPostconditionError("injected qdata research postcondition failure")
    catalog = dataset_cli.load_catalog(paths.catalog)
    qdata = plan["preflight"]["qdata_postconditions"] if "qdata_postconditions" in plan["preflight"] else None
    if qdata is None:
        preflight_path = ROOT.joinpath(*PurePosixPath(plan["preflight"]["relative_path"]).parts)
        qdata = preflight_module.preflight(preflight_path)["qdata_postconditions"]
    item = dataset_cli.find_dataset(
        catalog,
        plan["target"]["dataset_id"],
        plan["target"]["snapshot_id"],
    )
    dataset_cli.check_access(catalog, item, qdata["intent"], qdata["consumer"])
    errors = verify_snapshot_contract(
        catalog=catalog,
        item=item,
        snapshot_path=paths.snapshot_final,
        deep_payload=True,
    )
    if errors:
        raise QdataPostconditionError("qdata snapshot verification failed: " + " | ".join(errors))
    if item.get("quality_status") != "OK" or item.get("mutability") != "immutable":
        raise QdataPostconditionError("qdata resolved a non-OK or mutable identity")


def _rollback_after_qdata_failure(
    paths: Paths,
    plan: dict[str, Any],
    prepared: dict[str, Any],
    reason: str,
) -> None:
    current = atomic.sha256_file(paths.catalog)
    if current == prepared["catalog_hash_after"]:
        before = (paths.stage / "catalog-before.bin").read_bytes()
        _replace_catalog_bytes(
            paths.catalog,
            before,
            expected_before=prepared["catalog_hash_after"],
            expected_after=prepared["catalog_hash_before"],
        )
    elif current != prepared["catalog_hash_before"]:
        raise IsolatedExecutionError("cannot rollback because catalog CAS identity drifted")
    paths.rollback.mkdir(parents=True, exist_ok=False)
    for label, source in (
        ("raw", paths.raw_final),
        ("curated", paths.curated_final),
        ("snapshot", paths.snapshot_final),
        ("stage", paths.stage),
    ):
        if source.exists():
            os.replace(source, paths.rollback / label)
    _update_ledger(paths, plan, "ABORTED_NOT_CONSUMED")
    _write_journal(
        paths,
        plan,
        "ROLLED_BACK_QDATA_FAILED",
        reason=reason,
        catalog_hash_after=atomic.sha256_file(paths.catalog),
        rollback_path=_path_text(paths.rollback),
    )


def _result(paths: Paths, plan: dict[str, Any], status: str, state: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contract_id": CONTRACT_ID,
        "execution_id": plan["execution_id"],
        "status": status,
        "state": state,
        "write_performed": status not in {"PASS__ISOLATED_EXECUTION_DISABLED__NO_WRITE"},
        "isolated_workspace": _path_text(paths.workspace),
        "journal_path": _path_text(paths.journal),
        "authorization_ledger_path": _path_text(paths.ledger),
        "catalog_path": _path_text(paths.catalog),
        "snapshot_path": _path_text(paths.snapshot_final),
        "formal_effect": "ISOLATED_COPY_ONLY",
        "real_workspace_write": False,
    }


def execute(
    *,
    request: Path,
    isolated_workspace: Path,
    enabled: bool,
    recover_hash: str | None,
    failpoint: str | None,
    inject_qdata_failure: bool,
) -> dict[str, Any]:
    try:
        plan = execution_contract.validate_execution(request)
    except Exception as exc:  # exact validator converts all expected failures
        raise IsolatedExecutionError(str(exc)) from exc
    catalog = _assert_isolated_workspace(
        isolated_workspace,
        plan["catalog_before"]["sha256"],
    )
    authorization = _authorization_value(plan)
    paths = _build_paths(
        Path(os.path.abspath(isolated_workspace)),
        plan,
        authorization["authorization_id"],
    )
    if not enabled:
        return _result(
            paths,
            plan,
            "PASS__ISOLATED_EXECUTION_DISABLED__NO_WRITE",
            "INPUT_INVENTORY_SEALED",
        )
    if failpoint is not None and failpoint not in ALLOWED_FAILPOINTS:
        raise IsolatedExecutionError(f"unknown isolated failpoint: {failpoint}")
    journal = _existing_journal(paths, plan)
    if paths.ledger.exists():
        ledger = atomic.load_json(paths.ledger)
        if ledger.get("execution_id") != plan["execution_id"]:
            raise IsolatedExecutionError("authorization single-use replay detected")
    if journal is None and any(
        path.exists()
        for path in (paths.stage, paths.raw_final, paths.curated_final, paths.snapshot_final)
    ):
        raise IsolatedExecutionError("isolated immutable target or publication path already exists")
    if journal is not None and journal.get("state") == "PUBLISHED":
        ledger = atomic.load_json(paths.ledger)
        if ledger.get("state") != "CONSUMED":
            raise IsolatedExecutionError("published journal lacks consumed authorization")
        return _result(paths, plan, "NO_CHANGE", "PUBLISHED")
    _reserve_authorization(paths, plan, authorization)
    if journal is None:
        _write_journal(paths, plan, "EVIDENCE_VERIFIED")
        _write_journal(paths, plan, "INPUT_INVENTORY_SEALED")
    prepared = _prepare_stage(paths, plan, catalog)
    _write_journal(
        paths,
        plan,
        "PRIVATE_STAGING_SEALED",
        catalog_hash_before=prepared["catalog_hash_before"],
        catalog_hash_after=prepared["catalog_hash_after"],
        content_hash=prepared["content_hash"],
    )
    if failpoint == "hard_crash_after_private_staging":
        os._exit(90)
    lock = _acquire_lock(paths, plan, recover_hash)
    lock_id = lock["lock_id"]
    try:
        _write_journal(paths, plan, "CATALOG_LOCK_ACQUIRED", lock_id=lock_id)
        _finalize_payload(paths, plan, prepared)
        _write_journal(paths, plan, "PAYLOAD_FINALIZED_INVISIBLE", lock_id=lock_id)
        if failpoint == "hard_crash_after_payload_finalize":
            os._exit(91)
        _publish_catalog(paths, prepared)
        _write_journal(
            paths,
            plan,
            "CATALOG_ATOMICALLY_VISIBLE",
            lock_id=lock_id,
            catalog_hash_after=atomic.sha256_file(paths.catalog),
        )
        if failpoint == "hard_crash_after_catalog_visible":
            os._exit(92)
        try:
            _qdata_postcondition(
                paths,
                plan,
                inject_failure=inject_qdata_failure,
            )
        except Exception as exc:
            _rollback_after_qdata_failure(paths, plan, prepared, str(exc))
            raise QdataPostconditionError(str(exc)) from exc
        _write_journal(paths, plan, "QDATA_RESEARCH_RESOLVED", lock_id=lock_id)
        if failpoint == "hard_crash_after_qdata_success":
            os._exit(93)
        _update_ledger(paths, plan, "CONSUMED")
        _write_journal(paths, plan, "AUTHORIZATION_CONSUMED", lock_id=lock_id)
        if paths.stage.exists():
            shutil.rmtree(paths.stage)
        _write_journal(
            paths,
            plan,
            "PUBLISHED",
            lock_id=lock_id,
            catalog_hash_after=atomic.sha256_file(paths.catalog),
            content_hash=prepared["content_hash"],
        )
    finally:
        if paths.lock.exists():
            _remove_lock(paths, lock_id)
    return _result(paths, plan, "PUBLISHED", "PUBLISHED")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Run the formal atomic state machine only in a sentinel-bound tmp copy."
    )
    value.add_argument("--request", required=True, type=Path)
    value.add_argument("--isolated-workspace", required=True, type=Path)
    value.add_argument("--execute-isolated", action="store_true")
    value.add_argument("--recover-stale-lock-sha256")
    value.add_argument("--failpoint", choices=sorted(ALLOWED_FAILPOINTS))
    value.add_argument("--inject-qdata-failure", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        result = execute(
            request=args.request,
            isolated_workspace=args.isolated_workspace,
            enabled=bool(args.execute_isolated),
            recover_hash=args.recover_stale_lock_sha256,
            failpoint=args.failpoint,
            inject_qdata_failure=bool(args.inject_qdata_failure),
        )
    except IsolatedExecutionError as exc:
        print(f"ISOLATED FORMAL PUBLISH REJECTED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
