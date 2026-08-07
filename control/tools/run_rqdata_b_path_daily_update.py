#!/usr/bin/env python3
"""Build a bounded RQData B-path daily quarantine candidate.

The program is intentionally not a formal publisher.  ``--plan`` is local,
read-only and network-free.  ``--run`` is fail-closed and may only seal an
``INSPECTION_ONLY`` candidate; it never edits catalog or Roadmap authority,
promotes quality, runs factors, creates a scheduler, or captures credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

import run_rqdata_shsz_daily_update as pv


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "data/catalog/contracts/rqdata_b_path_daily_candidate_v1/contract.json"
CANDIDATE_PARENT = ROOT / "data/market/quarantine/rqdata-b-path-daily"
TRANSACTION_PARENT = ROOT / "data/market/transactions/rqdata-b-path-daily"
PRODUCER_ID = "run_rqdata_b_path_daily_update.py/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ACTION_ENDPOINTS = (
    "get_dividend_info",
    "get_dividend",
    "get_split",
    "get_allotment",
    "get_ex_factor",
)


class UpdateError(RuntimeError):
    """The candidate must not be committed."""


def load_contract() -> dict[str, Any]:
    contract = pv.load_json(CONTRACT_PATH)
    if contract.get("contract_id") != "rqdata_b_path_daily_candidate_v1":
        raise UpdateError("unexpected B-path updater contract")
    safety = contract.get("safety", {})
    forbidden_true = (
        "catalog_write", "roadmap_write", "source_registry_write",
        "quality_promotion", "formal_snapshot_write", "research_use",
        "credential_value_capture", "scheduled_task_creation",
    )
    if any(safety.get(name) is not False for name in forbidden_true):
        raise UpdateError("candidate-only safety contract is not fail-closed")
    if safety.get("purpose_ceiling") != "INSPECTION_ONLY":
        raise UpdateError("candidate purpose ceiling must be INSPECTION_ONLY")
    return contract


def chunks(values: Sequence[str], size: int) -> Iterable[list[str]]:
    if size <= 0:
        raise UpdateError("chunk size must be positive")
    for start in range(0, len(values), size):
        yield list(values[start:start + size])


def clean_frame(value: Any, value_name: str = "value") -> pd.DataFrame:
    if value is None:
        return pd.DataFrame()
    if isinstance(value, pd.Series):
        frame = value.rename(str(value.name or value_name)).to_frame().reset_index()
    elif isinstance(value, pd.DataFrame):
        frame = value.reset_index()
    elif isinstance(value, (list, tuple)) and all(isinstance(item, Mapping) for item in value):
        frame = pd.DataFrame(list(value))
    else:
        raise UpdateError(f"unsupported endpoint response type: {type(value).__name__}")
    columns: list[str] = []
    seen: dict[str, int] = {}
    for raw in frame.columns:
        base = str(raw or "index")
        number = seen.get(base, 0)
        seen[base] = number + 1
        columns.append(base if number == 0 else f"{base}_{number}")
    frame.columns = columns
    for column in frame.columns:
        if frame[column].dtype == "object":
            frame[column] = frame[column].map(_csv_scalar)
    return frame


def _csv_scalar(value: Any) -> Any:
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(pv.json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return value


def write_csv(path: Path, frame: pd.DataFrame) -> None:
    payload = frame.to_csv(index=False, lineterminator="\n", float_format="%.12g").encode("utf-8")
    pv.atomic_write(path, payload)


def count_rows(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], (list, tuple, set)):
        return len(value[0])
    if isinstance(value, (pd.DataFrame, pd.Series, list, tuple, set, dict)):
        return len(value)
    return 1


@dataclass
class ApiBudget:
    maximum_calls: int
    maximum_rows: int
    calls: int = 0
    rows: int = 0
    observations: list[dict[str, Any]] = field(default_factory=list)

    def call(self, name: str, function: Callable[[], Any]) -> Any:
        if self.calls >= self.maximum_calls:
            raise UpdateError(f"business API call cap reached before {name}")
        self.calls += 1
        value = function()
        rows = count_rows(value)
        if self.rows + rows > self.maximum_rows:
            raise UpdateError(f"returned row cap exceeded by {name}")
        self.rows += rows
        self.observations.append({"name": name, "rows": rows, "status": "PASS_EMPTY" if rows == 0 else "PASS"})
        return value


def normalize_master(frame: Any, start: date, end: date) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise UpdateError("all_instruments did not return a DataFrame")
    value = frame.copy()
    if "order_book_id" not in value.columns or "listed_date" not in value.columns:
        raise UpdateError("stock master identity/listed_date fields missing")
    value["order_book_id"] = value["order_book_id"].astype(str)
    value = value[value["order_book_id"].map(pv.is_shsz_a_share)].copy()
    value["listed_date"] = value["listed_date"].map(pv.date_or_none)
    if "de_listed_date" not in value.columns:
        value["de_listed_date"] = None
    value["de_listed_date"] = value["de_listed_date"].map(pv.date_or_none)
    value = value[
        value["listed_date"].notna()
        & (value["listed_date"] <= end)
        & (value["de_listed_date"].isna() | (value["de_listed_date"] >= start))
    ].copy()
    if value["order_book_id"].duplicated().any():
        raise UpdateError("duplicate current-master order_book_id")
    preferred = [
        "order_book_id", "symbol", "abbrev_symbol", "type", "exchange",
        "status", "listed_date", "de_listed_date", "board_type",
        "special_type", "sector_code", "industry_code",
    ]
    value = value[[column for column in preferred if column in value.columns]].copy()
    value["snapshot_semantics"] = "LATEST_ATTRIBUTES_OBSERVED_AT_RUN_TIME__NOT_HISTORICAL_PIT_MASTER"
    return value.sort_values("order_book_id", kind="mergesort").reset_index(drop=True)


def normalize_transformations(value: Any, observed_at: str) -> pd.DataFrame:
    frame = clean_frame(value)
    if frame.empty:
        return pd.DataFrame(columns=["predecessor", "successor", "source_observed_at_utc"])
    if not {"predecessor", "successor"}.issubset(frame.columns):
        raise UpdateError("share transformation identity fields missing")
    keep = frame["predecessor"].astype(str).map(pv.is_shsz_a_share) | frame["successor"].astype(str).map(pv.is_shsz_a_share)
    frame = frame[keep].copy()
    frame["source_observed_at_utc"] = observed_at
    frame["formal_available_at"] = pd.NA
    return frame.sort_values(["predecessor", "successor"], kind="mergesort").reset_index(drop=True)


def near(left: pd.Series, right: pd.Series) -> pd.Series:
    return pd.Series(np.isclose(left, right, rtol=1e-10, atol=1e-10, equal_nan=False), index=left.index)


def build_daily_status(
    price: pd.DataFrame,
    st: pd.DataFrame,
    suspended: pd.DataFrame,
    master: pd.DataFrame,
    next_dates: Mapping[date, date],
) -> pd.DataFrame:
    value = price.merge(st, how="left", on=["order_book_id", "trade_date"], validate="one_to_one")
    value = value.merge(suspended, how="left", on=["order_book_id", "trade_date"], validate="one_to_one")
    value = value.merge(
        master[["order_book_id", "listed_date", "de_listed_date"]],
        how="left", on="order_book_id", validate="many_to_one",
    )
    if value[["is_st", "is_suspended"]].isna().any().any():
        raise UpdateError("ST or suspension state is incomplete")
    value["listed_episode_eligible"] = (
        value["listed_date"].notna()
        & (value["trade_date"] >= value["listed_date"])
        & (value["de_listed_date"].isna() | (value["trade_date"] <= value["de_listed_date"]))
    )
    value["had_trade"] = value["volume"].fillna(0).gt(0) & value["total_turnover"].fillna(0).gt(0)
    value["limit_up_touched"] = value["limit_up"].notna() & value["high"].ge(value["limit_up"])
    value["limit_down_touched"] = value["limit_down"].notna() & value["low"].le(value["limit_down"])
    value["closed_at_limit_up"] = near(value["close"], value["limit_up"])
    value["closed_at_limit_down"] = near(value["close"], value["limit_down"])
    value["whole_session_locked_limit_up"] = (
        near(value["open"], value["limit_up"])
        & near(value["high"], value["limit_up"])
        & near(value["low"], value["limit_up"])
        & near(value["close"], value["limit_up"])
    )
    value["whole_session_locked_limit_down"] = (
        near(value["open"], value["limit_down"])
        & near(value["high"], value["limit_down"])
        & near(value["low"], value["limit_down"])
        & near(value["close"], value["limit_down"])
    )
    value["daily_bar_cannot_prove_intraday_queue_fill"] = True
    value["next_trade_date"] = value["trade_date"].map(next_dates)
    if value["next_trade_date"].isna().any():
        raise UpdateError("next trading date missing from status projection")
    value["available_at"] = pd.to_datetime(
        value["next_trade_date"].astype(str) + " 09:30:00"
    ).dt.tz_localize(pv.SHANGHAI)
    value = value.rename(columns={
        "prev_close": "previous_close", "volume": "volume_shares",
        "total_turnover": "turnover_cny", "limit_up": "upper_limit_price",
        "limit_down": "lower_limit_price",
    })
    fields = [
        "order_book_id", "trade_date", "previous_close", "open", "high", "low", "close",
        "volume_shares", "turnover_cny", "upper_limit_price", "lower_limit_price",
        "is_st", "is_suspended", "listed_episode_eligible", "had_trade",
        "limit_up_touched", "limit_down_touched", "closed_at_limit_up",
        "closed_at_limit_down", "whole_session_locked_limit_up",
        "whole_session_locked_limit_down", "daily_bar_cannot_prove_intraday_queue_fill",
        "next_trade_date", "available_at",
    ]
    result = value[fields].sort_values(["trade_date", "order_book_id"], kind="mergesort")
    if result.duplicated(["order_book_id", "trade_date"]).any():
        raise UpdateError("duplicate daily status key")
    return result.reset_index(drop=True)


def collect_actions(
    client: Any,
    budget: ApiBudget,
    ids: list[str],
    start: date,
    end: date,
    batch_size: int,
    observed_at: str,
) -> pd.DataFrame:
    tables: list[pd.DataFrame] = []
    for batch_number, batch in enumerate(chunks(ids, batch_size), start=1):
        calls = {
            "get_dividend_info": lambda b=batch: client.get_dividend_info(b, start.isoformat(), end.isoformat()),
            "get_dividend": lambda b=batch: client.get_dividend(b, start.isoformat(), end.isoformat()),
            "get_split": lambda b=batch: client.get_split(b, start.isoformat(), end.isoformat()),
            "get_allotment": lambda b=batch: client.get_allotment(b, start.isoformat(), end.isoformat()),
            "get_ex_factor": lambda b=batch: client.get_ex_factor(b, start.isoformat(), end.isoformat()),
        }
        for endpoint in ACTION_ENDPOINTS:
            response = budget.call(f"{endpoint}[{batch_number}]", calls[endpoint])
            frame = clean_frame(response)
            if frame.empty:
                continue
            frame.insert(0, "source_endpoint", endpoint)
            frame["query_start_date"] = start.isoformat()
            frame["query_end_date"] = end.isoformat()
            frame["source_observed_at_utc"] = observed_at
            frame["formal_available_at"] = pd.NA
            frame["revision_chain_status"] = "OBSERVATION_ONLY__CORRECTION_WITHDRAWAL_SUPERSESSION_UNPROVEN"
            tables.append(frame)
    if not tables:
        return pd.DataFrame(columns=[
            "source_endpoint", "query_start_date", "query_end_date",
            "source_observed_at_utc", "formal_available_at", "revision_chain_status",
        ])
    result = pd.concat(tables, ignore_index=True, sort=False)
    sort_fields = [field for field in ("source_endpoint", "order_book_id", "info_date", "declaration_announcement_date", "ex_date") if field in result.columns]
    return result.sort_values(sort_fields, kind="mergesort", na_position="last").reset_index(drop=True)


def collect_industry(
    client: Any, budget: ApiBudget, ids: list[str], dates: list[date], source: str,
) -> pd.DataFrame:
    tables: list[pd.DataFrame] = []
    for day in dates:
        response = budget.call(
            f"get_instrument_industry[{day}]",
            lambda d=day: client.get_instrument_industry(ids, source=source, level=0, date=d.isoformat()),
        )
        frame = clean_frame(response)
        if frame.empty or "order_book_id" not in frame.columns:
            raise UpdateError(f"empty or malformed Shenwan industry response for {day}")
        frame["as_of_date"] = day.isoformat()
        frame["classification_source"] = source
        frame["formal_available_at"] = pd.NA
        frame["pit_status"] = "SOURCE_AS_OF_QUERY__LEGAL_AVAILABLE_AT_AND_REVISION_CHAIN_UNPROVEN"
        if frame["order_book_id"].duplicated().any():
            raise UpdateError(f"duplicate industry assignment for {day}")
        tables.append(frame)
    return pd.concat(tables, ignore_index=True, sort=False).sort_values(
        ["as_of_date", "order_book_id"], kind="mergesort"
    ).reset_index(drop=True)


def normalize_components(index_id: str, day: date, value: Any) -> pd.DataFrame:
    create_tm: Any = None
    components = value
    if isinstance(value, tuple) and len(value) == 2:
        components, create_tm = value
    if not isinstance(components, (list, tuple, set)) or not components:
        raise UpdateError(f"empty index components: {index_id} {day}")
    ids = sorted({str(item) for item in components})
    return pd.DataFrame({
        "index_order_book_id": index_id,
        "as_of_date": day.isoformat(),
        "component_order_book_id": ids,
        "rice_create_tm_vendor_metadata": _csv_scalar(create_tm),
        "formal_available_at": pd.NA,
    })


def normalize_weights(index_id: str, day: date, value: Any) -> pd.DataFrame:
    frame = clean_frame(value, "weight")
    if frame.empty:
        raise UpdateError(f"empty index weights: {index_id} {day}")
    id_candidates = [column for column in frame.columns if "order_book" in column.lower()]
    if not id_candidates:
        raise UpdateError(f"index weight identity missing: {index_id} {day}")
    weight_candidates = [column for column in frame.columns if column.lower() == "weight"]
    if not weight_candidates:
        numeric = [column for column in frame.columns if pd.api.types.is_numeric_dtype(frame[column])]
        if len(numeric) != 1:
            raise UpdateError(f"index weight value field ambiguous: {index_id} {day}")
        weight_candidates = numeric
    frame = frame.rename(columns={id_candidates[0]: "component_order_book_id", weight_candidates[0]: "weight"})
    frame["component_order_book_id"] = frame["component_order_book_id"].astype(str)
    frame["weight"] = pd.to_numeric(frame["weight"], errors="raise")
    frame["index_order_book_id"] = index_id
    frame["as_of_date"] = day.isoformat()
    frame["formal_available_at"] = pd.NA
    return frame[[
        "index_order_book_id", "as_of_date", "component_order_book_id", "weight", "formal_available_at"
    ]].sort_values("component_order_book_id", kind="mergesort").reset_index(drop=True)


def collect_indices(
    client: Any, budget: ApiBudget, index_ids: list[str], dates: list[date],
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict[str, Any]]]:
    components: list[pd.DataFrame] = []
    weights: list[pd.DataFrame] = []
    checks: list[dict[str, Any]] = []
    for day in dates:
        for index_id in index_ids:
            comp = normalize_components(
                index_id, day,
                budget.call(
                    f"index_components[{index_id},{day}]",
                    lambda i=index_id, d=day: client.index_components(i, date=d.isoformat(), return_create_tm=True),
                ),
            )
            weight = normalize_weights(
                index_id, day,
                budget.call(
                    f"index_weights[{index_id},{day}]",
                    lambda i=index_id, d=day: client.index_weights(i, date=d.isoformat()),
                ),
            )
            left = set(comp["component_order_book_id"])
            right = set(weight["component_order_book_id"])
            if not left or len(left & right) / len(left | right) < 0.95:
                raise UpdateError(f"component/weight mismatch: {index_id} {day}")
            weight_sum = float(weight["weight"].sum())
            if not (0.8 <= weight_sum <= 1.2 or 80 <= weight_sum <= 120):
                raise UpdateError(f"index weights do not sum near 1 or 100: {index_id} {day}: {weight_sum}")
            checks.append({
                "index_order_book_id": index_id, "as_of_date": day.isoformat(),
                "components": len(left), "weights": len(right), "intersection": len(left & right),
                "weight_sum": weight_sum,
            })
            components.append(comp)
            weights.append(weight)
    return (
        pd.concat(components, ignore_index=True),
        pd.concat(weights, ignore_index=True),
        checks,
    )


def build_financial_queue(
    ids: list[str], as_of: date, fields: list[str], lookback_quarters: int, batch_size: int,
) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = []
    for number, batch in enumerate(chunks(ids, batch_size), start=1):
        request = {
            "endpoint": "get_pit_financials_ex",
            "order_book_ids": batch,
            "fields": fields,
            "as_of_date": as_of.isoformat(),
            "lookback_quarters": lookback_quarters,
        }
        request_hash = hashlib.sha256(pv.canonical_bytes(request)).hexdigest()
        tasks.append({
            "task_id": f"financial-pit-{as_of:%Y%m%d}-{number:04d}-{request_hash[:12]}",
            "request": request,
            "request_sha256": request_hash,
            "state": "QUEUED_NOT_EXECUTED",
            "attempts": 0,
            "checkpoint": None,
            "payload_path": None,
        })
    flattened = [oid for task in tasks for oid in task["request"]["order_book_ids"]]
    if flattened != ids or len(flattened) != len(set(flattened)):
        raise UpdateError("financial queue does not exactly partition the universe")
    return {
        "schema_version": 1,
        "queue_id": f"rqdata-financial-pit-{as_of:%Y%m%d}-v1",
        "status": "QUEUED_NOT_EXECUTED__NO_FINANCIAL_API_CALLS",
        "resume_contract": "TASK_STATE_AND_REQUEST_SHA256_MUST_MATCH_BEFORE_RETRY",
        "task_count": len(tasks),
        "security_count": len(ids),
        "completed_tasks": 0,
        "tasks": tasks,
        "formal_available_at_proven": False,
        "revision_chain_proven": False,
        "formal_effect": "NONE",
    }


def numeric_quota(value: Any, name: str) -> int | None:
    found = pv.quota_number(value, name)
    return None if found is None else int(round(found))


def enforce_initial_quota(quota: Any, minimum_remaining: int) -> None:
    limit = numeric_quota(quota, "bytes_limit")
    used = numeric_quota(quota, "bytes_used")
    if limit is None or used is None:
        raise UpdateError("quota bytes_limit/bytes_used unavailable")
    if limit != 0 and limit - used < minimum_remaining:
        raise UpdateError(f"insufficient quota remaining: {limit - used} < {minimum_remaining}")


def candidate_watermark() -> date:
    values = [pv.formal_watermark()]
    if CANDIDATE_PARENT.is_dir():
        for path in sorted(CANDIDATE_PARENT.iterdir()):
            if path.is_dir() and re.fullmatch(r"delta-\d{8}-\d{8}-v1", path.name):
                values.append(date.fromisoformat(verify_candidate(path)["date_end"]))
    return max(values)


def verify_candidate(path: Path) -> dict[str, Any]:
    manifest = pv.load_json(path / "manifest.json")
    if manifest.get("producer") != PRODUCER_ID or manifest.get("status") != "PASS__SEALED_B_PATH_CANDIDATE":
        raise UpdateError(f"untrusted B-path candidate manifest: {path}")
    artifacts = manifest.get("artifacts", [])
    if not isinstance(artifacts, list) or not artifacts:
        raise UpdateError(f"candidate artifact list missing: {path}")
    seen: set[str] = set()
    for item in artifacts:
        relative = str(item.get("path", ""))
        target = (path / relative).resolve()
        if not target.is_relative_to(path.resolve()):
            raise UpdateError(f"candidate artifact escapes candidate root: {relative}")
        if relative in seen:
            raise UpdateError(f"duplicate candidate artifact reference: {relative}")
        seen.add(relative)
        digest = item.get("sha256")
        if not target.is_file() or target.stat().st_size != item.get("bytes"):
            raise UpdateError(f"candidate artifact size mismatch: {target}")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest) or pv.sha256_file(target) != digest:
            raise UpdateError(f"candidate artifact hash mismatch: {target}")
    return manifest


def artifact_ref(path: Path, base: Path) -> dict[str, Any]:
    return pv.file_ref(path, base)


def plan(through: date) -> dict[str, Any]:
    contract = load_contract()
    watermark = candidate_watermark()
    return {
        "schema_version": 1,
        "status": "PLAN__NO_NETWORK_NO_WRITE",
        "producer": PRODUCER_ID,
        "through": through.isoformat(),
        "base_formal_watermark": pv.formal_watermark().isoformat(),
        "effective_candidate_watermark": watermark.isoformat(),
        "calendar_gap_days": max(0, (through - watermark).days),
        "maximum_catch_up_trading_dates": contract["scope"]["maximum_catch_up_trading_dates"],
        "maximum_business_api_calls": contract["limits"]["maximum_business_api_calls"],
        "maximum_measured_quota_increment_bytes": contract["limits"]["maximum_measured_quota_increment_bytes"],
        "candidate_root": CANDIDATE_PARENT.resolve().relative_to(ROOT.resolve()).as_posix(),
        "financial_pit": "QUEUE_ONLY__NOT_EXECUTED",
        "formal_effect": "NONE",
        "catalog_write": False,
        "roadmap_write": False,
        "scheduled_task_creation": False,
    }


def quality_summary(
    master: pd.DataFrame, status: pd.DataFrame, actions: pd.DataFrame,
    industry: pd.DataFrame, index_checks: list[dict[str, Any]], dates: list[date],
    contract: Mapping[str, Any], budget: ApiBudget, quota_increment: int,
) -> dict[str, Any]:
    limits = contract["limits"]
    ids = set(master["order_book_id"])
    industry_ratios: dict[str, float] = {}
    for day in dates:
        observed = set(industry.loc[industry["as_of_date"] == day.isoformat(), "order_book_id"])
        industry_ratios[day.isoformat()] = len(observed & ids) / len(ids) if ids else 0.0
    status_dates = sorted(status["trade_date"].unique().tolist())
    checks = {
        "universe_bounded": limits["minimum_universe_rows"] <= len(master) <= limits["maximum_universe_rows"],
        "master_unique": not master["order_book_id"].duplicated().any(),
        "status_dates_exact": status_dates == dates,
        "status_key_unique": not status.duplicated(["order_book_id", "trade_date"]).any(),
        "st_and_suspension_complete": bool(status[["is_st", "is_suspended"]].notna().all().all()),
        "next_session_complete": bool(status["next_trade_date"].notna().all()),
        "industry_coverage": all(ratio >= limits["minimum_industry_coverage_ratio"] for ratio in industry_ratios.values()),
        "all_configured_index_date_pairs_present": len(index_checks) == len(dates) * len(contract["scope"]["core_indices"]),
        "business_call_cap": budget.calls <= limits["maximum_business_api_calls"],
        "returned_row_cap": budget.rows <= limits["maximum_returned_rows"],
        "measured_quota_cap": quota_increment <= limits["maximum_measured_quota_increment_bytes"],
    }
    return {
        "schema_version": 1,
        "mechanical_status": "PASS" if all(checks.values()) else "FAIL_CLOSED",
        "checks": checks,
        "master_rows": len(master),
        "status_rows": len(status),
        "action_observation_rows": len(actions),
        "industry_rows": len(industry),
        "industry_coverage_ratio_by_date": industry_ratios,
        "index_checks": index_checks,
        "business_api_calls": budget.calls,
        "returned_rows_measured": budget.rows,
        "quota_increment_bytes": quota_increment,
        "financial_api_calls": 0,
        "factor_runs": 0,
        "formal_effect": "NONE__QUARANTINE_ONLY",
    }


def run(through: date, client: Any | None = None) -> dict[str, Any]:
    contract = load_contract()
    descriptor = pv.acquire_lock()
    staging: Path | None = None
    try:
        watermark = candidate_watermark()
        if through <= watermark:
            return {"status": "NO_CHANGE__WATERMARK_CURRENT__NO_NETWORK", "watermark": watermark.isoformat()}
        if client is None:
            if str(pv.RQ_SITE) not in sys.path:
                sys.path.insert(0, str(pv.RQ_SITE))
            import rqdatac
            rqdatac.init(enable_bjse=False)
            client = rqdatac
        limits = contract["limits"]
        budget = ApiBudget(
            maximum_calls=int(limits["maximum_business_api_calls"]),
            maximum_rows=int(limits["maximum_returned_rows"]),
        )
        quota_before = client.user.get_quota()
        enforce_initial_quota(quota_before, int(limits["minimum_quota_remaining_before_run_bytes"]))
        calendar_end = through + timedelta(days=14)
        calendar = sorted(set(budget.call(
            "get_trading_dates",
            lambda: client.get_trading_dates(watermark.isoformat(), calendar_end.isoformat()),
        )))
        delta_dates = [day for day in calendar if watermark < day <= through]
        if not delta_dates:
            return {"status": "NO_CHANGE__NO_TRADING_DATE", "watermark": watermark.isoformat()}
        maximum_dates = int(contract["scope"]["maximum_catch_up_trading_dates"])
        if len(delta_dates) > maximum_dates:
            raise UpdateError(f"catch-up gap has {len(delta_dates)} trading dates; cap is {maximum_dates}")
        next_dates: dict[date, date] = {}
        for day in delta_dates:
            later = [candidate for candidate in calendar if candidate > day]
            if not later:
                raise UpdateError(f"next session unavailable after {day}")
            next_dates[day] = later[0]

        candidate_id = f"delta-{delta_dates[0]:%Y%m%d}-{delta_dates[-1]:%Y%m%d}-v1"
        final = CANDIDATE_PARENT / candidate_id
        if final.exists():
            return {"status": "NO_CHANGE__CANDIDATE_EXISTS", "candidate": str(final)}
        CANDIDATE_PARENT.mkdir(parents=True, exist_ok=True)
        staging = CANDIDATE_PARENT / f".{candidate_id}.staging-{uuid.uuid4().hex}"
        staging.mkdir(parents=False, exist_ok=False)
        observed_at = datetime.now(timezone.utc).isoformat()

        master = normalize_master(
            budget.call("all_instruments", lambda: client.all_instruments(type="CS")),
            delta_dates[0], delta_dates[-1],
        )
        if not limits["minimum_universe_rows"] <= len(master) <= limits["maximum_universe_rows"]:
            raise UpdateError(f"unexpected SH/SZ universe size: {len(master)}")
        ids = sorted(master["order_book_id"].tolist())
        transformations = normalize_transformations(
            budget.call("get_share_transformation", lambda: client.get_share_transformation()),
            observed_at,
        )
        start, end = delta_dates[0], delta_dates[-1]
        price = pv.normalize_price(budget.call(
            "get_price",
            lambda: client.get_price(
                ids, start_date=start.isoformat(), end_date=end.isoformat(), frequency="1d",
                fields=list(pv.PRICE_FIELDS), adjust_type="none", skip_suspended=False, expect_df=True,
            ),
        ))
        st = pv.normalize_boolean(
            budget.call("is_st_stock", lambda: client.is_st_stock(ids, start.isoformat(), end.isoformat())),
            "is_st",
        )
        suspended = pv.normalize_boolean(
            budget.call("is_suspended", lambda: client.is_suspended(ids, start.isoformat(), end.isoformat())),
            "is_suspended",
        )
        status = build_daily_status(price, st, suspended, master, next_dates)

        action_start = start - timedelta(days=int(contract["scope"]["corporate_action_lookback_calendar_days"]))
        actions = collect_actions(
            client, budget, ids, action_start, end,
            int(limits["action_security_batch_size"]), observed_at,
        )
        industry_scope = contract["scope"]["industry"]
        industry = collect_industry(
            client, budget, ids, delta_dates, str(industry_scope["source"]),
        )
        components, weights, index_checks = collect_indices(
            client, budget, list(contract["scope"]["core_indices"]), delta_dates,
        )
        financial_scope = contract["scope"]["financial_pit"]
        financial_queue = build_financial_queue(
            ids, end, list(financial_scope["fields"]), int(financial_scope["lookback_quarters"]),
            int(financial_scope["task_security_batch_size"]),
        )

        quota_after = client.user.get_quota()
        before_used = numeric_quota(quota_before, "bytes_used")
        after_used = numeric_quota(quota_after, "bytes_used")
        if before_used is None or after_used is None:
            raise UpdateError("quota bytes_used unavailable")
        quota_increment = max(0, after_used - before_used)
        quality = quality_summary(
            master, status, actions, industry, index_checks, delta_dates,
            contract, budget, quota_increment,
        )
        if quality["mechanical_status"] != "PASS":
            raise UpdateError("B-path mechanical gate failed: " + json.dumps(quality["checks"], sort_keys=True))

        artifacts: list[Path] = []
        tables = {
            "master/current_master.csv": master,
            "master/share_transformations.csv": transformations,
            "status/daily_status.csv": status,
            "actions/rolling_observations.csv": actions,
            "industry/shenwan_asof_assignments.csv": industry,
            "indices/components.csv": components,
            "indices/weights.csv": weights,
        }
        for relative, frame in tables.items():
            target = staging / relative
            write_csv(target, frame)
            artifacts.append(target)
        pv.write_json(staging / "financial/queue.json", financial_queue)
        pv.write_json(staging / "quality/summary.json", quality)
        acquisition = {
            "schema_version": 1,
            "observed_at_utc": observed_at,
            "source": "RQDATA",
            "date_start": start.isoformat(),
            "date_end": end.isoformat(),
            "action_query_start": action_start.isoformat(),
            "business_api_calls": budget.calls,
            "endpoint_observations": budget.observations,
            "quota_before_numeric_only": pv.numeric_only(quota_before),
            "quota_after_numeric_only": pv.numeric_only(quota_after),
            "quota_increment_bytes": quota_increment,
            "financial_api_calls": 0,
            "credential_value_read": False,
            "credential_value_captured": False,
            "formal_effect": "NONE__QUARANTINE_ONLY",
        }
        pv.write_json(staging / "ACQUISITION_RECEIPT.json", acquisition)
        artifacts.extend([
            staging / "financial/queue.json",
            staging / "quality/summary.json",
            staging / "ACQUISITION_RECEIPT.json",
        ])
        total_bytes = sum(path.stat().st_size for path in artifacts)
        if total_bytes > int(limits["maximum_candidate_bytes"]):
            raise UpdateError(f"candidate byte cap exceeded: {total_bytes}")
        refs = [artifact_ref(path, staging) for path in sorted(artifacts, key=lambda item: item.as_posix())]
        manifest = {
            "schema_version": 1,
            "producer": PRODUCER_ID,
            "contract_id": contract["contract_id"],
            "candidate_id": candidate_id,
            "status": "PASS__SEALED_B_PATH_CANDIDATE",
            "purpose_ceiling": "INSPECTION_ONLY",
            "quality_status": "NOT_ADMITTED",
            "source_admission": False,
            "formal_catalog_write": False,
            "roadmap_write": False,
            "research_ready": False,
            "date_start": start.isoformat(),
            "date_end": end.isoformat(),
            "base_watermark": watermark.isoformat(),
            "artifacts": refs,
            "content_hash": hashlib.sha256(pv.canonical_bytes(refs)).hexdigest(),
            "financial_pit": "QUEUED_NOT_EXECUTED",
            "formal_available_at_complete": False,
            "revision_chain_complete": False,
            "factor_runs": 0,
        }
        pv.write_json(staging / "manifest.json", manifest)
        os.replace(staging, final)
        staging = None
        verified = verify_candidate(final)
        transaction = {
            "schema_version": 1,
            "status": "PASS__B_PATH_CANDIDATE_SEALED",
            "candidate": final.resolve().relative_to(ROOT.resolve()).as_posix(),
            "manifest_sha256": pv.sha256_file(final / "manifest.json"),
            "date_start": verified["date_start"],
            "date_end": verified["date_end"],
            "business_api_calls": budget.calls,
            "quota_increment_bytes": quota_increment,
            "financial_api_calls": 0,
            "formal_effect": "NONE",
            "factor_runs": 0,
        }
        pv.write_json(TRANSACTION_PARENT / f"rqdata-b-path-{start:%Y%m%d}-{end:%Y%m%d}-v1.json", transaction)
        return transaction
    except Exception as exc:
        if staging is not None and staging.exists():
            pv.write_json(staging / "FAILURE.json", {
                "status": "FAIL_CLOSED",
                "observed_at_utc": datetime.now(timezone.utc),
                "error": pv.safe_error(exc),
                "formal_effect": "NONE",
                "factor_runs": 0,
            })
            failed = CANDIDATE_PARENT / f"failed-{datetime.now(pv.SHANGHAI):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
            os.replace(staging, failed)
        raise
    finally:
        pv.release_lock(descriptor)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--through", type=date.fromisoformat)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    through = args.through or pv.default_through()
    try:
        result = plan(through) if args.plan else run(through)
    except Exception as exc:
        print(json.dumps({"status": "FAIL_CLOSED", "error": pv.safe_error(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(pv.json_safe(result), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
