#!/usr/bin/env python3
"""Build a fail-closed control queue for selected-CORE Stage-A closure.

The tool is report-only.  It never writes the formal catalog, publishes a
snapshot, promotes quality, creates an authorization receipt, or consumes a
candidate as research data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUN_ID = "stage-a-closure-control-20260730-v2"
TARGETS = ("CORE-01", "CORE-02", "CORE-03", "CORE-06")
ALLOWED_CATEGORIES = {
    "AUTOMATABLE_OFFICIAL_SOURCE",
    "AUTOMATABLE_LOCAL_NORMALIZATION",
    "REQUIRES_RIGHTS_CONFIRMATION",
    "REQUIRES_PORTAL_MANUAL_DATA",
    "REQUIRES_STAGE_B_PUBLISH",
}

DEFAULT_MEASUREMENTS = (
    "reports/data-governance/"
    "core01-02-03-06-local-evidence-closure-20260730-v1/"
    "MEASUREMENTS.json"
)
DEFAULT_STAGE_MACHINE = (
    "reports/data-governance/"
    "selected-core-two-stage-admission-architecture-audit-20260730-v1/"
    "STATE_MACHINE.json"
)
DEFAULT_SUPPLEMENTAL = (
    "reports/data-governance/"
    "csmar-intake-supplemental-evidence-20260730-v1/"
    "EVIDENCE_MAP.json"
)
DEFAULT_RIGHTS_PACKAGE = (
    "reports/data-governance/"
    "csmar-user-attested-rights-receipt-20260730-v1"
)
DEFAULT_CORE02_PACKAGE = (
    "reports/data-governance/"
    "stage-a-core02-calendar-session-closure-20260730-v1"
)
DEFAULT_RULES_PACKAGE = (
    "reports/data-governance/"
    "stage-a-core03-core06-official-rules-closure-20260730-v1"
)
DEFAULT_OUTPUT = f"reports/data-governance/{RUN_ID}"

EXPECTED_EVIDENCE_GATE_STATUSES = {
    "CORE02-Q1": "FAIL",
    "CORE02-Q2": "FAIL",
    "CORE03-Q2": "UNKNOWN",
    "CORE03-Q3": "UNKNOWN",
    "CORE06-Q3": "FAIL",
    "CORE06-Q4": "FAIL",
}
EXPECTED_RIGHTS_ALLOWED = {
    "LOCAL_LONG_TERM_RETENTION_ON_THIS_MACHINE",
    "CAMPUS_INTERNAL_NONCOMMERCIAL_ACADEMIC_RESEARCH",
    "INTERNAL_DERIVED_FACTORS_AND_STATISTICS",
}
EXPECTED_RIGHTS_NOT_COVERED = {
    "API_OR_AUTOMATED_BULK_COLLECTION",
    "POST_AFFILIATION_DISPOSITION",
    "OFFICIAL_SOURCE_RIGHTS",
    "INDEPENDENT_CONTRACT_VERIFICATION",
}
EXPECTED_RIGHTS_PROHIBITED = {
    "RAW_EXTERNAL_REDISTRIBUTION",
    "COMMERCIAL_USE",
}


def item(
    category: str,
    track: str,
    acceptance: str,
    *,
    prerequisites: tuple[str, ...] = (),
    portal_or_user_needed: bool = False,
) -> dict[str, Any]:
    return {
        "category": category,
        "track": track,
        "acceptance": acceptance,
        "prerequisites": list(prerequisites),
        "portal_or_user_needed": portal_or_user_needed,
    }


# Every current non-PASS clause must appear exactly once here.  The exhaustive
# coverage assertion is tested against the hash-bound MEASUREMENTS input.
CHECK_ACTIONS: dict[str, dict[str, Any]] = {
    "local_code_is_not_permanent_identity": item(
        "AUTOMATABLE_LOCAL_NORMALIZATION",
        "CORE01_IDENTITY_NORMALIZATION",
        "Generate a permanent security identity independent of local code, "
        "retain every listing episode, and prove the known reuse counterexample "
        "does not collapse two identities.",
        prerequisites=("CORE01_OFFICIAL_IDENTITY_INTERVALS",),
    ),
    "permanent_identity_contract_fields": item(
        "AUTOMATABLE_LOCAL_NORMALIZATION",
        "CORE01_IDENTITY_NORMALIZATION",
        "Materialize source_id, issuer_id, permanent security_id, valid_from, "
        "valid_to, published_at, available_at, revision_id and provenance with "
        "no inferred unknowns.",
        prerequisites=("CORE01_OFFICIAL_IDENTITY_INTERVALS",),
    ),
    "uncertain_mapping_quarantine": item(
        "AUTOMATABLE_LOCAL_NORMALIZATION",
        "CORE01_IDENTITY_NORMALIZATION",
        "Emit a complete mapping-decision ledger; every unresolved or "
        "many-to-many relation must be quarantined and excluded from admission.",
        prerequisites=("CORE01_OFFICIAL_IDENTITY_INTERVALS",),
    ),
    "code_and_listing_status_intervals_exist": item(
        "AUTOMATABLE_OFFICIAL_SOURCE",
        "CORE01_OFFICIAL_IDENTITY_INTERVALS",
        "Acquire hash-bound official current and historical code, listing, "
        "delisting and relisting facts for SSE, SZSE and BSE with dated source "
        "receipts and conservative availability.",
    ),
    "interval_overlap_and_gap_audit": item(
        "AUTOMATABLE_LOCAL_NORMALIZATION",
        "CORE01_IDENTITY_NORMALIZATION",
        "For each permanent security_id, prove normalized code and listing-state "
        "intervals have no unexplained overlap or gap; retain an exception ledger.",
        prerequisites=("CORE01_OFFICIAL_IDENTITY_INTERVALS",),
    ),
    "current_and_delisted_a_share_completeness": item(
        "AUTOMATABLE_OFFICIAL_SOURCE",
        "CORE01_OFFICIAL_IDENTITY_INTERVALS",
        "Reconcile candidate identities against dated official current and "
        "delisted A-share universes for all three exchanges and retain set diffs.",
    ),
    "year_venue_board_coverage_matrix": item(
        "AUTOMATABLE_LOCAL_NORMALIZATION",
        "CORE01_IDENTITY_NORMALIZATION",
        "Build and verify the complete year x venue x board coverage matrix from "
        "official identity intervals, including empty cells and explanations.",
        prerequisites=("CORE01_OFFICIAL_IDENTITY_INTERVALS",),
    ),
    "history_publication_and_availability": item(
        "AUTOMATABLE_OFFICIAL_SOURCE",
        "CORE01_OFFICIAL_IDENTITY_INTERVALS",
        "Bind each identity/name/code/status event to first-publication evidence, "
        "conservative available_at, correction/withdrawal state and revision chain.",
    ),
    "cross_official_conflict_adjudication": item(
        "AUTOMATABLE_LOCAL_NORMALIZATION",
        "CORE01_IDENTITY_NORMALIZATION",
        "Create a multi-official conflict register with source precedence, "
        "non-destructive retention and an adjudication receipt for every conflict.",
        prerequisites=("CORE01_OFFICIAL_IDENTITY_INTERVALS",),
    ),
    "market64_calendar_consistency": item(
        "AUTOMATABLE_LOCAL_NORMALIZATION",
        "CORE02_CALENDAR_NORMALIZATION",
        "Resolve Markettype 64 to an official venue/session scope or quarantine "
        "the unmatched rows; prove daily rows never predate the applicable calendar.",
        prerequisites=("CORE02_OFFICIAL_CALENDAR_SESSIONS",),
    ),
    "official_exchange_and_session_rules": item(
        "AUTOMATABLE_OFFICIAL_SOURCE",
        "CORE02_OFFICIAL_CALENDAR_SESSIONS",
        "Capture dated official SSE, SZSE and BSE calendar/session rules, including "
        "MIC mapping, auctions, continuous trading and effective intervals.",
    ),
    "official_notice_revision_chain": item(
        "AUTOMATABLE_OFFICIAL_SOURCE",
        "CORE02_OFFICIAL_CALENDAR_SESSIONS",
        "Capture temporary closures, supplemental notices and corrections with "
        "published_at, available_at, revision_id, supersedes and withdrawal state.",
    ),
    "formal_lineage_key_fields": item(
        "AUTOMATABLE_LOCAL_NORMALIZATION",
        "CORE03_DAILY_NORMALIZATION",
        "Add deterministic source_id, permanent security_id and revision_id to "
        "every daily row, with one-to-one raw lineage and no code-only identity.",
        prerequisites=(
            "CORE01_IDENTITY_NORMALIZATION",
            "CORE02_CALENDAR_NORMALIZATION",
        ),
    ),
    "official_calendar_membership": item(
        "AUTOMATABLE_LOCAL_NORMALIZATION",
        "CORE03_DAILY_NORMALIZATION",
        "Join each daily row to the exact official venue calendar and emit "
        "zero unexplained non-session dates plus a retained exception ledger.",
        prerequisites=("CORE02_CALENDAR_NORMALIZATION",),
    ),
    "suspension_no_trade_null_zero_semantics": item(
        "REQUIRES_PORTAL_MANUAL_DATA",
        "CSMAR_TABLE_SEMANTICS",
        "Obtain table-specific, versioned proof for every price/volume/turnover "
        "NULL and zero encoding under suspension and no-trade states; do not "
        "transfer TRDNEW semantics to TRD_Dalyr.",
        portal_or_user_needed=True,
    ),
    "unknown_not_encoded_as_zero": item(
        "REQUIRES_PORTAL_MANUAL_DATA",
        "CSMAR_TABLE_SEMANTICS",
        "Obtain versioned source semantics or a complete source-side null audit "
        "proving historical unknown values are not silently represented as zero.",
        portal_or_user_needed=True,
    ),
    "expected_universe_state_distinctions": item(
        "AUTOMATABLE_LOCAL_NORMALIZATION",
        "CORE03_DAILY_NORMALIZATION",
        "Build the official security-day skeleton and assign exactly one of "
        "not-listed, suspended, no-trade, observed-trade or missing-source.",
        prerequisites=(
            "CORE01_IDENTITY_NORMALIZATION",
            "CORE02_CALENDAR_NORMALIZATION",
        ),
    ),
    "close_availability_revision_chain": item(
        "AUTOMATABLE_LOCAL_NORMALIZATION",
        "CORE03_DAILY_NORMALIZATION",
        "Derive exchange_close_at from official session versions; set conservative "
        "available_at and immutable revision_id without backdating corrections.",
        prerequisites=("CORE02_OFFICIAL_CALENDAR_SESSIONS",),
    ),
    "independent_cross_source_tolerance": item(
        "AUTOMATABLE_OFFICIAL_SOURCE",
        "CORE03_OFFICIAL_CROSS_SOURCE",
        "Compare a deterministic stratified sample of prices and dates with an "
        "independent official or admitted second source; retain tolerances, diffs "
        "and conflicts without overwriting either source.",
    ),
    "complete_listed_security_day_explanation": item(
        "AUTOMATABLE_LOCAL_NORMALIZATION",
        "CORE06_TRADABILITY_NORMALIZATION",
        "For every listed security and exchange day, emit a complete state "
        "explanation distinguishing not-listed, suspended, no-trade and missing.",
        prerequisites=(
            "CORE01_IDENTITY_NORMALIZATION",
            "CORE02_CALENDAR_NORMALIZATION",
            "CORE03_DAILY_NORMALIZATION",
        ),
    ),
    "announcement_not_after_opening_use": item(
        "AUTOMATABLE_LOCAL_NORMALIZATION",
        "CORE06_TRADABILITY_NORMALIZATION",
        "Apply a conservative availability rule to all status events; prove no "
        "opening state uses information first published after that opening.",
        prerequisites=("CORE01_OFFICIAL_IDENTITY_INTERVALS",),
    ),
    "status_interval_overlap": item(
        "AUTOMATABLE_LOCAL_NORMALIZATION",
        "CORE06_TRADABILITY_NORMALIZATION",
        "Normalize versioned status intervals and prove no unexplained overlap; "
        "quarantine ambiguous event sequences.",
        prerequisites=("CORE01_OFFICIAL_IDENTITY_INTERVALS",),
    ),
    "historical_limit_rule_recomputation_inputs": item(
        "AUTOMATABLE_OFFICIAL_SOURCE",
        "CORE03_CORE06_OFFICIAL_RULES",
        "Freeze official effective-dated board, risk-state, tick-size, rounding "
        "and price-limit rules sufficient to recompute historical limits.",
    ),
    "historical_special_rule_versions": item(
        "AUTOMATABLE_OFFICIAL_SOURCE",
        "CORE03_CORE06_OFFICIAL_RULES",
        "Freeze effective-dated IPO, relisting, ST, delisting, special and "
        "no-limit rules with first-publication and revision evidence.",
    ),
    "formal_orderable_traded_tradeable_model": item(
        "REQUIRES_STAGE_B_PUBLISH",
        "STAGE_B_FORMAL_PUBLISH",
        "After Stage A passes and explicit hash-bound authorization is supplied, "
        "publish the versioned orderable/traded/strategy-tradeable projection "
        "and verify its exact catalog and qdata research identity.",
    ),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
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


def input_record(path: Path, workspace: Path) -> dict[str, Any]:
    return {
        "path": workspace_relative(path, workspace),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def resolve_input(workspace: Path, value: Path) -> Path:
    path = value if value.is_absolute() else workspace / value
    resolved = path.resolve(strict=True)
    resolved.relative_to(workspace.resolve())
    return resolved


def report_manifest_records(root: Any) -> list[dict[str, Any]]:
    if isinstance(root, list):
        records = root
    elif isinstance(root, dict) and isinstance(root.get("files"), list):
        records = root["files"]
    else:
        raise ValueError("report manifest must be a list or contain a files list")
    if not records:
        raise ValueError("report manifest must not be empty")
    normalized = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise TypeError(f"manifest record {index} is not an object")
        if not {"path", "bytes", "sha256"}.issubset(record):
            raise ValueError(f"manifest record {index} is not hash-bound")
        path = record["path"]
        size = record["bytes"]
        digest = record["sha256"]
        if not isinstance(path, str) or not path:
            raise ValueError(f"manifest record {index} has invalid path")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"manifest record {index} has invalid bytes")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in digest)
        ):
            raise ValueError(f"manifest record {index} has invalid sha256")
        normalized.append(
            {
                "path": path,
                "bytes": size,
                "sha256": digest.lower(),
            }
        )
    return normalized


def load_verified_report_package(
    *,
    workspace: Path,
    package_value: Path,
    package_id: str,
    required_artifact: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    package_root = resolve_input(workspace, package_value)
    reports_root = (workspace / "reports" / "data-governance").resolve()
    package_root.relative_to(reports_root)
    if not package_root.is_dir():
        raise NotADirectoryError(package_root)
    manifest_path = package_root / "REPORT_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = report_manifest_records(manifest)
    verified = []
    artifact_paths: dict[str, Path] = {}
    for record in records:
        raw = Path(record["path"])
        if raw.is_absolute():
            artifact = raw.resolve(strict=True)
        else:
            workspace_candidate = (workspace / raw).resolve()
            package_candidate = (package_root / raw).resolve()
            candidates = [
                candidate
                for candidate in (workspace_candidate, package_candidate)
                if candidate.is_file()
            ]
            unique = list(dict.fromkeys(candidates))
            if len(unique) != 1:
                raise ValueError(
                    f"manifest path must resolve to exactly one file: {record['path']}"
                )
            artifact = unique[0]
        artifact.relative_to(workspace.resolve())
        if artifact.stat().st_size != record["bytes"]:
            raise ValueError(f"manifest byte mismatch: {record['path']}")
        if sha256_file(artifact) != record["sha256"]:
            raise ValueError(f"manifest sha256 mismatch: {record['path']}")
        binding = input_record(artifact, workspace)
        verified.append(binding)
        if artifact.name in artifact_paths:
            raise ValueError(f"duplicate manifest artifact name: {artifact.name}")
        artifact_paths[artifact.name] = artifact
    evidence_path = artifact_paths.get(required_artifact)
    if evidence_path is None:
        raise ValueError(
            f"{package_id} manifest does not bind required artifact "
            f"{required_artifact}"
        )
    package = {
        "package_id": package_id,
        "package_root": workspace_relative(package_root, workspace),
        "report_manifest": input_record(manifest_path, workspace),
        "artifact_bindings": verified,
        "required_artifact": required_artifact,
        "manifest_and_artifacts_verified": True,
    }
    return package, load_json(evidence_path)


def build_evidence_projection(
    *,
    rights_receipt: dict[str, Any],
    core02_receipt: dict[str, Any],
    rules_receipt: dict[str, Any],
) -> dict[str, Any]:
    allowed = {
        row.get("code")
        for row in rights_receipt.get("rights_scope", {}).get("allowed", [])
    }
    not_covered = {
        row.get("code")
        for row in rights_receipt.get("rights_scope", {}).get(
            "not_covered", []
        )
    }
    prohibited = {
        row.get("code")
        for row in rights_receipt.get("rights_scope", {}).get(
            "prohibited", []
        )
    }
    if allowed != EXPECTED_RIGHTS_ALLOWED:
        raise ValueError(f"unexpected attested rights allowed set: {sorted(allowed)}")
    if not_covered != EXPECTED_RIGHTS_NOT_COVERED:
        raise ValueError(
            f"unexpected attested rights not-covered set: {sorted(not_covered)}"
        )
    if prohibited != EXPECTED_RIGHTS_PROHIBITED:
        raise ValueError(
            f"unexpected attested rights prohibited set: {sorted(prohibited)}"
        )
    attestation = rights_receipt.get("attestation", {})
    effect = rights_receipt.get("admission_effect", {})
    if (
        attestation.get("basis") != "DIRECT_USER_ATTESTATION"
        or attestation.get("institution_contract_independently_verified") is not False
        or attestation.get("provider_contract_independently_verified") is not False
        or effect.get("rights_boundary_recorded_for_exact_assets") is not True
        or effect.get("admission_decision_preserved") != "NOT_ADMITTED"
        or effect.get("formal_snapshot_published") is not False
        or effect.get("quality_status_changed") is not False
        or effect.get("research_authorized") is not False
    ):
        raise ValueError("rights receipt does not preserve the fail-closed contract")

    observed_statuses = {}
    for receipt in (core02_receipt, rules_receipt):
        for gate_id, gate in receipt.get("gates", {}).items():
            if (
                gate_id in EXPECTED_EVIDENCE_GATE_STATUSES
                and isinstance(gate, dict)
            ):
                observed_statuses[gate_id] = gate.get("status")
    if observed_statuses != EXPECTED_EVIDENCE_GATE_STATUSES:
        raise ValueError(
            "unexpected official evidence gate statuses: "
            f"{dict(sorted(observed_statuses.items()))}"
        )
    return {
        "rights": {
            "global_gate_status": "FAIL",
            "csmar_exact_asset_subscope_status": (
                "USER_ATTESTED__EXACT_ASSETS__LOCAL_RETENTION__"
                "CAMPUS_NONCOMMERCIAL_RESEARCH__INTERNAL_DERIVATION"
            ),
            "attestation_basis": "DIRECT_USER_ATTESTATION",
            "independent_contract_verification": False,
            "allowed": sorted(allowed),
            "not_covered": sorted(not_covered),
            "prohibited": sorted(prohibited),
        },
        "official_evidence_gate_statuses": dict(sorted(observed_statuses.items())),
    }


def execution_state(category: str, track: str) -> str:
    if category == "REQUIRES_RIGHTS_CONFIRMATION":
        return "BLOCKED__RIGHTS_RECEIPT_REQUIRED"
    if category == "REQUIRES_PORTAL_MANUAL_DATA":
        return "BLOCKED__VERSIONED_TABLE_SEMANTICS_REQUIRED"
    if category == "REQUIRES_STAGE_B_PUBLISH":
        return "DOWNSTREAM__NOT_STAGE_A_EXECUTION"
    if track in {
        "CORE01_OFFICIAL_IDENTITY_INTERVALS",
        "CORE02_OFFICIAL_CALENDAR_SESSIONS",
        "CORE03_CORE06_OFFICIAL_RULES",
    }:
        return "ACTIVE__PARALLEL_OFFICIAL_EVIDENCE_TRACK"
    return "QUEUED__AUTOMATABLE_AFTER_PREREQUISITES"


def extract_nonpass(measurements: dict[str, Any]) -> list[dict[str, Any]]:
    targets = measurements["gate_evaluations"]["targets"]
    if sorted(targets) != sorted(TARGETS):
        raise ValueError(f"unexpected target set: {sorted(targets)}")
    rows: list[dict[str, Any]] = []
    for core_id in TARGETS:
        for gate in targets[core_id]:
            for check in gate["subchecks"]:
                if check["status"] == "PASS":
                    continue
                check_id = check["check_id"]
                action = CHECK_ACTIONS.get(check_id)
                if action is None:
                    raise ValueError(f"unmapped non-PASS check: {core_id}/{check_id}")
                queue_id = f"{core_id}-{gate['gate_id']}-{check_id}".upper()
                rows.append(
                    {
                        "queue_id": queue_id,
                        "core_id": core_id,
                        "gate_id": gate["gate_id"],
                        "severity": gate["severity"],
                        "blocks_stage_a": gate["severity"] == "BLOCK",
                        "check_id": check_id,
                        "current_status": check["status"],
                        "current_assertion": check["assertion"],
                        "measurement_paths": check.get("measurement_paths", []),
                        **action,
                        "execution_state": execution_state(
                            action["category"], action["track"]
                        ),
                    }
                )
    observed = {row["check_id"] for row in rows}
    unused = sorted(set(CHECK_ACTIONS) - observed)
    if unused:
        raise ValueError(f"stale action mappings: {unused}")
    return rows


def extract_supplemental_claims(
    supplemental: dict[str, Any],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    result: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for claim in supplemental.get("stage_a_claims", []):
        cores = claim.get("target_cores") or [claim.get("target_core")]
        checks = claim.get("target_checks") or [claim.get("target_check")]
        for core_id in cores:
            for gate_id in checks:
                if not core_id or not gate_id:
                    continue
                result.setdefault((core_id, gate_id), []).append(
                    {
                        "claim_id": claim["claim_id"],
                        "effect": claim["effect"],
                        "gate_status_change": claim["gate_status_change"],
                        "formal_admission": claim["formal_admission"],
                    }
                )
    return result


def build_queue(
    *,
    measurements: dict[str, Any],
    stage_machine: dict[str, Any],
    supplemental: dict[str, Any],
    inputs: dict[str, Any],
    evidence_packages: list[dict[str, Any]],
    evidence_projection: dict[str, Any],
) -> dict[str, Any]:
    rows = extract_nonpass(measurements)
    claims = extract_supplemental_claims(supplemental)
    official_statuses = evidence_projection[
        "official_evidence_gate_statuses"
    ]
    evidence_package_by_gate = {
        "CORE02-Q1": "CORE02_CALENDAR_SESSION_CLOSURE",
        "CORE02-Q2": "CORE02_CALENDAR_SESSION_CLOSURE",
        "CORE03-Q2": "CORE03_CORE06_OFFICIAL_RULES_CLOSURE",
        "CORE03-Q3": "CORE03_CORE06_OFFICIAL_RULES_CLOSURE",
        "CORE06-Q3": "CORE03_CORE06_OFFICIAL_RULES_CLOSURE",
        "CORE06-Q4": "CORE03_CORE06_OFFICIAL_RULES_CLOSURE",
    }
    for row in rows:
        row["supplemental_claims"] = claims.get(
            (row["core_id"], row["gate_id"]), []
        )
        if row["gate_id"] in official_statuses:
            evidence_status = official_statuses[row["gate_id"]]
            if evidence_status != row["current_status"]:
                raise ValueError(
                    "official package status disagrees with the formal baseline: "
                    f"{row['gate_id']} package={evidence_status} "
                    f"baseline={row['current_status']}"
                )
            row["hash_bound_evidence"] = {
                "package_id": evidence_package_by_gate[row["gate_id"]],
                "declared_gate_status": evidence_status,
                "queue_status_change": "NONE",
                "formal_stage_a_effect": "NONE__FAIL_CLOSED",
            }

    rights = evidence_projection["rights"]
    rights_item = {
        "queue_id": "GLOBAL-RIGHTS-PACKAGE-SCOPE",
        "core_id": "GLOBAL",
        "gate_id": "STAGE-A-RIGHTS",
        "severity": "BLOCK",
        "blocks_stage_a": True,
        "check_id": "package_specific_rights_receipt",
        "current_status": "FAIL",
        "current_assertion": (
            "The exact CSMAR assets have a direct-user-attested boundary for "
            "local retention, campus-internal noncommercial research and "
            "internal derivatives.  API/automated bulk collection, "
            "post-affiliation disposition, official-source rights, raw external "
            "redistribution and commercial use are not closed; therefore the "
            "GLOBAL rights gate remains FAIL."
        ),
        "measurement_paths": [],
        **item(
            "REQUIRES_RIGHTS_CONFIRMATION",
            "SOURCE_RIGHTS",
            "Bind each source/package to a dated authoritative rights record "
            "covering retention, internal research use, automation, derived "
            "artifacts and redistribution; unknown permissions remain blocked.",
            portal_or_user_needed=True,
        ),
        "execution_state": (
            "BLOCKED__GLOBAL_RIGHTS_INCOMPLETE__"
            "CSMAR_LOCAL_SUBSCOPE_USER_ATTESTED"
        ),
        "supplemental_claims": [],
        "hash_bound_evidence": {
            "package_id": "CSMAR_USER_ATTESTED_RIGHTS",
            "global_gate_status": rights["global_gate_status"],
            "csmar_exact_asset_subscope_status": rights[
                "csmar_exact_asset_subscope_status"
            ],
            "attestation_basis": rights["attestation_basis"],
            "independent_contract_verification": rights[
                "independent_contract_verification"
            ],
            "allowed": rights["allowed"],
            "not_covered": rights["not_covered"],
            "prohibited": rights["prohibited"],
            "queue_status_change": "NONE",
            "formal_stage_a_effect": "NONE__FAIL_CLOSED",
        },
    }
    rows.append(rights_item)

    stage_a = stage_machine["phase_a"]
    if stage_a["ready_count"] != 0 or stage_a["target_count"] != 4:
        raise ValueError("baseline Stage A is no longer 0/4; refresh mappings")
    if stage_machine["phase_b"]["status"] != "BLOCKED__PHASE_A_NOT_READY":
        raise ValueError("unexpected Phase-B baseline")

    category_counts = Counter(row["category"] for row in rows)
    state_counts = Counter(row["execution_state"] for row in rows)
    core_counts = Counter(row["core_id"] for row in rows)
    gate_nonpass_count = len(rows) - 1
    blocking_clause_count = sum(
        row["blocks_stage_a"] for row in rows if row["core_id"] != "GLOBAL"
    )

    cross_cutting = {}
    for target in stage_a["targets"]:
        cross_cutting[target["core_id"]] = {
            name: value["status"]
            for name, value in target["stage_a"]["checks"].items()
        }

    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "ACTIVE__FAIL_CLOSED__NO_FORMAL_ADMISSION",
        "inputs": inputs,
        "hash_bound_evidence_packages": evidence_packages,
        "evidence_projection": evidence_projection,
        "formal_state": {
            "stage_a_ready": "0/4",
            "stage_b": "BLOCKED__PHASE_A_NOT_READY",
            "formal_catalog_mutated": False,
            "snapshot_published": False,
            "quality_promoted": False,
            "research_use_authorized": False,
        },
        "summary": {
            "nonpass_clause_count": gate_nonpass_count,
            "blocking_nonpass_clause_count": blocking_clause_count,
            "cross_cutting_rights_items": 1,
            "hash_bound_evidence_package_count": len(evidence_packages),
            "total_queue_items": len(rows),
            "category_counts": dict(sorted(category_counts.items())),
            "execution_state_counts": dict(sorted(state_counts.items())),
            "core_counts": dict(sorted(core_counts.items())),
        },
        "active_parallel_tracks": [
            {
                "track": "CORE01_OFFICIAL_IDENTITY_INTERVALS",
                "state": "ACTIVE",
                "output_contract": (
                    "official current/delisted/relisted identity, code and "
                    "publication evidence; no catalog mutation"
                ),
            },
            {
                "track": "CORE02_OFFICIAL_CALENDAR_SESSIONS",
                "state": "ACTIVE",
                "output_contract": (
                    "official calendar, sessions, temporary closures and "
                    "revision evidence; no catalog mutation"
                ),
            },
            {
                "track": "CORE03_CORE06_OFFICIAL_RULES",
                "state": "ACTIVE",
                "output_contract": (
                    "effective-dated trading, price-limit, special and "
                    "no-limit rules; no catalog mutation"
                ),
            },
        ],
        "cross_cutting_stage_a_checks": cross_cutting,
        "queue": rows,
        "transition_policy": {
            "clause_status_change": (
                "Only a recomputed hash-bound gate may change FAIL/UNKNOWN; "
                "agent narrative or file presence is insufficient."
            ),
            "stage_a_pass": (
                "All Stage-A checks and every blocking quality gate must PASS."
            ),
            "stage_b": (
                "Requires a separate exact hash-bound user authorization after "
                "Stage A passes; this report cannot authorize or publish."
            ),
        },
    }


def build_readme(queue: dict[str, Any]) -> str:
    counts = queue["summary"]["category_counts"]
    return "\n".join(
        [
            "# Stage A 正式准入闭合控制队列",
            "",
            f"- 运行：`{queue['run_id']}`",
            "- 当前真实状态：`Stage A 0/4`，`Stage B` 未启动。",
            f"- 逐条非 PASS 子检查：`{queue['summary']['nonpass_clause_count']}`；"
            f"其中阻断条款 `{queue['summary']['blocking_nonpass_clause_count']}`。",
            f"- 加上跨来源权利项后，控制队列共 `{queue['summary']['total_queue_items']}` 项。",
            f"- 本轮精确哈希绑定证据包："
            f"`{queue['summary']['hash_bound_evidence_package_count']}` 个。",
            "",
            "## 分类",
            "",
            f"- 官方来源可自动闭合：`{counts.get('AUTOMATABLE_OFFICIAL_SOURCE', 0)}`",
            f"- 本地标准化可自动闭合：`{counts.get('AUTOMATABLE_LOCAL_NORMALIZATION', 0)}`",
            f"- 需要来源权利确认：`{counts.get('REQUIRES_RIGHTS_CONFIRMATION', 0)}`",
            f"- 需要版本化门户/表语义：`{counts.get('REQUIRES_PORTAL_MANUAL_DATA', 0)}`",
            f"- 仅可在 Stage B 发布：`{counts.get('REQUIRES_STAGE_B_PUBLISH', 0)}`",
            "",
            "## 当前并行执行",
            "",
            "1. `CORE-01` 官方身份、代码、上市/退市/重上市与发布时间证据。",
            "2. `CORE-02` 三交易所日历、交易时段、临时休市与修订证据。",
            "3. `CORE-03/06` 历史涨跌停、特殊状态、IPO/重上市、tick 与舍入规则。",
            "",
            "本地标准化任务已排在这些官方输入之后；没有依赖的工作可直接继续。"
            "CSMAR 精确资产的本机留存、校内非商业研究和内部派生子范围已按"
            "用户声明记录；GLOBAL 权利总门、表级 NULL/零值语义和官方历史"
            "完整性仍保持失败关闭，不会用推测代替。",
            "",
            "## 边界",
            "",
            "- 本报告只编排闭合工作，不写 `datasets.json`，不发布快照，不晋级质量。",
            "- 下载成功、字段存在、机械校验 PASS 或代理文字结论都不能自动关门。",
            "- 只有重新计算的哈希绑定门结果可以改变 FAIL/UNKNOWN。",
            "- Stage A 全通过后仍需单独的、绑定精确摘要的用户授权才能进入 Stage B。",
            "",
            "机器可读明细见 `CLOSURE_QUEUE.json`。",
            "",
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--measurements", type=Path, default=Path(DEFAULT_MEASUREMENTS))
    parser.add_argument("--stage-machine", type=Path, default=Path(DEFAULT_STAGE_MACHINE))
    parser.add_argument("--supplemental", type=Path, default=Path(DEFAULT_SUPPLEMENTAL))
    parser.add_argument(
        "--rights-package",
        type=Path,
        default=Path(DEFAULT_RIGHTS_PACKAGE),
    )
    parser.add_argument(
        "--core02-package",
        type=Path,
        default=Path(DEFAULT_CORE02_PACKAGE),
    )
    parser.add_argument(
        "--rules-package",
        type=Path,
        default=Path(DEFAULT_RULES_PACKAGE),
    )
    parser.add_argument("--output-dir", type=Path, default=Path(DEFAULT_OUTPUT))
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = args.workspace.resolve(strict=True)
    measurements_path = resolve_input(workspace, args.measurements)
    stage_machine_path = resolve_input(workspace, args.stage_machine)
    supplemental_path = resolve_input(workspace, args.supplemental)
    rights_package, rights_receipt = load_verified_report_package(
        workspace=workspace,
        package_value=args.rights_package,
        package_id="CSMAR_USER_ATTESTED_RIGHTS",
        required_artifact="RIGHTS_RECEIPT.json",
    )
    core02_package, core02_receipt = load_verified_report_package(
        workspace=workspace,
        package_value=args.core02_package,
        package_id="CORE02_CALENDAR_SESSION_CLOSURE",
        required_artifact="CORE02_GATE_RECEIPT.json",
    )
    rules_package, rules_receipt = load_verified_report_package(
        workspace=workspace,
        package_value=args.rules_package,
        package_id="CORE03_CORE06_OFFICIAL_RULES_CLOSURE",
        required_artifact="GATE_RECEIPT.json",
    )
    evidence_packages = [
        rights_package,
        core02_package,
        rules_package,
    ]
    evidence_projection = build_evidence_projection(
        rights_receipt=rights_receipt,
        core02_receipt=core02_receipt,
        rules_receipt=rules_receipt,
    )
    output = (
        args.output_dir
        if args.output_dir.is_absolute()
        else workspace / args.output_dir
    ).resolve()
    output.relative_to((workspace / "reports" / "data-governance").resolve())
    if output.exists() and not args.replace:
        raise FileExistsError(f"refusing to overwrite report: {output}")
    if output.exists():
        allowed = {
            "CLOSURE_QUEUE.json",
            "README.md",
            "VERIFICATION.json",
            "REPORT_MANIFEST.json",
        }
        residuals = sorted(path.name for path in output.iterdir() if path.name not in allowed)
        if residuals:
            raise ValueError(f"unregistered report residuals: {residuals}")
    output.mkdir(parents=True, exist_ok=True)

    inputs = {
        "measurements": input_record(measurements_path, workspace),
        "stage_machine": input_record(stage_machine_path, workspace),
        "supplemental_evidence": input_record(supplemental_path, workspace),
        "rights_report_manifest": rights_package["report_manifest"],
        "core02_report_manifest": core02_package["report_manifest"],
        "rules_report_manifest": rules_package["report_manifest"],
    }
    queue = build_queue(
        measurements=load_json(measurements_path),
        stage_machine=load_json(stage_machine_path),
        supplemental=load_json(supplemental_path),
        inputs=inputs,
        evidence_packages=evidence_packages,
        evidence_projection=evidence_projection,
    )
    queue_path = output / "CLOSURE_QUEUE.json"
    readme_path = output / "README.md"
    verification_path = output / "VERIFICATION.json"
    manifest_path = output / "REPORT_MANIFEST.json"
    write_json(queue_path, queue)
    readme_path.write_text(build_readme(queue), encoding="utf-8", newline="\n")

    ids = [row["queue_id"] for row in queue["queue"]]
    categories = {row["category"] for row in queue["queue"]}
    verification = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "status": "PASS",
        "checks": {
            "input_json_parse": True,
            "exact_target_set": True,
            "nonpass_mapping_exhaustive": True,
            "nonpass_clause_count_is_25": (
                queue["summary"]["nonpass_clause_count"] == 25
            ),
            "queue_ids_unique": len(ids) == len(set(ids)),
            "categories_allowed": categories <= ALLOWED_CATEGORIES,
            "stage_a_baseline_is_zero_of_four": (
                queue["formal_state"]["stage_a_ready"] == "0/4"
            ),
            "formal_catalog_mutated": False,
            "snapshot_published": False,
            "quality_promoted": False,
            "hash_bound_evidence_package_count_is_3": (
                queue["summary"]["hash_bound_evidence_package_count"] == 3
            ),
            "global_rights_gate_remains_fail": (
                queue["evidence_projection"]["rights"][
                    "global_gate_status"
                ]
                == "FAIL"
            ),
            "official_gate_statuses_fail_closed": (
                queue["evidence_projection"][
                    "official_evidence_gate_statuses"
                ]
                == EXPECTED_EVIDENCE_GATE_STATUSES
            ),
        },
        "inputs": inputs,
    }
    required_true = {
        "input_json_parse",
        "exact_target_set",
        "nonpass_mapping_exhaustive",
        "nonpass_clause_count_is_25",
        "queue_ids_unique",
        "categories_allowed",
        "stage_a_baseline_is_zero_of_four",
        "hash_bound_evidence_package_count_is_3",
        "global_rights_gate_remains_fail",
        "official_gate_statuses_fail_closed",
    }
    required_false = {
        "formal_catalog_mutated",
        "snapshot_published",
        "quality_promoted",
    }
    if not all(verification["checks"][key] is True for key in required_true):
        raise AssertionError("verification failed")
    if not all(verification["checks"][key] is False for key in required_false):
        raise AssertionError("mutation guard failed")
    write_json(verification_path, verification)

    payload = [queue_path, readme_path, verification_path]
    manifest = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "files": [
            {
                "path": path.name,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in payload
        ],
    }
    write_json(manifest_path, manifest)
    for record in manifest["files"]:
        path = output / record["path"]
        if path.stat().st_size != record["bytes"]:
            raise AssertionError(f"byte mismatch: {path}")
        if sha256_file(path) != record["sha256"]:
            raise AssertionError(f"hash mismatch: {path}")

    print(
        json.dumps(
            {
                "status": "PASS",
                "report": workspace_relative(output, workspace),
                "queue_items": len(queue["queue"]),
                "nonpass_clauses": queue["summary"]["nonpass_clause_count"],
                "stage_a_ready": queue["formal_state"]["stage_a_ready"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
