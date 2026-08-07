#!/usr/bin/env python3
"""Build a dry-run-only formal publication package for R0_SHSZ_PV v2.

The default mode is a no-write plan.  ``--materialize-package`` may write only
under the dedicated ``tmp`` package/request roots, then runs the existing
preflight, execution-plan validator and workspace publisher with publication
disabled.  It never reads credentials and has no formal publication switch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import build_r0_shsz_pv_panel_candidate as panel_candidate  # noqa: E402
import execute_formal_atomic_publish_workspace as workspace_publisher  # noqa: E402
import run_formal_publisher_preflight as preflight_module  # noqa: E402
import validate_formal_atomic_publish_execution as execution_module  # noqa: E402


CONTRACT_ID = "r0_shsz_pv_publication_package_request_v2"
PACKAGE_CONTRACT_ID = "r0_shsz_pv_publication_package_v2"
EXECUTION_CONTRACT_ID = "formal_atomic_publish_execution_v2"
AUTHORIZATION_CONTRACT_ID = "formal_publish_authorization_receipt_v2"
R0_CONTRACT_ID = "r0_shsz_pv_v2"
DATASET_ID = "cn-a-share-r0-shsz-pv-panel"
SNAPSHOT_ID = "20050104-20260803-v1"
TARGET_RELATIVE = (
    "data/market/snapshots/"
    f"{DATASET_ID}/{SNAPSHOT_ID}"
)
PRODUCTION_CANDIDATE_ROOT = (
    "data/market/quarantine/r0-shsz-pv-panel/candidate-20260804-v1"
)
REQUEST_ROOT = ROOT / "tmp/r0-shsz-pv-publication-package/requests"
PACKAGE_ROOT = ROOT / "tmp/r0-shsz-pv-publication-package/packages"
PREFLIGHT_REQUEST_ROOT = ROOT / "tmp/formal-publisher-preflight/requests"
EXECUTION_REQUEST_ROOT = ROOT / "tmp/formal-publisher-execution/requests"
CATALOG_RELATIVE = "data/catalog/datasets.json"
CATALOG_PATH = ROOT / CATALOG_RELATIVE
R0_CONTRACT_RELATIVE = "data/catalog/contracts/r0_shsz_pv_v2/contract.json"
R0_CONTRACT_PATH = ROOT / R0_CONTRACT_RELATIVE
OFFICIAL_EXPORT_DOCUMENTATION_URL = (
    "https://www.ricequant.com/doc/rqdata/python/manual.html"
)
NARROW_STAGE_TARGETS = [
    "R0-SHSZ-IDENTITY",
    "R0-SHSZ-CALENDAR",
    "R0-SHSZ-PV",
    "R0-SHSZ-EXECUTION-MASK",
]
SOURCE_ID = "CSMAR_RQDATA_DECLARED"
RIGHTS = {
    "local_storage": True,
    "internal_research": True,
    "derived_research": True,
    "raw_retention": True,
    "no_external_redistribution": True,
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}$")
REQUEST_FIELDS = {
    "schema_version",
    "contract_id",
    "package_id",
    "candidate_manifest",
    "r0_contract",
    "rights_basis_bindings",
    "education_license_observation",
    "execution_authorization",
    "publication_timestamps",
}
BINDING_FIELDS = {"relative_path", "sha256", "bytes"}
RIGHTS_BINDING_FIELDS = {
    "csmar_rights_receipt",
    "csmar_rights_verification",
    "rqdata_quota_receipt",
}
LICENSE_FIELDS = {
    "license_type",
    "valid_until",
    "observation_source",
    "independent_verification",
    "credential_value_captured",
}
AUTHORIZATION_FIELDS = {
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
TIMESTAMP_FIELDS = {"available_at"}
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


class PackageBuildError(RuntimeError):
    """Fail-closed package request, evidence, path or validation error."""


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_constant(value: str) -> None:
    raise PackageBuildError(f"non-standard JSON constant is forbidden: {value}")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PackageBuildError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageBuildError(f"invalid {label}: {path}") from exc
    if not isinstance(value, dict):
        raise PackageBuildError(f"{label} must be a JSON object")
    return value


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _plain_chain(root: Path, path: Path) -> None:
    root = Path(os.path.abspath(root))
    path = Path(os.path.abspath(path))
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PackageBuildError(f"path escapes workspace: {path}") from exc
    current = root
    if _is_link_or_reparse(current):
        raise PackageBuildError("workspace root cannot be a link/reparse point")
    for component in relative.parts:
        current = current / component
        if not current.exists():
            break
        if _is_link_or_reparse(current):
            raise PackageBuildError(f"link/reparse path is forbidden: {current}")


def _canonical_relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise PackageBuildError(f"{label} must be a canonical POSIX path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise PackageBuildError(f"unsafe {label}: {value!r}")
    return pure.as_posix()


def _require_fields(value: dict[str, Any], fields: set[str], label: str) -> None:
    missing = sorted(fields - set(value))
    extra = sorted(set(value) - fields)
    if missing or extra:
        raise PackageBuildError(
            f"{label} fields mismatch; missing={missing}; extra={extra}"
        )


def _require_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        raise PackageBuildError(f"invalid {label}")
    if value.lower() in {"latest", "current", "working", "mutable", "tmp", "temp"}:
        raise PackageBuildError(f"mutable {label} is forbidden")
    return value


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PackageBuildError(f"{label} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PackageBuildError(f"{label} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise PackageBuildError(f"{label} must contain a timezone")
    return value


def _actual_execution_timestamp() -> str:
    """Return the observed execution clock; never a requested/future timestamp."""
    observed = datetime.now(timezone.utc)
    value = observed.isoformat(timespec="microseconds").replace("+00:00", "Z")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed > datetime.now(timezone.utc):
        raise PackageBuildError("published_at cannot be in the future")
    return value


def _binding(path: Path, root: Path) -> dict[str, Any]:
    return {
        "relative_path": path.relative_to(root).as_posix(),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _verify_binding(
    value: Any,
    *,
    label: str,
    root: Path,
) -> tuple[dict[str, Any], Path]:
    if not isinstance(value, dict):
        raise PackageBuildError(f"{label} binding must be an object")
    _require_fields(value, BINDING_FIELDS, f"{label} binding")
    relative = _canonical_relative(value.get("relative_path"), f"{label}.relative_path")
    path = root.joinpath(*PurePosixPath(relative).parts)
    _plain_chain(root, path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise PackageBuildError(f"{label} must bind a plain regular file")
    expected_sha = value.get("sha256")
    expected_bytes = value.get("bytes")
    if not isinstance(expected_sha, str) or SHA256_RE.fullmatch(expected_sha) is None:
        raise PackageBuildError(f"{label}.sha256 is invalid")
    if not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool) or expected_bytes < 1:
        raise PackageBuildError(f"{label}.bytes is invalid")
    if path.stat().st_size != expected_bytes or sha256_file(path) != expected_sha:
        raise PackageBuildError(f"{label} hash/byte binding mismatch")
    return dict(value), path


def _request_path(path: Path, request_root: Path, workspace_root: Path) -> Path:
    candidate = path if path.is_absolute() else workspace_root / path
    candidate = Path(os.path.abspath(candidate))
    expected_root = Path(os.path.abspath(request_root))
    try:
        relative = candidate.relative_to(expected_root)
    except ValueError as exc:
        raise PackageBuildError("request is outside the dedicated package request root") from exc
    if len(relative.parts) != 1 or candidate.suffix.lower() != ".json":
        raise PackageBuildError("request must be one direct JSON child")
    _plain_chain(workspace_root, candidate)
    if not candidate.is_file() or candidate.stat().st_nlink != 1:
        raise PackageBuildError("package request must be a plain existing file")
    return candidate


def _guard_state(root: Path) -> dict[str, Any]:
    guarded_files = [
        "data/catalog/datasets.json",
        "control/ROADMAP.md",
        "data/catalog/contracts/source_admission_v1/current_source_registry.json",
    ]
    files = {}
    for relative in guarded_files:
        path = root / relative
        files[relative] = (
            {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
            if path.is_file()
            else None
        )
    trees = {}
    for relative in ("data/market/snapshots", "data/published"):
        path = root / relative
        entries = []
        if path.exists():
            for item in sorted(path.rglob("*")):
                if item.is_file():
                    entries.append(
                        {
                            "relative_path": item.relative_to(path).as_posix(),
                            "bytes": item.stat().st_size,
                            "sha256": sha256_file(item),
                        }
                    )
        trees[relative] = sha256_bytes(canonical_bytes(entries))
    return {"files": files, "trees": trees}


def _validate_r0_contract(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") != 1 or value.get("contract_id") != R0_CONTRACT_ID:
        raise PackageBuildError("R0 contract identity mismatch")
    target = value.get("formal_target")
    if not isinstance(target, dict):
        raise PackageBuildError("R0 formal_target is missing")
    if target.get("dataset_id") != DATASET_ID or target.get("snapshot_id") != SNAPSHOT_ID:
        raise PackageBuildError("R0 formal target drifted")
    semantic = value.get("research_input_contract")
    if not isinstance(semantic, dict) or not semantic:
        raise PackageBuildError("R0 research_input_contract is missing")
    if (
        semantic.get("contract_id") != R0_CONTRACT_ID
        or semantic.get("dataset_id") != DATASET_ID
        or semantic.get("snapshot_id") != SNAPSHOT_ID
        or semantic.get("research_tier") != "R0_SHSZ_PV"
    ):
        raise PackageBuildError("R0 research_input_contract identity drifted")
    return semantic


def _candidate_payload_layout(
    candidate_root: Path,
    candidate: dict[str, Any],
    workspace_root: Path,
) -> list[dict[str, Any]]:
    artifacts = candidate.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise PackageBuildError("candidate artifacts are missing")
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    layers: set[str] = set()
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise PackageBuildError(f"candidate artifact {index} is malformed")
        _require_fields(artifact, {"relative_path", "sha256", "bytes"}, "candidate artifact")
        _, source = _verify_binding(artifact, label=f"candidate artifact {index}", root=workspace_root)
        try:
            relative = source.relative_to(candidate_root).as_posix()
        except ValueError as exc:
            raise PackageBuildError("candidate artifact escapes the candidate root") from exc
        if relative.startswith("raw/"):
            layer = "raw"
            destination = relative[len("raw/") :]
        elif relative.startswith("curated/"):
            layer = "curated"
            destination = relative[len("curated/") :]
        else:
            layer = "raw"
            destination = f"candidate-evidence/{relative}"
        if not destination:
            raise PackageBuildError("empty candidate payload destination")
        key = (layer, destination.casefold())
        if key in seen:
            raise PackageBuildError("candidate payload destination collision")
        seen.add(key)
        layers.add(layer)
        records.append(
            {
                "source_relative_path": source.relative_to(workspace_root).as_posix(),
                "layer": layer,
                "snapshot_relative_path": destination,
                "sha256": artifact["sha256"],
                "bytes": artifact["bytes"],
            }
        )
    if layers != {"raw", "curated"}:
        raise PackageBuildError("candidate must provide raw and curated payload")
    return sorted(records, key=lambda item: (item["layer"], item["snapshot_relative_path"]))


def _validate_candidate(
    candidate_path: Path,
    *,
    workspace_root: Path,
    allow_test_candidate: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    candidate_root = candidate_path.parent.resolve()
    if not allow_test_candidate:
        expected = (workspace_root / PRODUCTION_CANDIDATE_ROOT).resolve()
        if candidate_root != expected:
            raise PackageBuildError("candidate root is not the fixed production candidate")
    try:
        verification = panel_candidate.verify_candidate(
            candidate_root,
            allow_test_paths=allow_test_candidate,
        )
    except Exception as exc:
        raise PackageBuildError(f"composite candidate verification failed: {exc}") from exc
    candidate = load_json(candidate_path, "candidate manifest")
    if (
        candidate.get("contract_id") != "candidate_manifest_v1"
        or candidate.get("dataset_id") != DATASET_ID
        or candidate.get("snapshot_id") != SNAPSHOT_ID
        or candidate.get("purpose_ceiling") != "INSPECTION_ONLY"
        or candidate.get("formal_source_admission") is not False
        or candidate.get("formal_catalog_written") is not False
        or candidate.get("research_ready") is not False
    ):
        raise PackageBuildError("candidate manifest boundary mismatch")
    quality = load_json(candidate_root / "quality/summary.json", "candidate quality")
    if (
        quality.get("contract_id") != R0_CONTRACT_ID
        or quality.get("dataset_id") != DATASET_ID
        or quality.get("snapshot_id") != SNAPSHOT_ID
        or quality.get("status")
        != "PASS__CANDIDATE_MECHANICAL_QUALITY__NOT_FORMALLY_ADMITTED"
        or quality.get("all_checks_pass") is not True
        or not isinstance(quality.get("checks"), dict)
        or not quality["checks"]
        or not all(value is True for value in quality["checks"].values())
    ):
        raise PackageBuildError("candidate quality is not an exact mechanical PASS")
    layout = _candidate_payload_layout(
        candidate_root,
        candidate,
        workspace_root,
    )
    return candidate, layout, verification


def _validate_rights_sources(
    bindings: dict[str, Any],
    *,
    root: Path,
) -> tuple[dict[str, Any], dict[str, Path]]:
    _require_fields(bindings, RIGHTS_BINDING_FIELDS, "rights_basis_bindings")
    normalized: dict[str, Any] = {}
    paths: dict[str, Path] = {}
    for label in sorted(RIGHTS_BINDING_FIELDS):
        normalized[label], paths[label] = _verify_binding(
            bindings[label], label=label, root=root
        )
    csmar = load_json(paths["csmar_rights_receipt"], "CSMAR rights receipt")
    csmar_verification = load_json(
        paths["csmar_rights_verification"], "CSMAR rights verification"
    )
    quota = load_json(paths["rqdata_quota_receipt"], "RQData quota receipt")
    if (
        csmar.get("receipt_id")
        != "csmar-user-attested-rights-receipt-20260730-v1"
        or csmar.get("declaration")
        != "USER_ATTESTED__LOCAL_RETENTION__INTERNAL_ACADEMIC_DERIVATION__NO_RAW_EXTERNAL_REDISTRIBUTION__NO_COMMERCIAL_USE"
        or csmar.get("admission_effect", {}).get("rights_boundary_recorded_for_exact_assets")
        is not True
        or csmar.get("admission_effect", {}).get("independent_contract_verification_completed")
        is not False
    ):
        raise PackageBuildError("CSMAR user-attested rights basis drifted")
    if (
        csmar_verification.get("status")
        != "PASS__EXACT_MANIFEST_AND_PAYLOAD_BINDINGS__USER_ATTESTATION_SCOPE_RECORDED__NO_FORMAL_ADMISSION"
        or csmar_verification.get("independent_contract_verification") is not False
        or csmar_verification.get("formal_admission") is not False
    ):
        raise PackageBuildError("CSMAR rights verification boundary drifted")
    if (
        quota.get("status") != "FRESH_PROCESS_QUOTA_RECONCILIATION_PASS"
        or quota.get("credential_value_captured") is not False
        or not isinstance(quota.get("remaining_days"), int)
        or quota["remaining_days"] < 1
    ):
        raise PackageBuildError("RQData quota evidence is ineligible")
    return normalized, paths


def _validate_spec(
    request_path: Path,
    *,
    workspace_root: Path,
    request_root: Path,
    allow_test_candidate: bool,
) -> dict[str, Any]:
    path = _request_path(request_path, request_root, workspace_root)
    request = load_json(path, "publication package request")
    _require_fields(request, REQUEST_FIELDS, "publication package request")
    if request.get("schema_version") != 2 or request.get("contract_id") != CONTRACT_ID:
        raise PackageBuildError("package request contract/schema mismatch")
    package_id = _require_id(request.get("package_id"), "package_id")
    candidate_binding, candidate_path = _verify_binding(
        request.get("candidate_manifest"),
        label="candidate_manifest",
        root=workspace_root,
    )
    r0_binding, r0_path = _verify_binding(
        request.get("r0_contract"), label="r0_contract", root=workspace_root
    )
    if r0_path.resolve() != (workspace_root / R0_CONTRACT_RELATIVE).resolve():
        raise PackageBuildError("request must bind the canonical R0 v2 contract")
    semantic = _validate_r0_contract(load_json(r0_path, "R0 v2 contract"))
    candidate, layout, candidate_verification = _validate_candidate(
        candidate_path,
        workspace_root=workspace_root,
        allow_test_candidate=allow_test_candidate,
    )
    rights_bindings, rights_paths = _validate_rights_sources(
        request.get("rights_basis_bindings"), root=workspace_root
    )
    license_observation = request.get("education_license_observation")
    if not isinstance(license_observation, dict):
        raise PackageBuildError("education_license_observation must be an object")
    _require_fields(license_observation, LICENSE_FIELDS, "education_license_observation")
    if (
        license_observation.get("license_type") != "EDUCATION"
        or license_observation.get("valid_until") != "2028-08-31"
        or license_observation.get("observation_source")
        != "USER_PROVIDED_SCREENSHOT_AND_BOUNDED_QUOTA_RECEIPT"
        or license_observation.get("independent_verification") is not False
        or license_observation.get("credential_value_captured") is not False
    ):
        raise PackageBuildError("education license observation boundary mismatch")
    authorization_binding, authorization_path = _verify_binding(
        request.get("execution_authorization"),
        label="execution_authorization",
        root=workspace_root,
    )
    authorization = load_json(authorization_path, "execution authorization")
    _require_fields(authorization, AUTHORIZATION_FIELDS, "execution_authorization")
    _require_id(authorization.get("authorization_id"), "authorization_id")
    _timestamp(authorization.get("authorized_at_utc"), "authorization.authorized_at_utc")
    _require_id(authorization.get("source_thread_id"), "source_thread_id")
    if (
        authorization.get("schema_version") != 2
        or authorization.get("contract_id") != AUTHORIZATION_CONTRACT_ID
        or authorization.get("action")
        != "AUTHORIZE_EXACT_FORMAL_SNAPSHOT_PUBLICATION"
        or authorization.get("authorized_by") != "USER"
        or not isinstance(authorization.get("statement"), str)
        or not authorization["statement"].strip()
        or authorization.get("scope") != NARROW_STAGE_TARGETS
        or authorization.get("dataset_id") != DATASET_ID
        or authorization.get("snapshot_id") != SNAPSHOT_ID
        or authorization.get("target_relative_path") != TARGET_RELATIVE
        or authorization.get("candidate_manifest_sha256")
        != candidate_binding["sha256"]
        or authorization.get("rights_status")
        != "DECLARED__NOT_VERIFIED__NO_LEGAL_OPINION"
        or authorization.get("independent_contract_verification") is not False
        or authorization.get("legal_opinion") is not False
        or authorization.get("credential_value_captured") is not False
        or authorization.get("single_use") is not True
    ):
        raise PackageBuildError("independent execution authorization is incomplete")
    for field in (
        "stage_a_bundle_sha256",
        "source_rights_receipt_sha256",
        "rights_basis_sha256",
    ):
        if not isinstance(authorization.get(field), str) or SHA256_RE.fullmatch(
            authorization[field]
        ) is None:
            raise PackageBuildError(f"execution authorization {field} is invalid")
    upstream_authorization = authorization.get("upstream_evidence_sha256")
    if not isinstance(upstream_authorization, dict):
        raise PackageBuildError("execution authorization upstream evidence is missing")
    _require_fields(
        upstream_authorization,
        RIGHTS_BINDING_FIELDS,
        "execution authorization upstream_evidence_sha256",
    )
    if any(
        not isinstance(value, str) or SHA256_RE.fullmatch(value) is None
        for value in upstream_authorization.values()
    ):
        raise PackageBuildError("execution authorization upstream evidence hash is invalid")
    timestamps = request.get("publication_timestamps")
    if not isinstance(timestamps, dict):
        raise PackageBuildError("publication_timestamps must be an object")
    _require_fields(timestamps, TIMESTAMP_FIELDS, "publication_timestamps")
    _timestamp(timestamps.get("available_at"), "publication.available_at")
    return {
        "request_path": path,
        "request": request,
        "package_id": package_id,
        "candidate_binding": candidate_binding,
        "candidate": candidate,
        "candidate_verification": candidate_verification,
        "payload_layout": layout,
        "r0_binding": r0_binding,
        "research_input_contract": semantic,
        "rights_bindings": rights_bindings,
        "rights_paths": rights_paths,
        "license_observation": dict(license_observation),
        "authorization": dict(authorization),
        "authorization_binding": authorization_binding,
        "authorization_path": authorization_path,
        "timestamps": dict(timestamps),
    }


def _rights_basis(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contract_id": "r0_shsz_pv_rights_basis_v1",
        "status": "DECLARED__NOT_VERIFIED__NO_LEGAL_OPINION",
        "dataset_id": DATASET_ID,
        "snapshot_id": SNAPSHOT_ID,
        "source_id": SOURCE_ID,
        "basis_bindings": plan["rights_bindings"],
        "education_license_observation": plan["license_observation"],
        "rqdata_export_capability": {
            "official_documentation_url": OFFICIAL_EXPORT_DOCUMENTATION_URL,
            "documented_return_type": "PANDAS_DATAFRAME",
            "documented_local_export_method": "DataFrame.to_csv",
            "capability_is_not_legal_verification": True,
        },
        "user_scope": {
            "source_thread_id": plan["authorization"]["source_thread_id"],
            "api_and_local_integration_authorized": True,
            "r0_completion_authorized": True,
            "local_machine_internal_noncommercial_research_only": True,
            "raw_retention": True,
            "internal_derivation": True,
            "no_external_redistribution": True,
        },
        "rights": RIGHTS,
        "independent_contract_verification": False,
        "legal_opinion": False,
        "credential_value_captured": False,
        "formal_source_admission_effect": "NONE",
        "canonical_core_effect": "NONE",
    }


def _rights_markdown(value: dict[str, Any]) -> str:
    bindings = value["basis_bindings"]
    return "\n".join(
        [
            "# R0_SHSZ_PV declared rights basis",
            "",
            "状态：`DECLARED__NOT_VERIFIED__NO_LEGAL_OPINION`。",
            "",
            "本文件只记录用户声明、教育版许可观察、无凭据配额回执以及米筐官方文档",
            "公开说明的 DataFrame/CSV 导出能力。它不是独立合同核验或法律意见，也不把",
            "窄门候选提升为 canonical CORE、正式来源准入或已发布数据。",
            "",
            "允许范围：本机长期保存、本机内部非商业研究、内部派生；禁止原始数据外部分发。",
            "",
            f"- CSMAR rights receipt: `{bindings['csmar_rights_receipt']['relative_path']}`",
            f"- CSMAR verification: `{bindings['csmar_rights_verification']['relative_path']}`",
            f"- RQData quota receipt: `{bindings['rqdata_quota_receipt']['relative_path']}`",
            f"- RQData export documentation: {OFFICIAL_EXPORT_DOCUMENTATION_URL}",
            "- Credential captured: `false`",
            "- Independent legal verification: `false`",
            "",
        ]
    )


def _stage_receipt() -> dict[str, Any]:
    bundle = {
        "all_ready": True,
        "ready_count": 4,
        "target_count": 4,
        "targets": NARROW_STAGE_TARGETS,
        "formal_effect": "NONE",
    }
    return {
        "schema_version": 1,
        "contract_id": "selected_core_stage_a_bundle_v1",
        "status": "PASS__ALL_TARGETS_PRE_ADMISSION_READY",
        "bundle": bundle,
        "bundle_sha256": sha256_bytes(
            json.dumps(
                bundle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ),
    }


def _package_paths(
    plan: dict[str, Any],
    *,
    workspace_root: Path,
    package_root: Path,
    preflight_root: Path,
    execution_root: Path,
) -> dict[str, Path]:
    package_id = plan["package_id"]
    return {
        "package": package_root / package_id,
        "preflight": preflight_root / f"preflight-{package_id}.json",
        "execution": execution_root / f"execution-{package_id}.json",
        "target": workspace_root / TARGET_RELATIVE,
    }


def _build_rights_chain(
    plan: dict[str, Any],
    paths: dict[str, Path],
    *,
    workspace_root: Path,
) -> tuple[dict[str, bytes], dict[str, Any], dict[str, Any]]:
    """Build deterministic rights artifacts without creating authorization."""
    rights_basis = _rights_basis(plan)
    stage = _stage_receipt()
    generated: dict[str, bytes] = {
        "RIGHTS_BASIS.json": canonical_bytes(rights_basis),
        "RIGHTS_BASIS.md": _rights_markdown(rights_basis).encode("utf-8"),
        "stage-a.json": canonical_bytes(stage),
    }
    rights_basis_binding = {
        "relative_path": (paths["package"] / "RIGHTS_BASIS.json")
        .relative_to(workspace_root)
        .as_posix(),
        "sha256": sha256_bytes(generated["RIGHTS_BASIS.json"]),
        "bytes": len(generated["RIGHTS_BASIS.json"]),
    }
    rights_receipt = {
        "schema_version": 2,
        "contract_id": "source_rights_receipt_v2",
        "receipt_id": f"rights-{plan['package_id']}",
        "status": "PASS__DECLARED_SCOPE_BOUND__NOT_LEGALLY_VERIFIED",
        "dataset_id": DATASET_ID,
        "source_id": SOURCE_ID,
        "candidate_manifest_sha256": plan["candidate_binding"]["sha256"],
        "rights_basis": rights_basis_binding,
        "upstream_evidence": plan["rights_bindings"],
        "rights_status": "DECLARED__NOT_VERIFIED__NO_LEGAL_OPINION",
        "independent_contract_verification": False,
        "legal_opinion": False,
        "rights": RIGHTS,
    }
    generated["source-rights.json"] = canonical_bytes(rights_receipt)
    expected = {
        "stage_a_bundle_sha256": stage["bundle_sha256"],
        "source_rights_receipt_sha256": sha256_bytes(
            generated["source-rights.json"]
        ),
        "rights_basis_sha256": rights_basis_binding["sha256"],
        "upstream_evidence_sha256": {
            key: value["sha256"]
            for key, value in sorted(plan["rights_bindings"].items())
        },
    }
    return generated, stage, expected


def _build_documents(
    plan: dict[str, Any],
    paths: dict[str, Path],
    *,
    workspace_root: Path,
) -> tuple[dict[str, bytes], dict[str, Any], dict[str, Any]]:
    generated, stage, expected_authorization_bindings = _build_rights_chain(
        plan,
        paths,
        workspace_root=workspace_root,
    )
    if any(
        plan["authorization"].get(key) != expected
        for key, expected in expected_authorization_bindings.items()
    ):
        raise PackageBuildError(
            "independent execution authorization does not bind the generated rights chain"
        )
    candidate_sha = plan["candidate_binding"]["sha256"]
    boundary = {
        "schema_version": 1,
        "contract_id": PACKAGE_CONTRACT_ID,
        "package_id": plan["package_id"],
        "gate_id": "R0_SHSZ_PV",
        "stage_targets": NARROW_STAGE_TARGETS,
        "canonical_r0_pv_effect": "NONE",
        "canonical_core20_effect": "NONE",
        "source_admission_effect": "NONE",
        "catalog_effect": "NONE",
        "formal_publication_effect": "NONE__PACKAGE_AND_DRY_RUN_ONLY",
        "rights_status": "DECLARED__NOT_VERIFIED__NO_LEGAL_OPINION",
        "credential_value_captured": False,
    }
    generated["PACKAGE_BOUNDARY.json"] = canonical_bytes(boundary)

    def package_binding(name: str, contract_id: str) -> dict[str, Any]:
        payload = generated[name]
        return {
            "relative_path": (paths["package"] / name).relative_to(workspace_root).as_posix(),
            "sha256": sha256_bytes(payload),
            "bytes": len(payload),
            "contract_id": contract_id,
        }

    preflight = {
        "schema_version": 1,
        "contract_id": "formal_publisher_preflight_v1",
        "preflight_id": f"preflight-{plan['package_id']}",
        "candidate_manifest": {
            **plan["candidate_binding"],
            "contract_id": "candidate_manifest_v1",
        },
        "stage_a_bundle": package_binding(
            "stage-a.json", "selected_core_stage_a_bundle_v1"
        ),
        "source_rights_receipt": package_binding(
            "source-rights.json", "source_rights_receipt_v2"
        ),
        "user_authorization_receipt": {
            **plan["authorization_binding"],
            "contract_id": AUTHORIZATION_CONTRACT_ID,
        },
        "target": {
            "dataset_id": DATASET_ID,
            "snapshot_id": SNAPSHOT_ID,
            "formal_root": "data/market/snapshots",
            "target_relative_path": TARGET_RELATIVE,
        },
        "allowed_formal_roots": ["data/market/snapshots"],
        "qdata_postconditions": {
            "dataset_id": DATASET_ID,
            "snapshot_id": SNAPSHOT_ID,
            "intent": "research",
            "consumer": "multi-factor",
            "require_exact_identity": True,
            "forbid_latest": True,
            "require_immutable_snapshot": True,
            "require_catalog_atomic_visibility": True,
            "require_research_resolution_after_publish": True,
            "failure_action": "ROLLBACK_OR_FAIL_CLOSED",
        },
    }
    preflight_payload = canonical_bytes(preflight)
    catalog_payload = (workspace_root / CATALOG_RELATIVE).read_bytes()
    execution = {
        "schema_version": 2,
        "contract_id": EXECUTION_CONTRACT_ID,
        "execution_id": f"execution-{plan['package_id']}",
        "preflight_request": {
            "relative_path": paths["preflight"].relative_to(workspace_root).as_posix(),
            "sha256": sha256_bytes(preflight_payload),
            "bytes": len(preflight_payload),
            "contract_id": "formal_publisher_preflight_v1",
        },
        "catalog_before": {
            "relative_path": CATALOG_RELATIVE,
            "sha256": sha256_bytes(catalog_payload),
            "bytes": len(catalog_payload),
        },
        "target": dict(preflight["target"]),
        "payload_layout": plan["payload_layout"],
        "publication_profile": {
            "store_id": "workspace-market-snapshots",
            "owner_project": "workspace",
            "lifecycle": "frozen",
            "mutability": "immutable",
            "purpose_ceiling": "RESEARCH_ONLY",
            "license_status": "DECLARED",
            "quality_status": "OK",
            "consumers": ["multi-factor"],
            "known_limits": [
                "R0_SHSZ_PV narrow gate only; canonical R0_PV and CORE20 remain unchanged",
                "Declared user-scoped rights; no independent contract verification or legal opinion",
                "XSHG/XSHE ordinary A shares only; BJSE, B shares and 600849.XSHG excluded",
                "ST and suspension unknowns remain explicit and are never signal-rank inputs",
                "Coverage ends at the complete trade date 2026-08-03",
            ],
            "notes": (
                "R0_SHSZ_PV v2 immutable price-volume panel; publication package is "
                "dry-run validated and does not itself publish."
            ),
            "source_id": SOURCE_ID,
            "dataset_contract_id": R0_CONTRACT_ID,
            "dataset_schema_version": "r0-shsz-pv-panel-v2",
            "research_input_contract": plan["research_input_contract"],
            "available_at": plan["timestamps"]["available_at"],
            "published_at_policy": "WORKSPACE_ATOMIC_COMMIT_CLOCK",
            "partition_segments": [
                "schema=r0-shsz-pv-panel-v2",
                "year=2005-2026",
                "month=01-12",
            ],
        },
        "state_machine_requirements": STATE_REQUIREMENTS,
    }
    execution_payload = canonical_bytes(execution)
    generated_bindings = []
    for name, payload in sorted(generated.items()):
        generated_bindings.append(
            {
                "relative_path": (paths["package"] / name)
                .relative_to(workspace_root)
                .as_posix(),
                "sha256": sha256_bytes(payload),
                "bytes": len(payload),
            }
        )
    package_manifest = {
        "schema_version": 1,
        "contract_id": PACKAGE_CONTRACT_ID,
        "package_id": plan["package_id"],
        "status": "SEALED__DRY_RUN_PENDING",
        "input_request": _binding(plan["request_path"], workspace_root),
        "r0_contract": plan["r0_binding"],
        "candidate_manifest": plan["candidate_binding"],
        "rights_basis_bindings": plan["rights_bindings"],
        "execution_authorization": plan["authorization_binding"],
        "generated_artifacts": generated_bindings,
        "preflight_request": {
            "relative_path": paths["preflight"].relative_to(workspace_root).as_posix(),
            "sha256": sha256_bytes(preflight_payload),
            "bytes": len(preflight_payload),
        },
        "execution_request": {
            "relative_path": paths["execution"].relative_to(workspace_root).as_posix(),
            "sha256": sha256_bytes(execution_payload),
            "bytes": len(execution_payload),
        },
        "payload_files": len(plan["payload_layout"]),
        "payload_bytes": sum(item["bytes"] for item in plan["payload_layout"]),
        "canonical_r0_pv_effect": "NONE",
        "canonical_core20_effect": "NONE",
        "formal_publication_effect": "NONE__PACKAGE_ONLY",
        "credential_value_captured": False,
    }
    generated["PACKAGE_MANIFEST.json"] = canonical_bytes(package_manifest)
    return generated, preflight, execution


def _write_file_atomic(path: Path, payload: bytes) -> None:
    if path.exists():
        raise PackageBuildError(f"immutable output collision: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _materialize(
    plan: dict[str, Any],
    paths: dict[str, Path],
    generated: dict[str, bytes],
    preflight: dict[str, Any],
    execution: dict[str, Any],
    *,
    workspace_root: Path,
) -> dict[str, Any]:
    for path in (paths["package"], paths["preflight"], paths["execution"]):
        if path.exists():
            raise PackageBuildError(f"immutable package collision: {path}")
    paths["package"].parent.mkdir(parents=True, exist_ok=True)
    stage = paths["package"].parent / f".{paths['package'].name}.staging"
    if stage.exists():
        raise PackageBuildError(f"stale package staging path exists: {stage}")
    stage.mkdir(parents=False)
    try:
        for name, payload in generated.items():
            destination = stage / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        os.replace(stage, paths["package"])
    finally:
        if stage.exists() and not _is_link_or_reparse(stage):
            shutil.rmtree(stage)
    _write_file_atomic(paths["preflight"], canonical_bytes(preflight))
    _write_file_atomic(paths["execution"], canonical_bytes(execution))

    try:
        preflight_result = preflight_module.preflight(
            paths["preflight"], workspace_root=workspace_root
        )
        execution_result = execution_module.validate_execution(
            paths["execution"], workspace_root=workspace_root
        )
        publisher_result = workspace_publisher._execute_at_workspace(  # noqa: SLF001
            request=paths["execution"],
            workspace_root=workspace_root,
            catalog_baseline_sha256=execution["catalog_before"]["sha256"],
            enabled=False,
            recover_hash=None,
            trusted_workspace_root=workspace_root,
        )
    except Exception as exc:
        raise PackageBuildError(f"package dry-run validation failed: {exc}") from exc
    dry_run = {
        "schema_version": 1,
        "contract_id": PACKAGE_CONTRACT_ID,
        "package_id": plan["package_id"],
        "status": "PASS__PACKAGE_MATERIALIZED__ALL_PUBLISHERS_DRY_RUN__NO_FORMAL_EFFECT",
        "package_manifest_sha256": sha256_file(
            paths["package"] / "PACKAGE_MANIFEST.json"
        ),
        "preflight_status": preflight_result["status"],
        "execution_plan_status": execution_result["status"],
        "workspace_publisher_status": publisher_result["status"],
        "workspace_publisher_enabled": False,
        "formal_publication_performed": False,
        "catalog_modified": False,
        "target_created": paths["target"].exists(),
        "credential_value_captured": False,
    }
    _write_file_atomic(
        paths["package"] / "DRY_RUN_RESULT.json",
        canonical_bytes(dry_run),
    )
    return dry_run


def build_package(
    *,
    request: Path,
    materialize: bool,
    workspace_root: Path = ROOT,
    request_root: Path = REQUEST_ROOT,
    package_root: Path = PACKAGE_ROOT,
    preflight_root: Path = PREFLIGHT_REQUEST_ROOT,
    execution_root: Path = EXECUTION_REQUEST_ROOT,
    allow_test_candidate: bool = False,
) -> dict[str, Any]:
    root = Path(os.path.abspath(workspace_root))
    before = _guard_state(root)
    plan = _validate_spec(
        request,
        workspace_root=root,
        request_root=request_root,
        allow_test_candidate=allow_test_candidate,
    )
    paths = _package_paths(
        plan,
        workspace_root=root,
        package_root=package_root,
        preflight_root=preflight_root,
        execution_root=execution_root,
    )
    generated, preflight, execution = _build_documents(
        plan, paths, workspace_root=root
    )
    if paths["target"].exists():
        raise PackageBuildError("immutable formal target already exists")
    for label in ("package", "preflight", "execution"):
        if paths[label].exists():
            raise PackageBuildError(f"immutable {label} output already exists")
    if not materialize:
        after = _guard_state(root)
        if before != after:
            raise PackageBuildError("protected state changed during no-write planning")
        return {
            "schema_version": 1,
            "contract_id": PACKAGE_CONTRACT_ID,
            "package_id": plan["package_id"],
            "status": "PASS__PACKAGE_PLAN_READY__NO_WRITE",
            "write_performed": False,
            "formal_effect": "NONE",
            "package_path": paths["package"].relative_to(root).as_posix(),
            "preflight_request_path": paths["preflight"].relative_to(root).as_posix(),
            "execution_request_path": paths["execution"].relative_to(root).as_posix(),
            "payload_files": len(plan["payload_layout"]),
            "payload_bytes": sum(item["bytes"] for item in plan["payload_layout"]),
            "rights_status": "DECLARED__NOT_VERIFIED__NO_LEGAL_OPINION",
            "canonical_r0_pv_effect": "NONE",
            "canonical_core20_effect": "NONE",
            "credential_value_captured": False,
        }
    dry_run = _materialize(
        plan,
        paths,
        generated,
        preflight,
        execution,
        workspace_root=root,
    )
    after = _guard_state(root)
    if before != after:
        raise PackageBuildError("protected state changed during package materialization")
    if paths["target"].exists():
        raise PackageBuildError("dry-run unexpectedly created the formal target")
    return {
        **dry_run,
        "write_performed": True,
        "write_scope": "TMP_PACKAGE_AND_DRY_RUN_REQUESTS_ONLY",
        "package_path": paths["package"].relative_to(root).as_posix(),
        "preflight_request_path": paths["preflight"].relative_to(root).as_posix(),
        "execution_request_path": paths["execution"].relative_to(root).as_posix(),
        "formal_effect": "NONE",
        "protected_state_unchanged": True,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Build the R0_SHSZ_PV v2 publication package; default is no-write plan, "
            "and materialization still performs dry-run validation only."
        )
    )
    value.add_argument("--request", required=True, type=Path)
    value.add_argument("--materialize-package", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        result = build_package(
            request=args.request,
            materialize=bool(args.materialize_package),
        )
    except Exception as exc:
        safe = re.sub(
            r"(?:tcp|rqdata|rqdatac)://\S+",
            "<redacted>",
            str(exc),
            flags=re.IGNORECASE,
        )[:800]
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "contract_id": PACKAGE_CONTRACT_ID,
                    "status": "FAIL_CLOSED",
                    "error": safe,
                    "formal_effect": "NONE",
                    "credential_value_captured": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
