#!/usr/bin/env python3
"""Read-only, fail-closed verifier for the independent R0_SHSZ_PV gate."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT = (
    WORKSPACE_ROOT
    / "data"
    / "catalog"
    / "contracts"
    / "r0_shsz_pv_v1"
    / "contract.json"
)
EXPECTED_SCOPE_NAME = "SSE_SZSE_A_SHARES_2005_2024_BOUNDED_R0"


class VerificationError(RuntimeError):
    """Raised when an evidence path or manifest is unsafe or malformed."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise VerificationError(f"JSON object required: {path}")
    return value


def safe_workspace_path(workspace: Path, relative: str) -> Path:
    value = Path(relative)
    if value.is_absolute() or ".." in value.parts:
        raise VerificationError(f"unsafe workspace-relative path: {relative}")
    root = workspace.resolve(strict=True)
    candidate = (root / value).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise VerificationError(f"path escapes workspace: {relative}") from exc
    if not candidate.is_file():
        raise VerificationError(f"evidence is not a file: {relative}")
    return candidate


def resolve_manifest_entry(workspace: Path, manifest_path: Path, relative: str) -> Path:
    value = Path(relative)
    if value.is_absolute() or ".." in value.parts:
        raise VerificationError(f"unsafe manifest entry path: {relative}")
    root = workspace.resolve(strict=True)
    candidates = []
    for candidate in (manifest_path.parent / value, root / value):
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (FileNotFoundError, ValueError):
            continue
        if resolved.is_file() and resolved not in candidates:
            candidates.append(resolved)
    if len(candidates) != 1:
        raise VerificationError(
            f"manifest entry must resolve uniquely inside workspace: {relative}; found={len(candidates)}"
        )
    return candidates[0]


def record_check(path: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    actual = {
        "path": path.as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    passed = (
        actual["bytes"] == expected.get("bytes")
        and actual["sha256"] == expected.get("sha256")
    )
    return {"verdict": "PASS" if passed else "FAIL", "expected": dict(expected), "actual": actual}


def declared_entries(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = manifest.get("artifacts")
    if rows is None:
        rows = manifest.get("entries")
    if not isinstance(rows, list):
        raise VerificationError("manifest must contain artifacts or entries list")
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise VerificationError("invalid manifest entry")
        path = row["path"]
        if path in result:
            raise VerificationError(f"duplicate manifest entry: {path}")
        result[path] = row
    return result


def verify_critical_entries(
    workspace: Path,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    names: list[str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    entries = declared_entries(manifest)
    checks: dict[str, Any] = {}
    documents: dict[str, dict[str, Any]] = {}
    for name in names:
        row = entries.get(name)
        if row is None:
            checks[name] = {"verdict": "FAIL", "reason": "critical entry is not declared"}
            continue
        try:
            path = resolve_manifest_entry(workspace, manifest_path, name)
            check = record_check(path, row)
            checks[name] = check
            if check["verdict"] == "PASS":
                documents[name] = load_json(path)
        except Exception as exc:  # fail closed and preserve the precise reason in JSON output
            checks[name] = {"verdict": "FAIL", "reason": str(exc)}
    return checks, documents


def nested(value: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, Mapping) or key not in current:
            return default
        current = current[key]
    return current


def add_check(
    checks: dict[str, Any], code: str, actual: Any, expected: Any
) -> bool:
    passed = actual == expected
    checks[code] = {
        "verdict": "PASS" if passed else "FAIL",
        "expected": expected,
        "actual": actual,
    }
    return passed


def verify_gate(workspace: Path, contract: Mapping[str, Any]) -> dict[str, Any]:
    workspace = workspace.resolve(strict=True)
    checks: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    documents: dict[str, dict[str, dict[str, Any]]] = {}

    add_check(checks, "contract.schema_version", contract.get("schema_version"), 1)
    add_check(checks, "contract.gate_id", contract.get("gate_id"), "R0_SHSZ_PV")
    add_check(
        checks,
        "contract.canonical_unchanged",
        nested(contract, "independence", "canonical_gate_modified"),
        False,
    )
    add_check(checks, "scope.venues", nested(contract, "scope", "venues"), ["XSHG", "XSHE"])
    add_check(checks, "scope.window_start", nested(contract, "scope", "window_start"), "2005-01-01")
    add_check(checks, "scope.window_end", nested(contract, "scope", "window_end"), "2024-12-31")
    add_check(checks, "scope.excluded_conflict_keys", nested(contract, "scope", "excluded_conflict_key_count"), 375)
    add_check(checks, "scope.bjse_excluded", nested(contract, "scope", "bjse_in_scope"), False)

    manifest_specs = contract.get("evidence_manifests")
    if not isinstance(manifest_specs, Mapping):
        checks["contract.evidence_manifests"] = {
            "verdict": "FAIL",
            "reason": "evidence_manifests must be an object",
        }
        manifest_specs = {}

    manifests: dict[str, dict[str, Any]] = {}
    manifest_paths: dict[str, Path] = {}
    for name, raw_spec in manifest_specs.items():
        if not isinstance(raw_spec, Mapping):
            evidence[name] = {"manifest": {"verdict": "FAIL", "reason": "invalid spec"}}
            continue
        try:
            path = safe_workspace_path(workspace, str(raw_spec["path"]))
            manifest_check = record_check(path, raw_spec)
            manifest = load_json(path) if manifest_check["verdict"] == "PASS" else {}
            run_id_check = add_check(
                checks,
                f"evidence.{name}.run_id",
                manifest.get("run_id"),
                raw_spec.get("run_id"),
            )
            critical_checks, critical_documents = verify_critical_entries(
                workspace,
                path,
                manifest,
                list(raw_spec.get("critical_entries", [])),
            ) if manifest else ({}, {})
            evidence[name] = {
                "manifest": manifest_check,
                "run_id_verdict": "PASS" if run_id_check else "FAIL",
                "critical_entries": critical_checks,
            }
            manifests[name] = manifest
            manifest_paths[name] = path
            documents[name] = critical_documents
        except Exception as exc:
            evidence[name] = {"manifest": {"verdict": "FAIL", "reason": str(exc)}}

    price = manifests.get("price_volume", {})
    core_state = manifests.get("core03_core06", {})
    action = manifests.get("core04_core05", {})
    v4 = manifests.get("preadmission_v4", {})
    core_state_docs = documents.get("core03_core06", {})
    action_docs = documents.get("core04_core05", {})
    v4_docs = documents.get("preadmission_v4", {})

    add_check(checks, "price.scope.venues", nested(price, "scope", "venues"), ["XSHG", "XSHE"])
    add_check(checks, "price.scope.window_start", nested(price, "scope", "window_start"), "2005-01-01")
    add_check(checks, "price.scope.window_end", nested(price, "scope", "window_end"), "2024-12-31")
    add_check(checks, "price.research_ready", price.get("research_ready"), False)
    add_check(checks, "v4.scope", v4.get("scope"), EXPECTED_SCOPE_NAME)
    add_check(checks, "v4.research_ready", v4.get("research_ready"), False)

    core01_gate = v4_docs.get(
        "data/market/quarantine/r0-core01-06-bounded-preadmission/candidate-20260801-v4/CORE01/GATE_EVIDENCE.json",
        {},
    )
    add_check(
        checks,
        "core01.excluded_conflict_keys",
        nested(core01_gate, "bounded_restricted_evidence", "excluded_keys"),
        375,
    )
    add_check(checks, "core01.scope", core01_gate.get("scope"), EXPECTED_SCOPE_NAME)

    state_bindings = core_state_docs.get("INPUT_BINDINGS.json", {})
    add_check(checks, "core03_06.scope.venues", nested(state_bindings, "selection", "venues"), ["XSHG", "XSHE"])
    add_check(checks, "core03_06.scope.window_start", nested(state_bindings, "selection", "window_start"), "2005-01-01")
    add_check(checks, "core03_06.scope.window_end", nested(state_bindings, "selection", "window_end"), "2024-12-31")
    add_check(
        checks,
        "core03_06.availability_policy",
        nested(state_bindings, "selection", "available_at_policy"),
        nested(contract, "scope", "availability_policy"),
    )

    v4_aggregate = v4_docs.get(
        "data/market/quarantine/r0-core01-06-bounded-preadmission/candidate-20260801-v4/AGGREGATE_DECISION.json",
        {},
    )
    core04_gate = action_docs.get(
        "data/market/quarantine/r0-core04-core05-split/candidate-20260801-v1/GATE_DECISION.json",
        {},
    )

    current = {
        "formal_ready_count": v4.get("formal_ready_count"),
        "formal_fail_count": v4.get("formal_fail_count"),
        "formal_research_rows": v4.get("formal_research_rows"),
        "core04_complete_revision_chain": nested(v4, "upstream_checks", "complete_revision_chain"),
        "core04_strict_action_rows": v4.get("core04_strict_action_rows"),
        "core05_strict_projection_event_input_rows": v4.get("core05_strict_projection_event_input_rows"),
        "core06_open_execution_eligible_rows": nested(core_state, "statistics", "summary", "core06_open_execution_eligible_rows"),
    }
    expected_current = contract.get("current_required_fail_closed_assertions", {})
    for key, expected in expected_current.items():
        add_check(checks, f"current_fail_closed.{key}", current.get(key), expected)

    add_check(
        checks,
        "core04.source_status",
        nested(core04_gate, "CORE04", "status"),
        "PARTIAL_OBSERVATION_CLOSURE__NOT_ADMITTED",
    )
    add_check(
        checks,
        "core05.source_status",
        nested(core04_gate, "CORE05", "status"),
        "MECHANICAL_ENGINE_REPLAY_PASSED__REAL_PROJECTION_BLOCKED",
    )
    add_check(
        checks,
        "v4.parent_gate_fail_closed",
        nested(v4_aggregate, "bounded_restricted", "full_parent_gate"),
        "FAIL_CLOSED__CORE04_COMPLETE_REVISION_CHAIN_AND_CORE05_STRICT_INPUTS_OPEN__FORMAL_PUBLICATION_L2_OPEN",
    )

    evidence_checks = [item.get("verdict") == "PASS" for item in checks.values()]
    for item in evidence.values():
        evidence_checks.append(nested(item, "manifest", "verdict") == "PASS")
        evidence_checks.append(item.get("run_id_verdict") == "PASS")
        for entry in item.get("critical_entries", {}).values():
            evidence_checks.append(entry.get("verdict") == "PASS")
    integrity_pass = bool(evidence_checks) and all(evidence_checks)

    formal_statuses = nested(v4_aggregate, "formal", "per_core", default={})
    per_core = {
        core: {
            "evidence_status": (
                "RESTRICTED_EVIDENCE_ONLY" if core in {"CORE-01", "CORE-02", "CORE-03"}
                else "OPEN_BLOCKERS_CONFIRMED"
            ),
            "research_gate_status": "FAIL_CLOSED__NOT_ADMITTED",
            "source_formal_status": formal_statuses.get(core) if isinstance(formal_statuses, Mapping) else None,
        }
        for core in ("CORE-01", "CORE-02", "CORE-03", "CORE-04", "CORE-05", "CORE-06")
    }

    requirements = contract.get("gate_open_requirements", {})
    gate_conditions = {
        "evidence_integrity": integrity_pass,
        "formal_ready_count": current["formal_ready_count"] == requirements.get("formal_ready_count"),
        "formal_fail_count": current["formal_fail_count"] == requirements.get("formal_fail_count"),
        "formal_research_rows": isinstance(current["formal_research_rows"], int)
        and current["formal_research_rows"] >= requirements.get("formal_research_rows_minimum", 1),
        "core04_complete_revision_chain": current["core04_complete_revision_chain"] is True,
        "core04_strict_action_rows": isinstance(current["core04_strict_action_rows"], int)
        and current["core04_strict_action_rows"] >= requirements.get("core04_strict_action_rows_minimum", 1),
        "core05_strict_projection_inputs": isinstance(current["core05_strict_projection_event_input_rows"], int)
        and current["core05_strict_projection_event_input_rows"]
        >= requirements.get("core05_strict_projection_event_input_rows_minimum", 1),
        "core06_open_execution": isinstance(current["core06_open_execution_eligible_rows"], int)
        and current["core06_open_execution_eligible_rows"]
        >= requirements.get("core06_open_execution_eligible_rows_minimum", 1),
        "all_six_core_admitted": isinstance(formal_statuses, Mapping)
        and len(formal_statuses) == 6
        and all(value == requirements.get("all_six_core_formal_status") for value in formal_statuses.values()),
    }
    gate_pass = all(gate_conditions.values())

    return {
        "schema_version": 1,
        "contract_id": contract.get("contract_id"),
        "gate_id": "R0_SHSZ_PV",
        "canonical_r0_pv_effect": "NONE",
        "scope": dict(contract.get("scope", {})),
        "evidence_integrity_verdict": "PASS" if integrity_pass else "FAIL",
        "research_gate_verdict": "PASS" if gate_pass else "FAIL",
        "overall_verdict": "PASS" if integrity_pass and gate_pass else "FAIL",
        "current_gate_state": "OPEN" if gate_pass else "FAIL_CLOSED",
        "checks": checks,
        "evidence": evidence,
        "current_measurements": current,
        "per_core": per_core,
        "gate_conditions": {
            key: "PASS" if value else "FAIL" for key, value in gate_conditions.items()
        },
        "writes_performed": 0,
        "network_requests": 0,
        "factor_runs": 0,
    }


def exit_code_for(result: Mapping[str, Any]) -> int:
    if result.get("evidence_integrity_verdict") != "PASS":
        return 3
    if result.get("research_gate_verdict") != "PASS":
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=WORKSPACE_ROOT)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    args = parser.parse_args()
    try:
        contract = load_json(args.contract.resolve(strict=True))
        result = verify_gate(args.workspace, contract)
    except Exception as exc:
        result = {
            "schema_version": 1,
            "gate_id": "R0_SHSZ_PV",
            "evidence_integrity_verdict": "FAIL",
            "research_gate_verdict": "FAIL",
            "overall_verdict": "FAIL",
            "current_gate_state": "FAIL_CLOSED",
            "error": str(exc),
            "writes_performed": 0,
            "network_requests": 0,
            "factor_runs": 0,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code_for(result)


if __name__ == "__main__":
    raise SystemExit(main())
