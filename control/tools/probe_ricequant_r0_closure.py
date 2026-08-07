from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from numbers import Number
from pathlib import Path
from typing import Any, Callable

from probe_ricequant_environment import validate_output_path, write_json_atomic


ORDER_BOOK_IDS = (
    "603986.XSHG",
    "000001.XSHE",
    "300750.XSHE",
    "688981.XSHG",
)
PRICE_START = "2026-07-28"
PRICE_END = "2026-08-03"
ACTION_START = "2024-01-01"
ACTION_END = "2026-08-03"
UNIVERSE_ANCHORS = ("2026-08-03",)
MAX_BUSINESS_API_CALLS = 17
DEFAULT_OUTPUT = "live-r0-closure-20260803-v1/result.json"
SECRET_KEY_PARTS = (
    "account",
    "conf",
    "credential",
    "license",
    "password",
    "secret",
    "token",
    "uri",
    "user",
)


def safe_error(exc: Exception) -> dict[str, str]:
    message = str(exc)
    message = re.sub(r"(?:tcp|rqdata|rqdatac)://\S+", "<redacted>", message)
    return {"type": type(exc).__name__, "message": message[:300]}


def numeric_summary(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.lower() for part in SECRET_KEY_PARTS):
                continue
            cleaned = numeric_summary(item)
            if cleaned is not None:
                result[key_text] = cleaned
        return result
    if isinstance(value, Number) and not isinstance(value, complex):
        return value
    if isinstance(value, (list, tuple)):
        cleaned_items = [numeric_summary(item) for item in value]
        return [item for item in cleaned_items if item is not None]
    return None


def frame_summary(value: Any) -> dict[str, Any]:
    import pandas as pd

    if value is None:
        return {"returned": False, "rows": 0, "columns": []}
    if isinstance(value, pd.Series):
        value = value.to_frame(name=str(value.name or "value"))
    if not isinstance(value, pd.DataFrame):
        return {"returned": True, "python_type": type(value).__name__}

    summary: dict[str, Any] = {
        "returned": True,
        "rows": int(len(value)),
        "columns": [str(item) for item in value.columns],
        "index_names": [str(item) for item in value.index.names],
        "dtypes": {str(key): str(item) for key, item in value.dtypes.items()},
        "null_counts": {
            str(key): int(item) for key, item in value.isna().sum().items()
        },
    }
    if len(value.index):
        try:
            date_level = value.index.get_level_values(-1)
            inferred = pd.api.types.infer_dtype(date_level, skipna=True)
            is_date_like = isinstance(date_level, pd.DatetimeIndex) or inferred in {
                "date",
                "datetime",
                "datetime64",
            }
            if not is_date_like:
                return summary
            dates = pd.to_datetime(date_level, errors="coerce")
            dates = dates[~dates.isna()]
            if len(dates):
                summary["index_date_min"] = str(dates.min())
                summary["index_date_max"] = str(dates.max())
        except (TypeError, ValueError):
            pass
    return summary


def universe_summary(value: Any) -> dict[str, Any]:
    summary = frame_summary(value)
    if value is None or not hasattr(value, "columns"):
        return summary
    categorical_fields = (
        "type",
        "board_type",
        "status",
        "exchange",
        "round_lot",
    )
    counts: dict[str, dict[str, int]] = {}
    for field in categorical_fields:
        if field not in value.columns:
            continue
        field_counts = value[field].astype(str).value_counts(dropna=False).head(20)
        counts[field] = {str(key): int(item) for key, item in field_counts.items()}
    summary["categorical_counts"] = counts
    if "order_book_id" in value.columns:
        ids = value["order_book_id"].astype(str)
        summary["exchange_suffix_counts"] = {
            ".XSHG": int(ids.str.endswith(".XSHG").sum()),
            ".XSHE": int(ids.str.endswith(".XSHE").sum()),
            ".BJSE": int(ids.str.endswith(".BJSE").sum()),
        }
    return summary


def boolean_summary(value: Any) -> dict[str, Any]:
    import pandas as pd

    summary = frame_summary(value)
    if isinstance(value, pd.Series):
        array = value
    elif isinstance(value, pd.DataFrame):
        array = value.stack(dropna=False)
    else:
        return summary
    summary["true_count"] = int((array == True).sum())  # noqa: E712
    summary["false_count"] = int((array == False).sum())  # noqa: E712
    summary["missing_count"] = int(array.isna().sum())
    return summary


def adjustment_comparison(raw: Any, adjusted: Any) -> dict[str, Any]:
    import pandas as pd

    if not isinstance(raw, pd.DataFrame) or not isinstance(adjusted, pd.DataFrame):
        return {"comparable": False}
    common = raw.index.intersection(adjusted.index)
    if "close" not in raw.columns or "close" not in adjusted.columns or not len(common):
        return {"comparable": False}
    raw_close = pd.to_numeric(raw.loc[common, "close"], errors="coerce")
    adjusted_close = pd.to_numeric(adjusted.loc[common, "close"], errors="coerce")
    valid = raw_close.notna() & adjusted_close.notna() & raw_close.ne(0)
    ratio = (adjusted_close[valid] / raw_close[valid]).round(10)
    return {
        "comparable": True,
        "common_rows": int(len(common)),
        "valid_ratio_rows": int(len(ratio)),
        "equal_close_rows": int((raw_close[valid] == adjusted_close[valid]).sum()),
        "distinct_rounded_ratios": int(ratio.nunique(dropna=True)),
    }


def trading_dates_summary(value: Any) -> dict[str, Any]:
    count = 0 if value is None else int(len(value))
    return {
        "returned": value is not None,
        "count": count,
        "first": None if count == 0 else str(value[0]),
        "last": None if count == 0 else str(value[-1]),
    }


class ProbeRunner:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.calls = 0
        self.results: dict[str, dict[str, Any]] = {}

    def call(
        self,
        name: str,
        function: Callable[[], Any],
        summarizer: Callable[[Any], dict[str, Any]] = frame_summary,
    ) -> Any:
        if self.calls >= MAX_BUSINESS_API_CALLS:
            raise RuntimeError("business API call budget exceeded")
        self.calls += 1
        try:
            value = function()
            result_summary = summarizer(value)
            status = (
                "PASS_EMPTY"
                if result_summary.get("returned") is False
                or result_summary.get("rows") == 0
                else "PASS"
            )
            self.results[name] = {"status": status, **result_summary}
            return value
        except Exception as exc:  # preserve independent capability evidence
            self.results[name] = {"status": "ERROR", "error": safe_error(exc)}
            return None


def build_live_payload() -> dict[str, Any]:
    import rqdatac

    payload: dict[str, Any] = {
        "schema_version": 1,
        "probe_id": "ricequant-r0-closure-live-v1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "contract_id": "ricequant-r0-closure-probe-v1",
        "scope": "CORE01_TO_CORE06__SUMMARY_ONLY__NO_PAYLOAD_PERSISTENCE",
        "purpose_ceiling": "INSPECTION_ONLY",
        "formal_effect": "NONE",
        "formal_admission": False,
        "formal_catalog_write": False,
        "quality_promotion": False,
        "research_use": False,
        "backtest_use": False,
        "bulk_download": False,
        "license_value_captured": False,
        "maximum_business_api_calls": MAX_BUSINESS_API_CALLS,
        "request_bounds": {
            "order_book_ids": list(ORDER_BOOK_IDS),
            "price_start": PRICE_START,
            "price_end": PRICE_END,
            "action_start": ACTION_START,
            "action_end": ACTION_END,
            "universe_anchors": list(UNIVERSE_ANCHORS),
        },
    }

    try:
        rqdatac.init(enable_bjse=True)
        payload["initialization"] = "PASS"
    except Exception as exc:
        payload["initialization"] = "ERROR"
        payload["initialization_error"] = safe_error(exc)
        payload["status"] = "INITIALIZATION_FAILED__NO_DATA_REQUESTED"
        payload["r0_gate_status"] = "NOT_MET"
        payload["business_api_calls_made"] = 0
        return payload

    runner = ProbeRunner(rqdatac)
    quota_before = runner.call(
        "quota_before_numeric_summary",
        lambda: rqdatac.user.get_quota(),
        lambda value: {"numeric_fields": numeric_summary(value)},
    )
    for anchor in UNIVERSE_ANCHORS:
        runner.call(
            f"core01_all_instruments_{anchor}",
            lambda anchor=anchor: rqdatac.all_instruments(type="CS", date=anchor),
            universe_summary,
        )
    runner.call(
        "core01_share_transformation",
        lambda: rqdatac.get_share_transformation(),
    )
    runner.call(
        "core01_symbol_change_info",
        lambda: rqdatac.get_symbol_change_info(list(ORDER_BOOK_IDS)),
    )
    runner.call(
        "core02_trading_dates",
        lambda: rqdatac.get_trading_dates(PRICE_START, PRICE_END),
        trading_dates_summary,
    )
    runner.call(
        "core02_trading_periods",
        lambda: rqdatac.get_trading_periods(
            list(ORDER_BOOK_IDS), PRICE_START, PRICE_END, frequency="1m"
        ),
    )
    raw = runner.call(
        "core03_price_unadjusted",
        lambda: rqdatac.get_price(
            list(ORDER_BOOK_IDS),
            start_date=PRICE_START,
            end_date=PRICE_END,
            frequency="1d",
            adjust_type="none",
            skip_suspended=False,
        ),
    )
    adjusted = runner.call(
        "core05_price_forward_adjusted",
        lambda: rqdatac.get_price(
            list(ORDER_BOOK_IDS),
            start_date=PRICE_START,
            end_date=PRICE_END,
            frequency="1d",
            adjust_type="pre",
            skip_suspended=False,
        ),
    )
    runner.call(
        "core04_dividend_info",
        lambda: rqdatac.get_dividend_info(
            list(ORDER_BOOK_IDS), ACTION_START, ACTION_END
        ),
    )
    runner.call(
        "core04_dividend",
        lambda: rqdatac.get_dividend(
            list(ORDER_BOOK_IDS), ACTION_START, ACTION_END, adjusted=False
        ),
    )
    runner.call(
        "core04_split",
        lambda: rqdatac.get_split(list(ORDER_BOOK_IDS), ACTION_START, ACTION_END),
    )
    runner.call(
        "core04_allotment",
        lambda: rqdatac.get_allotment(
            list(ORDER_BOOK_IDS), ACTION_START, ACTION_END
        ),
    )
    runner.call(
        "core05_ex_factor",
        lambda: rqdatac.get_ex_factor(
            list(ORDER_BOOK_IDS), ACTION_START, ACTION_END
        ),
    )
    runner.call(
        "core06_is_st_stock",
        lambda: rqdatac.is_st_stock(
            list(ORDER_BOOK_IDS), PRICE_START, PRICE_END
        ),
        boolean_summary,
    )
    runner.call(
        "core06_special_treatment_info",
        lambda: rqdatac.get_special_treatment_info(list(ORDER_BOOK_IDS)),
    )
    runner.call(
        "core06_is_suspended",
        lambda: rqdatac.is_suspended(
            list(ORDER_BOOK_IDS), PRICE_START, PRICE_END
        ),
        boolean_summary,
    )
    quota_after = runner.call(
        "quota_after_numeric_summary",
        lambda: rqdatac.user.get_quota(),
        lambda value: {"numeric_fields": numeric_summary(value)},
    )

    payload["checks"] = runner.results
    payload["business_api_calls_made"] = runner.calls
    payload["derived_checks"] = {
        "raw_vs_forward_adjusted_close_diagnostic_only": adjustment_comparison(
            raw, adjusted
        ),
        "core05_formal_rule": (
            "DERIVE_FROM_PINNED_CORE03_RAW_AND_CORE04_ACTIONS; "
            "RQDATA_PRE_ADJUSTED_PRICE_IS_CALIBRATION_ONLY"
        ),
        "quota_measurement": {
            "before_available": quota_before is not None,
            "after_available": quota_after is not None,
            "note": "Only numeric non-sensitive quota fields are retained.",
        },
    }
    statuses = [item["status"] for item in runner.results.values()]
    payload["empty_result_checks"] = [
        name
        for name, item in runner.results.items()
        if item["status"] == "PASS_EMPTY"
    ]
    payload["status"] = (
        "BOUNDED_CAPABILITY_PASS__FORMAL_EFFECT_NONE"
        if statuses and all(status in {"PASS", "PASS_EMPTY"} for status in statuses)
        else "PARTIAL__ONE_OR_MORE_CAPABILITIES_UNAVAILABLE__FORMAL_EFFECT_NONE"
    )
    payload["r0_gate_status"] = "NOT_MET__FORMAL_SNAPSHOTS_AND_L2_GATES_ABSENT"
    return payload


def offline_check() -> dict[str, Any]:
    import rqdatac

    names = (
        "all_instruments",
        "get_share_transformation",
        "get_symbol_change_info",
        "get_trading_dates",
        "get_trading_periods",
        "get_price",
        "get_dividend_info",
        "get_dividend",
        "get_split",
        "get_allotment",
        "get_ex_factor",
        "is_st_stock",
        "is_suspended",
        "get_special_treatment_info",
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
        "license_value_captured": False,
        "entrypoints": entrypoints,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe bounded Ricequant capabilities for the R0 CORE-01..06 gate."
    )
    parser.add_argument("--offline-check", action="store_true")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = offline_check() if args.offline_check else build_live_payload()
    if not args.offline_check:
        output_path = validate_output_path(args.output)
        write_json_atomic(output_path, payload)
        payload["output_path"] = str(output_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if "FAIL" not in str(payload.get("status", "")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
