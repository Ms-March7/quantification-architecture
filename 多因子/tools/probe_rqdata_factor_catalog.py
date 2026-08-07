"""Bounded RQData factor catalog and category-value capability probe.

The probe persists factor names and summary-only value evidence.  It never
persists API values, credentials, account identity, IC, returns, or backtests.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
from typing import Any


FACTOR_TYPES = (
    "income_statement",
    "balance_sheet",
    "cash_flow_statement",
    "eod_indicator",
    "operational_indicator",
    "cash_flow_indicator",
    "financial_indicator",
    "growth_indicator",
    "alpha101",
    "moving_average_indicator",
    "obos_indicator",
    "energy_indicator",
    "other",
)


def _sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _error(error: Exception) -> dict[str, str]:
    return {
        "status": "ERROR",
        "error_type": type(error).__name__,
        "message": str(error)[:240],
    }


def run(output_path: Path) -> dict[str, Any]:
    import rqdatac

    rqdatac.init()
    all_names = sorted(set(rqdatac.get_all_factor_names(market="cn")))
    factor_types: dict[str, dict[str, Any]] = {}
    value_probes: dict[str, dict[str, Any]] = {}

    for factor_type in FACTOR_TYPES:
        names = sorted(set(rqdatac.get_all_factor_names(type=factor_type, market="cn")))
        factor_types[factor_type] = {
            "count": len(names),
            "names": names,
            "names_sha256": _sha256_json(names),
        }
        sample_factors = names[:3]
        try:
            frame = rqdatac.get_factor(
                ["000001.XSHE", "603986.XSHG"],
                sample_factors,
                start_date="2026-08-03",
                end_date="2026-08-03",
            )
            if frame is None:
                value_probes[factor_type] = {
                    "status": "OK_EMPTY",
                    "sample_factor_count": len(sample_factors),
                    "row_count": 0,
                    "non_null_count": 0,
                    "value_payload_persisted": False,
                }
            else:
                non_null = int(frame.notna().sum().sum())
                value_probes[factor_type] = {
                    "status": "OK",
                    "sample_factor_count": len(sample_factors),
                    "row_count": int(len(frame)),
                    "column_count": int(len(frame.columns)),
                    "non_null_count": non_null,
                    "schema": [str(value) for value in frame.columns],
                    "in_memory_value_sha256": hashlib.sha256(
                        frame.to_csv(index=True).encode("utf-8")
                    ).hexdigest(),
                    "value_payload_persisted": False,
                }
        except Exception as error:  # permission and endpoint errors are evidence
            value_probes[factor_type] = {
                **_error(error),
                "sample_factor_count": len(sample_factors),
                "value_payload_persisted": False,
            }

    type_union = sorted({name for value in factor_types.values() for name in value["names"]})
    result: dict[str, Any] = {
        "schema_version": 1,
        "probe_id": "rqdata-factor-catalog-and-category-value-probe-20260805-v1",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "INSPECTION_ONLY__FACTOR_METADATA_AND_BOUNDED_CATEGORY_VALUE_PROBE",
        "purpose_ceiling": "INSPECTION_ONLY",
        "rqdatac_version": importlib.metadata.version("rqdatac"),
        "market": "cn",
        "all_factor_name_count": len(all_names),
        "all_factor_names_sha256": _sha256_json(all_names),
        "factor_types": factor_types,
        "type_union_count": len(type_union),
        "type_union_matches_all_names": type_union == all_names,
        "value_probes": value_probes,
        "successful_value_probe_categories": sum(
            value.get("status") in {"OK", "OK_EMPTY"} for value in value_probes.values()
        ),
        "non_null_value_probe_categories": sum(
            int(value.get("non_null_count", 0)) > 0 for value in value_probes.values()
        ),
        "credentials_captured": False,
        "account_identity_captured": False,
        "api_value_payload_persisted": False,
        "bulk_factor_value_download": False,
        "ic_runs": 0,
        "backtest_runs": 0,
        "validation_consumed": False,
        "holdout_opened": False,
        "formal_effect": "NONE",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.output.resolve())
    print(json.dumps({
        "status": result["status"],
        "all_factor_name_count": result["all_factor_name_count"],
        "successful_value_probe_categories": result["successful_value_probe_categories"],
        "non_null_value_probe_categories": result["non_null_value_probe_categories"],
        "output": str(args.output.resolve()),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
