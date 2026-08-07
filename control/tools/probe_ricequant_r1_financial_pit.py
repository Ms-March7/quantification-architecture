from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from probe_ricequant_environment import validate_output_path, write_json_atomic
from probe_ricequant_r0_closure import ProbeRunner, numeric_summary, safe_error
from probe_ricequant_r0_coverage import hashed_frame_summary, quota_bytes


CONTRACT_ID = "ricequant-r1-financial-pit-probe-v1"
DEFAULT_OUTPUT = "live-r1-financial-pit-20260804-v1/result.json"
MAX_FINANCIAL_CALLS = 10
CORE13_IDS = ("000048.XSHE", "000001.XSHE")
CORE13_DATES = ("2018-08-15", "2018-09-01", "2018-11-01", "2019-06-16")
CORE15_EXPRESS_DATES = ("2018-04-13", "2018-04-15")
CORE15_FORECAST_DATES = ("2016-01-20", "2016-01-22")
CORE16_DATES = ("2018-12-31", "2019-12-31")


class FinancialProbeRunner(ProbeRunner):
    def call(
        self,
        name: str,
        function: Callable[[], Any],
        summarizer: Callable[[Any], dict[str, Any]] = hashed_frame_summary,
    ) -> Any:
        if self.calls >= MAX_FINANCIAL_CALLS:
            raise RuntimeError("financial business API call budget exceeded")
        return super().call(name, function, summarizer)


def _date_column_summary(value: Any, column: str) -> dict[str, Any] | None:
    import pandas as pd

    if not isinstance(value, pd.DataFrame):
        return None
    frame = value.reset_index()
    if column not in frame.columns:
        return None
    dates = pd.to_datetime(frame[column], errors="coerce").dropna()
    if dates.empty:
        return {"count": 0, "min": None, "max": None}
    return {
        "count": int(len(dates)),
        "min": str(dates.min()),
        "max": str(dates.max()),
    }


def financial_frame_summary(value: Any) -> dict[str, Any]:
    import pandas as pd

    summary = hashed_frame_summary(value)
    if not isinstance(value, pd.DataFrame):
        return summary
    frame = value.reset_index()
    summary["timestamp_ranges"] = {
        column: item
        for column in ("info_date", "rice_create_tm", "end_date")
        if (item := _date_column_summary(value, column)) is not None
    }
    if "order_book_id" in frame.columns:
        summary["unique_order_book_ids"] = int(
            frame["order_book_id"].astype(str).nunique()
        )
    if "quarter" in frame.columns:
        summary["unique_quarters"] = int(frame["quarter"].astype(str).nunique())
    if "if_adjusted" in frame.columns:
        counts = frame["if_adjusted"].astype(str).value_counts(dropna=False)
        summary["if_adjusted_counts"] = {
            str(key): int(item) for key, item in counts.items()
        }
    return summary


def _row_hashes(value: Any) -> set[str]:
    import pandas as pd

    if not isinstance(value, pd.DataFrame) or value.empty:
        return set()
    frame = value.reset_index()
    frame.columns = [str(item) for item in frame.columns]
    frame = frame.reindex(sorted(frame.columns), axis=1)
    hashes: set[str] = set()
    for row in frame.itertuples(index=False, name=None):
        payload = json.dumps(
            [None if pd.isna(item) else str(item) for item in row],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        hashes.add(hashlib.sha256(payload).hexdigest())
    return hashes


def temporal_sequence_summary(
    values: dict[str, Any],
) -> dict[str, Any]:
    dates = list(values)
    row_counts = {
        date: 0 if value is None else int(len(value))
        for date, value in values.items()
    }
    pairwise: list[dict[str, Any]] = []
    for left_date, right_date in zip(dates, dates[1:]):
        left_hashes = _row_hashes(values[left_date])
        right_hashes = _row_hashes(values[right_date])
        pairwise.append(
            {
                "from": left_date,
                "to": right_date,
                "added_row_hashes": int(len(right_hashes - left_hashes)),
                "removed_row_hashes": int(len(left_hashes - right_hashes)),
                "unchanged_row_hashes": int(len(left_hashes & right_hashes)),
            }
        )
    counts = list(row_counts.values())
    return {
        "row_counts": row_counts,
        "row_counts_monotonic": counts == sorted(counts),
        "first_nonempty_date": next(
            (date for date, count in row_counts.items() if count > 0), None
        ),
        "pairwise_hash_differences": pairwise,
        "interpretation": "BOUNDED_ASOF_DIFFERENCE_ONLY__NOT_COMPLETE_REVISION_CHAIN",
    }


def _quota_summary(value: Any) -> dict[str, Any]:
    return {
        "numeric_fields": numeric_summary(value),
        "bytes_used": quota_bytes(value),
    }


def quota_snapshot() -> dict[str, Any]:
    import rqdatac

    payload: dict[str, Any] = {
        "schema_version": 1,
        "probe_id": "ricequant-r1-financial-pit-quota-v1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "contract_id": CONTRACT_ID,
        "credential_value_captured": False,
        "api_payload_persisted": False,
        "business_api_calls_made": 0,
    }
    try:
        rqdatac.init(enable_bjse=True)
        payload["initialization"] = "PASS"
        payload["quota"] = _quota_summary(rqdatac.user.get_quota())
        payload["business_api_calls_made"] = 1
        payload["status"] = "NUMERIC_QUOTA_SUMMARY_PASS"
    except Exception as exc:
        payload["initialization"] = "ERROR"
        payload["error"] = safe_error(exc)
        payload["status"] = "NUMERIC_QUOTA_SUMMARY_ERROR"
    return payload


def build_live_payload() -> dict[str, Any]:
    import rqdatac

    payload: dict[str, Any] = {
        "schema_version": 1,
        "probe_id": "ricequant-r1-financial-pit-live-v1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "contract_id": CONTRACT_ID,
        "scope": "CORE13_CORE15_CORE16__SUMMARY_ONLY__NO_PAYLOAD_PERSISTENCE",
        "purpose_ceiling": "INSPECTION_ONLY",
        "formal_effect": "NONE",
        "formal_admission": False,
        "formal_catalog_write": False,
        "quality_promotion": False,
        "research_use": False,
        "bulk_download": False,
        "credential_value_captured": False,
        "api_payload_persisted": False,
        "maximum_financial_business_api_calls": MAX_FINANCIAL_CALLS,
    }
    try:
        rqdatac.init(enable_bjse=True)
        payload["initialization"] = "PASS"
    except Exception as exc:
        payload.update(
            {
                "initialization": "ERROR",
                "initialization_error": safe_error(exc),
                "business_api_calls_made": 0,
                "status": "INITIALIZATION_FAILED__NO_DATA_REQUESTED",
                "r1_gate_status": "NOT_MET",
            }
        )
        return payload

    runner = FinancialProbeRunner(rqdatac)
    core13_values: dict[str, Any] = {}
    for date in CORE13_DATES:
        core13_values[date] = runner.call(
            f"core13_pit_financials_2018q2_asof_{date}",
            lambda date=date: rqdatac.get_pit_financials_ex(
                list(CORE13_IDS),
                fields=["revenue", "net_profit"],
                start_quarter="2018q2",
                end_quarter="2018q2",
                date=date,
                statements="all",
            ),
            financial_frame_summary,
        )

    express_values: dict[str, Any] = {}
    for date in CORE15_EXPRESS_DATES:
        express_values[date] = runner.call(
            f"core15_current_performance_asof_{date}",
            lambda date=date: rqdatac.current_performance(
                "000004.XSHE",
                info_date=date,
                interval="1q",
                fields=["total_profit"],
            ),
            financial_frame_summary,
        )

    forecast_values: dict[str, Any] = {}
    for date in CORE15_FORECAST_DATES:
        forecast_values[date] = runner.call(
            f"core15_performance_forecast_asof_{date}",
            lambda date=date: rqdatac.performance_forecast(
                "000001.XSHE",
                info_date=date,
                fields=[
                    "forecast_type",
                    "forecast_description",
                    "forecast_np_floor",
                    "forecast_np_ceiling",
                ],
            ),
            financial_frame_summary,
        )

    audit_values: dict[str, Any] = {}
    for date in CORE16_DATES:
        audit_values[date] = runner.call(
            f"core16_audit_opinion_2018q4_asof_{date}",
            lambda date=date: rqdatac.get_audit_opinion(
                list(("000001.XSHE", "600519.XSHG")),
                start_quarter="2018q4",
                end_quarter="2018q4",
                date=date,
                type="financial_statements",
            ),
            financial_frame_summary,
        )

    sequences = {
        "core13": temporal_sequence_summary(core13_values),
        "core15_express": temporal_sequence_summary(express_values),
        "core15_forecast": temporal_sequence_summary(forecast_values),
        "core16_audit": temporal_sequence_summary(audit_values),
    }
    payload.update(
        {
            "checks": runner.results,
            "business_api_calls_made": runner.calls,
            "temporal_differences": sequences,
            "empty_result_checks": [
                name
                for name, item in runner.results.items()
                if item["status"] == "PASS_EMPTY"
            ],
            "formal_limits": [
                "BOUNDED_ASOF_BEHAVIOR_IS_NOT_COMPLETE_REVISION_CLOSURE",
                "INFO_DATE_IS_DATE_LEVEL_AND_NOT_INTRADAY_AVAILABLE_AT",
                "RICE_CREATE_TM_IS_NOT_ASSUMED_LEGAL_PUBLICATION_TIME",
                "SOURCE_RIGHTS_AND_IMMUTABLE_EXPORT_ARE_UNVERIFIED",
                "CORE14_REMAINS_DERIVED_FROM_FORMAL_CORE13",
                "CORE18_HAS_NO_PROBED_ENDPOINT",
            ],
            "r1_gate_status": "NOT_MET__CORE20_FORMAL_SNAPSHOTS_AND_L2_SAFETY_ABSENT",
        }
    )
    statuses = [item["status"] for item in runner.results.values()]
    positive_anchors = {
        "core13": max(sequences["core13"]["row_counts"].values(), default=0) > 0,
        "core15_express": max(
            sequences["core15_express"]["row_counts"].values(), default=0
        )
        > 0,
        "core15_forecast": max(
            sequences["core15_forecast"]["row_counts"].values(), default=0
        )
        > 0,
        "core16": max(sequences["core16_audit"]["row_counts"].values(), default=0)
        > 0,
    }
    payload["positive_anchor_checks"] = positive_anchors
    payload["status"] = (
        "BOUNDED_FINANCIAL_ENDPOINT_AND_ASOF_CAPABILITY_PASS__FORMAL_EFFECT_NONE"
        if runner.calls == MAX_FINANCIAL_CALLS
        and all(status in {"PASS", "PASS_EMPTY"} for status in statuses)
        and all(positive_anchors.values())
        else "PARTIAL__FINANCIAL_CAPABILITY_OR_POSITIVE_ANCHOR_NOT_MET__FORMAL_EFFECT_NONE"
    )
    return payload


def offline_check() -> dict[str, Any]:
    import rqdatac

    names = (
        "get_pit_financials_ex",
        "current_performance",
        "performance_forecast",
        "get_audit_opinion",
    )
    entrypoints = {name: callable(getattr(rqdatac, name, None)) for name in names}
    entrypoints["user.get_quota"] = callable(
        getattr(getattr(rqdatac, "user", None), "get_quota", None)
    )
    return {
        "status": "OFFLINE_ENTRYPOINT_CHECK_PASS"
        if all(entrypoints.values())
        else "OFFLINE_ENTRYPOINT_CHECK_FAIL",
        "network_requests": 0,
        "business_api_calls": 0,
        "credential_value_captured": False,
        "entrypoints": entrypoints,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe bounded Ricequant financial PIT endpoint capabilities."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline-check", action="store_true")
    mode.add_argument("--quota-only", action="store_true")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.offline_check:
        payload = offline_check()
    elif args.quota_only:
        payload = quota_snapshot()
    else:
        payload = build_live_payload()
    if not args.offline_check:
        output_path = validate_output_path(args.output)
        payload["output_path"] = str(output_path)
        write_json_atomic(output_path, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if args.offline_check:
        return 0 if payload.get("status") == "OFFLINE_ENTRYPOINT_CHECK_PASS" else 2
    return 0 if "ERROR" not in str(payload.get("status", "")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
