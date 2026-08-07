#!/usr/bin/env python3
"""Seal a no-write, hash-bound plan for one future formal atomic publication.

This is the production control-plane skeleton, not the commit engine.  It
revalidates the complete formal preflight closure, current catalog and exact
raw/curated payload layout, then prints a deterministic INPUT_INVENTORY_SEALED
plan.  It deliberately has no write or command-launch surface.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import atomic_snapshot_publish as atomic  # noqa: E402
import run_formal_publisher_preflight as preflight_module  # noqa: E402


CONTRACT_ID = "formal_atomic_publish_execution_v1"
CONTRACT_ID_V2 = "formal_atomic_publish_execution_v2"
SUPPORTED_CONTRACTS = {CONTRACT_ID, CONTRACT_ID_V2}
CONTRACT_ROOT = ROOT / "data" / "catalog" / "contracts" / CONTRACT_ID
REQUEST_SCHEMA = CONTRACT_ROOT / "request.schema.json"
REQUEST_SCHEMA_V2 = (
    ROOT / "data" / "catalog" / "contracts" / CONTRACT_ID_V2 / "request.schema.json"
)
REQUEST_ROOT = ROOT / "tmp" / "formal-publisher-execution" / "requests"
PREFLIGHT_REQUEST_ROOT = ROOT / "tmp" / "formal-publisher-preflight" / "requests"
CATALOG_RELATIVE = "data/catalog/datasets.json"
CATALOG_PATH = ROOT / "data" / "catalog" / "datasets.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}$")
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
REQUEST_FIELDS = {
    "schema_version",
    "contract_id",
    "execution_id",
    "preflight_request",
    "catalog_before",
    "target",
    "payload_layout",
    "publication_profile",
    "state_machine_requirements",
}
PREFLIGHT_BINDING_FIELDS = {"relative_path", "sha256", "bytes", "contract_id"}
CATALOG_BINDING_FIELDS = {"relative_path", "sha256", "bytes"}
LAYOUT_FIELDS = {
    "source_relative_path",
    "layer",
    "snapshot_relative_path",
    "sha256",
    "bytes",
}
PROFILE_FIELDS_V1 = {
    "store_id",
    "owner_project",
    "lifecycle",
    "mutability",
    "purpose_ceiling",
    "license_status",
    "quality_status",
    "consumers",
    "known_limits",
    "notes",
    "source_id",
    "dataset_contract_id",
    "dataset_schema_version",
    "research_input_contract",
    "available_at",
    "published_at",
    "partition_segments",
}
PROFILE_FIELDS_V2 = (PROFILE_FIELDS_V1 - {"published_at"}) | {"published_at_policy"}
STATE_REQUIREMENTS = {
    "require_private_staging": True,
    "require_same_volume_finalize": True,
    "require_catalog_compare_and_swap": True,
    "require_crash_recovery": True,
    "require_qdata_postcondition_before_success": True,
    "require_catalog_rollback_on_qdata_failure": True,
    "consume_authorization_only_after_qdata_success": True,
    "failure_action": "ROLLBACK_OR_FAIL_CLOSED",
}
SUCCESS_STATES = [
    "EVIDENCE_VERIFIED",
    "INPUT_INVENTORY_SEALED",
    "PRIVATE_STAGING_SEALED",
    "CATALOG_LOCK_ACQUIRED",
    "PAYLOAD_FINALIZED_INVISIBLE",
    "CATALOG_ATOMICALLY_VISIBLE",
    "QDATA_RESEARCH_RESOLVED",
    "AUTHORIZATION_CONSUMED",
    "PUBLISHED",
]


class ExecutionPlanError(RuntimeError):
    """Fail-closed request, evidence, payload, catalog or state error."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse_or_symlink(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _assert_plain_existing_chain(base: Path, target: Path) -> None:
    base_abs = Path(os.path.abspath(base))
    target_abs = Path(os.path.abspath(target))
    try:
        relative = target_abs.relative_to(base_abs)
    except ValueError as exc:
        raise ExecutionPlanError(f"path escapes workspace: {target}") from exc
    current = base_abs
    if _is_reparse_or_symlink(current):
        raise ExecutionPlanError(f"workspace root is a link/reparse point: {current}")
    for part in relative.parts:
        current = current / part
        if not current.exists():
            break
        if _is_reparse_or_symlink(current):
            raise ExecutionPlanError(f"link/reparse point is forbidden: {current}")


def _canonical_relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ExecutionPlanError(f"{label} must be a canonical POSIX path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ExecutionPlanError(f"{label} is not a safe relative path")
    for part in pure.parts:
        if part.rstrip(" .") != part:
            raise ExecutionPlanError(f"{label} contains a trailing dot/space component")
        if part.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
            raise ExecutionPlanError(f"{label} contains a reserved Windows name")
    return pure.as_posix()


def _require_fields(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        raise ExecutionPlanError(
            f"{label} fields mismatch; missing={missing}; extra={extra}"
        )


def _require_positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ExecutionPlanError(f"{label} must be a positive integer")
    return value


def _require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ExecutionPlanError(f"{label} must be a lowercase SHA-256")
    return value


def _require_identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        raise ExecutionPlanError(f"{label} is not a safe identity")
    if value.lower() in {"latest", "current", "working", "mutable", "tmp", "temp"}:
        raise ExecutionPlanError(f"{label} uses a mutable alias")
    return value


def _resolve_direct_request(path: Path, workspace_root: Path = ROOT) -> Path:
    root = Path(os.path.abspath(workspace_root))
    candidate = path if path.is_absolute() else root / path
    candidate = Path(os.path.abspath(candidate))
    request_root = root / "tmp/formal-publisher-execution/requests"
    try:
        relative = candidate.relative_to(request_root)
    except ValueError as exc:
        raise ExecutionPlanError(
            "request must be below the dedicated execution request root"
        ) from exc
    if len(relative.parts) != 1 or candidate.suffix.lower() != ".json":
        raise ExecutionPlanError("request must be one direct JSON child of request root")
    _assert_plain_existing_chain(root, candidate)
    if not candidate.is_file() or candidate.stat().st_nlink != 1:
        raise ExecutionPlanError("request must be a plain existing regular file")
    return candidate


def _load_and_validate_request(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        request, payload = preflight_module.load_json_object(path, "execution request")
        contract_id = request.get("contract_id")
        schema_path = REQUEST_SCHEMA_V2 if contract_id == CONTRACT_ID_V2 else REQUEST_SCHEMA
        schema, _ = preflight_module.load_json_object(schema_path, "execution schema")
        atomic.validate_supported_schema(schema, "formal execution request schema")
        atomic.validate_json_schema_value(
            request,
            schema,
            schema,
            "request",
        )
    except (atomic.PublishError, preflight_module.PreflightError) as exc:
        raise ExecutionPlanError(str(exc)) from exc
    _require_fields(request, REQUEST_FIELDS, "execution request")
    expected_schema_version = 2 if request.get("contract_id") == CONTRACT_ID_V2 else 1
    if (
        request.get("contract_id") not in SUPPORTED_CONTRACTS
        or request.get("schema_version") != expected_schema_version
    ):
        raise ExecutionPlanError("execution request contract/schema mismatch")
    _require_identity(request.get("execution_id"), "execution_id")
    return request, payload


def _resolve_preflight_binding(
    binding: Any,
    workspace_root: Path = ROOT,
    allow_recovery_state: bool = False,
) -> tuple[Path, dict[str, Any]]:
    root = Path(os.path.abspath(workspace_root))
    if not isinstance(binding, dict):
        raise ExecutionPlanError("preflight_request binding must be an object")
    _require_fields(binding, PREFLIGHT_BINDING_FIELDS, "preflight_request binding")
    if binding.get("contract_id") != preflight_module.CONTRACT_ID:
        raise ExecutionPlanError("preflight_request contract_id mismatch")
    relative = _canonical_relative(
        binding.get("relative_path"),
        "preflight_request.relative_path",
    )
    path = root.joinpath(*PurePosixPath(relative).parts)
    request_root = root / "tmp/formal-publisher-preflight/requests"
    try:
        child = Path(os.path.abspath(path)).relative_to(request_root)
    except ValueError as exc:
        raise ExecutionPlanError("preflight request is outside its dedicated root") from exc
    if len(child.parts) != 1 or path.suffix.lower() != ".json":
        raise ExecutionPlanError("preflight request must be one direct JSON child")
    _assert_plain_existing_chain(root, path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ExecutionPlanError("preflight request must be a plain regular file")
    expected_bytes = _require_positive_int(binding.get("bytes"), "preflight_request.bytes")
    expected_sha = _require_sha(binding.get("sha256"), "preflight_request.sha256")
    if path.stat().st_size != expected_bytes or sha256_file(path) != expected_sha:
        raise ExecutionPlanError("preflight request hash/byte binding mismatch")
    try:
        result = preflight_module.preflight(
            path,
            workspace_root=root,
            allow_existing_target_for_recovery=allow_recovery_state,
        )
    except (OSError, ValueError, preflight_module.PreflightError) as exc:
        raise ExecutionPlanError(str(exc)) from exc
    if result.get("status") != "PASS__NON_EXECUTABLE_PREFLIGHT_ONLY":
        raise ExecutionPlanError("formal preflight did not return its exact PASS status")
    return path, result


def _validate_catalog(
    binding: Any,
    workspace_root: Path = ROOT,
    allow_recovery_state: bool = False,
) -> dict[str, Any]:
    root = Path(os.path.abspath(workspace_root))
    catalog_path = root / CATALOG_RELATIVE
    if not isinstance(binding, dict):
        raise ExecutionPlanError("catalog_before must be an object")
    _require_fields(binding, CATALOG_BINDING_FIELDS, "catalog_before")
    if binding.get("relative_path") != CATALOG_RELATIVE:
        raise ExecutionPlanError("catalog_before must bind the exact formal datasets.json")
    _assert_plain_existing_chain(root, catalog_path)
    if not catalog_path.is_file() or catalog_path.stat().st_nlink != 1:
        raise ExecutionPlanError("formal catalog must be a plain existing regular file")
    expected_bytes = _require_positive_int(binding.get("bytes"), "catalog_before.bytes")
    expected_sha = _require_sha(binding.get("sha256"), "catalog_before.sha256")
    actual_bytes = catalog_path.stat().st_size
    actual_sha = sha256_file(catalog_path)
    if (
        actual_bytes != expected_bytes or actual_sha != expected_sha
    ) and not allow_recovery_state:
        raise ExecutionPlanError("catalog_before hash/byte binding mismatch")
    return {
        "relative_path": CATALOG_RELATIVE,
        "bytes": expected_bytes,
        "sha256": expected_sha,
    }


def _validate_target(
    target: Any,
    preflight: dict[str, Any],
    workspace_root: Path = ROOT,
    allow_recovery_state: bool = False,
) -> dict[str, Any]:
    root = Path(os.path.abspath(workspace_root))
    if not isinstance(target, dict):
        raise ExecutionPlanError("target must be an object")
    preflight_target = preflight.get("target")
    if not isinstance(preflight_target, dict):
        raise ExecutionPlanError("preflight target is missing")
    expected = {
        key: preflight_target.get(key)
        for key in (
            "dataset_id",
            "snapshot_id",
            "formal_root",
            "target_relative_path",
        )
    }
    if target != expected:
        raise ExecutionPlanError("execution target does not exactly match preflight target")
    dataset_id = _require_identity(target.get("dataset_id"), "target.dataset_id")
    snapshot_id = _require_identity(target.get("snapshot_id"), "target.snapshot_id")
    expected_path = f"{target.get('formal_root')}/{dataset_id}/{snapshot_id}"
    if target.get("target_relative_path") != expected_path:
        raise ExecutionPlanError("target path is not the exact immutable identity path")
    target_path = root.joinpath(*PurePosixPath(expected_path).parts)
    _assert_plain_existing_chain(root, target_path)
    if target_path.exists() and not allow_recovery_state:
        raise ExecutionPlanError("immutable target already exists")
    return dict(target)


def _candidate_artifacts(
    preflight_path: Path,
    workspace_root: Path = ROOT,
) -> dict[str, dict[str, Any]]:
    root = Path(os.path.abspath(workspace_root))
    request, _ = preflight_module.load_json_object(preflight_path, "preflight request")
    binding = request.get("candidate_manifest")
    if not isinstance(binding, dict):
        raise ExecutionPlanError("preflight candidate manifest binding is missing")
    candidate_relative = _canonical_relative(
        binding.get("relative_path"),
        "candidate_manifest.relative_path",
    )
    candidate_path = root.joinpath(*PurePosixPath(candidate_relative).parts)
    _assert_plain_existing_chain(root, candidate_path)
    if not candidate_path.is_file() or candidate_path.stat().st_nlink != 1:
        raise ExecutionPlanError("candidate manifest must be a plain regular file")
    candidate, candidate_payload = preflight_module.load_json_object(
        candidate_path,
        "candidate manifest",
    )
    expected_sha = _require_sha(binding.get("sha256"), "candidate_manifest.sha256")
    expected_bytes = _require_positive_int(
        binding.get("bytes"),
        "candidate_manifest.bytes",
    )
    if len(candidate_payload) != expected_bytes or sha256_bytes(candidate_payload) != expected_sha:
        raise ExecutionPlanError("candidate manifest changed after formal preflight")
    raw = candidate.get("artifacts")
    if not isinstance(raw, list) or not raw:
        raise ExecutionPlanError("candidate manifest has no artifacts")
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ExecutionPlanError(f"candidate artifact {index} must be an object")
        relative = _canonical_relative(
            item.get("relative_path"),
            f"candidate artifact {index}.relative_path",
        )
        if relative in result:
            raise ExecutionPlanError(f"duplicate candidate artifact path: {relative}")
        result[relative] = {
            "sha256": _require_sha(item.get("sha256"), f"candidate artifact {index}.sha256"),
            "bytes": _require_positive_int(
                item.get("bytes"),
                f"candidate artifact {index}.bytes",
            ),
        }
    return result


def _validate_layout(
    layout: Any,
    artifacts: dict[str, dict[str, Any]],
    workspace_root: Path = ROOT,
) -> list[dict[str, Any]]:
    root = Path(os.path.abspath(workspace_root))
    if not isinstance(layout, list) or len(layout) < 2:
        raise ExecutionPlanError("payload_layout must contain at least raw and curated files")
    records: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    seen_destinations: set[tuple[str, str]] = set()
    layers: set[str] = set()
    for index, item in enumerate(layout):
        if not isinstance(item, dict):
            raise ExecutionPlanError(f"payload_layout[{index}] must be an object")
        _require_fields(item, LAYOUT_FIELDS, f"payload_layout[{index}]")
        source = _canonical_relative(
            item.get("source_relative_path"),
            f"payload_layout[{index}].source_relative_path",
        )
        layer = item.get("layer")
        if layer not in {"raw", "curated"}:
            raise ExecutionPlanError(f"payload_layout[{index}].layer is invalid")
        destination = _canonical_relative(
            item.get("snapshot_relative_path"),
            f"payload_layout[{index}].snapshot_relative_path",
        )
        folded_destination = (layer, destination.casefold())
        if source in seen_sources:
            raise ExecutionPlanError(f"duplicate source mapping: {source}")
        if folded_destination in seen_destinations:
            raise ExecutionPlanError(
                f"duplicate/case-folded payload destination: {layer}/{destination}"
            )
        seen_sources.add(source)
        seen_destinations.add(folded_destination)
        layers.add(layer)
        expected = artifacts.get(source)
        if expected is None:
            raise ExecutionPlanError(f"layout source is absent from candidate manifest: {source}")
        expected_sha = _require_sha(item.get("sha256"), f"payload_layout[{index}].sha256")
        expected_bytes = _require_positive_int(
            item.get("bytes"),
            f"payload_layout[{index}].bytes",
        )
        if expected != {"sha256": expected_sha, "bytes": expected_bytes}:
            raise ExecutionPlanError(f"layout hash/bytes differ from candidate manifest: {source}")
        source_path = root.joinpath(*PurePosixPath(source).parts)
        _assert_plain_existing_chain(root, source_path)
        if not source_path.is_file() or source_path.stat().st_nlink != 1:
            raise ExecutionPlanError(f"layout source must be a plain regular file: {source}")
        if source_path.stat().st_size != expected_bytes or sha256_file(source_path) != expected_sha:
            raise ExecutionPlanError(f"layout source hash/byte mismatch: {source}")
        records.append(
            {
                "source_relative_path": source,
                "layer": layer,
                "snapshot_relative_path": destination,
                "sha256": expected_sha,
                "bytes": expected_bytes,
            }
        )
    if seen_sources != set(artifacts):
        missing = sorted(set(artifacts) - seen_sources)
        extra = sorted(seen_sources - set(artifacts))
        raise ExecutionPlanError(
            f"payload layout must exactly cover candidate artifacts; missing={missing}; extra={extra}"
        )
    if layers != {"raw", "curated"}:
        raise ExecutionPlanError("payload layout must contain both raw and curated layers")
    return sorted(
        records,
        key=lambda item: (item["layer"], item["snapshot_relative_path"]),
    )


def _validate_state_requirements(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExecutionPlanError("state_machine_requirements must be an object")
    _require_fields(value, set(STATE_REQUIREMENTS), "state_machine_requirements")
    for field, expected in STATE_REQUIREMENTS.items():
        if value.get(field) != expected or type(value.get(field)) is not type(expected):
            raise ExecutionPlanError(f"state_machine_requirements.{field} must equal {expected!r}")
    return dict(value)


def _load_preflight_evidence_value(
    preflight_path: Path,
    field: str,
    label: str,
    workspace_root: Path = ROOT,
) -> dict[str, Any]:
    root = Path(os.path.abspath(workspace_root))
    request, _ = preflight_module.load_json_object(preflight_path, "preflight request")
    binding = request.get(field)
    if not isinstance(binding, dict):
        raise ExecutionPlanError(f"preflight {field} binding is missing")
    relative = _canonical_relative(binding.get("relative_path"), f"{field}.relative_path")
    path = root.joinpath(*PurePosixPath(relative).parts)
    _assert_plain_existing_chain(root, path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ExecutionPlanError(f"{label} must be a plain regular file")
    value, payload = preflight_module.load_json_object(path, label)
    expected_sha = _require_sha(binding.get("sha256"), f"{field}.sha256")
    expected_bytes = _require_positive_int(binding.get("bytes"), f"{field}.bytes")
    if len(payload) != expected_bytes or sha256_bytes(payload) != expected_sha:
        raise ExecutionPlanError(f"{label} changed after formal preflight")
    return value


def _parse_timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExecutionPlanError(f"{label} must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionPlanError(f"{label} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ExecutionPlanError(f"{label} must include an explicit timezone")
    return value


def _validate_publication_profile(
    value: Any,
    *,
    preflight_path: Path,
    preflight: dict[str, Any],
    target: dict[str, Any],
    execution_contract_id: str,
    workspace_root: Path = ROOT,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExecutionPlanError("publication_profile must be an object")
    profile_fields = (
        PROFILE_FIELDS_V2
        if execution_contract_id == CONTRACT_ID_V2
        else PROFILE_FIELDS_V1
    )
    _require_fields(value, profile_fields, "publication_profile")
    for field in ("store_id", "owner_project", "source_id", "dataset_contract_id", "dataset_schema_version"):
        _require_identity(value.get(field), f"publication_profile.{field}")
    required_values = {
        "lifecycle": "frozen",
        "mutability": "immutable",
        "purpose_ceiling": "RESEARCH_ONLY",
        "quality_status": "OK",
    }
    for field, expected in required_values.items():
        if value.get(field) != expected:
            raise ExecutionPlanError(f"publication_profile.{field} must equal {expected!r}")
    if value.get("license_status") not in {"DECLARED", "VERIFIED"}:
        raise ExecutionPlanError(
            "publication_profile.license_status must be DECLARED or VERIFIED"
        )
    expected_store = {
        "data/market/snapshots": "workspace-market-snapshots",
        "data/published": "workspace-published-features",
    }[target["formal_root"]]
    if value.get("store_id") != expected_store or value.get("owner_project") != "workspace":
        raise ExecutionPlanError("publication_profile store/owner does not match the formal root")
    rights = _load_preflight_evidence_value(
        preflight_path,
        "source_rights_receipt",
        "source rights receipt",
        workspace_root=workspace_root,
    )
    if value.get("source_id") != rights.get("source_id"):
        raise ExecutionPlanError("publication_profile.source_id differs from the rights receipt")
    consumers = value.get("consumers")
    if not isinstance(consumers, list) or not consumers or any(
        not isinstance(item, str) for item in consumers
    ) or len(consumers) != len(set(consumers)):
        raise ExecutionPlanError("publication_profile.consumers must be unique identities")
    for index, consumer in enumerate(consumers):
        _require_identity(consumer, f"publication_profile.consumers[{index}]")
    qdata = preflight.get("qdata_postconditions")
    if not isinstance(qdata, dict):
        raise ExecutionPlanError("preflight qdata postconditions are missing")
    qdata_consumer = qdata.get("consumer")
    if qdata_consumer != value.get("owner_project") and qdata_consumer not in consumers:
        raise ExecutionPlanError("qdata consumer is not authorized by publication_profile")
    known_limits = value.get("known_limits")
    if not isinstance(known_limits, list) or not known_limits or any(
        not isinstance(item, str) or not item for item in known_limits
    ):
        raise ExecutionPlanError("publication_profile.known_limits must be non-empty strings")
    if not isinstance(value.get("notes"), str) or not value.get("notes"):
        raise ExecutionPlanError("publication_profile.notes must be non-empty")
    research_input_contract = value.get("research_input_contract")
    if not isinstance(research_input_contract, dict) or not research_input_contract:
        raise ExecutionPlanError(
            "publication_profile.research_input_contract must be a non-empty JSON object"
        )
    try:
        normalized_research_input_contract = json.loads(
            canonical_bytes(research_input_contract).decode("utf-8")
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ExecutionPlanError(
            "publication_profile.research_input_contract is not deterministically JSON serializable"
        ) from exc
    _parse_timestamp(value.get("available_at"), "publication_profile.available_at")
    if execution_contract_id == CONTRACT_ID_V2:
        if value.get("published_at_policy") != "WORKSPACE_ATOMIC_COMMIT_CLOCK":
            raise ExecutionPlanError(
                "publication_profile.published_at_policy must use the workspace commit clock"
            )
        if value.get("license_status") != "DECLARED":
            raise ExecutionPlanError("v2 commit-clock execution requires DECLARED rights")
    else:
        _parse_timestamp(value.get("published_at"), "publication_profile.published_at")
    segments = value.get("partition_segments")
    if not isinstance(segments, list) or len(segments) != len(set(segments)):
        raise ExecutionPlanError("publication_profile.partition_segments must be a unique array")
    for index, segment in enumerate(segments):
        _require_identity(segment, f"publication_profile.partition_segments[{index}]")
    normalized = dict(value)
    normalized["research_input_contract"] = normalized_research_input_contract
    return normalized


def validate_execution(
    request_path: Path,
    *,
    workspace_root: Path = ROOT,
    allow_recovery_state: bool = False,
) -> dict[str, Any]:
    root = Path(os.path.abspath(workspace_root))
    path = _resolve_direct_request(request_path, root)
    request, request_payload = _load_and_validate_request(path)
    preflight_path, preflight = _resolve_preflight_binding(
        request.get("preflight_request"), root, allow_recovery_state
    )
    catalog = _validate_catalog(
        request.get("catalog_before"), root, allow_recovery_state
    )
    target = _validate_target(
        request.get("target"), preflight, root, allow_recovery_state
    )
    artifacts = _candidate_artifacts(preflight_path, root)
    layout = _validate_layout(request.get("payload_layout"), artifacts, root)
    profile = _validate_publication_profile(
        request.get("publication_profile"),
        preflight_path=preflight_path,
        preflight=preflight,
        target=target,
        execution_contract_id=request["contract_id"],
        workspace_root=root,
    )
    state_requirements = _validate_state_requirements(
        request.get("state_machine_requirements")
    )
    request_record = {
        "relative_path": path.relative_to(root).as_posix(),
        "bytes": len(request_payload),
        "sha256": sha256_bytes(request_payload),
    }
    preflight_record = {
        "relative_path": preflight_path.relative_to(root).as_posix(),
        "bytes": preflight_path.stat().st_size,
        "sha256": sha256_file(preflight_path),
        "preflight_id": preflight.get("preflight_id"),
        "stage_a_bundle_sha256": preflight.get("stage_a_bundle_sha256"),
    }
    state_machine = {
        "current_state": "INPUT_INVENTORY_SEALED",
        "success_order": SUCCESS_STATES,
        "requirements": state_requirements,
        "failure_before_catalog_visibility": "FAIL_CLOSED_WITHOUT_CATALOG_MUTATION",
        "failure_after_catalog_visibility_before_qdata": (
            "COMPARE_AND_SWAP_CATALOG_ROLLBACK_OR_FAIL_CLOSED"
        ),
        "authorization_rule": "CONSUME_ONLY_AFTER_QDATA_RESEARCH_RESOLUTION",
    }
    plan = {
        "execution_contract_id": request["contract_id"],
        "execution_id": request["execution_id"],
        "request": request_record,
        "preflight": preflight_record,
        "catalog_before": catalog,
        "target": target,
        "payload_layout": layout,
        "publication_profile": profile,
        "state_machine": state_machine,
    }
    plan_sha = sha256_bytes(canonical_bytes(plan))
    return {
        "schema_version": 1,
        "contract_id": request["contract_id"],
        "execution_contract_id": request["contract_id"],
        "execution_id": request["execution_id"],
        "status": "PASS__READY_FOR_ATOMIC_COMMIT__NO_WRITE",
        "state": "INPUT_INVENTORY_SEALED",
        "execution_contract_ready": True,
        "executable": False,
        "write_performed": False,
        "formal_effect": "NONE",
        "network_used": False,
        "request": request_record,
        "preflight": preflight_record,
        "catalog_before": catalog,
        "target": target,
        "payload_layout": layout,
        "publication_profile": profile,
        "candidate_artifact_count": len(layout),
        "candidate_artifact_bytes": sum(item["bytes"] for item in layout),
        "state_machine": state_machine,
        "execution_plan_sha256": plan_sha,
        "blocking_gaps": [],
        "boundary": (
            "The evidence and input inventory are sealed, but no staging, catalog lock, "
            "payload move, catalog mutation, qdata call, authorization consumption or "
            "formal publication was attempted."
        ),
    }


def failure_result(error: BaseException) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contract_id": CONTRACT_ID,
        "status": "FAIL_CLOSED",
        "state": "REJECTED_BEFORE_STAGING",
        "execution_contract_ready": False,
        "executable": False,
        "write_performed": False,
        "formal_effect": "NONE",
        "network_used": False,
        "blocking_gaps": [str(error)],
        "boundary": "No publication or formal-state mutation was attempted.",
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Validate and seal a no-write formal atomic publication plan."
    )
    value.add_argument("--request", type=Path, required=True)
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        result = validate_execution(args.request)
    except (
        OSError,
        ValueError,
        ExecutionPlanError,
        atomic.PublishError,
        preflight_module.PreflightError,
    ) as exc:
        print(json.dumps(failure_result(exc), ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
