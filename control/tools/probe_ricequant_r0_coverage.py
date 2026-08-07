from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from probe_ricequant_environment import validate_output_path, write_json_atomic
from probe_ricequant_r0_closure import (
    ProbeRunner,
    frame_summary,
    numeric_summary,
    safe_error,
    trading_dates_summary,
    universe_summary,
)


MAX_BUSINESS_API_CALLS = 17
MAX_INCREMENTAL_FLOW_BYTES = 8 * 1024 * 1024
DEFAULT_OUTPUT = "live-r0-coverage-pit-volume-20260803-v1/result.json"
CALENDAR_START = "2000-01-04"
CALENDAR_END = "2026-08-03"
ACTION_IDS = ("000001.XSHE", "600008.XSHG")
ACTION_START = "2010-01-01"
ACTION_END = "2021-12-31"
PIT_DATES = ("2019-03-06", "2019-03-08", "2019-06-21")
PRICE_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "prev_close",
    "volume",
    "total_turnover",
    "num_trades",
    "limit_up",
    "limit_down",
)
SMALL_BOARD_QUOTAS = {"MainBoard": 20, "GEM": 8, "KSH": 4, "BJS": 0}
LARGE_BOARD_QUOTAS = {"MainBoard": 163, "GEM": 65, "KSH": 28, "BJS": 0}
DYNAMIC_POOL_ANCHORS = (
    "2000-01-04",
    "2005-01-04",
    "2010-01-04",
    "2015-01-05",
    "2020-01-02",
    "2021-11-15",
    "2026-08-03",
)


def _stable_key(order_book_id: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}|{order_book_id}".encode("utf-8")).hexdigest()


def deterministic_board_sample(master: Any, quotas: dict[str, int], salt: str) -> list[str]:
    import pandas as pd

    if not isinstance(master, pd.DataFrame):
        return []
    required = {"order_book_id", "board_type"}
    if not required.issubset(master.columns):
        return []
    selected: list[str] = []
    for board, count in quotas.items():
        ids = sorted(
            set(master.loc[master["board_type"] == board, "order_book_id"].astype(str)),
            key=lambda item: _stable_key(item, f"{salt}|{board}"),
        )
        selected.extend(ids[:count])
    target = sum(quotas.values())
    if len(selected) < target:
        remainder = sorted(
            set(master["order_book_id"].astype(str)) - set(selected),
            key=lambda item: _stable_key(item, f"{salt}|remainder"),
        )
        selected.extend(remainder[: target - len(selected)])
    return selected[:target]


def _canonical_frame_hash(value: Any) -> str | None:
    import pandas as pd

    if not isinstance(value, pd.DataFrame) or value.empty:
        return None
    frame = value.reset_index()
    frame.columns = [str(item) for item in frame.columns]
    frame = frame.reindex(sorted(frame.columns), axis=1)
    frame = frame.sort_values(list(frame.columns), kind="mergesort", na_position="first")
    payload = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def hashed_frame_summary(value: Any) -> dict[str, Any]:
    summary = frame_summary(value)
    summary["canonical_sha256"] = _canonical_frame_hash(value)
    return summary


def _date_series(value: Any, column: str):
    import pandas as pd

    if column not in value.columns:
        return pd.Series(pd.NaT, index=value.index, dtype="datetime64[ns]")
    normalized = value[column].astype(str).str.slice(0, 10)
    normalized = normalized.replace(
        {"0000-00-00": None, "None": None, "NaT": None, "nan": None}
    )
    return pd.to_datetime(normalized, format="%Y-%m-%d", errors="coerce")


def master_coverage_summary(value: Any) -> dict[str, Any]:
    import pandas as pd

    summary = universe_summary(value)
    if not isinstance(value, pd.DataFrame) or value.empty:
        return summary
    ids = value["order_book_id"].astype(str)
    listed = _date_series(value, "listed_date")
    delisted = _date_series(value, "de_listed_date")
    summary.update(
        {
            "unique_order_book_ids": int(ids.nunique()),
            "duplicate_order_book_ids": int(ids.duplicated().sum()),
            "listed_date_min": None if listed.dropna().empty else str(listed.min().date()),
            "listed_date_max": None if listed.dropna().empty else str(listed.max().date()),
            "finite_delisted_dates": int(delisted.notna().sum()),
            "delisted_before_listed": int(((delisted < listed) & delisted.notna()).sum()),
            "master_core_fields_sha256": _canonical_frame_hash(
                value[
                    [
                        column
                        for column in (
                            "order_book_id",
                            "symbol",
                            "exchange",
                            "board_type",
                            "status",
                            "listed_date",
                            "de_listed_date",
                            "round_lot",
                            "trading_hours",
                        )
                        if column in value.columns
                    ]
                ]
            ),
            "attribute_semantics": "LATEST_ATTRIBUTES_ONLY__NOT_HISTORICAL_PIT_MASTER",
        }
    )
    dynamic_counts: dict[str, int] = {}
    for anchor_text in DYNAMIC_POOL_ANCHORS:
        anchor = pd.Timestamp(anchor_text)
        eligible = listed.le(anchor) & (delisted.isna() | delisted.ge(anchor))
        dynamic_counts[anchor_text] = int(eligible.sum())
    summary["dynamic_pool_counts_from_latest_master"] = dynamic_counts
    return summary


def dividend_amount_summary(value: Any) -> dict[str, Any]:
    import pandas as pd

    summary = hashed_frame_summary(value)
    if not isinstance(value, pd.DataFrame) or value.empty:
        summary["event_procedure_counts"] = {}
        return summary
    if "event_procedure" in value.columns:
        counts = value["event_procedure"].astype(str).value_counts(dropna=False)
        summary["event_procedure_counts"] = {
            str(key): int(item) for key, item in counts.items()
        }
    if "info_date" in value.columns:
        dates = pd.to_datetime(value["info_date"], errors="coerce").dropna()
        summary["info_date_max"] = None if dates.empty else str(dates.max().date())
    return summary


def price_sample_summary(value: Any) -> dict[str, Any]:
    import pandas as pd

    summary = hashed_frame_summary(value)
    if not isinstance(value, pd.DataFrame):
        return summary
    summary["dataframe_memory_bytes"] = int(value.memory_usage(index=True, deep=True).sum())
    if isinstance(value.index, pd.MultiIndex):
        summary["returned_order_book_ids"] = int(
            value.index.get_level_values(0).nunique()
        )
        summary["returned_dates"] = int(value.index.get_level_values(-1).nunique())
    return summary


def quota_bytes(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    raw = value.get("bytes_used")
    return int(raw) if isinstance(raw, (int, float)) else None


def expected_history_rows(master: Any, calendar: Any) -> int | None:
    import pandas as pd

    if not isinstance(master, pd.DataFrame) or master.empty or calendar is None:
        return None
    dates = pd.DatetimeIndex(pd.to_datetime(list(calendar), errors="coerce")).dropna().sort_values()
    if dates.empty:
        return None
    listed = _date_series(master, "listed_date")
    delisted = _date_series(master, "de_listed_date")
    total = 0
    for start, end in zip(listed, delisted):
        if pd.isna(start):
            continue
        effective_start = max(pd.Timestamp(start), dates[0])
        effective_end = dates[-1] if pd.isna(end) else min(pd.Timestamp(end), dates[-1])
        if effective_end < effective_start:
            continue
        left = int(dates.searchsorted(effective_start, side="left"))
        right = int(dates.searchsorted(effective_end, side="right"))
        total += max(0, right - left)
    return int(total)


def _quota_summary(value: Any) -> dict[str, Any]:
    return {"numeric_fields": numeric_summary(value)}


def _segment_delta(start: Any, end: Any) -> int | None:
    left, right = quota_bytes(start), quota_bytes(end)
    if left is None or right is None:
        return None
    return max(0, right - left)


def build_live_payload() -> dict[str, Any]:
    import rqdatac

    payload: dict[str, Any] = {
        "schema_version": 1,
        "probe_id": "ricequant-r0-coverage-pit-volume-live-v1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "contract_id": "ricequant-r0-coverage-probe-v1",
        "purpose_ceiling": "INSPECTION_ONLY",
        "formal_effect": "NONE",
        "formal_admission": False,
        "formal_catalog_write": False,
        "research_use": False,
        "bulk_download": False,
        "credential_value_captured": False,
        "maximum_business_api_calls": MAX_BUSINESS_API_CALLS,
        "maximum_incremental_flow_bytes": MAX_INCREMENTAL_FLOW_BYTES,
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
                "r0_gate_status": "NOT_MET",
            }
        )
        return payload

    runner = ProbeRunner(rqdatac)
    q0 = runner.call("quota_q0", lambda: rqdatac.user.get_quota(), _quota_summary)
    master = runner.call(
        "core01_latest_master_all_cs",
        lambda: rqdatac.all_instruments(type="CS", date=None),
        master_coverage_summary,
    )
    calendar = runner.call(
        "core02_calendar_2000_to_current",
        lambda: rqdatac.get_trading_dates(CALENDAR_START, CALENDAR_END),
        trading_dates_summary,
    )
    q1 = runner.call("quota_q1_after_master_calendar", lambda: rqdatac.user.get_quota(), _quota_summary)

    runner.call(
        "core04_dividend_info_positive_anchors",
        lambda: rqdatac.get_dividend_info(list(ACTION_IDS), ACTION_START, ACTION_END),
        hashed_frame_summary,
    )
    runner.call(
        "core04_dividend_positive_anchors",
        lambda: rqdatac.get_dividend(list(ACTION_IDS), ACTION_START, ACTION_END, adjusted=False),
        hashed_frame_summary,
    )
    runner.call(
        "core04_split_positive_anchors",
        lambda: rqdatac.get_split(list(ACTION_IDS), ACTION_START, ACTION_END),
        hashed_frame_summary,
    )
    runner.call(
        "core04_allotment_positive_anchors",
        lambda: rqdatac.get_allotment(list(ACTION_IDS), ACTION_START, ACTION_END),
        hashed_frame_summary,
    )
    runner.call(
        "core05_ex_factor_positive_anchors",
        lambda: rqdatac.get_ex_factor(list(ACTION_IDS), ACTION_START, ACTION_END),
        hashed_frame_summary,
    )

    pit_values: dict[str, Any] = {}
    for date in PIT_DATES:
        pit_values[date] = runner.call(
            f"core04_dividend_amount_asof_{date}",
            lambda date=date: rqdatac.get_dividend_amount(
                "000001.XSHE", start_quarter="2018q4", end_quarter="2018q4", date=date
            ),
            dividend_amount_summary,
        )
    q2 = runner.call("quota_q2_after_actions", lambda: rqdatac.user.get_quota(), _quota_summary)

    small_ids = deterministic_board_sample(master, SMALL_BOARD_QUOTAS, "small-v1")
    large_ids = deterministic_board_sample(master, LARGE_BOARD_QUOTAS, "large-v1")
    calendar_list = list(calendar) if calendar is not None else []
    small_dates = calendar_list[-5:]
    large_dates = calendar_list[-60:]
    small = runner.call(
        "core03_price_small_32x5",
        lambda: rqdatac.get_price(
            small_ids,
            start_date=small_dates[0],
            end_date=small_dates[-1],
            frequency="1d",
            fields=list(PRICE_FIELDS),
            adjust_type="none",
            skip_suspended=False,
        ),
        price_sample_summary,
    )
    q3 = runner.call("quota_q3_after_small_price", lambda: rqdatac.user.get_quota(), _quota_summary)

    current_delta = _segment_delta(q0, q3)
    small_delta = _segment_delta(q2, q3)
    small_rows = 0 if small is None else int(len(small))
    estimated_large_bytes = (
        None
        if small_delta is None or small_rows == 0
        else math.ceil((small_delta / small_rows) * (len(large_ids) * len(large_dates)))
    )
    allow_large = (
        current_delta is not None
        and estimated_large_bytes is not None
        and current_delta + (2 * estimated_large_bytes) <= MAX_INCREMENTAL_FLOW_BYTES
    )
    large = None
    if allow_large:
        large = runner.call(
            "core03_price_large_256x60",
            lambda: rqdatac.get_price(
                large_ids,
                start_date=large_dates[0],
                end_date=large_dates[-1],
                frequency="1d",
                fields=list(PRICE_FIELDS),
                adjust_type="none",
                skip_suspended=False,
            ),
            price_sample_summary,
        )
    else:
        payload["large_price_sample"] = "SKIPPED_BY_8_MIB_PREFLIGHT_CAP"
    q4 = runner.call("quota_q4_final", lambda: rqdatac.user.get_quota(), _quota_summary)

    pit_rows = {
        date: 0 if value is None else int(len(value)) for date, value in pit_values.items()
    }
    pit_monotonic = list(pit_rows.values()) == sorted(pit_rows.values())
    history_rows = expected_history_rows(master, calendar)
    large_rows = 0 if large is None else int(len(large))
    large_delta = _segment_delta(q3, q4)
    quota_counter_stale = (small_rows > 0 and small_delta == 0) or (
        large_rows > 0 and large_delta == 0
    )
    warm_bytes_per_row = (
        None
        if quota_counter_stale or large_delta is None or large_rows == 0
        else large_delta / large_rows
    )
    cold_setup_bytes = (
        None
        if small_delta is None or warm_bytes_per_row is None
        else max(0.0, small_delta - warm_bytes_per_row * small_rows)
    )
    estimated_full_history_flow = None
    if history_rows is not None and warm_bytes_per_row is not None:
        batches = math.ceil(history_rows / max(1, large_rows))
        estimated_full_history_flow = int(
            warm_bytes_per_row * history_rows + (cold_setup_bytes or 0.0) * batches
        )

    final_delta = _segment_delta(q0, q4)
    payload.update(
        {
            "checks": runner.results,
            "business_api_calls_made": runner.calls,
            "empty_result_checks": [
                name for name, item in runner.results.items() if item["status"] == "PASS_EMPTY"
            ],
            "sample_design": {
                "small_requested_ids": len(small_ids),
                "small_requested_dates": len(small_dates),
                "small_returned_ids": 0
                if small is None
                else int(small.index.get_level_values(0).nunique()),
                "small_board_quotas": SMALL_BOARD_QUOTAS,
                "large_requested_ids": len(large_ids),
                "large_requested_dates": len(large_dates),
                "large_returned_ids": 0
                if large is None
                else int(large.index.get_level_values(0).nunique()),
                "large_board_quotas": LARGE_BOARD_QUOTAS,
                "stable_hash_sampling": True,
                "price_market_scope": "XSHG_XSHE_ONLY__BJSE_DAILY_BAR_PERMISSION_DENIED_IN_FIRST_ATTEMPT",
            },
            "pit_checks": {
                "asof_row_counts": pit_rows,
                "row_counts_monotonic": pit_monotonic,
                "interpretation": "BOUNDED_ASOF_BEHAVIOR_ONLY__NOT_COMPLETE_REVISION_CHAIN",
            },
            "flow_model": {
                "master_calendar_bytes": _segment_delta(q0, q1),
                "action_and_pit_bytes": _segment_delta(q1, q2),
                "small_price_bytes_including_quota_call": small_delta,
                "large_price_bytes_including_quota_call": large_delta,
                "total_incremental_bytes": final_delta,
                "flow_cap_respected": final_delta is not None and final_delta <= MAX_INCREMENTAL_FLOW_BYTES,
                "in_process_quota_counter_stale": quota_counter_stale,
                "flow_estimation_status": "POST_RUN_FRESH_PROCESS_RECONCILIATION_REQUIRED"
                if quota_counter_stale
                else "AVAILABLE",
                "warm_bytes_per_returned_row": warm_bytes_per_row,
                "cold_setup_bytes_estimate": cold_setup_bytes,
                "history_rows_from_returned_latest_master": history_rows,
                "estimated_full_history_transfer_bytes": estimated_full_history_flow,
                "estimate_basis": "SAMPLE_PROJECTION_ONLY__NOT_COVERAGE_OR_STABILITY_PROOF",
            },
            "formal_limits": [
                "LATEST_MASTER_ATTRIBUTES_ARE_NOT_HISTORICAL_PIT",
                "RICE_CREATE_TM_IS_NOT_ASSUMED_AVAILABLE_AT",
                "RQDATA_RIGHTS_AND_IMMUTABLE_EXPORT_NOT_VERIFIED",
                "SAMPLE_FLOW_ESTIMATE_IS_NOT_FULL_HISTORY_DOWNLOAD",
            ],
            "r0_gate_status": "NOT_MET__FORMAL_SNAPSHOTS_RIGHTS_PIT_AND_L2_GATES_ABSENT",
        }
    )
    statuses = [item["status"] for item in runner.results.values()]
    payload["status"] = (
        (
            "COVERAGE_PIT_CAPABILITY_PASS__FLOW_COUNTER_STALE_RECONCILIATION_REQUIRED__FORMAL_EFFECT_NONE"
            if quota_counter_stale
            else "COVERAGE_PIT_VOLUME_CAPABILITY_PASS__FORMAL_EFFECT_NONE"
        )
        if statuses
        and all(status in {"PASS", "PASS_EMPTY"} for status in statuses)
        and payload["flow_model"]["flow_cap_respected"]
        else "PARTIAL__CAPABILITY_OR_FLOW_GUARD_NOT_MET__FORMAL_EFFECT_NONE"
    )
    return payload


def offline_check() -> dict[str, Any]:
    import rqdatac

    names = (
        "all_instruments",
        "get_trading_dates",
        "get_dividend_info",
        "get_dividend",
        "get_split",
        "get_allotment",
        "get_ex_factor",
        "get_dividend_amount",
        "get_price",
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
        description="Probe bounded Ricequant R0 coverage, PIT behavior and flow volume."
    )
    parser.add_argument("--offline-check", action="store_true")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = offline_check() if args.offline_check else build_live_payload()
    if not args.offline_check:
        output_path = validate_output_path(args.output)
        payload["output_path"] = str(output_path)
        write_json_atomic(output_path, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if args.offline_check:
        return 0 if payload.get("status") == "OFFLINE_ENTRYPOINT_CHECK_PASS" else 2
    return 0 if str(payload.get("status", "")).endswith("FORMAL_EFFECT_NONE") and not str(payload.get("status", "")).startswith("PARTIAL") else 2


if __name__ == "__main__":
    raise SystemExit(main())
