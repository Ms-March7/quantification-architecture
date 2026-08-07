#!/usr/bin/env python3
"""Audit and consolidate selected-CORE Stage-A closure evidence packages.

This tool is deliberately report-only.  It verifies package manifests and
their hash-bound evidence graph, keeps package audit, proposition closure and
declared gate status separate, and never mutates formal admission state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


RUN_ID = "stage-a-evidence-consolidation-20260730-v2"
DEFAULT_QUEUE_PACKAGE = (
    "reports/data-governance/stage-a-closure-control-20260730-v2"
)
DEFAULT_CORE01_PACKAGE = (
    "reports/data-governance/"
    "stage-a-core01-official-identity-closure-20260730-v1"
)
DEFAULT_CORE02_PACKAGE = (
    "reports/data-governance/"
    "stage-a-core02-calendar-session-closure-20260730-v1"
)
DEFAULT_RULES_PACKAGE = (
    "reports/data-governance/"
    "stage-a-core03-core06-official-rules-closure-20260730-v1"
)
DEFAULT_RIGHTS_PACKAGE = (
    "reports/data-governance/"
    "csmar-user-attested-rights-receipt-20260730-v1"
)
DEFAULT_STAGE_MACHINE = (
    "reports/data-governance/"
    "selected-core-two-stage-admission-architecture-audit-20260730-v1/"
    "STATE_MACHINE.json"
)
DEFAULT_OUTPUT = f"reports/data-governance/{RUN_ID}"

GUARDED_FILES = (
    "data/catalog/datasets.json",
    "data/catalog/contracts/source_admission_v1/current_source_registry.json",
    "control/ROADMAP.md",
    "control/projects.yaml",
    DEFAULT_STAGE_MACHINE,
)
GUARDED_TREES = (
    "data/market/snapshots",
    "data/published",
)
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
BINDING_KEYS = frozenset({"path", "bytes", "sha256"})
EXPECTED_PACKAGE_GATE_STATUSES = {
    "CORE02": {
        "CORE02-Q1": "FAIL",
        "CORE02-Q2": "FAIL",
    },
    "CORE03_CORE06_RULES": {
        "CORE03-Q2": "UNKNOWN",
        "CORE03-Q3": "UNKNOWN",
        "CORE06-Q3": "FAIL",
        "CORE06-Q4": "FAIL",
    },
    "CSMAR_RIGHTS": {
        "STAGE-A-RIGHTS": "FAIL",
    },
}


def is_historical_guard_container(key: str) -> bool:
    normalized = key.lower()
    return (
        "guard_pre" in normalized
        or "guard_post" in normalized
        or normalized in {
            "authority_inputs",
            "before_guards",
            "after_guards",
            "formal_guard",
            "mutation_guard",
        }
    )


def canonical_json_bytes(value: Any) -> bytes:
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


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def workspace_relative(path: Path, workspace: Path) -> str:
    return path.resolve().relative_to(workspace.resolve()).as_posix()


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def require_child(path: Path, parent: Path, label: str) -> None:
    if not is_within(path, parent) or path.resolve() == parent.resolve():
        raise ValueError(f"{label} must be below {parent.resolve()}: {path}")


def file_binding(path: Path, workspace: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    return {
        "path": workspace_relative(path, workspace),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def package_relative_binding(path: Path, package_root: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    return {
        "path": path.resolve().relative_to(package_root.resolve()).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def tree_inventory_guard(root: Path, workspace: Path) -> dict[str, Any]:
    files = (
        [
            file_binding(path, workspace)
            for path in sorted(root.rglob("*"))
            if path.is_file()
        ]
        if root.is_dir()
        else []
    )
    return {
        "path": workspace_relative(root, workspace),
        "exists": root.is_dir(),
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files),
        "tree_sha256": sha256_bytes(canonical_json_bytes(files)),
        "files": files,
    }


def formal_guards(workspace: Path) -> dict[str, Any]:
    return {
        "files": {
            relative: file_binding(workspace / relative, workspace)
            for relative in GUARDED_FILES
        },
        "trees": {
            relative: tree_inventory_guard(workspace / relative, workspace)
            for relative in GUARDED_TREES
        },
    }


def manifest_payload(root: Any) -> tuple[str, Any]:
    """Return a supported manifest payload and its explicit root format."""

    if isinstance(root, list):
        return "list", root
    if isinstance(root, dict):
        candidates = [
            (name, root[name])
            for name in ("files", "artifacts")
            if name in root
        ]
        if len(candidates) != 1:
            raise ValueError(
                "manifest dict must contain exactly one of 'files' or "
                "'artifacts'"
            )
        name, payload = candidates[0]
        if not isinstance(payload, (list, dict)):
            raise TypeError(f"manifest {name} payload must be list or dict")
        return f"dict.{name}", payload
    raise TypeError("manifest root must be list or dict")


def collect_manifest_bindings(
    node: Any,
    *,
    location: str = "$",
) -> list[dict[str, Any]]:
    """Recursively collect strict binding records from a manifest payload."""

    records: list[dict[str, Any]] = []
    if isinstance(node, dict):
        present = BINDING_KEYS.intersection(node)
        if present:
            if present != BINDING_KEYS:
                missing = sorted(BINDING_KEYS.difference(present))
                raise ValueError(
                    f"incomplete manifest binding at {location}; "
                    f"missing {missing}"
                )
            records.append(
                {
                    "location": location,
                    "path": node["path"],
                    "bytes": node["bytes"],
                    "sha256": node["sha256"],
                }
            )
        for key, value in node.items():
            collect_location = f"{location}.{key}"
            records.extend(
                collect_manifest_bindings(value, location=collect_location)
            )
    elif isinstance(node, list):
        for index, value in enumerate(node):
            records.extend(
                collect_manifest_bindings(
                    value,
                    location=f"{location}[{index}]",
                )
            )
    return records


def collect_embedded_bindings(
    node: Any,
    *,
    location: str = "$",
) -> list[dict[str, Any]]:
    """Collect every complete path/bytes/sha256 triple in an evidence document."""

    records: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if BINDING_KEYS.issubset(node):
            records.append(
                {
                    "location": location,
                    "path": node["path"],
                    "bytes": node["bytes"],
                    "sha256": node["sha256"],
                }
            )
        for key, value in node.items():
            # A source package's pre/post guard is a historical receipt of the
            # formal state at package-build time, not a promise that mutable
            # control files will retain that old hash forever.  The current
            # consolidation independently snapshots and compares live guards.
            if is_historical_guard_container(str(key)):
                continue
            records.extend(
                collect_embedded_bindings(
                    value,
                    location=f"{location}.{key}",
                )
            )
    elif isinstance(node, list):
        for index, value in enumerate(node):
            records.extend(
                collect_embedded_bindings(
                    value,
                    location=f"{location}[{index}]",
                )
            )
    return records


def _binding_shape_errors(binding: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(binding.get("path"), str) or not binding["path"].strip():
        errors.append("path must be a non-empty string")
    value = binding.get("bytes")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        errors.append("bytes must be a non-negative integer")
    digest = binding.get("sha256")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        errors.append("sha256 must be 64 hexadecimal characters")
    return errors


def resolve_binding_path(
    raw_path: str,
    *,
    workspace: Path,
    local_base: Path,
) -> tuple[Path | None, str, list[str]]:
    errors: list[str] = []
    value = Path(raw_path.replace("\\", os.sep))
    if value.is_absolute():
        resolved = value.resolve()
        if not is_within(resolved, workspace):
            return None, "absolute", ["binding escapes workspace"]
        return resolved, "absolute", errors

    workspace_candidate = (workspace / value).resolve()
    local_candidate = (local_base / value).resolve()
    candidates: list[tuple[str, Path]] = []
    if workspace_candidate.exists():
        candidates.append(("workspace_relative", workspace_candidate))
    if local_candidate.exists() and local_candidate != workspace_candidate:
        candidates.append(("document_or_package_relative", local_candidate))
    if len(candidates) > 1:
        return None, "ambiguous", [
            "relative binding resolves to distinct workspace and local files"
        ]
    if not candidates:
        if not is_within(workspace_candidate, workspace):
            errors.append("binding escapes workspace")
        else:
            errors.append("bound path does not exist")
        return None, "missing", errors
    basis, resolved = candidates[0]
    if not is_within(resolved, workspace):
        return None, basis, ["binding escapes workspace"]
    return resolved, basis, errors


def verify_binding(
    binding: dict[str, Any],
    *,
    workspace: Path,
    local_base: Path,
    discovered_from: str,
) -> dict[str, Any]:
    result = {
        "discovered_from": discovered_from,
        "location": binding.get("location"),
        "path": binding.get("path"),
        "expected_bytes": binding.get("bytes"),
        "expected_sha256": binding.get("sha256"),
        "resolved_path": None,
        "resolution_basis": None,
        "actual_bytes": None,
        "actual_sha256": None,
        "status": "FAIL",
        "errors": [],
    }
    errors = _binding_shape_errors(binding)
    if errors:
        result["errors"] = errors
        return result
    resolved, basis, path_errors = resolve_binding_path(
        binding["path"],
        workspace=workspace,
        local_base=local_base,
    )
    result["resolution_basis"] = basis
    if path_errors or resolved is None:
        result["errors"] = path_errors
        return result
    result["resolved_path"] = workspace_relative(resolved, workspace)
    if not resolved.is_file():
        result["errors"] = ["bound path is not a file"]
        return result
    actual_bytes = resolved.stat().st_size
    actual_sha256 = sha256_file(resolved)
    result["actual_bytes"] = actual_bytes
    result["actual_sha256"] = actual_sha256
    if actual_bytes != binding["bytes"]:
        errors.append("bytes mismatch")
    if actual_sha256.lower() != binding["sha256"].lower():
        errors.append("sha256 mismatch")
    result["errors"] = errors
    result["status"] = "PASS" if not errors else "FAIL"
    return result


def parse_evidence_document(path: Path) -> Any | None:
    if path.suffix.lower() == ".json":
        return load_json(path)
    if path.suffix.lower() == ".jsonl":
        rows = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if line.strip():
                    rows.append(
                        {
                            "line_number": line_number,
                            "value": json.loads(line),
                        }
                    )
        return rows
    return None


def audit_binding_graph(
    *,
    initial_results: list[dict[str, Any]],
    workspace: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Recursively verify bindings found in verified JSON/JSONL evidence."""

    queue: deque[Path] = deque()
    for result in initial_results:
        if result["status"] == "PASS" and result["resolved_path"]:
            queue.append(workspace / result["resolved_path"])
    seen_documents: set[Path] = set()
    discovered_results: list[dict[str, Any]] = []
    parse_failures: list[dict[str, Any]] = []
    seen_expectations: set[tuple[str, int, str, str]] = set()

    while queue:
        document = queue.popleft().resolve()
        if document in seen_documents:
            continue
        seen_documents.add(document)
        if document.suffix.lower() not in {".json", ".jsonl"}:
            continue
        try:
            value = parse_evidence_document(document)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            parse_failures.append(
                {
                    "path": workspace_relative(document, workspace),
                    "status": "FAIL",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if value is None:
            continue
        document_relative = workspace_relative(document, workspace)
        for binding in collect_embedded_bindings(value):
            expectation = (
                str(binding.get("path")),
                binding.get("bytes"),
                str(binding.get("sha256")).lower(),
                document_relative,
            )
            if expectation in seen_expectations:
                continue
            seen_expectations.add(expectation)
            result = verify_binding(
                binding,
                workspace=workspace,
                local_base=document.parent,
                discovered_from=document_relative,
            )
            discovered_results.append(result)
            if result["status"] == "PASS" and result["resolved_path"]:
                bound_path = workspace / result["resolved_path"]
                if bound_path.suffix.lower() in {".json", ".jsonl"}:
                    queue.append(bound_path)
    return discovered_results, parse_failures


def _find_verified_artifact(
    manifest_results: Iterable[dict[str, Any]],
    name: str,
    workspace: Path,
) -> Path | None:
    matches = [
        workspace / result["resolved_path"]
        for result in manifest_results
        if result["status"] == "PASS"
        and result["resolved_path"]
        and Path(result["resolved_path"]).name == name
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def extract_package_projection(
    matrix: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    propositions: list[dict[str, Any]] = []
    gates: list[dict[str, Any]] = []
    if not isinstance(matrix, dict):
        return propositions, gates

    clauses = matrix.get("clauses")
    if isinstance(clauses, list):
        for clause in clauses:
            if not isinstance(clause, dict):
                continue
            gate_id = clause.get("gate_id")
            gates.append(
                {
                    "gate_id": gate_id,
                    "declared_gate_status": clause.get("strict_gate_status"),
                    "closure_assessment": clause.get("closure_assessment"),
                    "status_origin": (
                        "PACKAGE_DECLARED_STRICT_GATE_STATUS"
                    ),
                }
            )
            for subcheck in clause.get("subchecks", []):
                if not isinstance(subcheck, dict):
                    continue
                propositions.append(
                    {
                        "gate_id": gate_id,
                        "subcheck_id": subcheck.get("subcheck_id"),
                        "baseline_status": subcheck.get("baseline_status"),
                        "strict_gate_status": subcheck.get(
                            "strict_gate_status"
                        ),
                        "closure_assessment": subcheck.get(
                            "closure_assessment"
                        ),
                        "status_semantics": subcheck.get(
                            "status_semantics",
                            "PACKAGE_DECLARED_SUBCHECK",
                        ),
                        "baseline_status_claimed": subcheck.get(
                            "baseline_status_claimed"
                        ),
                    }
                )

    gate_map = matrix.get("gates")
    if isinstance(gate_map, dict):
        for gate_id, gate in gate_map.items():
            if not isinstance(gate, dict):
                continue
            gates.append(
                {
                    "gate_id": gate_id,
                    "declared_gate_status": (
                        gate.get("strict_gate_status") or gate.get("status")
                    ),
                    "closure_assessment": gate.get("closure_assessment"),
                    "status_origin": "PACKAGE_DECLARED_GATE_STATUS",
                }
            )
            for subcheck in gate.get("subchecks", []):
                if not isinstance(subcheck, dict):
                    continue
                propositions.append(
                    {
                        "gate_id": gate_id,
                        "subcheck_id": subcheck.get("subcheck_id"),
                        "baseline_status": subcheck.get("baseline_status"),
                        "strict_gate_status": subcheck.get(
                            "strict_gate_status"
                        ),
                        "closure_assessment": subcheck.get(
                            "closure_assessment"
                        ),
                        "audit_status": subcheck.get("status"),
                        "status_semantics": subcheck.get(
                            "status_semantics",
                            "PACKAGE_DECLARED_SUBCHECK",
                        ),
                        "baseline_status_claimed": subcheck.get(
                            "baseline_status_claimed"
                        ),
                    }
                )
    return propositions, gates


def extract_rights_projection(
    receipt: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(receipt, dict):
        raise TypeError("RIGHTS_RECEIPT.json must be an object")
    scope = receipt.get("rights_scope", {})
    allowed = {
        row.get("code")
        for row in scope.get("allowed", [])
        if isinstance(row, dict)
    }
    not_covered = {
        row.get("code")
        for row in scope.get("not_covered", [])
        if isinstance(row, dict)
    }
    prohibited = {
        row.get("code")
        for row in scope.get("prohibited", [])
        if isinstance(row, dict)
    }
    expected_allowed = {
        "LOCAL_LONG_TERM_RETENTION_ON_THIS_MACHINE",
        "CAMPUS_INTERNAL_NONCOMMERCIAL_ACADEMIC_RESEARCH",
        "INTERNAL_DERIVED_FACTORS_AND_STATISTICS",
    }
    expected_not_covered = {
        "API_OR_AUTOMATED_BULK_COLLECTION",
        "POST_AFFILIATION_DISPOSITION",
        "OFFICIAL_SOURCE_RIGHTS",
        "INDEPENDENT_CONTRACT_VERIFICATION",
    }
    expected_prohibited = {
        "RAW_EXTERNAL_REDISTRIBUTION",
        "COMMERCIAL_USE",
    }
    attestation = receipt.get("attestation", {})
    effect = receipt.get("admission_effect", {})
    if (
        allowed != expected_allowed
        or not_covered != expected_not_covered
        or prohibited != expected_prohibited
        or attestation.get("basis") != "DIRECT_USER_ATTESTATION"
        or attestation.get("institution_contract_independently_verified")
        is not False
        or attestation.get("provider_contract_independently_verified")
        is not False
        or effect.get("rights_boundary_recorded_for_exact_assets") is not True
        or effect.get("admission_decision_preserved") != "NOT_ADMITTED"
        or effect.get("research_authorized") is not False
    ):
        raise ValueError("rights receipt violates the expected fail-closed scope")
    propositions = [
        {
            "gate_id": "STAGE-A-RIGHTS",
            "subcheck_id": "package_specific_rights_receipt",
            "baseline_status": "FAIL",
            "strict_gate_status": "FAIL",
            "closure_assessment": (
                "CSMAR_EXACT_ASSET_LOCAL_CAMPUS_SUBSCOPE_USER_ATTESTED__"
                "GLOBAL_RIGHTS_INCOMPLETE"
            ),
            "audit_status": "PARTIAL",
            "status_semantics": (
                "USER_ATTESTED_SUBSCOPE__NOT_INDEPENDENT_CONTRACT_VERIFICATION"
            ),
            "baseline_status_claimed": False,
            "allowed": sorted(allowed),
            "not_covered": sorted(not_covered),
            "prohibited": sorted(prohibited),
        }
    ]
    gates = [
        {
            "gate_id": "STAGE-A-RIGHTS",
            "declared_gate_status": "FAIL",
            "closure_assessment": (
                "GLOBAL_RIGHTS_REMAIN_FAIL__CSMAR_LOCAL_CAMPUS_SUBSCOPE_ONLY"
            ),
            "status_origin": "RIGHTS_RECEIPT_FAIL_CLOSED_PROJECTION",
        }
    ]
    return propositions, gates


def validate_fail_closed_projections(
    package_contexts: dict[str, dict[str, Any]],
) -> tuple[bool, dict[str, Any]]:
    observed: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for package_id, expected in EXPECTED_PACKAGE_GATE_STATUSES.items():
        gates = package_contexts.get(package_id, {}).get("gates", [])
        gate_map = {
            gate.get("gate_id"): gate.get("declared_gate_status")
            for gate in gates
            if isinstance(gate, dict) and gate.get("gate_id") in expected
        }
        observed[package_id] = dict(sorted(gate_map.items()))
        if gate_map != expected:
            errors.append(
                f"{package_id} expected {expected!r}, observed {gate_map!r}"
            )
    return not errors, {
        "expected": EXPECTED_PACKAGE_GATE_STATUSES,
        "observed": observed,
        "errors": errors,
    }


def audit_package(
    *,
    package_id: str,
    package_root: Path,
    workspace: Path,
    required: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    relative_root = (
        workspace_relative(package_root, workspace)
        if is_within(package_root, workspace)
        else str(package_root)
    )
    base = {
        "package_id": package_id,
        "package_root": relative_root,
        "required": required,
        "availability_status": None,
        "package_audit": {
            "status": None,
            "manifest_verified": False,
            "manifest_binding_count": 0,
            "recursive_binding_count": 0,
            "failed_binding_count": 0,
            "parse_failure_count": 0,
        },
        "manifest": None,
        "manifest_bindings": [],
        "recursive_evidence_bindings": [],
        "parse_failures": [],
        "proposition_assessments": [],
        "declared_gate_statuses": [],
        "projection_status": "NOT_ATTEMPTED",
        "formal_stage_a_effect": "NONE__AUDIT_ONLY",
    }
    context: dict[str, Any] = {
        "manifest_artifact_paths": {},
        "propositions": [],
        "gates": [],
    }
    if not package_root.is_dir():
        base["availability_status"] = "MISSING"
        base["package_audit"]["status"] = (
            "AUDIT_FAIL__REQUIRED_PACKAGE_MISSING"
            if required
            else "PENDING__OPTIONAL_PACKAGE_NOT_PRESENT"
        )
        base["projection_status"] = (
            "UNAVAILABLE__REQUIRED_PACKAGE_MISSING"
            if required
            else "PENDING__PACKAGE_NOT_PRESENT"
        )
        return base, context

    base["availability_status"] = "PRESENT"
    manifest_path = package_root / "REPORT_MANIFEST.json"
    if not manifest_path.is_file():
        base["package_audit"]["status"] = "AUDIT_FAIL__MANIFEST_MISSING"
        base["projection_status"] = "UNAVAILABLE__MANIFEST_MISSING"
        return base, context

    base["manifest"] = file_binding(manifest_path, workspace)
    try:
        root = load_json(manifest_path)
        root_format, payload = manifest_payload(root)
        records = collect_manifest_bindings(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        base["package_audit"]["status"] = "AUDIT_FAIL__MANIFEST_INVALID"
        base["package_audit"]["manifest_error"] = (
            f"{type(exc).__name__}: {exc}"
        )
        base["projection_status"] = "UNAVAILABLE__MANIFEST_INVALID"
        return base, context
    base["manifest"]["root_format"] = root_format
    if not records:
        base["package_audit"]["status"] = "AUDIT_FAIL__EMPTY_MANIFEST"
        base["projection_status"] = "UNAVAILABLE__EMPTY_MANIFEST"
        return base, context

    manifest_results = [
        verify_binding(
            record,
            workspace=workspace,
            local_base=package_root,
            discovered_from=workspace_relative(manifest_path, workspace),
        )
        for record in records
    ]
    base["manifest_bindings"] = manifest_results
    base["package_audit"]["manifest_binding_count"] = len(manifest_results)
    base["package_audit"]["manifest_verified"] = all(
        result["status"] == "PASS" for result in manifest_results
    )
    recursive_results, parse_failures = audit_binding_graph(
        initial_results=manifest_results,
        workspace=workspace,
    )
    base["recursive_evidence_bindings"] = recursive_results
    base["parse_failures"] = parse_failures
    base["package_audit"]["recursive_binding_count"] = len(
        recursive_results
    )
    failed_bindings = sum(
        result["status"] != "PASS"
        for result in manifest_results + recursive_results
    )
    base["package_audit"]["failed_binding_count"] = failed_bindings
    base["package_audit"]["parse_failure_count"] = len(parse_failures)
    audit_pass = (
        base["package_audit"]["manifest_verified"]
        and failed_bindings == 0
        and not parse_failures
    )
    base["package_audit"]["status"] = (
        "AUDITED_PASS__MANIFEST_AND_RECURSIVE_BINDINGS"
        if audit_pass
        else "AUDIT_FAIL__HASH_OR_PARSE_ERROR"
    )

    for result in manifest_results:
        if result["status"] == "PASS" and result["resolved_path"]:
            context["manifest_artifact_paths"][
                Path(result["resolved_path"]).name
            ] = workspace / result["resolved_path"]

    verification_path = _find_verified_artifact(
        manifest_results,
        "VERIFICATION.json",
        workspace,
    )
    if verification_path:
        try:
            verification = load_json(verification_path)
            if isinstance(verification, dict):
                base["declared_report_verification_status"] = (
                    verification.get("status")
                )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            base["declared_report_verification_status"] = (
                f"UNREADABLE: {type(exc).__name__}: {exc}"
            )

    projection_path = _find_verified_artifact(
        manifest_results,
        "SUBCHECK_EVIDENCE_MATRIX.json",
        workspace,
    )
    projection_kind = "SUBCHECK_EVIDENCE_MATRIX"
    if projection_path is None:
        projection_path = _find_verified_artifact(
            manifest_results,
            "GATE_RECEIPT.json",
            workspace,
        )
        projection_kind = "GATE_RECEIPT"
    if projection_path is None:
        projection_path = _find_verified_artifact(
            manifest_results,
            "RIGHTS_RECEIPT.json",
            workspace,
        )
        projection_kind = "RIGHTS_RECEIPT"
    if audit_pass and projection_path:
        try:
            projection = load_json(projection_path)
            if projection_kind == "RIGHTS_RECEIPT":
                propositions, gates = extract_rights_projection(projection)
            else:
                propositions, gates = extract_package_projection(projection)
            base["proposition_assessments"] = propositions
            base["declared_gate_statuses"] = gates
            context["propositions"] = propositions
            context["gates"] = gates
            base["projection_artifact"] = file_binding(
                projection_path,
                workspace,
            )
            base["projection_kind"] = projection_kind
            base["projection_status"] = (
                "AVAILABLE__PACKAGE_AUDITED__NO_FORMAL_EFFECT"
            )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            base["projection_status"] = (
                f"UNAVAILABLE__MATRIX_PARSE_ERROR: {type(exc).__name__}: {exc}"
            )
    elif audit_pass:
        base["projection_status"] = "UNAVAILABLE__MATRIX_NOT_MANIFEST_BOUND"
    else:
        base["projection_status"] = "UNTRUSTED__PACKAGE_AUDIT_FAILED"
    return base, context


def stage_a_baseline(state: dict[str, Any]) -> dict[str, Any]:
    phase = state.get("phase_a")
    if not isinstance(phase, dict):
        raise ValueError("STATE_MACHINE.json has no phase_a object")
    ready_count = phase.get("ready_count")
    target_count = phase.get("target_count")
    if (
        isinstance(ready_count, bool)
        or not isinstance(ready_count, int)
        or isinstance(target_count, bool)
        or not isinstance(target_count, int)
    ):
        raise ValueError("phase_a ready_count/target_count are not integers")
    return {
        "ready": f"{ready_count}/{target_count}",
        "ready_count": ready_count,
        "target_count": target_count,
        "all_ready": phase.get("all_ready"),
        "status": phase.get("status"),
    }


def baseline_gate_statuses(state: dict[str, Any]) -> dict[str, str]:
    phase = state.get("phase_a", {})
    rows = phase.get("digest_projection") or phase.get("targets") or []
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        stage = row.get("stage_a", row)
        for gate in stage.get("blocking_quality_gates", []):
            if isinstance(gate, dict) and gate.get("gate_id"):
                result[str(gate["gate_id"])] = gate.get("status")
    return result


def build_queue_delta(
    *,
    queue_document: dict[str, Any],
    state: dict[str, Any],
    package_ledgers: dict[str, dict[str, Any]],
    package_contexts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    core_to_package = {
        "CORE-01": "CORE01",
        "CORE-02": "CORE02",
        "CORE-03": "CORE03_CORE06_RULES",
        "CORE-06": "CORE03_CORE06_RULES",
        "GLOBAL": "CSMAR_RIGHTS",
    }
    baseline_gates = baseline_gate_statuses(state)
    items = []
    for row in queue_document.get("queue", []):
        package_id = core_to_package.get(row.get("core_id"))
        ledger = package_ledgers.get(package_id) if package_id else None
        context = package_contexts.get(package_id, {}) if package_id else {}
        package_audit_status = (
            ledger["package_audit"]["status"]
            if ledger
            else "NOT_APPLICABLE__NO_EVIDENCE_PACKAGE"
        )
        gate_id = row.get("gate_id")
        declared_gate = next(
            (
                gate
                for gate in context.get("gates", [])
                if gate.get("gate_id") == gate_id
            ),
            None,
        )
        gate_status = (
            declared_gate.get("declared_gate_status")
            if declared_gate
            else baseline_gates.get(gate_id)
        )
        gate_origin = (
            declared_gate.get("status_origin")
            if declared_gate
            else "FORMAL_STAGE_A_BASELINE"
        )
        check_id = row.get("check_id")
        exact_matches = [
            proposition
            for proposition in context.get("propositions", [])
            if proposition.get("subcheck_id") == check_id
            or str(proposition.get("subcheck_id", "")).endswith(
                f".{check_id}"
            )
        ]
        gate_context = [
            proposition.get("subcheck_id")
            for proposition in context.get("propositions", [])
            if proposition.get("gate_id") == gate_id
        ]
        items.append(
            {
                "queue_id": row.get("queue_id"),
                "core_id": row.get("core_id"),
                "gate_id": gate_id,
                "check_id": check_id,
                "baseline_queue_status": row.get("current_status"),
                "package_id": package_id,
                "package_audit_status": package_audit_status,
                "exact_proposition_matches": exact_matches,
                "proposition_closure_status": (
                    "EXACT_PACKAGE_SUBCHECKS_REPORTED_SEPARATELY"
                    if exact_matches
                    else "NO_EXACT_PACKAGE_SUBCHECK_MATCH"
                ),
                "gate_scope_context_only": (
                    [] if exact_matches else gate_context
                ),
                "declared_or_baseline_gate_status": gate_status,
                "gate_status_origin": gate_origin,
                "queue_status_after_audit": row.get("current_status"),
                "queue_status_change": "NONE",
                "formal_stage_a_effect": "NONE__AUDIT_ONLY",
            }
        )
    queue_state = queue_document.get("formal_state", {})
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "separation_contract": {
            "package_audit": (
                "Only verifies bytes/SHA-256 and parseability of the "
                "hash-bound evidence graph."
            ),
            "proposition_closure": (
                "Copied only from an audited package and never aggregated "
                "into a gate."
            ),
            "gate_status": (
                "Copied from an audited package's explicit gate declaration "
                "or inherited from the formal Stage-A baseline."
            ),
            "formal_stage_a": (
                "Inherited unchanged; package audit and local PASS do not "
                "promote formal state."
            ),
        },
        "before": {
            "formal_state": queue_state,
            "summary": queue_document.get("summary"),
        },
        "after_audit_only": {
            "formal_state": queue_state,
            "summary": queue_document.get("summary"),
        },
        "items": items,
        "summary": {
            "queue_item_count": len(items),
            "nonpass_clause_count": queue_document.get("summary", {}).get(
                "nonpass_clause_count"
            ),
            "blocking_nonpass_clause_count": queue_document.get(
                "summary", {}
            ).get("blocking_nonpass_clause_count"),
            "queue_status_changes": 0,
            "formal_stage_a_effect": "NONE",
        },
    }


def report_readme(
    *,
    required_pass: bool,
    projections_fail_closed: bool,
    queue_ready: str,
    state_ready: str,
) -> str:
    return f"""# Stage-A 证据汇总审计

本包只做证据审计与机器可读汇总，不做正式准入、catalog/registry 修改、质量晋级、快照发布或研究授权。

## 结果

- 必需输入包（closure queue、CORE01、CORE02、CORE03/06 rules、CSMAR rights）严格审计通过：`{str(required_pass).lower()}`。
- 证据包声明状态与 fail-closed 预期完全一致：`{str(projections_fail_closed).lower()}`。
- closure queue 正式状态：`{queue_ready}`。
- 当前 `STATE_MACHINE.json` 正式状态：`{state_ready}`。
- 本轮正式状态变化：`NONE`，Stage A 固定继承当前 `0/4`。

## 三层语义

1. `PACKAGE_LEDGER.json` 的 package audit 只说明清单和递归证据绑定通过 bytes/SHA-256/JSON 校验。
2. `QUEUE_DELTA.json` 的 proposition closure 只复制已审计包中的明确子命题结论，不向上聚合。
3. gate status 只复制包的显式 gate 声明或正式 baseline；CSMAR 用户声明只关闭精确资产的本机留存、校内非商业研究和内部派生子范围，GLOBAL rights 仍为 `FAIL`。
4. CORE02 Q1/Q2 保持 `FAIL`，CORE03 Q2/Q3 保持 `UNKNOWN`，CORE06 Q3/Q4 保持 `FAIL`。

`REPORT_MANIFEST.json` 使用包内相对路径，便于安全的 `--replace` 重跑。旧输出在替换时被移动到可恢复的同级 `.previous-*` 目录，不会静默删除。
"""


def consolidation_status(
    *,
    required_pass: bool,
    projections_fail_closed: bool,
    guards_unchanged: bool,
) -> tuple[bool, str]:
    """Return fail-closed report publish eligibility and report status."""

    overall_pass = (
        required_pass
        and projections_fail_closed
        and guards_unchanged
    )
    if not overall_pass:
        return False, "FAIL__EVIDENCE_OR_FORMAL_GUARD_CHECK"
    return (
        True,
        "PASS__5_REQUIRED_PACKAGES_AUDITED__FAIL_CLOSED_PROJECTIONS_MATCH__"
        "FORMAL_STAGE_A_0_OF_4_UNCHANGED",
    )


def write_report_manifest(root: Path, status: str) -> None:
    files = [
        package_relative_binding(path, root)
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.relative_to(root).as_posix() != "REPORT_MANIFEST.json"
    ]
    write_json(
        root / "REPORT_MANIFEST.json",
        {
            "schema_version": 1,
            "run_id": RUN_ID,
            "status": status,
            "files": files,
        },
    )


def publish_staging(
    *,
    staging: Path,
    output: Path,
    replace: bool,
) -> Path | None:
    if output.exists() and not replace:
        raise FileExistsError(
            f"refusing to overwrite existing output without --replace: {output}"
        )
    previous: Path | None = None
    if output.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        previous = output.with_name(f".{output.name}.previous-{stamp}")
        output.rename(previous)
    try:
        staging.rename(output)
    except Exception:
        if previous and previous.exists() and not output.exists():
            previous.rename(output)
        raise
    return previous


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--queue-package", default=DEFAULT_QUEUE_PACKAGE)
    parser.add_argument("--core01-package", default=DEFAULT_CORE01_PACKAGE)
    parser.add_argument("--core02-package", default=DEFAULT_CORE02_PACKAGE)
    parser.add_argument("--rules-package", default=DEFAULT_RULES_PACKAGE)
    parser.add_argument("--rights-package", default=DEFAULT_RIGHTS_PACKAGE)
    parser.add_argument("--stage-machine", default=DEFAULT_STAGE_MACHINE)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--replace",
        action="store_true",
        help=(
            "Build in a staging directory, then replace the exact tool-owned "
            "output while retaining the prior report as .previous-*."
        ),
    )
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    output = (workspace / args.output).resolve()
    reports_root = (workspace / "reports" / "data-governance").resolve()
    require_child(output, reports_root, "output")
    if output.exists() and not args.replace:
        raise FileExistsError(
            f"refusing to overwrite existing output without --replace: {output}"
        )
    staging = output.with_name(f".{output.name}.building-{os.getpid()}")
    require_child(staging, reports_root, "staging")
    if staging.exists():
        raise FileExistsError(f"staging directory already exists: {staging}")
    staging.mkdir(parents=True, exist_ok=False)

    before_guards = formal_guards(workspace)
    stage_machine_path = (workspace / args.stage_machine).resolve()
    state = load_json(stage_machine_path)
    state_baseline = stage_a_baseline(state)

    package_specs = (
        (
            "CLOSURE_QUEUE",
            (workspace / args.queue_package).resolve(),
            True,
        ),
        (
            "CORE01",
            (workspace / args.core01_package).resolve(),
            True,
        ),
        (
            "CORE02",
            (workspace / args.core02_package).resolve(),
            True,
        ),
        (
            "CORE03_CORE06_RULES",
            (workspace / args.rules_package).resolve(),
            True,
        ),
        (
            "CSMAR_RIGHTS",
            (workspace / args.rights_package).resolve(),
            True,
        ),
    )
    ledgers: dict[str, dict[str, Any]] = {}
    contexts: dict[str, dict[str, Any]] = {}
    for package_id, package_root, required in package_specs:
        require_child(package_root, reports_root, f"{package_id} package")
        ledger, context = audit_package(
            package_id=package_id,
            package_root=package_root,
            workspace=workspace,
            required=required,
        )
        ledgers[package_id] = ledger
        contexts[package_id] = context

    queue_ledger = ledgers["CLOSURE_QUEUE"]
    queue_path = contexts["CLOSURE_QUEUE"]["manifest_artifact_paths"].get(
        "CLOSURE_QUEUE.json"
    )
    if (
        queue_ledger["package_audit"]["status"]
        != "AUDITED_PASS__MANIFEST_AND_RECURSIVE_BINDINGS"
        or queue_path is None
    ):
        raise RuntimeError(
            "closure queue package did not pass strict audit; no report "
            "will be published"
        )
    queue_document = load_json(queue_path)
    if not isinstance(queue_document, dict):
        raise TypeError("CLOSURE_QUEUE.json must be an object")
    queue_ready = queue_document.get("formal_state", {}).get("stage_a_ready")
    if queue_ready != state_baseline["ready"] or queue_ready != "0/4":
        raise RuntimeError(
            "formal Stage-A baseline disagreement; expected current 0/4, "
            f"queue={queue_ready!r}, state={state_baseline['ready']!r}"
        )

    required_ids = (
        "CLOSURE_QUEUE",
        "CORE01",
        "CORE02",
        "CORE03_CORE06_RULES",
        "CSMAR_RIGHTS",
    )
    required_pass = all(
        ledgers[package_id]["package_audit"]["status"]
        == "AUDITED_PASS__MANIFEST_AND_RECURSIVE_BINDINGS"
        for package_id in required_ids
    )
    projections_fail_closed, projection_verification = (
        validate_fail_closed_projections(contexts)
    )
    queue_delta = build_queue_delta(
        queue_document=queue_document,
        state=state,
        package_ledgers=ledgers,
        package_contexts=contexts,
    )
    generated_at = datetime.now(timezone.utc).isoformat()
    package_ledger = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "generated_at_utc": generated_at,
        "semantics": {
            "package_audited": (
                "All manifest and recursively discovered evidence bindings "
                "matched exact bytes/SHA-256 and parsed where applicable."
            ),
            "proposition_closed": (
                "Only an explicit package subcheck assessment; it is not a "
                "gate transition."
            ),
            "gate_status": (
                "Only an explicit package declaration or formal baseline."
            ),
        },
        "packages": list(ledgers.values()),
        "summary": {
            "required_package_count": len(required_ids),
            "required_packages_audited_pass": sum(
                ledgers[package_id]["package_audit"]["status"]
                == "AUDITED_PASS__MANIFEST_AND_RECURSIVE_BINDINGS"
                for package_id in required_ids
            ),
            "fail_closed_projections_match": projections_fail_closed,
            "projection_verification": projection_verification,
            "formal_stage_a_effect": "NONE",
        },
    }

    write_json(staging / "PACKAGE_LEDGER.json", package_ledger)
    write_json(staging / "QUEUE_DELTA.json", queue_delta)
    (staging / "README.md").write_text(
        report_readme(
            required_pass=required_pass,
            projections_fail_closed=projections_fail_closed,
            queue_ready=queue_ready,
            state_ready=state_baseline["ready"],
        ),
        encoding="utf-8",
        newline="\n",
    )

    after_analysis_guards = formal_guards(workspace)
    guards_unchanged = before_guards == after_analysis_guards
    guard_document = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "before": before_guards,
        "after": after_analysis_guards,
        "unchanged": guards_unchanged,
        "protected_meaning": (
            "Formal catalog/registry/Roadmap/projects, Stage-A state, "
            "snapshots inventory and published inventory."
        ),
    }
    write_json(staging / "FORMAL_STATE_GUARDS.json", guard_document)

    overall_pass, status = consolidation_status(
        required_pass=required_pass,
        projections_fail_closed=projections_fail_closed,
        guards_unchanged=guards_unchanged,
    )
    verification = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "generated_at_utc": generated_at,
        "required_packages_audited": {
            package_id: ledgers[package_id]["package_audit"]["status"]
            for package_id in required_ids
        },
        "fail_closed_projection_verification": projection_verification,
        "fail_closed_projections_match": projections_fail_closed,
        "formal_stage_a": {
            "queue_before": queue_ready,
            "state_machine_before": state_baseline["ready"],
            "after": "0/4",
            "changed": False,
        },
        "package_audit_is_gate_transition": False,
        "proposition_pass_is_gate_transition": False,
        "queue_status_changes": 0,
        "formal_guards_unchanged": guards_unchanged,
        "catalog_update": False,
        "source_registry_update": False,
        "roadmap_update": False,
        "projects_update": False,
        "snapshot_publish": False,
        "quality_promotion": False,
        "formal_admission": False,
        "research_authorization": False,
        "status": status,
    }
    write_json(staging / "VERIFICATION.json", verification)
    write_report_manifest(staging, status)

    after_final_guards = formal_guards(workspace)
    if (
        not overall_pass
        or after_final_guards != before_guards
        or after_final_guards != after_analysis_guards
    ):
        raise RuntimeError(
            "required package audit or formal guard check failed; staging "
            f"preserved for inspection at {staging}"
        )
    publish_staging(staging=staging, output=output, replace=args.replace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
