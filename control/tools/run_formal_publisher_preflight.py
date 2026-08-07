#!/usr/bin/env python3
"""Validate a real-data publication request without making any write.

This tool is deliberately a preflight only.  It reads one explicit request,
verifies the complete evidence/hash chain and prints a non-executable plan to
stdout.  It has no execution mode, does not invoke another program and never
writes the catalog, registry, snapshots, published data, reports or tmp.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ID = "formal_publisher_preflight_v1"
REQUEST_ROOT = ROOT / "tmp" / "formal-publisher-preflight" / "requests"
FORMAL_ROOT_ALLOWLIST = (
    "data/market/snapshots",
    "data/published",
)
EVIDENCE_ROOTS = (
    "tmp",
    "reports/data-governance",
    "data/market/quarantine",
)
CANDIDATE_ARTIFACT_ROOTS = (
    "tmp",
    "data/market/quarantine",
)
EXPECTED_CONTRACTS = {
    "candidate_manifest": "candidate_manifest_v1",
    "stage_a_bundle": "selected_core_stage_a_bundle_v1",
    "source_rights_receipt": (
        "source_rights_receipt_v1",
        "source_rights_receipt_v2",
    ),
    "user_authorization_receipt": (
        "formal_publish_authorization_receipt_v1",
        "formal_publish_authorization_receipt_v2",
    ),
}
AUTHORIZATION_ACTION = "AUTHORIZE_EXACT_FORMAL_SNAPSHOT_PUBLICATION"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}$")
DATASET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
FORBIDDEN_MUTABLE_IDS = {
    "latest",
    "current",
    "working",
    "mutable",
    "temp",
    "tmp",
}
REQUEST_FIELDS = {
    "schema_version",
    "contract_id",
    "preflight_id",
    "candidate_manifest",
    "stage_a_bundle",
    "source_rights_receipt",
    "user_authorization_receipt",
    "target",
    "allowed_formal_roots",
    "qdata_postconditions",
}
BINDING_FIELDS = {"relative_path", "sha256", "bytes", "contract_id"}


class PreflightError(RuntimeError):
    """Fail-closed request, evidence, identity, path or contract error."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_constant(value: str) -> None:
    raise PreflightError(f"non-standard JSON constant is forbidden: {value}")


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PreflightError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    payload = path.read_bytes()
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise PreflightError(f"{label} must be a JSON object")
    return value, payload


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
        raise PreflightError(f"path escapes workspace: {target}") from exc
    current = base_abs
    if _is_reparse_or_symlink(current):
        raise PreflightError(f"workspace root is a link/reparse point: {current}")
    for part in relative.parts:
        current = current / part
        if not current.exists():
            break
        if _is_reparse_or_symlink(current):
            raise PreflightError(f"link/reparse point is forbidden: {current}")


def _canonical_relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise PreflightError(f"{label} must be a canonical POSIX workspace path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise PreflightError(f"{label} is not a safe workspace-relative path")
    return pure.as_posix()


def _under_prefix(relative: str, prefixes: tuple[str, ...]) -> bool:
    pure = PurePosixPath(relative)
    return any(
        pure == PurePosixPath(prefix)
        or PurePosixPath(prefix) in pure.parents
        for prefix in prefixes
    )


def resolve_existing(
    relative: Any,
    *,
    label: str,
    allowed_prefixes: tuple[str, ...],
    workspace_root: Path = ROOT,
) -> tuple[str, Path]:
    canonical = _canonical_relative(relative, label)
    if not _under_prefix(canonical, allowed_prefixes):
        raise PreflightError(f"{label} is outside its reviewed roots")
    root = Path(os.path.abspath(workspace_root))
    path = root.joinpath(*PurePosixPath(canonical).parts)
    _assert_plain_existing_chain(root, path)
    if not path.is_file():
        raise PreflightError(f"{label} is not an existing regular file: {canonical}")
    if path.stat().st_nlink != 1:
        raise PreflightError(f"{label} must not be a hardlink: {canonical}")
    return canonical, path


def _require_exact_fields(value: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        raise PreflightError(f"{label} fields mismatch; missing={missing}; extra={extra}")


def _require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise PreflightError(f"{label} must be a lowercase SHA-256")
    return value


def _require_identity(value: Any, label: str, *, dataset: bool = False) -> str:
    pattern = DATASET_ID_RE if dataset else ID_RE
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise PreflightError(f"{label} is not a safe immutable identity component")
    if value.lower() in FORBIDDEN_MUTABLE_IDS:
        raise PreflightError(f"{label} uses a mutable alias: {value}")
    return value


def verify_binding(
    binding: Any,
    *,
    label: str,
    expected_contract: str | tuple[str, ...],
    workspace_root: Path = ROOT,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    if not isinstance(binding, dict):
        raise PreflightError(f"{label} binding must be an object")
    _require_exact_fields(binding, BINDING_FIELDS, f"{label} binding")
    allowed_contracts = (
        (expected_contract,) if isinstance(expected_contract, str) else expected_contract
    )
    actual_contract = binding.get("contract_id")
    if actual_contract not in allowed_contracts:
        raise PreflightError(f"{label} binding contract_id mismatch")
    relative, path = resolve_existing(
        binding.get("relative_path"),
        label=label,
        allowed_prefixes=EVIDENCE_ROOTS,
        workspace_root=workspace_root,
    )
    expected_sha = _require_sha(binding.get("sha256"), f"{label}.sha256")
    expected_bytes = binding.get("bytes")
    if not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool) or expected_bytes < 1:
        raise PreflightError(f"{label}.bytes must be a positive integer")
    actual_bytes = path.stat().st_size
    actual_sha = sha256_file(path)
    if actual_bytes != expected_bytes or actual_sha != expected_sha:
        raise PreflightError(f"{label} hash/byte binding mismatch")
    value, _ = load_json_object(path, label)
    if value.get("contract_id") != actual_contract:
        raise PreflightError(f"{label} embedded contract_id mismatch")
    return (
        {
            "relative_path": relative,
            "bytes": actual_bytes,
            "sha256": actual_sha,
            "contract_id": actual_contract,
        },
        value,
        path,
    )


def validate_candidate(
    value: dict[str, Any],
    *,
    dataset_id: str,
    snapshot_id: str,
    workspace_root: Path = ROOT,
) -> list[dict[str, Any]]:
    fields = {
        "schema_version",
        "contract_id",
        "candidate_id",
        "dataset_id",
        "snapshot_id",
        "purpose_ceiling",
        "formal_source_admission",
        "formal_catalog_written",
        "research_ready",
        "artifacts",
    }
    _require_exact_fields(value, fields, "candidate manifest")
    if value.get("schema_version") != 1:
        raise PreflightError("candidate manifest schema_version must be 1")
    _require_identity(value.get("candidate_id"), "candidate_id")
    if value.get("dataset_id") != dataset_id or value.get("snapshot_id") != snapshot_id:
        raise PreflightError("candidate manifest identity does not match target")
    if value.get("purpose_ceiling") != "INSPECTION_ONLY":
        raise PreflightError("candidate purpose_ceiling must remain INSPECTION_ONLY")
    for flag in ("formal_source_admission", "formal_catalog_written", "research_ready"):
        if value.get(flag) is not False:
            raise PreflightError(f"candidate {flag} must be false before publication")
    raw_artifacts = value.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise PreflightError("candidate artifacts must be a non-empty array")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, artifact in enumerate(raw_artifacts):
        if not isinstance(artifact, dict):
            raise PreflightError(f"candidate artifact {index} must be an object")
        _require_exact_fields(
            artifact,
            {"relative_path", "sha256", "bytes"},
            f"candidate artifact {index}",
        )
        relative, path = resolve_existing(
            artifact.get("relative_path"),
            label=f"candidate artifact {index}",
            allowed_prefixes=CANDIDATE_ARTIFACT_ROOTS,
            workspace_root=workspace_root,
        )
        if relative in seen:
            raise PreflightError(f"duplicate candidate artifact path: {relative}")
        seen.add(relative)
        expected_sha = _require_sha(artifact.get("sha256"), f"artifact {index}.sha256")
        expected_bytes = artifact.get("bytes")
        if not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool) or expected_bytes < 1:
            raise PreflightError(f"artifact {index}.bytes must be a positive integer")
        if path.stat().st_size != expected_bytes or sha256_file(path) != expected_sha:
            raise PreflightError(f"candidate artifact {index} hash/byte mismatch")
        records.append(
            {"relative_path": relative, "bytes": expected_bytes, "sha256": expected_sha}
        )
    return records


def validate_stage_a(value: dict[str, Any]) -> str:
    fields = {"schema_version", "contract_id", "status", "bundle", "bundle_sha256"}
    _require_exact_fields(value, fields, "Stage-A bundle")
    if value.get("schema_version") != 1:
        raise PreflightError("Stage-A schema_version must be 1")
    bundle = value.get("bundle")
    if not isinstance(bundle, dict):
        raise PreflightError("Stage-A bundle payload must be an object")
    _require_exact_fields(
        bundle,
        {"all_ready", "ready_count", "target_count", "targets", "formal_effect"},
        "Stage-A bundle payload",
    )
    targets = bundle.get("targets")
    if not isinstance(targets, list) or len(targets) != 4 or len(set(targets)) != 4:
        raise PreflightError("Stage-A targets must contain four unique identities")
    for index, target in enumerate(targets):
        _require_identity(target, f"Stage-A target {index}")
    if (
        value.get("status") != "PASS__ALL_TARGETS_PRE_ADMISSION_READY"
        or bundle.get("all_ready") is not True
        or bundle.get("ready_count") != 4
        or bundle.get("target_count") != 4
        or bundle.get("formal_effect") != "NONE"
    ):
        raise PreflightError("Stage-A is not a four-of-four, no-effect PASS bundle")
    expected = _require_sha(value.get("bundle_sha256"), "Stage-A bundle_sha256")
    if sha256_bytes(canonical_json(bundle).encode("utf-8")) != expected:
        raise PreflightError("Stage-A content hash mismatch")
    return expected


def validate_rights(
    value: dict[str, Any],
    *,
    dataset_id: str,
    candidate_manifest_sha256: str,
    workspace_root: Path = ROOT,
) -> dict[str, Any]:
    if value.get("contract_id") == "source_rights_receipt_v2":
        fields = {
            "schema_version",
            "contract_id",
            "receipt_id",
            "status",
            "dataset_id",
            "source_id",
            "candidate_manifest_sha256",
            "rights_basis",
            "upstream_evidence",
            "rights_status",
            "independent_contract_verification",
            "legal_opinion",
            "rights",
        }
        _require_exact_fields(value, fields, "source-rights receipt v2")
        if (
            value.get("schema_version") != 2
            or value.get("status") != "PASS__DECLARED_SCOPE_BOUND__NOT_LEGALLY_VERIFIED"
            or value.get("rights_status")
            != "DECLARED__NOT_VERIFIED__NO_LEGAL_OPINION"
            or value.get("independent_contract_verification") is not False
            or value.get("legal_opinion") is not False
        ):
            raise PreflightError("source-rights receipt v2 boundary mismatch")
        _require_identity(value.get("receipt_id"), "rights receipt_id")
        _require_identity(value.get("source_id"), "source_id")
        if value.get("dataset_id") != dataset_id:
            raise PreflightError("source-rights dataset_id mismatch")
        if value.get("candidate_manifest_sha256") != candidate_manifest_sha256:
            raise PreflightError("source-rights receipt is not bound to candidate manifest")
        rights_basis = _verify_plain_file_binding(
            value.get("rights_basis"),
            label="rights_basis",
            workspace_root=workspace_root,
        )
        upstream = value.get("upstream_evidence")
        expected_upstream = {
            "csmar_rights_receipt",
            "csmar_rights_verification",
            "rqdata_quota_receipt",
        }
        if not isinstance(upstream, dict):
            raise PreflightError("upstream_evidence must be an object")
        _require_exact_fields(upstream, expected_upstream, "upstream_evidence")
        upstream_records = {
            key: _verify_plain_file_binding(
                upstream[key],
                label=f"upstream_evidence.{key}",
                workspace_root=workspace_root,
            )
            for key in sorted(expected_upstream)
        }
        rights = value.get("rights")
        expected = {
            "local_storage": True,
            "internal_research": True,
            "derived_research": True,
            "raw_retention": True,
            "no_external_redistribution": True,
        }
        if rights != expected:
            raise PreflightError("source-rights scope is missing or broader than contract")
        return {
            "rights_basis_sha256": rights_basis["sha256"],
            "upstream_evidence_sha256": {
                key: record["sha256"] for key, record in upstream_records.items()
            },
            "rights_status": value["rights_status"],
            "independent_contract_verification": False,
            "legal_opinion": False,
        }
    fields = {
        "schema_version",
        "contract_id",
        "receipt_id",
        "status",
        "dataset_id",
        "source_id",
        "candidate_manifest_sha256",
        "rights",
    }
    _require_exact_fields(value, fields, "source-rights receipt")
    if value.get("schema_version") != 1 or value.get("status") != "PASS":
        raise PreflightError("source-rights receipt must be schema v1 PASS")
    _require_identity(value.get("receipt_id"), "rights receipt_id")
    _require_identity(value.get("source_id"), "source_id")
    if value.get("dataset_id") != dataset_id:
        raise PreflightError("source-rights dataset_id mismatch")
    if value.get("candidate_manifest_sha256") != candidate_manifest_sha256:
        raise PreflightError("source-rights receipt is not bound to candidate manifest")
    rights = value.get("rights")
    expected = {
        "local_storage": True,
        "internal_research": True,
        "derived_research": True,
        "raw_retention": True,
        "no_external_redistribution": True,
    }
    if rights != expected:
        raise PreflightError("source-rights scope is missing or broader than contract")
    return {}


def _verify_plain_file_binding(
    binding: Any,
    *,
    label: str,
    workspace_root: Path = ROOT,
) -> dict[str, Any]:
    if not isinstance(binding, dict):
        raise PreflightError(f"{label} binding must be an object")
    _require_exact_fields(binding, {"relative_path", "sha256", "bytes"}, f"{label} binding")
    relative, path = resolve_existing(
        binding.get("relative_path"),
        label=label,
        allowed_prefixes=EVIDENCE_ROOTS,
        workspace_root=workspace_root,
    )
    expected_sha = _require_sha(binding.get("sha256"), f"{label}.sha256")
    expected_bytes = binding.get("bytes")
    if not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool) or expected_bytes < 1:
        raise PreflightError(f"{label}.bytes must be a positive integer")
    if path.stat().st_size != expected_bytes or sha256_file(path) != expected_sha:
        raise PreflightError(f"{label} hash/byte binding mismatch")
    return {
        "relative_path": relative,
        "sha256": expected_sha,
        "bytes": expected_bytes,
    }


def validate_authorization(
    value: dict[str, Any],
    *,
    dataset_id: str,
    snapshot_id: str,
    target_relative_path: str,
    candidate_sha: str,
    stage_a_sha: str,
    rights_sha: str,
    stage_a_targets: list[str],
    rights_metadata: dict[str, Any] | None = None,
) -> None:
    if value.get("contract_id") == "formal_publish_authorization_receipt_v2":
        fields = {
            "schema_version",
            "contract_id",
            "authorization_id",
            "action",
            "authorized_by",
            "authorized_at_utc",
            "statement",
            "source_thread_id",
            "scope",
            "dataset_id",
            "snapshot_id",
            "target_relative_path",
            "candidate_manifest_sha256",
            "stage_a_bundle_sha256",
            "source_rights_receipt_sha256",
            "rights_basis_sha256",
            "upstream_evidence_sha256",
            "rights_status",
            "independent_contract_verification",
            "legal_opinion",
            "credential_value_captured",
            "single_use",
        }
        _require_exact_fields(value, fields, "user authorization receipt v2")
        if value.get("schema_version") != 2:
            raise PreflightError("authorization v2 schema_version must be 2")
        _require_identity(value.get("source_thread_id"), "source_thread_id")
        metadata = rights_metadata or {}
        if (
            value.get("rights_basis_sha256") != metadata.get("rights_basis_sha256")
            or value.get("upstream_evidence_sha256")
            != metadata.get("upstream_evidence_sha256")
            or value.get("rights_status") != metadata.get("rights_status")
            or value.get("independent_contract_verification") is not False
            or value.get("legal_opinion") is not False
            or value.get("credential_value_captured") is not False
        ):
            raise PreflightError("authorization v2 rights/evidence binding mismatch")
    else:
        fields = {
            "schema_version",
            "contract_id",
            "authorization_id",
            "action",
            "authorized_by",
            "authorized_at_utc",
            "statement",
            "scope",
            "dataset_id",
            "snapshot_id",
            "target_relative_path",
            "candidate_manifest_sha256",
            "stage_a_bundle_sha256",
            "source_rights_receipt_sha256",
            "single_use",
        }
    _require_exact_fields(value, fields, "user authorization receipt")
    if value.get("schema_version") not in {1, 2}:
        raise PreflightError("authorization schema_version must be 1")
    _require_identity(value.get("authorization_id"), "authorization_id")
    if value.get("action") != AUTHORIZATION_ACTION or value.get("authorized_by") != "USER":
        raise PreflightError("authorization action/authorizer mismatch")
    if not isinstance(value.get("authorized_at_utc"), str) or not value["authorized_at_utc"].strip():
        raise PreflightError("authorization timestamp is required")
    if not isinstance(value.get("statement"), str) or not value["statement"].strip():
        raise PreflightError("authorization statement is required")
    if value.get("single_use") is not True:
        raise PreflightError("authorization must be single_use")
    if sorted(value.get("scope", [])) != sorted(stage_a_targets):
        raise PreflightError("authorization scope does not match Stage-A targets")
    bindings = {
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "target_relative_path": target_relative_path,
        "candidate_manifest_sha256": candidate_sha,
        "stage_a_bundle_sha256": stage_a_sha,
        "source_rights_receipt_sha256": rights_sha,
    }
    for key, expected in bindings.items():
        if value.get(key) != expected:
            raise PreflightError(f"authorization {key} binding mismatch")


def validate_qdata(value: Any, *, dataset_id: str, snapshot_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PreflightError("qdata_postconditions must be an object")
    fields = {
        "dataset_id",
        "snapshot_id",
        "intent",
        "consumer",
        "require_exact_identity",
        "forbid_latest",
        "require_immutable_snapshot",
        "require_catalog_atomic_visibility",
        "require_research_resolution_after_publish",
        "failure_action",
    }
    _require_exact_fields(value, fields, "qdata_postconditions")
    if value.get("dataset_id") != dataset_id or value.get("snapshot_id") != snapshot_id:
        raise PreflightError("qdata postcondition identity mismatch")
    if value.get("intent") != "research":
        raise PreflightError("qdata postcondition intent must be research")
    _require_identity(value.get("consumer"), "qdata consumer")
    for flag in (
        "require_exact_identity",
        "forbid_latest",
        "require_immutable_snapshot",
        "require_catalog_atomic_visibility",
        "require_research_resolution_after_publish",
    ):
        if value.get(flag) is not True:
            raise PreflightError(f"qdata postcondition {flag} must be true")
    if value.get("failure_action") != "ROLLBACK_OR_FAIL_CLOSED":
        raise PreflightError("qdata failure_action must be ROLLBACK_OR_FAIL_CLOSED")
    return dict(value)


def _resolve_request(path: Path, workspace_root: Path = ROOT) -> Path:
    root = Path(os.path.abspath(workspace_root))
    candidate = path if path.is_absolute() else root / path
    candidate = Path(os.path.abspath(candidate))
    request_root = root / "tmp/formal-publisher-preflight/requests"
    try:
        relative = candidate.relative_to(request_root)
    except ValueError as exc:
        raise PreflightError("request must be below the dedicated tmp request root") from exc
    if len(relative.parts) != 1 or candidate.suffix.lower() != ".json":
        raise PreflightError("request must be one direct JSON child of request root")
    _assert_plain_existing_chain(root, candidate)
    if not candidate.is_file() or candidate.stat().st_nlink != 1:
        raise PreflightError("request must be a plain existing regular file")
    return candidate


def preflight(
    request_path: Path,
    *,
    workspace_root: Path = ROOT,
    allow_existing_target_for_recovery: bool = False,
) -> dict[str, Any]:
    root = Path(os.path.abspath(workspace_root))
    path = _resolve_request(request_path, root)
    request, payload = load_json_object(path, "preflight request")
    _require_exact_fields(request, REQUEST_FIELDS, "preflight request")
    if request.get("schema_version") != 1 or request.get("contract_id") != CONTRACT_ID:
        raise PreflightError("request contract/schema mismatch")
    preflight_id = _require_identity(request.get("preflight_id"), "preflight_id")

    target = request.get("target")
    if not isinstance(target, dict):
        raise PreflightError("target must be an object")
    _require_exact_fields(
        target,
        {"dataset_id", "snapshot_id", "formal_root", "target_relative_path"},
        "target",
    )
    dataset_id = _require_identity(target.get("dataset_id"), "dataset_id", dataset=True)
    snapshot_id = _require_identity(target.get("snapshot_id"), "snapshot_id")
    allowed_roots = request.get("allowed_formal_roots")
    if (
        not isinstance(allowed_roots, list)
        or not allowed_roots
        or len(set(allowed_roots)) != len(allowed_roots)
        or any(root not in FORMAL_ROOT_ALLOWLIST for root in allowed_roots)
    ):
        raise PreflightError("allowed_formal_roots must be a unique non-empty allowlist subset")
    formal_root = target.get("formal_root")
    if formal_root not in allowed_roots:
        raise PreflightError("target formal_root is not explicitly allowed")
    expected_target = f"{formal_root}/{dataset_id}/{snapshot_id}"
    target_relative = _canonical_relative(target.get("target_relative_path"), "target path")
    if target_relative != expected_target:
        raise PreflightError("target path is not the exact immutable identity path")
    target_path = root.joinpath(*PurePosixPath(target_relative).parts)
    _assert_plain_existing_chain(root, target_path)
    if target_path.exists() and not allow_existing_target_for_recovery:
        raise PreflightError("immutable target path already exists; overwrite is forbidden")

    evidence: dict[str, dict[str, Any]] = {}
    values: dict[str, dict[str, Any]] = {}
    for label in (
        "candidate_manifest",
        "stage_a_bundle",
        "source_rights_receipt",
        "user_authorization_receipt",
    ):
        record, value, _ = verify_binding(
            request.get(label),
            label=label,
            expected_contract=EXPECTED_CONTRACTS[label],
            workspace_root=root,
        )
        evidence[label] = record
        values[label] = value

    artifacts = validate_candidate(
        values["candidate_manifest"],
        dataset_id=dataset_id,
        snapshot_id=snapshot_id,
        workspace_root=root,
    )
    stage_a_sha = validate_stage_a(values["stage_a_bundle"])
    rights_metadata = validate_rights(
        values["source_rights_receipt"],
        dataset_id=dataset_id,
        candidate_manifest_sha256=evidence["candidate_manifest"]["sha256"],
        workspace_root=root,
    )
    stage_targets = values["stage_a_bundle"]["bundle"]["targets"]
    validate_authorization(
        values["user_authorization_receipt"],
        dataset_id=dataset_id,
        snapshot_id=snapshot_id,
        target_relative_path=target_relative,
        candidate_sha=evidence["candidate_manifest"]["sha256"],
        stage_a_sha=stage_a_sha,
        rights_sha=evidence["source_rights_receipt"]["sha256"],
        stage_a_targets=stage_targets,
        rights_metadata=rights_metadata,
    )
    qdata = validate_qdata(
        request.get("qdata_postconditions"),
        dataset_id=dataset_id,
        snapshot_id=snapshot_id,
    )
    request_relative = path.relative_to(root).as_posix()
    return {
        "schema_version": 1,
        "contract_id": CONTRACT_ID,
        "preflight_id": preflight_id,
        "status": "PASS__NON_EXECUTABLE_PREFLIGHT_ONLY",
        "executable": False,
        "formal_write": False,
        "write_performed": False,
        "formal_effect": "NONE",
        "network_used": False,
        "request": {
            "relative_path": request_relative,
            "bytes": len(payload),
            "sha256": sha256_bytes(payload),
        },
        "target": {
            "dataset_id": dataset_id,
            "snapshot_id": snapshot_id,
            "formal_root": formal_root,
            "target_relative_path": target_relative,
            "target_exists": target_path.exists(),
        },
        "allowed_formal_roots": allowed_roots,
        "evidence": evidence,
        "candidate_artifacts": artifacts,
        "candidate_artifact_count": len(artifacts),
        "candidate_artifact_bytes": sum(item["bytes"] for item in artifacts),
        "stage_a_bundle_sha256": stage_a_sha,
        "qdata_postconditions": qdata,
        "blocking_gaps": [],
        "plan_steps": [
            "separate production publisher must reverify every sealed input",
            "publisher must atomically expose the exact immutable catalog identity",
            "publisher must run exact qdata research resolution postcondition",
            "any failed postcondition must rollback or fail closed",
        ],
        "boundary": (
            "This result is evidence readiness only; it is not authorization "
            "to execute and does not publish or admit data."
        ),
    }


def failure_result(error: BaseException) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contract_id": CONTRACT_ID,
        "status": "FAIL_CLOSED",
        "executable": False,
        "formal_write": False,
        "write_performed": False,
        "formal_effect": "NONE",
        "network_used": False,
        "blocking_gaps": [str(error)],
        "boundary": "No publication or formal-state mutation was attempted.",
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Validate and print a non-executable formal publication preflight plan."
    )
    value.add_argument("--request", type=Path, required=True)
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        result = preflight(args.request)
    except (OSError, ValueError, PreflightError) as exc:
        print(json.dumps(failure_result(exc), ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
