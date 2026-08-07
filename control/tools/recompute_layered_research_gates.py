#!/usr/bin/env python3
"""Fail-closed recomputation for Quantification layered research gates.

This tool is deliberately read-only.  It never discovers candidates by scanning
directories, never mutates the formal catalog, and never promotes a dataset.  A
research tier can pass only when every required plan identity is selected by an
exact ``dataset_id + snapshot_id``, resolves to a formally usable immutable
catalog entry, and is bound by a tier-specific L2 safety receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).resolve().parents[2]
CATALOG_LIB = WORKSPACE / "data" / "catalog"
if str(CATALOG_LIB) not in sys.path:
    sys.path.insert(0, str(CATALOG_LIB))

import dataset_cli  # noqa: E402


PURPOSE_RANK = dataset_cli.PURPOSE_RANK
FORMAL_LIFECYCLES = {"active", "frozen"}
FORMAL_LICENSES = {"VERIFIED", "NOT_APPLICABLE"}
FORMAL_QUALITIES = {"OK"}
SUPPORTED_HASH_ALGORITHMS = {
    "sha256-file-v1",
    "sha256-contract-tree-v1",
    "sha256-snapshot-contract-v1",
}

R0_PLAN_IDS = tuple(f"CORE-{index:02d}" for index in range(1, 7))
TIER_ORDER = ("R0_PV", "R1_CLASSIC_CN", "R2_A_SHARE_GLOBAL")
L2_REQUIRED_CHECKS = {
    "R0_PV": {
        "pit",
        "anti_lookahead",
        "survivorship",
        "adjustment",
        "tradability",
        "train_validation_blind_test",
        "reproducibility",
    },
    "R1_CLASSIC_CN": {
        "pit",
        "anti_lookahead",
        "survivorship",
        "adjustment",
        "tradability",
        "transaction_costs",
        "industry_classification_pit",
        "financial_revision_pit",
        "train_validation_blind_test",
        "reproducibility",
    },
    "R2_A_SHARE_GLOBAL": {
        "pit",
        "anti_lookahead",
        "survivorship",
        "adjustment",
        "tradability",
        "transaction_costs",
        "industry_classification_pit",
        "financial_revision_pit",
        "cross_market_decision_time",
        "local_sessions",
        "currency_fx",
        "global_revision_chain",
        "train_validation_blind_test",
        "reproducibility",
    },
}


class GateError(RuntimeError):
    """Raised for an invalid gate input rather than a failed research tier."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GateError(f"JSON duplicate key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GateError(f"JSON root must be object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalize_input_path(raw: str, *, base: Path) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise GateError("receipt path must be a non-empty string")
    candidate = Path(raw.replace("/", "\\"))
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def validate_planned_catalog(planned: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if planned.get("schema_version") != 1:
        raise GateError("planned catalog schema_version must be 1")
    rows = planned.get("datasets")
    if not isinstance(rows, list):
        raise GateError("planned catalog datasets must be an array")
    by_plan: dict[str, dict[str, Any]] = {}
    seen_dataset_ids: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise GateError(f"planned datasets[{index}] must be object")
        plan_id = row.get("plan_id")
        dataset_id = row.get("dataset_id")
        gate = row.get("gate")
        if not all(isinstance(value, str) and value for value in (plan_id, dataset_id, gate)):
            raise GateError(f"planned datasets[{index}] has invalid plan/dataset/gate identity")
        if plan_id in by_plan:
            raise GateError(f"duplicate plan_id: {plan_id}")
        if dataset_id in seen_dataset_ids:
            raise GateError(f"duplicate planned dataset_id: {dataset_id}")
        by_plan[plan_id] = row
        seen_dataset_ids.add(dataset_id)
    expected_counts = {
        "G_DATA_CORE": 20,
        "G_DATA_GLOBAL_CONTEXT": 8,
    }
    for gate, expected in expected_counts.items():
        actual = sum(row.get("gate") == gate for row in rows)
        if actual != expected:
            raise GateError(f"planned gate {gate} expected {expected}, got {actual}")
    if any(plan_id not in by_plan for plan_id in R0_PLAN_IDS):
        raise GateError("planned catalog is missing one or more CORE-01..CORE-06 identities")
    return by_plan


def tier_plan_ids(by_plan: dict[str, dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    core = tuple(sorted(key for key, row in by_plan.items() if row.get("gate") == "G_DATA_CORE"))
    gctx = tuple(
        sorted(key for key, row in by_plan.items() if row.get("gate") == "G_DATA_GLOBAL_CONTEXT")
    )
    return {
        "R0_PV": R0_PLAN_IDS,
        "R1_CLASSIC_CN": core,
        "R2_A_SHARE_GLOBAL": core + gctx,
    }


def validate_selection(selection: dict[str, Any], by_plan: dict[str, dict[str, Any]]) -> dict[str, dict[str, str]]:
    if selection.get("schema_version") != 1:
        raise GateError("selection schema_version must be 1")
    allowed = {"schema_version", "selection_id", "selected_snapshots", "notes"}
    unknown = set(selection) - allowed
    if unknown:
        raise GateError(f"selection contains unsupported fields: {sorted(unknown)}")
    rows = selection.get("selected_snapshots")
    if not isinstance(rows, dict):
        raise GateError("selected_snapshots must be an object")
    normalized: dict[str, dict[str, str]] = {}
    for plan_id, identity in rows.items():
        if plan_id not in by_plan:
            raise GateError(f"selection references unknown plan_id: {plan_id}")
        if not isinstance(identity, dict) or set(identity) != {"dataset_id", "snapshot_id"}:
            raise GateError(f"selection {plan_id} must contain only dataset_id and snapshot_id")
        dataset_id = identity.get("dataset_id")
        snapshot_id = identity.get("snapshot_id")
        if not isinstance(dataset_id, str) or not dataset_id:
            raise GateError(f"selection {plan_id}.dataset_id must be non-empty")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise GateError(f"selection {plan_id}.snapshot_id must be non-empty")
        expected_dataset_id = by_plan[plan_id]["dataset_id"]
        if dataset_id != expected_dataset_id:
            raise GateError(
                f"selection {plan_id} dataset_id mismatch: expected {expected_dataset_id}, got {dataset_id}"
            )
        if "latest" in snapshot_id.lower():
            raise GateError(f"selection {plan_id} uses forbidden latest snapshot identity")
        normalized[plan_id] = {"dataset_id": dataset_id, "snapshot_id": snapshot_id}
    return normalized


def catalog_index(catalog: dict[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    dataset_cli.validate_catalog_shape(catalog)
    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in catalog.get("datasets", []):
        key = (str(row.get("dataset_id", "")), str(row.get("snapshot_id", "")))
        index.setdefault(key, []).append(row)
    return index


def evaluate_catalog_identity(
    *,
    catalog: dict[str, Any],
    index: dict[tuple[str, str], list[dict[str, Any]]],
    plan_id: str,
    identity: dict[str, str] | None,
    consumer: str,
) -> dict[str, Any]:
    if identity is None:
        return {
            "plan_id": plan_id,
            "selected_identity": None,
            "status": "FAIL",
            "reasons": ["EXACT_SNAPSHOT_SELECTION_MISSING"],
        }
    key = (identity["dataset_id"], identity["snapshot_id"])
    matches = index.get(key, [])
    reasons: list[str] = []
    if len(matches) != 1:
        reasons.append("FORMAL_CATALOG_IDENTITY_MISSING" if not matches else "DUPLICATE_CATALOG_IDENTITY")
        return {
            "plan_id": plan_id,
            "selected_identity": identity,
            "status": "FAIL",
            "reasons": reasons,
        }
    row = matches[0]
    if row.get("lifecycle") not in FORMAL_LIFECYCLES:
        reasons.append(f"LIFECYCLE_NOT_FORMAL:{row.get('lifecycle')}")
    if row.get("mutability") != "immutable":
        reasons.append(f"NOT_IMMUTABLE:{row.get('mutability')}")
    if PURPOSE_RANK.get(str(row.get("purpose_ceiling")), -1) < PURPOSE_RANK["RESEARCH_ONLY"]:
        reasons.append(f"PURPOSE_BELOW_RESEARCH:{row.get('purpose_ceiling')}")
    if row.get("license_status") not in FORMAL_LICENSES:
        reasons.append(f"LICENSE_NOT_FORMAL:{row.get('license_status')}")
    if row.get("quality_status") not in FORMAL_QUALITIES:
        reasons.append(f"QUALITY_NOT_OK:{row.get('quality_status')}")
    if "latest" in str(row.get("snapshot_id", "")).lower():
        reasons.append("LATEST_FORBIDDEN")
    hash_algorithm = row.get("hash_algorithm")
    content_hash = row.get("content_hash")
    if hash_algorithm not in SUPPORTED_HASH_ALGORITHMS:
        reasons.append(f"UNSUPPORTED_OR_MISSING_HASH_ALGORITHM:{hash_algorithm}")
    if not isinstance(content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        reasons.append("CONTENT_HASH_MISSING_OR_INVALID")
    try:
        dataset_cli.check_access(catalog, row, "research", consumer)
    except dataset_cli.CatalogError as exc:
        reasons.append(f"QDATA_RESEARCH_POLICY_DENIED:{exc}")
    path = dataset_cli.normalize_path(str(row.get("physical_path", "")))
    if not path.exists():
        reasons.append("PHYSICAL_PATH_MISSING")
    for raw in row.get("authority_paths", []):
        if not dataset_cli.normalize_path(str(raw)).exists():
            reasons.append(f"AUTHORITY_PATH_MISSING:{raw}")
    if not reasons and hash_algorithm == "sha256-file-v1":
        if not path.is_file() or sha256_file(path) != content_hash:
            reasons.append("FILE_HASH_MISMATCH")
    elif not reasons and hash_algorithm == "sha256-contract-tree-v1":
        reasons.extend(dataset_cli.verify_contract_tree_snapshot(catalog, row, path))
    elif not reasons and hash_algorithm == "sha256-snapshot-contract-v1":
        try:
            errors = dataset_cli.verify_snapshot_contract(
                catalog=catalog,
                item=row,
                snapshot_path=path,
                deep_payload=True,
            )
        except Exception as exc:  # fail closed at an authority boundary
            errors = [f"snapshot verification exception: {exc}"]
        reasons.extend(errors)
    return {
        "plan_id": plan_id,
        "selected_identity": identity,
        "status": "PASS" if not reasons else "FAIL",
        "reasons": reasons,
        "catalog_attributes": {
            "lifecycle": row.get("lifecycle"),
            "mutability": row.get("mutability"),
            "purpose_ceiling": row.get("purpose_ceiling"),
            "license_status": row.get("license_status"),
            "quality_status": row.get("quality_status"),
            "hash_algorithm": row.get("hash_algorithm"),
        },
    }


def validate_l2_registry(registry: dict[str, Any]) -> dict[str, Any]:
    if registry.get("schema_version") != 1:
        raise GateError("L2 receipt registry schema_version must be 1")
    allowed = {"schema_version", "registry_id", "receipts", "notes"}
    unknown = set(registry) - allowed
    if unknown:
        raise GateError(f"L2 registry contains unsupported fields: {sorted(unknown)}")
    receipts = registry.get("receipts")
    if not isinstance(receipts, dict):
        raise GateError("L2 registry receipts must be object")
    unknown_tiers = set(receipts) - set(TIER_ORDER)
    if unknown_tiers:
        raise GateError(f"L2 registry contains unknown tiers: {sorted(unknown_tiers)}")
    return receipts


def selected_bindings(
    required_plan_ids: tuple[str, ...],
    selection: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    return [
        {
            "plan_id": plan_id,
            "dataset_id": selection[plan_id]["dataset_id"],
            "snapshot_id": selection[plan_id]["snapshot_id"],
        }
        for plan_id in required_plan_ids
        if plan_id in selection
    ]


def evaluate_l2_receipt(
    *,
    tier: str,
    registry_entry: Any,
    workspace: Path,
    catalog_sha256: str,
    required_plan_ids: tuple[str, ...],
    selection: dict[str, dict[str, str]],
) -> dict[str, Any]:
    reasons: list[str] = []
    if registry_entry is None:
        return {"status": "FAIL", "reasons": ["L2_RECEIPT_MISSING"], "receipt": None}
    if not isinstance(registry_entry, dict) or set(registry_entry) != {"path", "sha256"}:
        return {"status": "FAIL", "reasons": ["L2_REGISTRY_ENTRY_INVALID"], "receipt": None}
    try:
        receipt_path = normalize_input_path(registry_entry["path"], base=workspace)
    except GateError as exc:
        return {"status": "FAIL", "reasons": [f"L2_RECEIPT_PATH_INVALID:{exc}"], "receipt": None}
    expected_hash = registry_entry.get("sha256")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        reasons.append("L2_RECEIPT_EXPECTED_HASH_INVALID")
    if not receipt_path.is_file():
        reasons.append("L2_RECEIPT_FILE_MISSING")
        return {"status": "FAIL", "reasons": reasons, "receipt": str(receipt_path)}
    actual_hash = sha256_file(receipt_path)
    if actual_hash != expected_hash:
        reasons.append("L2_RECEIPT_HASH_MISMATCH")
    try:
        receipt = load_json(receipt_path)
    except GateError as exc:
        reasons.append(f"L2_RECEIPT_JSON_INVALID:{exc}")
        return {"status": "FAIL", "reasons": reasons, "receipt": str(receipt_path)}
    required_bindings = selected_bindings(required_plan_ids, selection)
    if receipt.get("schema_version") != 1:
        reasons.append("L2_RECEIPT_SCHEMA_UNSUPPORTED")
    if receipt.get("receipt_type") != "L2_RESEARCH_TIER_GATE":
        reasons.append("L2_RECEIPT_TYPE_INVALID")
    if receipt.get("research_tier") != tier:
        reasons.append("L2_RECEIPT_TIER_MISMATCH")
    if receipt.get("gate_status") != "PASS":
        reasons.append(f"L2_GATE_NOT_PASS:{receipt.get('gate_status')}")
    if receipt.get("catalog_sha256") != catalog_sha256:
        reasons.append("L2_CATALOG_BINDING_MISMATCH")
    if receipt.get("selected_inputs_sha256") != canonical_sha256(required_bindings):
        reasons.append("L2_SELECTED_INPUT_HASH_MISMATCH")
    if receipt.get("dataset_bindings") != required_bindings:
        reasons.append("L2_DATASET_BINDINGS_NOT_EXACT")
    checks = receipt.get("checks")
    if not isinstance(checks, dict):
        reasons.append("L2_CHECKS_MISSING")
        checks = {}
    for check in sorted(L2_REQUIRED_CHECKS[tier]):
        if checks.get(check) != "PASS":
            reasons.append(f"L2_REQUIRED_CHECK_NOT_PASS:{check}:{checks.get(check)}")
    if receipt.get("candidate_payload_read") is not False:
        reasons.append("L2_CANDIDATE_READ_GUARD_NOT_FALSE")
    if receipt.get("uses_latest") is not False:
        reasons.append("L2_LATEST_GUARD_NOT_FALSE")
    return {
        "status": "PASS" if not reasons else "FAIL",
        "reasons": reasons,
        "receipt": str(receipt_path),
        "receipt_sha256": actual_hash,
    }


def count_catalog_plan_intersection(
    catalog: dict[str, Any], by_plan: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    catalog_ids = {str(row.get("dataset_id", "")) for row in catalog.get("datasets", [])}
    matched = [plan_id for plan_id, row in by_plan.items() if row["dataset_id"] in catalog_ids]
    return {
        "planned_identities": len(by_plan),
        "active_catalog_identities": len(catalog.get("datasets", [])),
        "matched_plan_identities": len(matched),
        "matched_plan_ids": sorted(matched),
    }


def recompute(
    *,
    catalog_path: Path,
    planned_path: Path,
    selection_path: Path,
    l2_registry_path: Path,
    consumer: str,
    generated_at: str,
) -> dict[str, Any]:
    catalog = load_json(catalog_path)
    planned = load_json(planned_path)
    selection_doc = load_json(selection_path)
    registry_doc = load_json(l2_registry_path)
    by_plan = validate_planned_catalog(planned)
    tier_plans = tier_plan_ids(by_plan)
    selection = validate_selection(selection_doc, by_plan)
    registry = validate_l2_registry(registry_doc)
    index = catalog_index(catalog)
    catalog_hash = sha256_file(catalog_path)

    tiers: dict[str, Any] = {}
    for tier in TIER_ORDER:
        required = tier_plans[tier]
        evaluations = [
            evaluate_catalog_identity(
                catalog=catalog,
                index=index,
                plan_id=plan_id,
                identity=selection.get(plan_id),
                consumer=consumer,
            )
            for plan_id in required
        ]
        dataset_pass_count = sum(item["status"] == "PASS" for item in evaluations)
        data_status = "PASS" if dataset_pass_count == len(required) else "FAIL"
        evaluation_by_plan = {item["plan_id"]: item for item in evaluations}
        group_counts: dict[str, dict[str, int]] = {}
        if tier == "R0_PV":
            group_counts["CORE01_TO_CORE06"] = {
                "required": len(required),
                "passed": dataset_pass_count,
            }
        else:
            for gate_name in ("G_DATA_CORE", "G_DATA_GLOBAL_CONTEXT"):
                group_plan_ids = [
                    plan_id for plan_id in required if by_plan[plan_id].get("gate") == gate_name
                ]
                if group_plan_ids:
                    group_counts[gate_name] = {
                        "required": len(group_plan_ids),
                        "passed": sum(
                            evaluation_by_plan[plan_id]["status"] == "PASS"
                            for plan_id in group_plan_ids
                        ),
                    }
        l2 = evaluate_l2_receipt(
            tier=tier,
            registry_entry=registry.get(tier),
            workspace=WORKSPACE,
            catalog_sha256=catalog_hash,
            required_plan_ids=required,
            selection=selection,
        )
        final_status = "PASS" if data_status == "PASS" and l2["status"] == "PASS" else "NOT_MET"
        tiers[tier] = {
            "status": final_status,
            "required_plan_count": len(required),
            "formal_dataset_pass_count": dataset_pass_count,
            "formal_dataset_gate": data_status,
            "formal_dataset_group_counts": group_counts,
            "l2_gate": l2,
            "missing_or_failed_plan_ids": [
                item["plan_id"] for item in evaluations if item["status"] != "PASS"
            ],
            "dataset_evaluations": evaluations,
        }

    highest_passed = None
    for tier in TIER_ORDER:
        if tiers[tier]["status"] == "PASS":
            highest_passed = tier
        else:
            break
    inspectable = len(catalog.get("datasets", []))
    return {
        "schema_version": 1,
        "run_type": "LAYERED_RESEARCH_GATE_RECOMPUTE",
        "generated_at": generated_at,
        "status": "PASS__MECHANICAL_RECOMPUTE_COMPLETE__NO_FORMAL_EFFECT",
        "formal_effect": "NONE",
        "guards": {
            "candidate_directory_scan": False,
            "candidate_payload_read": False,
            "catalog_write": False,
            "quality_promotion": False,
            "source_admission": False,
            "latest_selection_allowed": False,
            "unknown_or_missing_fails_closed": True,
        },
        "inputs": {
            "catalog": str(catalog_path),
            "catalog_sha256": catalog_hash,
            "planned_catalog": str(planned_path),
            "planned_catalog_sha256": sha256_file(planned_path),
            "selection": str(selection_path),
            "selection_sha256": sha256_file(selection_path),
            "l2_receipt_registry": str(l2_registry_path),
            "l2_receipt_registry_sha256": sha256_file(l2_registry_path),
            "consumer": consumer,
        },
        "catalog_plan_intersection": count_catalog_plan_intersection(catalog, by_plan),
        "layers": {
            "L0_INSPECTION": {
                "status": "INSPECTION_ONLY__RESEARCH_FORBIDDEN",
                "research_allowed": False,
                "inspectable_catalog_identities": inspectable,
                "note": "L0 is an inspection boundary, never a research PASS tier.",
            },
            **tiers,
        },
        "highest_passed_research_tier": highest_passed,
        "research_run_allowed": highest_passed is not None,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fail-closed layered research gate recomputation")
    parser.add_argument("--catalog", type=Path, default=WORKSPACE / "data/catalog/datasets.json")
    parser.add_argument("--planned", type=Path, default=WORKSPACE / "data/catalog/planned_datasets.json")
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--l2-receipts", type=Path, required=True)
    parser.add_argument("--consumer", default="multi-factor")
    parser.add_argument("--generated-at")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    generated_at = args.generated_at or datetime.now(timezone.utc).astimezone().isoformat()
    try:
        result = recompute(
            catalog_path=args.catalog.resolve(),
            planned_path=args.planned.resolve(),
            selection_path=args.selection.resolve(),
            l2_registry_path=args.l2_receipts.resolve(),
            consumer=args.consumer,
            generated_at=generated_at,
        )
    except (GateError, dataset_cli.CatalogError) as exc:
        print(f"LAYERED GATE INPUT ERROR: {exc}", file=sys.stderr)
        return 2
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
