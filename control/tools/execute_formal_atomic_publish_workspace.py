#!/usr/bin/env python3
"""Publish one hash-bound formal snapshot into the exact Quantification workspace.

The command is a dry run unless ``--execute-workspace`` is present.  A write
requires three independent bindings: the checked execution request, the exact
workspace root compiled into this tool, and an explicit catalog baseline
SHA-256.  Tests use the private ``_execute_at_workspace`` entry point with a
temporary workspace; the CLI cannot redirect publication to another root.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import socket
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TOOLS = Path(__file__).resolve().parent
CATALOG_LIB = ROOT / "data/catalog"
FACTOR_ROOT = ROOT / "多因子"
for value in (TOOLS, CATALOG_LIB, FACTOR_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

import atomic_snapshot_publish as atomic  # noqa: E402
import dataset_cli  # noqa: E402
import run_formal_publisher_preflight as preflight_module  # noqa: E402
import validate_dataset_catalog  # noqa: E402
import validate_formal_atomic_publish_execution as execution_contract  # noqa: E402
from factor_factory import qdata_input as factor_qdata  # noqa: E402
from snapshot_contract import (  # noqa: E402
    AUTHORITY_RELATIVE_PATHS,
    HASH_ALGORITHM,
    request_contract_hash,
    snapshot_contract_hash,
    verify_snapshot_contract,
)


CONTRACT_ID = "formal_atomic_publish_workspace_v1"
PUBLISHER_ID = "execute_formal_atomic_publish_workspace.py/v1"
LOCK_NAME = ".datasets.json.formal-workspace.lock"
SHA256_PATTERN = execution_contract.SHA256_RE
_TEST_FAILPOINTS = {
    "hard_crash_after_private_staging",
    "hard_crash_after_payload_finalize",
    "hard_crash_after_catalog_visible",
    "hard_crash_after_qdata_success",
}


class WorkspaceExecutionError(RuntimeError):
    """Fail-closed workspace publication error."""


class QdataPostconditionError(WorkspaceExecutionError):
    """A post-visibility qdata failure that requires catalog rollback."""


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


def _safe_path(path: Path, workspace: Path, label: str) -> Path:
    try:
        return atomic.assert_path_under_workspace(path, workspace, label)
    except atomic.PublishError as exc:
        raise WorkspaceExecutionError(str(exc)) from exc


def _assert_exact_workspace(
    workspace: Path,
    *,
    trusted_workspace_root: Path,
    catalog_baseline_sha256: str,
    allow_recovery_state: bool,
) -> dict[str, Any]:
    if not isinstance(catalog_baseline_sha256, str) or SHA256_PATTERN.fullmatch(
        catalog_baseline_sha256
    ) is None:
        raise WorkspaceExecutionError("catalog baseline must be a lowercase SHA-256")
    workspace = Path(os.path.abspath(workspace))
    trusted = Path(os.path.abspath(trusted_workspace_root))
    if workspace != trusted:
        raise WorkspaceExecutionError(
            "workspace root must exactly equal the publisher's trusted workspace root"
        )
    if not workspace.is_dir() or atomic.is_reparse_point(workspace):
        raise WorkspaceExecutionError("workspace root must be a plain existing directory")
    catalog_path = _safe_path(
        workspace / "data/catalog/datasets.json", workspace, "catalog"
    )
    if (
        not catalog_path.is_file()
        or atomic.is_reparse_point(catalog_path)
        or catalog_path.stat().st_nlink != 1
    ):
        raise WorkspaceExecutionError("catalog must be one plain regular file")
    current_hash = atomic.sha256_file(catalog_path)
    if current_hash != catalog_baseline_sha256 and not allow_recovery_state:
        raise WorkspaceExecutionError("explicit catalog baseline SHA-256 mismatch")
    catalog = atomic.load_json(catalog_path)
    raw_root = catalog.get("workspace_root")
    if not isinstance(raw_root, str) or Path(
        raw_root.replace("/", os.sep)
    ).resolve() != workspace.resolve():
        raise WorkspaceExecutionError("catalog.workspace_root mismatch")
    errors = validate_dataset_catalog.validate_catalog(catalog)
    if errors:
        raise WorkspaceExecutionError("workspace catalog is invalid: " + " | ".join(errors))
    return catalog


def _authorization_binding(
    plan: dict[str, Any], workspace: Path
) -> tuple[dict[str, Any], str]:
    preflight_path = workspace.joinpath(
        *PurePosixPath(plan["preflight"]["relative_path"]).parts
    )
    request, _ = preflight_module.load_json_object(
        preflight_path, "preflight request"
    )
    binding = request.get("user_authorization_receipt")
    if not isinstance(binding, dict):
        raise WorkspaceExecutionError("user authorization binding is missing")
    try:
        value = execution_contract._load_preflight_evidence_value(  # noqa: SLF001
            preflight_path,
            "user_authorization_receipt",
            "user authorization receipt",
            workspace_root=workspace,
        )
    except Exception as exc:
        raise WorkspaceExecutionError(str(exc)) from exc
    receipt_sha = binding.get("sha256")
    if not isinstance(receipt_sha, str) or SHA256_PATTERN.fullmatch(receipt_sha) is None:
        raise WorkspaceExecutionError("authorization receipt hash is invalid")
    if (
        plan.get("execution_contract_id") == execution_contract.CONTRACT_ID_V2
        and value.get("contract_id") != "formal_publish_authorization_receipt_v2"
    ):
        raise WorkspaceExecutionError(
            "commit-clock execution requires an independent exact v2 authorization receipt"
        )
    return value, receipt_sha


def _rights_binding(plan: dict[str, Any], workspace: Path) -> dict[str, Any]:
    preflight_path = workspace.joinpath(
        *PurePosixPath(plan["preflight"]["relative_path"]).parts
    )
    try:
        value = execution_contract._load_preflight_evidence_value(  # noqa: SLF001
            preflight_path,
            "source_rights_receipt",
            "source rights receipt",
            workspace_root=workspace,
        )
    except Exception as exc:
        raise WorkspaceExecutionError(str(exc)) from exc
    if (
        plan.get("execution_contract_id") == execution_contract.CONTRACT_ID_V2
        and value.get("contract_id") != "source_rights_receipt_v2"
    ):
        raise WorkspaceExecutionError(
            "commit-clock execution requires source_rights_receipt_v2"
        )
    return value


def _assert_available_for_execution(plan: dict[str, Any]) -> None:
    raw = plan["publication_profile"].get("available_at")
    try:
        available_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkspaceExecutionError("available_at is not ISO-8601") from exc
    if available_at.tzinfo is None or available_at.utcoffset() is None:
        raise WorkspaceExecutionError("available_at must be timezone-aware")
    if datetime.now(timezone.utc) < available_at.astimezone(timezone.utc):
        raise WorkspaceExecutionError(
            f"formal publication is blocked until available_at {available_at.isoformat()}"
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
    paths = Paths(
        workspace=workspace,
        catalog=workspace / "data/catalog/datasets.json",
        market=market,
        stage=market / ".formal-workspace-staging" / dataset_id / execution_id,
        raw_final=market / "raw" / dataset_id / snapshot_id,
        curated_final=market / "curated" / dataset_id / snapshot_id,
        snapshot_final=workspace.joinpath(
            *PurePosixPath(plan["target"]["target_relative_path"]).parts
        ),
        journal=market / "transactions" / f"formal-workspace-{execution_id}.json",
        ledger=market / "authorization-ledger" / f"{authorization_id}.json",
        lock=workspace / "data/catalog" / LOCK_NAME,
        rollback=market / "rollback" / "formal-workspace" / execution_id,
    )
    for field, path in paths.__dict__.items():
        if field != "workspace":
            _safe_path(path, workspace, field)
    expected_target = workspace.joinpath(
        *PurePosixPath(plan["target"]["target_relative_path"]).parts
    )
    if paths.snapshot_final != expected_target:
        raise WorkspaceExecutionError("snapshot target derivation mismatch")
    return paths


def _write_exclusive_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as exc:
        raise WorkspaceExecutionError(f"exclusive state already exists: {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_pretty_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _identity_fields(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "publisher_id": PUBLISHER_ID,
        "execution_id": plan["execution_id"],
        "request_sha256": plan["request"]["sha256"],
        "execution_plan_sha256": plan["execution_plan_sha256"],
        "dataset_id": plan["target"]["dataset_id"],
        "snapshot_id": plan["target"]["snapshot_id"],
    }


def _write_journal(
    paths: Paths,
    plan: dict[str, Any],
    state: str,
    **extra: Any,
) -> None:
    current: dict[str, Any] = {}
    if paths.journal.exists():
        current = atomic.load_json(paths.journal)
        if any(current.get(key) != value for key, value in _identity_fields(plan).items()):
            raise WorkspaceExecutionError("journal identity binding mismatch")
    value = {
        "schema_version": 1,
        "contract_id": CONTRACT_ID,
        **_identity_fields(plan),
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
    if any(value.get(key) != expected for key, expected in _identity_fields(plan).items()):
        raise WorkspaceExecutionError("existing journal belongs to another sealed execution")
    return value


def _reserve_authorization(
    paths: Paths,
    plan: dict[str, Any],
    authorization: dict[str, Any],
    authorization_receipt_sha256: str,
) -> dict[str, Any]:
    expected = {
        "authorization_id": authorization["authorization_id"],
        "authorization_receipt_sha256": authorization_receipt_sha256,
        "execution_id": plan["execution_id"],
        "request_sha256": plan["request"]["sha256"],
        "execution_plan_sha256": plan["execution_plan_sha256"],
        "dataset_id": plan["target"]["dataset_id"],
        "snapshot_id": plan["target"]["snapshot_id"],
    }
    if paths.ledger.exists():
        ledger = atomic.load_json(paths.ledger)
        if any(ledger.get(key) != value for key, value in expected.items()):
            raise WorkspaceExecutionError("authorization single-use replay detected")
        if ledger.get("state") not in {"RESERVED", "CONSUMED"}:
            raise WorkspaceExecutionError(
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
    if (
        ledger.get("execution_id") != plan["execution_id"]
        or ledger.get("execution_plan_sha256") != plan["execution_plan_sha256"]
    ):
        raise WorkspaceExecutionError("authorization ledger binding mismatch")
    if state == "CONSUMED" and ledger.get("state") == "CONSUMED":
        return
    if ledger.get("state") != "RESERVED":
        raise WorkspaceExecutionError("authorization is not in RESERVED state")
    ledger["state"] = state
    if state == "CONSUMED":
        ledger["consumed_at"] = atomic.utc_now()
    else:
        ledger["aborted_at"] = atomic.utc_now()
    atomic.write_json_atomic(paths.ledger, ledger)


def _lock_value(paths: Paths, plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contract_id": "formal_publisher_workspace_lock_v1",
        **_identity_fields(plan),
        "pid": os.getpid(),
        "process_start_token": atomic.process_start_token(os.getpid()),
        "hostname": socket.gethostname(),
        "lock_id": uuid.uuid4().hex,
        "acquired_at": atomic.utc_now(),
        "workspace_root": _path_text(paths.workspace),
    }


def _acquire_lock(
    paths: Paths,
    plan: dict[str, Any],
    recover_hash: str | None,
) -> dict[str, Any]:
    if recover_hash is not None and SHA256_PATTERN.fullmatch(recover_hash) is None:
        raise WorkspaceExecutionError(
            "stale lock confirmation must be a lowercase SHA-256"
        )
    with atomic.catalog_transition_guard(paths.catalog):
        if paths.lock.exists():
            payload = paths.lock.read_bytes()
            actual_hash = _sha(payload)
            old = atomic.load_json(paths.lock)
            if recover_hash is None:
                raise WorkspaceExecutionError("workspace catalog writer lock already exists")
            if actual_hash != recover_hash:
                raise WorkspaceExecutionError("stale lock confirmation hash mismatch")
            expected = _identity_fields(plan)
            if any(old.get(key) != value for key, value in expected.items()):
                raise WorkspaceExecutionError("stale lock belongs to another execution")
            if old.get("hostname") != socket.gethostname():
                raise WorkspaceExecutionError("stale lock belongs to another host")
            pid = old.get("pid")
            start_token = old.get("process_start_token")
            lock_id = old.get("lock_id")
            if (
                not isinstance(pid, int)
                or isinstance(pid, bool)
                or pid <= 0
                or (start_token is not None and not isinstance(start_token, str))
                or not isinstance(lock_id, str)
                or atomic.LOCK_ID_PATTERN.fullmatch(lock_id) is None
                or old.get("workspace_root") != _path_text(paths.workspace)
            ):
                raise WorkspaceExecutionError("stale lock contract is invalid")
            if atomic.process_is_alive(pid, start_token):
                raise WorkspaceExecutionError("catalog lock owner is still alive")
            archive = (
                paths.market
                / "transactions/stale-locks"
                / f"formal-workspace-{plan['execution_id']}-{lock_id}.json"
            )
            if archive.exists():
                if archive.read_bytes() != payload:
                    raise WorkspaceExecutionError("stale lock archive collision")
            else:
                archive.parent.mkdir(parents=True, exist_ok=True)
                try:
                    descriptor = os.open(
                        archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    )
                except FileExistsError as exc:
                    raise WorkspaceExecutionError("stale lock archive raced") from exc
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            if paths.lock.read_bytes() != payload:
                raise WorkspaceExecutionError("catalog lock changed before archival")
            paths.lock.unlink()
        elif recover_hash is not None:
            raise WorkspaceExecutionError(
                "recovery confirmation supplied but no workspace lock exists"
            )
        value = _lock_value(paths, plan)
        _write_exclusive_json(paths.lock, value)
    return value


def _remove_lock(paths: Paths, lock_id: str) -> None:
    with atomic.catalog_transition_guard(paths.catalog):
        if not paths.lock.exists():
            return
        value = atomic.load_json(paths.lock)
        if value.get("lock_id") != lock_id or value.get("pid") != os.getpid():
            raise WorkspaceExecutionError("refusing to remove an unowned catalog lock")
        paths.lock.unlink()


def _payload_entries(plan: dict[str, Any], paths: Paths) -> list[dict[str, Any]]:
    records = []
    for item in plan["payload_layout"]:
        base = paths.raw_final if item["layer"] == "raw" else paths.curated_final
        records.append(
            {
                "layer": item["layer"],
                "relative_path": item["snapshot_relative_path"],
                "workspace_relative_path": (
                    base / Path(item["snapshot_relative_path"])
                ).relative_to(paths.workspace).as_posix(),
                "bytes": item["bytes"],
                "sha256": item["sha256"],
            }
        )
    return sorted(records, key=lambda item: (item["layer"], item["relative_path"]))


def _build_snapshot_contract(
    plan: dict[str, Any],
    paths: Paths,
    *,
    authorization_receipt_sha256: str,
    commit_published_at: str,
    rights_receipt: dict[str, Any],
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
        "execution_contract_id": plan.get(
            "execution_contract_id", execution_contract.CONTRACT_ID
        ),
        "workspace_execution_contract_id": CONTRACT_ID,
        "execution_plan_sha256": plan["execution_plan_sha256"],
        "authorization_receipt_sha256": authorization_receipt_sha256,
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
        "created_at": commit_published_at,
        "available_at": profile["available_at"],
        "partition_segments": profile["partition_segments"],
        "raw_root": paths.raw_final.relative_to(paths.workspace).as_posix(),
        "curated_root": paths.curated_final.relative_to(paths.workspace).as_posix(),
        "status": "SEALED_SNAPSHOT",
        "formal_workspace_execution": True,
    }
    provenance = {
        "schema_version": 1,
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "mode": "FORMAL_ATOMIC_WORKSPACE_PUBLICATION",
        "source_id": profile["source_id"],
        "contract_id": profile["dataset_contract_id"],
        "request_contract_hash": request_contract_hash(request),
        "payload_hash": payload_hash,
        "available_at": profile["available_at"],
        "execution_plan_sha256": plan["execution_plan_sha256"],
        "authorization_receipt_sha256": authorization_receipt_sha256,
        "published_at": commit_published_at,
    }
    if rights_receipt.get("contract_id") == "source_rights_receipt_v2":
        provenance.update(
            {
                "rights_status": rights_receipt["rights_status"],
                "rights_basis_sha256": rights_receipt["rights_basis"]["sha256"],
                "upstream_evidence_sha256": {
                    key: binding["sha256"]
                    for key, binding in sorted(
                        rights_receipt["upstream_evidence"].items()
                    )
                },
                "independent_contract_verification": False,
                "legal_opinion": False,
            }
        )
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
        "formal_workspace_execution": True,
        "execution_plan_sha256": plan["execution_plan_sha256"],
    }
    content_hash = snapshot_contract_hash(
        request, manifest, provenance, partitions, quality
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
        "published_at": commit_published_at,
    }
    if rights_receipt.get("contract_id") == "source_rights_receipt_v2":
        item.update(
            {
                "rights_status": rights_receipt["rights_status"],
                "rights_basis_sha256": rights_receipt["rights_basis"]["sha256"],
                "upstream_evidence_sha256": {
                    key: binding["sha256"]
                    for key, binding in sorted(
                        rights_receipt["upstream_evidence"].items()
                    )
                },
                "independent_contract_verification": False,
                "legal_opinion": False,
            }
        )
    return documents, content_hash, item


def _copy_one(
    source: Path,
    target: Path,
    expected_bytes: int,
    expected_sha: str,
    workspace: Path,
) -> None:
    source = _safe_path(source, workspace, "payload source")
    target = _safe_path(target, workspace, "staging destination")
    if target.exists():
        raise WorkspaceExecutionError(f"staging destination already exists: {target}")
    if (
        not source.is_file()
        or atomic.is_reparse_point(source)
        or source.stat().st_nlink != 1
    ):
        raise WorkspaceExecutionError(f"payload source is not a plain regular file: {source}")
    if source.stat().st_size != expected_bytes or atomic.sha256_file(source) != expected_sha:
        raise WorkspaceExecutionError(f"payload source changed after plan sealing: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target, follow_symlinks=False)
    if (
        target.stat().st_size != expected_bytes
        or atomic.sha256_file(target) != expected_sha
        or atomic.sha256_file(source) != expected_sha
    ):
        raise WorkspaceExecutionError(f"staged payload verification failed: {target}")


def _verify_payload_root(
    root: Path,
    plan: dict[str, Any],
    layer: str,
    workspace: Path,
) -> None:
    _safe_path(root, workspace, f"{layer} payload root")
    expected = {
        item["snapshot_relative_path"]: (item["bytes"], item["sha256"])
        for item in plan["payload_layout"]
        if item["layer"] == layer
    }
    try:
        files = atomic.ensure_regular_tree(root, f"{layer} payload")
    except atomic.PublishError as exc:
        raise WorkspaceExecutionError(str(exc)) from exc
    actual = {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            atomic.sha256_file(path),
        )
        for path in files
    }
    if actual != expected:
        raise WorkspaceExecutionError(
            f"{layer} payload tree differs from the sealed layout"
        )


def _prepared_hash(value: dict[str, Any]) -> str:
    payload = dict(value)
    payload.pop("prepared_sha256", None)
    return _sha(execution_contract.canonical_bytes(payload))


def _verify_prepared(
    prepared: dict[str, Any],
    plan: dict[str, Any],
    paths: Paths,
) -> None:
    expected = {
        "publisher_id": PUBLISHER_ID,
        "execution_id": plan["execution_id"],
        "request_sha256": plan["request"]["sha256"],
        "execution_plan_sha256": plan["execution_plan_sha256"],
        "catalog_hash_before": plan["catalog_before"]["sha256"],
    }
    if any(prepared.get(key) != value for key, value in expected.items()):
        raise WorkspaceExecutionError("prepared stage binding mismatch")
    if prepared.get("prepared_sha256") != _prepared_hash(prepared):
        raise WorkspaceExecutionError("prepared stage seal mismatch")
    published = prepared.get("commit_published_at")
    try:
        published_at = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkspaceExecutionError("prepared commit_published_at is invalid") from exc
    if published_at.tzinfo is None or published_at.utcoffset() is None:
        raise WorkspaceExecutionError("prepared commit_published_at must be timezone-aware")
    if published_at.astimezone(timezone.utc) > datetime.now(timezone.utc):
        raise WorkspaceExecutionError("prepared commit_published_at cannot be in the future")
    owner_path = paths.stage / "owner.json"
    if not owner_path.is_file() or atomic.is_reparse_point(owner_path):
        raise WorkspaceExecutionError("prepared stage owner is missing or unsafe")
    owner = atomic.load_json(owner_path)
    if (
        owner.get("publisher_id") != PUBLISHER_ID
        or owner.get("execution_id") != plan["execution_id"]
        or owner.get("execution_plan_sha256") != plan["execution_plan_sha256"]
    ):
        raise WorkspaceExecutionError("prepared stage ownership mismatch")


def _prepare_stage(
    paths: Paths,
    plan: dict[str, Any],
    catalog: dict[str, Any],
    *,
    authorization_receipt_sha256: str,
    commit_published_at: str,
    rights_receipt: dict[str, Any],
) -> dict[str, Any]:
    prepared_path = paths.stage / "prepared.json"
    if paths.stage.exists():
        prepared = atomic.load_json(prepared_path)
        _verify_prepared(prepared, plan, paths)
        return prepared
    temporary = paths.stage.with_name(f".{paths.stage.name}.partial.{uuid.uuid4().hex}")
    _safe_path(temporary, paths.workspace, "private staging")
    if temporary.exists():
        raise WorkspaceExecutionError("temporary stage collision")
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
            source = paths.workspace.joinpath(
                *PurePosixPath(item["source_relative_path"]).parts
            )
            destination = (
                temporary
                / item["layer"]
                / Path(item["snapshot_relative_path"])
            )
            _copy_one(
                source,
                destination,
                item["bytes"],
                item["sha256"],
                paths.workspace,
            )
        documents, content_hash, catalog_item = _build_snapshot_contract(
            plan,
            paths,
            authorization_receipt_sha256=authorization_receipt_sha256,
            commit_published_at=commit_published_at,
            rights_receipt=rights_receipt,
        )
        for relative, document in documents.items():
            atomic.write_json(temporary / "snapshot" / relative, document)
        catalog_candidate = copy.deepcopy(catalog)
        if any(
            item.get("dataset_id") == plan["target"]["dataset_id"]
            and item.get("snapshot_id") == plan["target"]["snapshot_id"]
            for item in catalog_candidate.get("datasets", [])
        ):
            raise WorkspaceExecutionError("immutable catalog identity already exists")
        catalog_candidate["datasets"].append(catalog_item)
        catalog_candidate["updated_at"] = commit_published_at
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
            "authorization_receipt_sha256": authorization_receipt_sha256,
            "commit_published_at": commit_published_at,
        }
        if prepared["catalog_hash_before"] != plan["catalog_before"]["sha256"]:
            raise WorkspaceExecutionError("catalog drifted before private staging")
        prepared["prepared_sha256"] = _prepared_hash(prepared)
        atomic.write_json(temporary / "prepared.json", prepared)
        paths.stage.parent.mkdir(parents=True, exist_ok=True)
        atomic.assert_same_volume(paths.catalog, [temporary, paths.stage])
        os.replace(temporary, paths.stage)
    finally:
        if temporary.exists():
            owner = temporary / "owner.json"
            if owner.is_file():
                value = atomic.load_json(owner)
                if (
                    value.get("publisher_id") == PUBLISHER_ID
                    and value.get("execution_id") == plan["execution_id"]
                ):
                    shutil.rmtree(temporary)
    _verify_prepared(prepared, plan, paths)
    return prepared


def _verify_snapshot(
    paths: Paths,
    prepared: dict[str, Any],
    catalog: dict[str, Any],
) -> None:
    try:
        files = atomic.ensure_regular_tree(paths.snapshot_final, "formal snapshot")
    except atomic.PublishError as exc:
        raise WorkspaceExecutionError(str(exc)) from exc
    actual = {path.relative_to(paths.snapshot_final).as_posix() for path in files}
    if actual != set(AUTHORITY_RELATIVE_PATHS):
        raise WorkspaceExecutionError("formal snapshot authority file set mismatch")
    errors = verify_snapshot_contract(
        catalog=catalog,
        item=prepared["catalog_item"],
        snapshot_path=paths.snapshot_final,
        deep_payload=True,
    )
    if errors:
        raise WorkspaceExecutionError(
            "formal snapshot verification failed: " + " | ".join(errors)
        )


def _finalize_payload(
    paths: Paths,
    plan: dict[str, Any],
    prepared: dict[str, Any],
) -> None:
    _verify_prepared(prepared, plan, paths)
    atomic.assert_same_volume(
        paths.catalog,
        [paths.stage, paths.raw_final, paths.curated_final, paths.snapshot_final],
    )
    for layer, final in (("raw", paths.raw_final), ("curated", paths.curated_final)):
        staged = paths.stage / layer
        if final.exists():
            _verify_payload_root(final, plan, layer, paths.workspace)
            if staged.exists():
                _verify_payload_root(staged, plan, layer, paths.workspace)
        else:
            if not staged.is_dir():
                raise WorkspaceExecutionError(f"missing staged {layer} payload")
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, final)
            _verify_payload_root(final, plan, layer, paths.workspace)
    candidate = atomic.load_json(paths.stage / "catalog-candidate.json")
    if paths.snapshot_final.exists():
        _verify_snapshot(paths, prepared, candidate)
    else:
        staged_snapshot = paths.stage / "snapshot"
        if not staged_snapshot.is_dir():
            raise WorkspaceExecutionError("missing staged snapshot contract")
        paths.snapshot_final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged_snapshot, paths.snapshot_final)
        _verify_snapshot(paths, prepared, candidate)


def _replace_catalog_bytes(
    path: Path,
    payload: bytes,
    *,
    expected_before: str,
    expected_after: str,
) -> None:
    if _sha(payload) != expected_after:
        raise WorkspaceExecutionError("catalog replacement payload hash mismatch")
    if atomic.sha256_file(path) != expected_before:
        raise WorkspaceExecutionError("catalog compare-and-swap base hash mismatch")
    temporary: Path | None = None
    try:
        descriptor, raw = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.formal-workspace.",
            suffix=".tmp",
        )
        temporary = Path(raw)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if atomic.sha256_file(temporary) != expected_after:
            raise WorkspaceExecutionError("catalog temporary hash mismatch")
        os.replace(temporary, path)
        temporary = None
        if atomic.sha256_file(path) != expected_after:
            raise WorkspaceExecutionError("catalog hash mismatch after atomic replace")
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _publish_catalog(paths: Paths, prepared: dict[str, Any]) -> None:
    current = atomic.sha256_file(paths.catalog)
    if current == prepared["catalog_hash_after"]:
        return
    if current != prepared["catalog_hash_before"]:
        raise WorkspaceExecutionError("workspace catalog drifted after staging")
    candidate_path = paths.stage / "catalog-candidate.json"
    candidate = atomic.load_json(candidate_path)
    errors = validate_dataset_catalog.validate_catalog(candidate)
    if errors:
        raise WorkspaceExecutionError(
            "candidate catalog is invalid: " + " | ".join(errors)
        )
    _replace_catalog_bytes(
        paths.catalog,
        candidate_path.read_bytes(),
        expected_before=prepared["catalog_hash_before"],
        expected_after=prepared["catalog_hash_after"],
    )


def _qdata_postcondition(
    paths: Paths,
    plan: dict[str, Any],
    prepared: dict[str, Any],
    *,
    inject_failure: bool,
    qdata_runner: factor_qdata.Runner | None,
) -> None:
    if inject_failure:
        raise QdataPostconditionError("injected qdata research postcondition failure")
    catalog = dataset_cli.load_catalog(paths.catalog)
    qdata = plan.get("preflight", {}).get("qdata_postconditions")
    if qdata is None:
        preflight_path = paths.workspace.joinpath(
            *PurePosixPath(plan["preflight"]["relative_path"]).parts
        )
        qdata = preflight_module.preflight(
            preflight_path,
            workspace_root=paths.workspace,
            allow_existing_target_for_recovery=True,
        )["qdata_postconditions"]
    item = dataset_cli.find_dataset(
        catalog,
        plan["target"]["dataset_id"],
        plan["target"]["snapshot_id"],
    )
    dataset_cli.check_access(catalog, item, qdata["intent"], qdata["consumer"])
    if (
        item.get("dataset_id") != plan["target"]["dataset_id"]
        or item.get("snapshot_id") != plan["target"]["snapshot_id"]
        or Path(str(item.get("physical_path", "")).replace("/", os.sep)).resolve()
        != paths.snapshot_final.resolve()
    ):
        raise QdataPostconditionError("qdata resolved a non-exact identity or path")
    errors = verify_snapshot_contract(
        catalog=catalog,
        item=item,
        snapshot_path=paths.snapshot_final,
        deep_payload=True,
    )
    if errors:
        raise QdataPostconditionError(
            "qdata snapshot verification failed: " + " | ".join(errors)
        )
    if (
        item.get("quality_status") != "OK"
        or item.get("lifecycle") != "frozen"
        or item.get("mutability") != "immutable"
        or item.get("purpose_ceiling") != "RESEARCH_ONLY"
        or item.get("license_status") not in {"DECLARED", "VERIFIED"}
        or item.get("content_hash") != prepared["content_hash"]
        or "latest" in str(item.get("snapshot_id", "")).lower()
    ):
        raise QdataPostconditionError(
            "qdata resolved an ineligible research snapshot"
        )
    try:
        resolved = factor_qdata.QDataResearchResolver(
            workspace_root=paths.workspace,
            runner=qdata_runner,
        ).resolve(
            plan["target"]["dataset_id"],
            plan["target"]["snapshot_id"],
        )
    except factor_qdata.QDataInputError as exc:
        raise QdataPostconditionError(
            f"factor research qdata resolver rejected the snapshot: {exc}"
        ) from exc
    if (
        resolved.lifecycle != "frozen"
        or resolved.quality_status != "OK"
        or resolved.license_status not in {"DECLARED", "VERIFIED"}
        or resolved.content_hash != prepared["content_hash"]
        or resolved.resolved_path.resolve() != paths.snapshot_final.resolve()
    ):
        raise QdataPostconditionError(
            "factor research resolver returned a non-exact formal snapshot"
        )


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
        raise WorkspaceExecutionError(
            "cannot rollback because catalog CAS identity drifted"
        )
    if paths.rollback.exists():
        raise WorkspaceExecutionError("formal rollback target already exists")
    paths.rollback.mkdir(parents=True)
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


def _remove_owned_stage(paths: Paths, plan: dict[str, Any]) -> None:
    if not paths.stage.exists():
        return
    _verify_prepared(atomic.load_json(paths.stage / "prepared.json"), plan, paths)
    shutil.rmtree(paths.stage)


def _result(
    paths: Paths,
    plan: dict[str, Any],
    status: str,
    state: str,
    *,
    write_performed: bool,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contract_id": CONTRACT_ID,
        "execution_id": plan["execution_id"],
        "status": status,
        "state": state,
        "write_performed": write_performed,
        "workspace_root": _path_text(paths.workspace),
        "catalog_baseline_sha256": plan["catalog_before"]["sha256"],
        "catalog_path": _path_text(paths.catalog),
        "snapshot_path": _path_text(paths.snapshot_final),
        "journal_path": _path_text(paths.journal),
        "authorization_ledger_path": _path_text(paths.ledger),
        "formal_effect": "WORKSPACE" if write_performed else "NONE",
    }


def _execute_at_workspace(
    *,
    request: Path,
    workspace_root: Path,
    catalog_baseline_sha256: str,
    enabled: bool,
    recover_hash: str | None,
    trusted_workspace_root: Path,
    failpoint: str | None = None,
    inject_qdata_failure: bool = False,
    qdata_runner: factor_qdata.Runner | None = None,
) -> dict[str, Any]:
    """Internal engine; CLI always pins ``trusted_workspace_root`` to ``ROOT``."""

    if failpoint is not None and failpoint not in _TEST_FAILPOINTS:
        raise WorkspaceExecutionError(f"unknown internal failpoint: {failpoint}")
    allow_recovery_state = recover_hash is not None
    workspace = Path(os.path.abspath(workspace_root))
    try:
        plan = execution_contract.validate_execution(
            request,
            workspace_root=workspace,
            allow_recovery_state=allow_recovery_state,
        )
    except Exception as exc:
        raise WorkspaceExecutionError(str(exc)) from exc
    if catalog_baseline_sha256 != plan["catalog_before"]["sha256"]:
        raise WorkspaceExecutionError(
            "explicit catalog baseline does not match the sealed execution request"
        )
    catalog = _assert_exact_workspace(
        workspace,
        trusted_workspace_root=trusted_workspace_root,
        catalog_baseline_sha256=catalog_baseline_sha256,
        allow_recovery_state=allow_recovery_state,
    )
    authorization, authorization_receipt_sha256 = _authorization_binding(
        plan, workspace
    )
    rights_receipt = _rights_binding(plan, workspace)
    paths = _build_paths(workspace, plan, authorization["authorization_id"])
    if not enabled:
        return _result(
            paths,
            plan,
            "PASS__WORKSPACE_EXECUTION_DISABLED__NO_WRITE",
            "INPUT_INVENTORY_SEALED",
            write_performed=False,
        )

    commit_clock_execution = (
        plan.get("execution_contract_id") == execution_contract.CONTRACT_ID_V2
    )
    if commit_clock_execution:
        # This must run before a single-use authorization is reserved or any
        # publication state is written.
        _assert_available_for_execution(plan)

    journal = _existing_journal(paths, plan)
    if paths.ledger.exists():
        ledger = atomic.load_json(paths.ledger)
        if ledger.get("execution_id") != plan["execution_id"]:
            raise WorkspaceExecutionError("authorization single-use replay detected")
    if journal is None and any(
        path.exists()
        for path in (
            paths.stage,
            paths.raw_final,
            paths.curated_final,
            paths.snapshot_final,
        )
    ):
        raise WorkspaceExecutionError(
            "immutable target or publication path already exists without a bound journal"
        )
    if journal is not None and journal.get("state") == "PUBLISHED":
        ledger = atomic.load_json(paths.ledger)
        if ledger.get("state") != "CONSUMED":
            raise WorkspaceExecutionError(
                "published journal lacks consumed authorization"
            )
        _qdata_postcondition(
            paths, plan, atomic.load_json(paths.stage / "prepared.json")
            if paths.stage.exists()
            else {
                "content_hash": next(
                    item["content_hash"]
                    for item in atomic.load_json(paths.catalog)["datasets"]
                    if item.get("dataset_id") == plan["target"]["dataset_id"]
                    and item.get("snapshot_id") == plan["target"]["snapshot_id"]
                )
            },
            inject_failure=False,
            qdata_runner=qdata_runner,
        )
        return _result(
            paths,
            plan,
            "NO_CHANGE",
            "PUBLISHED",
            write_performed=False,
        )

    _reserve_authorization(
        paths,
        plan,
        authorization,
        authorization_receipt_sha256,
    )
    if journal is None:
        _write_journal(paths, plan, "EVIDENCE_VERIFIED")
        _write_journal(paths, plan, "INPUT_INVENTORY_SEALED")
    lock = None
    if commit_clock_execution:
        lock = _acquire_lock(paths, plan, recover_hash)
        commit_published_at = atomic.utc_now()
    else:
        commit_published_at = plan["publication_profile"]["published_at"]
    try:
        prepared = _prepare_stage(
            paths,
            plan,
            catalog,
            authorization_receipt_sha256=authorization_receipt_sha256,
            commit_published_at=commit_published_at,
            rights_receipt=rights_receipt,
        )
        _write_journal(
            paths,
            plan,
            "PRIVATE_STAGING_SEALED",
            catalog_hash_before=prepared["catalog_hash_before"],
            catalog_hash_after=prepared["catalog_hash_after"],
            content_hash=prepared["content_hash"],
            commit_published_at=prepared["commit_published_at"],
        )
        if failpoint == "hard_crash_after_private_staging":
            os._exit(90)
    except BaseException:
        if lock is not None and paths.lock.exists():
            _remove_lock(paths, lock["lock_id"])
        raise
    if lock is None:
        lock = _acquire_lock(paths, plan, recover_hash)
    lock_id = lock["lock_id"]
    catalog_visible = atomic.sha256_file(paths.catalog) == prepared["catalog_hash_after"]
    authorization_consumed = atomic.load_json(paths.ledger).get("state") == "CONSUMED"
    try:
        _write_journal(paths, plan, "CATALOG_LOCK_ACQUIRED", lock_id=lock_id)
        _finalize_payload(paths, plan, prepared)
        _write_journal(
            paths, plan, "PAYLOAD_FINALIZED_INVISIBLE", lock_id=lock_id
        )
        if failpoint == "hard_crash_after_payload_finalize":
            os._exit(91)
        _publish_catalog(paths, prepared)
        catalog_visible = True
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
                prepared,
                inject_failure=inject_qdata_failure,
                qdata_runner=qdata_runner,
            )
            _write_journal(paths, plan, "QDATA_RESEARCH_RESOLVED", lock_id=lock_id)
        except Exception as exc:
            _rollback_after_qdata_failure(paths, plan, prepared, str(exc))
            catalog_visible = False
            raise QdataPostconditionError(str(exc)) from exc
        if failpoint == "hard_crash_after_qdata_success":
            os._exit(93)
        _update_ledger(paths, plan, "CONSUMED")
        authorization_consumed = True
        _write_journal(paths, plan, "AUTHORIZATION_CONSUMED", lock_id=lock_id)
        cleanup_error: str | None = None
        try:
            _remove_owned_stage(paths, plan)
        except Exception as exc:  # published data remains valid and visible
            cleanup_error = f"{type(exc).__name__}: {exc}"
        final_state = "PUBLISHED_CLEANUP_PENDING" if cleanup_error else "PUBLISHED"
        _write_journal(
            paths,
            plan,
            final_state,
            lock_id=lock_id,
            catalog_hash_after=atomic.sha256_file(paths.catalog),
            content_hash=prepared["content_hash"],
            **({"cleanup_error": cleanup_error} if cleanup_error else {}),
        )
    except BaseException as exc:
        if catalog_visible and not authorization_consumed and paths.stage.exists():
            try:
                _rollback_after_qdata_failure(paths, plan, prepared, str(exc))
            except Exception as rollback_exc:
                raise WorkspaceExecutionError(
                    f"publication failed and rollback also failed: {rollback_exc}"
                ) from exc
        raise
    finally:
        if paths.lock.exists():
            _remove_lock(paths, lock_id)
    return _result(
        paths,
        plan,
        final_state,
        final_state,
        write_performed=True,
    )


def execute(
    *,
    request: Path,
    workspace_root: Path,
    catalog_baseline_sha256: str,
    enabled: bool,
    recover_hash: str | None,
) -> dict[str, Any]:
    return _execute_at_workspace(
        request=request,
        workspace_root=workspace_root,
        catalog_baseline_sha256=catalog_baseline_sha256,
        enabled=enabled,
        recover_hash=recover_hash,
        trusted_workspace_root=ROOT,
    )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Publish one sealed formal snapshot into the exact Quantification workspace; "
            "dry-run unless --execute-workspace is supplied."
        )
    )
    value.add_argument("--request", required=True, type=Path)
    value.add_argument("--workspace-root", required=True, type=Path)
    value.add_argument("--catalog-baseline-sha256", required=True)
    value.add_argument("--execute-workspace", action="store_true")
    value.add_argument("--recover-stale-lock-sha256")
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        result = execute(
            request=args.request,
            workspace_root=args.workspace_root,
            catalog_baseline_sha256=args.catalog_baseline_sha256,
            enabled=bool(args.execute_workspace),
            recover_hash=args.recover_stale_lock_sha256,
        )
    except (WorkspaceExecutionError, atomic.PublishError) as exc:
        print(f"WORKSPACE FORMAL PUBLISH REJECTED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
