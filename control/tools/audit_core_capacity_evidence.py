#!/usr/bin/env python3
"""Recalculate the currently auditable CORE-20 storage evidence.

This tool is intentionally read-only.  It does not fetch data, mutate the
catalog, or claim that planned datasets exist.  The output separates observed
local evidence from arithmetic checks on the preliminary capacity model.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


MIB = 1024**2
GIB = 1024**3
TIB = 1024**4


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def tree_size(root: Path) -> tuple[int, int]:
    total_bytes = 0
    total_files = 0
    if not root.exists():
        return total_bytes, total_files
    for path in root.rglob("*"):
        if path.is_file():
            total_files += 1
            total_bytes += path.stat().st_size
    return total_bytes, total_files


def round_float(value: float, digits: int = 6) -> float:
    return round(value, digits)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Quantification workspace root",
    )
    args = parser.parse_args()
    root = args.workspace_root.resolve()

    contract = load_json(
        root / "data/catalog/contracts/g_data_core_v1/contract.json"
    )
    planned = load_json(root / "data/catalog/planned_datasets.json")
    actual = load_json(root / "data/catalog/datasets.json")
    estimate = load_json(
        root
        / "reports/data-governance/data-capacity-estimate-20260721/assumptions.json"
    )
    receipt = load_json(
        root
        / "reports/storage-cleanup/legacy-data-retirement-20260720/"
        "DELETION_RECEIPT.json"
    )
    provenance = load_json(
        root
        / "多因子/data/market/snapshots/cn-a-share-qlib-daily/"
        "2026-07-18/PROVENANCE.json"
    )

    planned_core = {
        item["dataset_id"]
        for item in planned["datasets"]
        if item.get("gate") == "G_DATA_CORE"
    }
    actual_ids = {item["dataset_id"] for item in actual["datasets"]}
    actual_core = sorted(planned_core & actual_ids)

    data_bytes, data_files = tree_size(root / "data")
    disk_total, disk_used, disk_free = shutil.disk_usage(root)

    capacity = estimate["core_20_capacity"]
    financial = estimate["assumptions"]["financial_history"]
    raw_high_tib = capacity["layers"][0]["tib"][1]
    curated_high_tib = capacity["layers"][1]["tib"][1]
    snapshot_high_tib = capacity["layers"][2]["gib"][1] / 1024
    projection_high_tib = capacity["layers"][3]["tib"][1]
    layer_sum_high_tib = (
        raw_high_tib
        + curated_high_tib
        + snapshot_high_tib
        + projection_high_tib
    )
    declared_persistent_high_tib = capacity["persistent_steady_state"]["tib"][1]
    working_high_tib = capacity["temporary_quarantine_compaction"]["tib"][1]
    declared_margin_high_tib = capacity["safety_margin"]["tib"][1]
    declared_recommended_high_tib = capacity["recommended_available_capacity"]["tib"][
        1
    ]
    declared_pre_margin_high_tib = declared_persistent_high_tib + working_high_tib
    layer_sum_pre_margin_high_tib = layer_sum_high_tib + working_high_tib
    stated_margin_rate = (
        capacity["safety_margin"]["percent_of_persistent_plus_working_high"] / 100
    )
    recomputed_declared_at_stated_margin_tib = declared_pre_margin_high_tib * (
        1 + stated_margin_rate
    )
    recomputed_layer_sum_at_stated_margin_tib = layer_sum_pre_margin_high_tib * (
        1 + stated_margin_rate
    )
    implied_declared_margin_rate = (
        declared_margin_high_tib / declared_pre_margin_high_tib
    )

    financial_raw_high_bytes = (
        financial["periodic_document_revisions_high"]
        * financial["weighted_raw_document_mib_high"]
        * MIB
    )
    financial_raw_high_tib = financial_raw_high_bytes / TIB

    qlib_target = next(
        item
        for item in receipt["targets"]
        if item["path"].endswith(
            "/snapshots/cn-a-share-qlib-daily/2026-07-18/qlib_bin"
        )
    )
    qlib = provenance["qlib"]
    feature_count = qlib["feature_files"] / qlib["feature_instruments"]
    calendar_rows = provenance["calendar"]["rows"]
    scaled_projection_gib = {}
    for field_count in (30, 100):
        projected_bytes = (
            qlib_target["bytes"]
            * (8800 / calendar_rows)
            * (field_count / feature_count)
        )
        scaled_projection_gib[str(field_count)] = round_float(
            projected_bytes / GIB, 3
        )

    checks = {
        "core_contract_is_contract_only": (
            contract["status"] == "D3A_COMPLETE_CONTRACT_ONLY__NO_DATA_INGESTED"
        ),
        "no_planned_core_dataset_is_in_actual_catalog": not actual_core,
        "financial_document_high_alone_exceeds_declared_raw_high": (
            financial_raw_high_tib > raw_high_tib
        ),
        "declared_recommended_high_is_below_its_stated_35pct_margin_math": (
            declared_recommended_high_tib
            < recomputed_layer_sum_at_stated_margin_tib
        ),
        "declared_persistent_high_differs_from_layer_sum": (
            abs(declared_persistent_high_tib - layer_sum_high_tib) > 1e-9
        ),
    }

    result = {
        "schema_version": 1,
        "audit_status": "OBSERVED_EVIDENCE_AND_ARITHMETIC_COMPLETE__CAPACITY_NOT_STATISTICALLY_DETERMINED",
        "workspace_root": root.as_posix(),
        "observed": {
            "data_tree": {
                "bytes": data_bytes,
                "files": data_files,
                "gib": round_float(data_bytes / GIB, 3),
            },
            "workspace_volume": {
                "total_bytes": disk_total,
                "used_bytes": disk_used,
                "free_bytes": disk_free,
                "total_gib": round_float(disk_total / GIB, 3),
                "free_gib": round_float(disk_free / GIB, 3),
            },
            "core_contract": {
                "status": contract["status"],
                "sources": contract["validation"]["sources"],
                "datasets": contract["validation"]["datasets"],
                "tables": contract["validation"]["tables"],
                "fields_including_inherited": contract["validation"][
                    "field_definitions_including_inherited_sets"
                ],
                "blocked_source_bindings": contract["validation"][
                    "blocked_source_bindings"
                ],
            },
            "catalog": {
                "actual_dataset_identities": len(actual["datasets"]),
                "planned_core_dataset_ids": len(planned_core),
                "planned_core_ids_present_in_actual_catalog": actual_core,
            },
            "retired_qlib_projection": {
                "snapshot_id": provenance["snapshot_id"],
                "expanded_bytes": qlib_target["bytes"],
                "expanded_gib": round_float(qlib_target["bytes"] / GIB, 3),
                "calendar_rows": calendar_rows,
                "instruments": qlib["feature_instruments"],
                "feature_files": qlib["feature_files"],
                "inferred_numeric_features": feature_count,
                "linear_scale_to_8800_days_gib": scaled_projection_gib,
                "scaling_warning": (
                    "Linear evidence anchor only; future universe, metadata, "
                    "layout and retention can change the result."
                ),
            },
        },
        "preliminary_model_arithmetic": {
            "financial_documents_high_case": {
                "documents": financial["periodic_document_revisions_high"],
                "mib_each": financial["weighted_raw_document_mib_high"],
                "bytes": financial_raw_high_bytes,
                "tib": round_float(financial_raw_high_tib, 6),
            },
            "declared_raw_high_tib": raw_high_tib,
            "layer_sum_high_tib": round_float(layer_sum_high_tib, 6),
            "declared_persistent_high_tib": declared_persistent_high_tib,
            "working_high_tib": working_high_tib,
            "declared_pre_margin_high_tib": declared_pre_margin_high_tib,
            "layer_sum_pre_margin_high_tib": round_float(
                layer_sum_pre_margin_high_tib, 6
            ),
            "stated_high_margin_rate": stated_margin_rate,
            "recomputed_declared_high_at_stated_margin_tib": round_float(
                recomputed_declared_at_stated_margin_tib, 6
            ),
            "recomputed_layer_sum_high_at_stated_margin_tib": round_float(
                recomputed_layer_sum_at_stated_margin_tib, 6
            ),
            "declared_margin_high_tib": declared_margin_high_tib,
            "implied_declared_margin_rate": round_float(
                implied_declared_margin_rate, 6
            ),
            "declared_recommended_high_tib": declared_recommended_high_tib,
            "shortfall_to_stated_margin_math_tib": round_float(
                recomputed_layer_sum_at_stated_margin_tib
                - declared_recommended_high_tib,
                6,
            ),
        },
        "checks": checks,
        "conclusion": {
            "previous_1_5_to_6_tib_is_measured": False,
            "six_tib_is_a_defensible_hard_upper": False,
            "exact_core_20_capacity_can_be_determined_from_current_assets": False,
            "blocking_reason": (
                "Zero CORE-20 snapshots exist in the actual catalog; official "
                "source endpoints, storage permissions and production adapters "
                "are not admitted for the blocked bindings."
            ),
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
