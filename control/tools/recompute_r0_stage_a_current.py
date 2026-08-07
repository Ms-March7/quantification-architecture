#!/usr/bin/env python3
"""Recompute the current non-circular R0 Stage-A gate fail-closed.

This tool binds the 2026-08-01 identity, calendar/rules and action/PIT
quarantine candidates, including the final bounded official identity/history
and calendar/session searches, to the already sealed CORE-03/CORE-06 semantic
package and the exact user-attested CSMAR asset rights receipt.  It is
deliberately a report-only tool: it never edits the catalog, source registry,
quality state or formal snapshot trees.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).resolve().parents[2]
RUN_ID = "r0-stage-a-current-recompute-20260801-v3"
REPORT = WORKSPACE / "reports" / "data-governance" / RUN_ID

AUTHORITY_PATHS = (
    "control/ROADMAP.md",
    "control/WORKSPACE_INDEX.md",
    "control/projects.yaml",
    "data/catalog/SOURCE_POLICY.md",
    "data/catalog/DATA_QUALITY_POLICY.md",
    "data/catalog/datasets.json",
    "data/catalog/contracts/source_admission_v1/current_source_registry.json",
    "data/market/snapshots/README.md",
    "data/published/README.md",
)

UPSTREAM_MANIFESTS = {
    "identity_candidate_v2": "data/market/quarantine/r0-security-identity-intervals/intake-20260801-v2/CANDIDATE_MANIFEST.json",
    "identity_report_v2": "reports/data-governance/r0-security-identity-intervals-20260801-v2/REPORT_MANIFEST.json",
    "bse_current_observation": "data/market/quarantine/r0-official-identity/bse-current-20260801-v1/CANDIDATE_MANIFEST.json",
    "identity_history_search_candidate": "data/market/quarantine/r0-official-identity/history-search-20260801-v1/CANDIDATE_MANIFEST.json",
    "identity_history_search_report": "reports/data-governance/r0-official-identity-history-search-20260801-v1/REPORT_MANIFEST.json",
    "calendar_candidate_v2": "data/market/quarantine/r0-official-calendar-rules/normalized-stagea-20260801-v2/CANDIDATE_MANIFEST.json",
    "calendar_report_v2": "reports/data-governance/r0-official-calendar-rules-normalized-stagea-20260801-v2/REPORT_MANIFEST.json",
    "calendar_minimum_gap_candidate": "data/market/quarantine/r0-official-calendar-rules/minimum-gap-closure-20260801-v1/CANDIDATE_MANIFEST.json",
    "calendar_minimum_gap_report": "reports/data-governance/r0-official-calendar-rules-minimum-gap-closure-20260801-v1/REPORT_MANIFEST.json",
    "action_pit_candidate": "data/market/quarantine/r0-action-pit-normalized/candidate-20260801-v1/CANDIDATE_MANIFEST.json",
    "action_pit_report": "reports/data-governance/r0-action-pit-normalized-candidate-20260801-v1/REPORT_MANIFEST.json",
    "core03_core06_candidate": "tmp/d2a04b-source-closure/stage-a-core03-core06-semantic-closure-20260730-v2/CANDIDATE_MANIFEST.json",
    "core03_core06_report": "reports/data-governance/stage-a-core03-core06-semantic-closure-20260730-v2/REPORT_MANIFEST.json",
    "csmar_user_attested_rights": "reports/data-governance/csmar-user-attested-rights-receipt-20260730-v1/REPORT_MANIFEST.json",
}


class RecomputeError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecomputeError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RecomputeError(f"JSON root must be object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def binding(relative: str) -> dict[str, Any]:
    path = WORKSPACE / relative
    if not path.is_file():
        raise RecomputeError(f"missing bound file: {relative}")
    return {
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def manifest_entries(value: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("files", "entries", "artifacts"):
        rows = value.get(key)
        if isinstance(rows, list):
            return rows
    raise RecomputeError("manifest has no supported entries list")


def verify_manifest(relative: str) -> dict[str, Any]:
    manifest_path = WORKSPACE / relative
    value = load_json(manifest_path)
    rows = manifest_entries(value)
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise RecomputeError(f"manifest row {index} is not an object: {relative}")
        entry_path = row.get("path")
        expected_hash = row.get("sha256")
        expected_bytes = row.get("bytes")
        if not isinstance(entry_path, str) or not entry_path or entry_path in seen:
            raise RecomputeError(f"invalid or duplicate manifest path: {relative} row {index}")
        seen.add(entry_path)
        path = WORKSPACE / entry_path
        if not path.is_file():
            raise RecomputeError(f"manifest entry is missing: {entry_path}")
        if path.stat().st_size != expected_bytes:
            raise RecomputeError(f"manifest byte mismatch: {entry_path}")
        if sha256_file(path) != expected_hash:
            raise RecomputeError(f"manifest hash mismatch: {entry_path}")
    return {
        **binding(relative),
        "entry_count": len(rows),
        "entries_verified": True,
    }


def authority_guard() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "files": {relative: binding(relative) for relative in AUTHORITY_PATHS},
        "formal_trees": {
            "data/market/snapshots": sorted(
                str(path.relative_to(WORKSPACE)).replace("\\", "/")
                for path in (WORKSPACE / "data/market/snapshots").rglob("*")
                if path.is_file()
            ),
            "data/published": sorted(
                str(path.relative_to(WORKSPACE)).replace("\\", "/")
                for path in (WORKSPACE / "data/published").rglob("*")
                if path.is_file()
            ),
        },
    }


def validate_source_decisions() -> dict[str, Any]:
    identity = load_json(
        WORKSPACE
        / "reports/data-governance/r0-security-identity-intervals-20260801-v2/STAGE_A_REEVALUATION.json"
    )
    if identity.get("core01_stage_a_ready") is not False:
        raise RecomputeError("identity package unexpectedly reports CORE-01 ready")
    identity_gates = {
        gate: details.get("strict_status")
        for gate, details in identity.get("quality_gates", {}).items()
    }
    if any(identity_gates.get(gate) != "FAIL" for gate in ("CORE01-Q1", "CORE01-Q2", "CORE01-Q3", "CORE01-Q4")):
        raise RecomputeError("identity package strict gate status changed")

    identity_search_verification = load_json(
        WORKSPACE
        / "reports/data-governance/r0-official-identity-history-search-20260801-v1/VERIFICATION.json"
    )
    identity_search = load_json(
        WORKSPACE
        / "reports/data-governance/r0-official-identity-history-search-20260801-v1/BOUNDED_SEARCH_CONCLUSION.json"
    )
    if (
        identity_search_verification.get("passed") is not True
        or identity_search_verification.get("errors") != []
        or identity_search.get("bounded_search_complete") is not True
        or identity_search.get("permanent_identity_found") is not False
        or identity_search.get(
            "complete_current_delisted_relisted_code_name_board_history_found"
        )
        is not False
        or identity_search.get("formal_stage_a_after") != "0/4"
    ):
        raise RecomputeError("final identity/history search boundary changed")

    calendar = load_json(
        WORKSPACE
        / "reports/data-governance/r0-official-calendar-rules-normalized-stagea-20260801-v2/CORE02_STAGE_A_RECOMPUTE.json"
    )
    calendar_gates = {
        gate: details.get("status") for gate, details in calendar.get("gates", {}).items()
    }
    if calendar_gates != {
        "CORE02-Q1": "FAIL",
        "CORE02-Q2": "FAIL",
        "CORE02-Q3": "UNKNOWN",
        "CORE02-Q4": "PASS",
    }:
        raise RecomputeError("calendar package strict gate status changed")

    calendar_search = load_json(
        WORKSPACE
        / "reports/data-governance/r0-official-calendar-rules-minimum-gap-closure-20260801-v1/CORE02_GATE_RECOMPUTE.json"
    )
    calendar_search_gates = {
        gate: details.get("status")
        for gate, details in calendar_search.get("gates", {}).items()
    }
    if (
        calendar_search_gates != calendar_gates
        or calendar_search.get("formal_admission") is not False
        or calendar_search.get("decision") != "FAIL_CLOSED__NOT_ADMITTED"
    ):
        raise RecomputeError("final calendar/session search boundary changed")

    action = load_json(
        WORKSPACE
        / "reports/data-governance/r0-action-pit-normalized-candidate-20260801-v1/VERIFICATION.json"
    )
    if (
        action.get("formal_effect") != "NONE"
        or action.get("core_04_assessment")
        != "PARTIAL__EVENT_TERMS_AND_PIT_CHAIN_UNCLOSED"
        or action.get("core_05_assessment")
        != "BLOCKED__NO_FORMAL_CORE04_ACTION_PROJECTION"
    ):
        raise RecomputeError("action/PIT package boundary changed")

    semantic = load_json(
        WORKSPACE
        / "tmp/d2a04b-source-closure/stage-a-core03-core06-semantic-closure-20260730-v2/GATE_IMPACT.json"
    )
    semantic_gates = {
        gate: details.get("status") for gate, details in semantic.get("gates", {}).items()
    }
    if semantic_gates != {
        "CORE03-Q2": "UNKNOWN",
        "CORE03-Q3": "UNKNOWN",
        "CORE06-Q3": "FAIL",
        "CORE06-Q4": "FAIL",
    }:
        raise RecomputeError("CORE-03/06 semantic gate status changed")

    rights = load_json(
        WORKSPACE
        / "reports/data-governance/csmar-user-attested-rights-receipt-20260730-v1/RIGHTS_RECEIPT.json"
    )
    rights_scope = rights.get("rights_scope", {})
    return {
        "identity_gates": identity_gates,
        "identity_history_search": {
            "official_artifacts_downloaded": identity_search.get(
                "official_artifacts_downloaded"
            ),
            "bse_official_old_new_code_mapping_rows": identity_search.get(
                "bse_official_old_new_code_mapping_rows"
            ),
            "permanent_identity_found": identity_search.get(
                "permanent_identity_found"
            ),
            "complete_history_found": identity_search.get(
                "complete_current_delisted_relisted_code_name_board_history_found"
            ),
        },
        "calendar_gates": calendar_gates,
        "calendar_minimum_gap_search": {
            "decision": calendar_search.get("decision"),
            "formal_admission": calendar_search.get("formal_admission"),
        },
        "action_pit": {
            "source_rows": action.get("source_rows"),
            "candidate_rows": action.get("candidate_rows"),
            "core_04_assessment": action.get("core_04_assessment"),
            "core_05_assessment": action.get("core_05_assessment"),
        },
        "semantic_gates": semantic_gates,
        "rights_boundary": {
            "user_attested_scope_present": bool(rights_scope),
            "strict_interpretation": "LOCAL_RETENTION_AND_CAMPUS_NONCOMMERCIAL_INTERNAL_RESEARCH_ONLY__NO_API_AUTOMATION_OR_REDISTRIBUTION_INFERENCE",
        },
    }


def stage_a_decision(source: dict[str, Any]) -> dict[str, Any]:
    identity_gates = source["identity_gates"]
    calendar_gates = source["calendar_gates"]
    semantic_gates = source["semantic_gates"]
    targets = [
        {
            "core_id": "CORE-01",
            "dataset_id": "cn-a-share-security-master-pit",
            "ready": False,
            "status": "BLOCKED__PRE_ADMISSION_EVIDENCE_NOT_READY",
            "quality_gates": identity_gates,
            "remaining": [
                "permanent cross-code security identity and listing-episode mapping",
                "complete SSE/SZSE/BSE current, delisted, relisted, code/name/board history",
                "first-publication, available_at, correction and supersession chain",
            ],
        },
        {
            "core_id": "CORE-02",
            "dataset_id": "cn-trading-calendar",
            "ready": False,
            "status": "BLOCKED__PRE_ADMISSION_EVIDENCE_NOT_READY",
            "quality_gates": calendar_gates,
            "remaining": [
                "SSE 2005-2008 annual master closure notices and BSE 2021 partial year",
                "complete three-venue temporary/emergency closure and revision archive",
                "complete historical session-version and official daily-calendar chain",
            ],
        },
        {
            "core_id": "CORE-03",
            "dataset_id": "cn-a-share-daily-raw",
            "ready": False,
            "status": "BLOCKED__PRE_ADMISSION_EVIDENCE_NOT_READY",
            "quality_gates": {
                "CORE03-Q1": "FAIL",
                "CORE03-Q2": semantic_gates["CORE03-Q2"],
                "CORE03-Q3": semantic_gates["CORE03-Q3"],
                "CORE03-Q4": "FAIL",
                "CORE03-Q5": "FAIL",
            },
            "remaining": [
                "table-specific versioned TRD_Dalyr NULL/zero/suspension semantics",
                "official security-day skeleton distinguishing non-listed, suspended, no-trade and missing-source",
                "exchange_close_at, conservative available_at and immutable revision chain",
            ],
        },
        {
            "core_id": "CORE-06",
            "dataset_id": "cn-a-share-tradability-daily-pit",
            "ready": False,
            "status": "BLOCKED__PRE_ADMISSION_EVIDENCE_NOT_READY",
            "quality_gates": {
                "CORE06-Q1": "FAIL",
                "CORE06-Q2": "FAIL",
                "CORE06-Q3": semantic_gates["CORE06-Q3"],
                "CORE06-Q4": semantic_gates["CORE06-Q4"],
            },
            "remaining": [
                "complete effective-dated ST/suspension/delisting/relisting state history",
                "publication-time and conservative opening-availability chain",
                "effective board/risk/tick/rounding/limit/IPO/relisting rule versions",
            ],
        },
    ]
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "work_package": "L1-D2A-04B_OFFICIAL_IDENTITY_CALENDAR_ACTION_STATUS_SOURCE_CLOSURE",
        "stage_a": {
            "scope": [row["core_id"] for row in targets],
            "target_count": 4,
            "ready_count": 0,
            "all_ready": False,
            "status": "BLOCKED__0_OF_4_PRE_ADMISSION_READY",
            "targets": targets,
        },
        "adjacent_core_state": source["action_pit"],
        "final_bounded_official_searches": {
            "identity_history": source["identity_history_search"],
            "calendar_session": source["calendar_minimum_gap_search"],
        },
        "rights_boundary": source["rights_boundary"],
        "stage_b": "BLOCKED__PHASE_A_NOT_READY",
        "formal_source_admissions": 0,
        "formal_snapshots_published": 0,
        "research_usable": False,
        "formal_effect": "NONE__REPORT_ONLY_FAIL_CLOSED",
        "decision": "DO_NOT_PUBLISH__MISSING_EXTERNAL_HISTORY_AND_VERSIONED_SEMANTICS",
    }


def minimum_remaining(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "items": [
            {
                "priority": index + 1,
                "core_id": row["core_id"],
                "requirements": row["remaining"],
                "local_automation_after_receipt": True,
            }
            for index, row in enumerate(decision["stage_a"]["targets"])
        ],
        "non_stage_a_but_r0_required": [
            "CORE-04 official company-action economic terms, publication time and revision/supersession chain",
            "CORE-05 deterministic adjustment projection derived from a formally admitted CORE-04",
        ],
        "not_a_blocker": "independent backup is not required by user decision",
    }


def report_manifest(root: Path) -> dict[str, Any]:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "REPORT_MANIFEST.json":
            relative = (
                f"reports/data-governance/{RUN_ID}/"
                + str(path.relative_to(root)).replace("\\", "/")
            )
            rows.append(
                {"path": relative, "bytes": path.stat().st_size, "sha256": sha256_file(path)}
            )
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "manifest_kind": "CURRENT_R0_STAGE_A_RECOMPUTE_REPORT",
        "files": rows,
        "formal_effect": "NONE",
    }


def validate_formal_tree_guard(guard: dict[str, Any]) -> None:
    expected = {
        "data/market/snapshots": ["data/market/snapshots/README.md"],
        "data/published": ["data/published/README.md"],
    }
    if guard.get("formal_trees") != expected:
        raise RecomputeError("formal snapshot or published tree changed")


def verify_existing() -> dict[str, Any]:
    if not REPORT.is_dir():
        raise RecomputeError(f"report does not exist: {REPORT}")
    manifest = verify_manifest(str((REPORT / "REPORT_MANIFEST.json").relative_to(WORKSPACE)))
    guard = load_json(REPORT / "CURRENT_AUTHORITY_GUARD.json")
    current = authority_guard()
    if guard != current:
        raise RecomputeError("current authority guard drift")
    validate_formal_tree_guard(current)
    upstream = load_json(REPORT / "UPSTREAM_BINDINGS.json")
    for name, relative in UPSTREAM_MANIFESTS.items():
        verified = verify_manifest(relative)
        if upstream["manifests"].get(name) != verified:
            raise RecomputeError(f"upstream binding drift: {name}")
    source = validate_source_decisions()
    decision = stage_a_decision(source)
    if load_json(REPORT / "CURRENT_STAGE_A_DECISION.json") != decision:
        raise RecomputeError("Stage-A decision drift")
    return {
        "run_id": RUN_ID,
        "status": "PASS__FAIL_CLOSED_CURRENT_RECOMPUTE_VERIFIED",
        "report_manifest": manifest,
        "stage_a_ready": "0/4",
        "formal_effect": "NONE",
    }


def build() -> dict[str, Any]:
    if REPORT.exists():
        raise RecomputeError(f"immutable report already exists: {REPORT}")
    source = validate_source_decisions()
    decision = stage_a_decision(source)
    guard_before = authority_guard()
    validate_formal_tree_guard(guard_before)
    upstream = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "manifests": {
            name: verify_manifest(relative) for name, relative in UPSTREAM_MANIFESTS.items()
        },
        "tools": {
            "generator": binding("control/tools/recompute_r0_stage_a_current.py"),
            "test": binding("control/tests/test_recompute_r0_stage_a_current.py"),
        },
    }

    parent = REPORT.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{RUN_ID}-staging-", dir=parent))
    try:
        write_json(staging / "CURRENT_STAGE_A_DECISION.json", decision)
        write_json(staging / "CURRENT_AUTHORITY_GUARD.json", guard_before)
        write_json(staging / "UPSTREAM_BINDINGS.json", upstream)
        write_json(staging / "MINIMUM_REMAINING_EVIDENCE.json", minimum_remaining(decision))
        verification = {
            "schema_version": 1,
            "run_id": RUN_ID,
            "status": "PASS__CURRENT_EVIDENCE_RECOMPUTED__STAGE_A_FAIL_CLOSED",
            "upstream_manifest_count": len(upstream["manifests"]),
            "all_upstream_entries_hash_verified": True,
            "authority_guard_unchanged_during_build": authority_guard() == guard_before,
            "stage_a_ready": "0/4",
            "formal_source_admissions": 0,
            "formal_snapshots_published": 0,
            "formal_effect": "NONE",
            "research_usable": False,
        }
        if not verification["authority_guard_unchanged_during_build"]:
            raise RecomputeError("authority changed during build")
        write_json(staging / "VERIFICATION.json", verification)
        (staging / "README.md").write_text(
            "# R0 current Stage-A recompute v3 (2026-08-01)\n\n"
            "This immutable report binds the latest identity, official calendar/rules, "
            "final bounded official searches, action/PIT and historical semantic evidence "
            "without mutating any formal "
            "catalog or snapshot.  Hash and package verification passes, but the strict "
            "non-circular Stage A remains **0/4** because complete external history and "
            "versioned source semantics are still absent.  The result is therefore "
            "fail-closed and not research-usable.\n",
            encoding="utf-8",
        )
        write_json(staging / "REPORT_MANIFEST.json", report_manifest(staging))
        os.replace(staging, REPORT)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return verify_existing()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    result = verify_existing() if args.verify_only else build()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
