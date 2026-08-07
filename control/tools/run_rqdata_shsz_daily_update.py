#!/usr/bin/env python3
"""Fetch and seal bounded RQData SH/SZ daily price-volume deltas.

The updater is deliberately fail-closed.  It writes immutable, verified delta
candidates under ``data/market/quarantine`` and never edits the formal catalog,
promotes quality, runs factors, or persists credential values.  Formal snapshot
promotion remains a separate atomic publication step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import msvcrt
import os
import re
import shutil
import sys
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
RQ_SITE = ROOT / "control/runtime/ricequant-rqsdk/.venv/Lib/site-packages"
CANDIDATE_PARENT = ROOT / "data/market/quarantine/r0-shsz-pv-daily-delta"
TRANSACTION_PARENT = ROOT / "data/market/transactions/rqdata-shsz-daily"
LOCK_PATH = TRANSACTION_PARENT / ".update.lock"
CATALOG_PATH = ROOT / "data/catalog/datasets.json"
DATASET_ID = "cn-a-share-r0-shsz-pv-panel"
PRODUCER_ID = "run_rqdata_shsz_daily_update.py/v1"
PANEL_SCHEMA = "r0-shsz-pv-panel-v2"
MAX_TRADING_DATES = 5
MAX_QUOTA_BYTES = 35 * 1024 * 1024
MIN_UNIVERSE_ROWS = 4500
MAX_UNIVERSE_ROWS = 6500
PRICE_FIELDS = (
    "open", "high", "low", "close", "prev_close", "volume",
    "total_turnover", "limit_up", "limit_down",
)
SH_PREFIXES = ("600", "601", "603", "605", "688")
SZ_PREFIXES = ("000", "001", "002", "003", "300", "301")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHANGHAI = ZoneInfo("Asia/Shanghai")


class UpdateError(RuntimeError):
    """Fail-closed updater error."""


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, canonical_bytes(json_safe(value)))


def json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return json_safe(value.item())
        except (AttributeError, ValueError):
            pass
    if isinstance(value, float) and not pd.notna(value):
        return None
    return value


def file_ref(path: Path, base: Path) -> dict[str, Any]:
    return {
        "path": path.resolve().relative_to(base.resolve()).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def safe_error(exc: Exception) -> dict[str, str]:
    message = re.sub(r"(?:tcp|rqdata|rqdatac)://\S+", "<redacted>", str(exc))
    message = re.sub(
        r"(?i)\b(license|token|password|secret|api[_-]?key)\b\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        message,
    )
    return {"type": type(exc).__name__, "message": message[:500]}


def is_shsz_a_share(value: str) -> bool:
    if re.fullmatch(r"\d{6}\.XSHG", value):
        return value[:3] in SH_PREFIXES
    if re.fullmatch(r"\d{6}\.XSHE", value):
        return value[:3] in SZ_PREFIXES
    return False


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise UpdateError(f"JSON object required: {path}")
    return value


def verify_candidate(path: Path) -> dict[str, Any]:
    manifest_path = path / "manifest.json"
    manifest = load_json(manifest_path)
    if manifest.get("producer") != PRODUCER_ID or manifest.get("status") != "PASS__SEALED_DELTA_CANDIDATE":
        raise UpdateError(f"untrusted updater manifest: {manifest_path}")
    for item in manifest.get("artifacts", []):
        target = path / str(item.get("path", ""))
        if not target.is_file() or target.stat().st_size != item.get("bytes"):
            raise UpdateError(f"candidate artifact size mismatch: {target}")
        if not isinstance(item.get("sha256"), str) or not SHA256_RE.fullmatch(item["sha256"]):
            raise UpdateError(f"candidate artifact digest malformed: {target}")
        if sha256_file(target) != item["sha256"]:
            raise UpdateError(f"candidate artifact hash mismatch: {target}")
    return manifest


def formal_watermark() -> date:
    catalog = load_json(CATALOG_PATH)
    dates: list[date] = []
    for item in catalog.get("datasets", []):
        if item.get("dataset_id") != DATASET_ID:
            continue
        root = Path(str(item["physical_path"]).replace("/", os.sep))
        manifest = load_json(root / "manifest.json")
        contract = manifest.get("research_input_contract", {})
        if contract.get("contract_id") != "r0_shsz_pv_v2":
            continue
        dates.append(date.fromisoformat(contract["coverage_end_date"]))
    if not dates:
        raise UpdateError("no admitted R0_SHSZ_PV v2 base snapshot")
    return max(dates)


def updater_watermark() -> date:
    values = [formal_watermark()]
    if CANDIDATE_PARENT.is_dir():
        for path in sorted(CANDIDATE_PARENT.iterdir()):
            if path.is_dir() and re.fullmatch(r"delta-\d{8}-\d{8}-v1", path.name):
                values.append(date.fromisoformat(verify_candidate(path)["date_end"]))
    return max(values)


def acquire_lock() -> int:
    TRANSACTION_PARENT.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        os.close(descriptor)
        raise UpdateError(f"updater lock is held by another process: {LOCK_PATH}") from exc
    try:
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(
            descriptor,
            canonical_bytes(
                {"pid": os.getpid(), "created_at": datetime.now(timezone.utc).isoformat()}
            ),
        )
        os.fsync(descriptor)
    except Exception:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)
        raise
    return descriptor


def release_lock(descriptor: int) -> None:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(descriptor)


def quota_number(value: Any, key_name: str) -> float | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() == key_name.lower() and isinstance(item, (int, float)):
                return float(item)
        for item in value.values():
            found = quota_number(item, key_name)
            if found is not None:
                return found
    return None


def numeric_only(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): cleaned for k, v in value.items() if (cleaned := numeric_only(v)) is not None}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def normalize_price(frame: Any) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise UpdateError("get_price did not return a DataFrame")
    value = frame.reset_index()
    aliases = {str(column).lower(): column for column in value.columns}
    oid = aliases.get("order_book_id")
    trade_date = aliases.get("date") or aliases.get("datetime")
    if oid is None or trade_date is None:
        raise UpdateError(f"get_price identity columns missing: {list(value.columns)}")
    value = value.rename(columns={oid: "order_book_id", trade_date: "trade_date"})
    missing = [field for field in PRICE_FIELDS if field not in value.columns]
    if missing:
        raise UpdateError(f"get_price fields missing: {missing}")
    value = value[["order_book_id", "trade_date", *PRICE_FIELDS]].copy()
    value["order_book_id"] = value["order_book_id"].astype(str)
    value["trade_date"] = pd.to_datetime(value["trade_date"], errors="raise").dt.date
    for field in PRICE_FIELDS:
        value[field] = pd.to_numeric(value[field], errors="coerce")
    value = value.sort_values(["trade_date", "order_book_id"], kind="mergesort")
    if value.duplicated(["order_book_id", "trade_date"]).any():
        raise UpdateError("duplicate RQData price keys")
    return value


def normalize_boolean(frame: Any, value_name: str) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise UpdateError(f"{value_name} endpoint did not return a DataFrame")
    value = frame.rename_axis("trade_date").stack(dropna=False).rename(value_name).reset_index()
    value.columns = ["trade_date", "order_book_id", value_name]
    value["trade_date"] = pd.to_datetime(value["trade_date"], errors="raise").dt.date
    value["order_book_id"] = value["order_book_id"].astype(str)
    value[value_name] = value[value_name].astype("boolean")
    return value


def normalize_factors(frame: Any) -> pd.DataFrame:
    columns = ["order_book_id", "trade_date", "ex_factor"]
    if frame is None:
        return pd.DataFrame(columns=columns)
    if not isinstance(frame, pd.DataFrame):
        raise UpdateError("get_ex_factor returned an unexpected type")
    value = frame.reset_index()
    aliases = {str(column).lower(): column for column in value.columns}
    oid = aliases.get("order_book_id")
    trade_date = aliases.get("ex_date") or aliases.get("date")
    factor = aliases.get("ex_factor")
    if oid is None or trade_date is None or factor is None:
        raise UpdateError(f"get_ex_factor fields missing: {list(value.columns)}")
    value = value.rename(columns={oid: "order_book_id", trade_date: "trade_date", factor: "ex_factor"})
    value = value[columns].copy()
    value["order_book_id"] = value["order_book_id"].astype(str)
    value["trade_date"] = pd.to_datetime(value["trade_date"], errors="raise").dt.date
    value["ex_factor"] = pd.to_numeric(value["ex_factor"], errors="raise")
    if value.duplicated(["order_book_id", "trade_date"]).any():
        raise UpdateError("duplicate ex-factor keys")
    return value


def write_csv(path: Path, frame: pd.DataFrame) -> None:
    atomic_write(path, frame.to_csv(index=False, lineterminator="\n", float_format="%.12g").encode("utf-8"))


def build_panel(
    price: pd.DataFrame,
    universe: pd.DataFrame,
    factors: pd.DataFrame,
    st: pd.DataFrame,
    suspended: pd.DataFrame,
    next_dates: Mapping[date, date],
    run_id: str,
) -> pd.DataFrame:
    value = price.merge(factors, how="left", on=["order_book_id", "trade_date"])
    value = value.merge(st, how="left", on=["order_book_id", "trade_date"])
    value = value.merge(suspended, how="left", on=["order_book_id", "trade_date"])
    master = universe[["order_book_id", "listed_date", "de_listed_date"]].copy()
    value = value.merge(master, how="left", on="order_book_id")
    value["ex_factor"] = value["ex_factor"].fillna(1.0)
    value["next_trade_date"] = value["trade_date"].map(next_dates)
    value["listed_episode_eligible"] = (
        value["listed_date"].notna()
        & (value["trade_date"] >= value["listed_date"])
        & (value["de_listed_date"].isna() | (value["trade_date"] <= value["de_listed_date"]))
    )
    value["had_trade"] = value["volume"].fillna(0).gt(0) & value["total_turnover"].fillna(0).gt(0)
    value["st_state_known"] = value["is_st"].notna()
    value["suspension_state_known"] = value["is_suspended"].notna()
    value["st_state_code"] = value["is_st"].map({True: "ST_OR_RISK_WARNING", False: "NOT_ST_OR_RISK_WARNING"}).fillna("UNKNOWN")
    value["suspension_state_code"] = value["is_suspended"].map({True: "SUSPENDED", False: "NOT_SUSPENDED"}).fillna("UNKNOWN")
    value["trading_status_code"] = value["is_suspended"].map({True: "RQDATA_SUSPENDED", False: "RQDATA_NOT_SUSPENDED"}).fillna("RQDATA_SUSPENSION_UNKNOWN")
    valid_open = (
        value["listed_episode_eligible"] & value["had_trade"] & ~value["is_suspended"].fillna(True)
        & value[["prev_close", "open", "high", "low", "close"]].gt(0).all(axis=1)
        & value["high"].gt(value["low"])
        & (value["limit_up"].isna() | value["open"].lt(value["limit_up"]))
        & (value["limit_down"].isna() | value["open"].gt(value["limit_down"]))
    )
    value["open_execution_eligible"] = valid_open
    value["realized_total_return"] = value["close"] * value["ex_factor"] / value["prev_close"] - 1.0
    value["available_at"] = pd.to_datetime(value["next_trade_date"].astype(str) + " 09:30:00").dt.tz_localize(SHANGHAI)
    value["exchange_mic"] = value["order_book_id"].str.rsplit(".", n=1).str[-1]
    value["local_code"] = value["order_book_id"].str.slice(0, 6)
    value["source_id"] = "RQDATA_UNADJUSTED_DAILY"
    value["source_snapshot_id"] = run_id
    value["source_market_type"] = pd.Series([pd.NA] * len(value), dtype="Int64")
    value["return_calibration_method"] = value["ex_factor"].ne(1.0).map({True: "RQDATA_CLOSE_TIMES_EX_FACTOR_OVER_PREVIOUS_CLOSE_MINUS_ONE", False: "RQDATA_CLOSE_OVER_PREVIOUS_CLOSE_MINUS_ONE"})
    value["corporate_action_event_flag"] = value["ex_factor"].ne(1.0)
    value["corporate_action_event_count"] = value["corporate_action_event_flag"].astype("int64")
    value["total_return_event_factor"] = value["ex_factor"]
    value["execution_mask_role"] = "SIMULATION_ONLY__NEVER_SIGNAL_RANK_INPUT"
    value["source_segment"] = "RQDATA_DAILY_INCREMENT"
    value["schema"] = PANEL_SCHEMA
    value["year"] = pd.to_datetime(value["trade_date"]).dt.year.astype("int64")
    value["month"] = pd.to_datetime(value["trade_date"]).dt.month.astype("int64")
    value = value.rename(columns={
        "prev_close": "previous_close", "volume": "volume_shares",
        "total_turnover": "turnover_cny", "limit_up": "upper_limit_price",
        "limit_down": "lower_limit_price",
    })
    fields = [
        "order_book_id", "exchange_mic", "local_code", "trade_date",
        "previous_close", "open", "high", "low", "close", "volume_shares",
        "turnover_cny", "realized_total_return", "trading_status_code",
        "upper_limit_price", "lower_limit_price", "available_at",
        "listed_episode_eligible", "had_trade", "open_execution_eligible",
        "source_id", "source_snapshot_id", "source_market_type",
        "return_calibration_method", "corporate_action_event_flag",
        "corporate_action_event_count", "total_return_event_factor",
        "st_state_code", "st_state_known", "suspension_state_code",
        "suspension_state_known", "next_trade_date", "execution_mask_role",
        "source_segment", "month", "schema", "year",
    ]
    return value[fields].sort_values(["trade_date", "order_book_id"], kind="mergesort")


def quality_summary(panel: pd.DataFrame, expected_dates: list[date], quota_used: int) -> dict[str, Any]:
    duplicate_rows = int(panel.duplicated(["order_book_id", "trade_date"]).sum())
    traded = panel["had_trade"]
    invalid_ohlc = int((traded & (
        panel["high"].lt(panel[["open", "close", "low"]].max(axis=1))
        | panel["low"].gt(panel[["open", "close", "high"]].min(axis=1))
        | panel[["previous_close", "open", "high", "low", "close"]].le(0).any(axis=1)
    )).sum())
    counts = panel.groupby("trade_date").size().to_dict()
    checks = {
        "dates_exact": sorted(panel["trade_date"].unique().tolist()) == expected_dates,
        "rows_per_date_bounded": all(MIN_UNIVERSE_ROWS <= int(counts.get(day, 0)) <= MAX_UNIVERSE_ROWS for day in expected_dates),
        "duplicate_primary_keys_zero": duplicate_rows == 0,
        "invalid_traded_ohlc_zero": invalid_ohlc == 0,
        "identity_complete": bool(panel[["order_book_id", "exchange_mic", "local_code"]].notna().all().all()),
        "return_complete_and_finite": bool(panel["realized_total_return"].notna().all() and pd.Series(panel["realized_total_return"]).map(lambda x: abs(float(x)) != float("inf")).all()),
        "next_session_complete": bool(panel["next_trade_date"].notna().all() and (panel["next_trade_date"] > panel["trade_date"]).all()),
        "st_state_complete": bool(panel["st_state_known"].all()),
        "suspension_state_complete": bool(panel["suspension_state_known"].all()),
        "quota_cap": quota_used <= MAX_QUOTA_BYTES,
    }
    return {
        "schema_version": 1,
        "mechanical_status": "PASS" if all(checks.values()) else "FAIL_CLOSED",
        "checks": checks,
        "rows": int(len(panel)),
        "securities": int(panel["order_book_id"].nunique()),
        "date_start": min(expected_dates).isoformat(),
        "date_end": max(expected_dates).isoformat(),
        "rows_by_date": {str(key): int(value) for key, value in counts.items()},
        "duplicate_primary_key_rows": duplicate_rows,
        "invalid_traded_ohlc_rows": invalid_ohlc,
        "quota_bytes_used": quota_used,
        "factor_runs": 0,
        "formal_effect": "NONE__QUARANTINE_ONLY",
    }


def default_through() -> date:
    now = datetime.now(SHANGHAI)
    return now.date() if now.time() >= time(16, 0) else now.date() - timedelta(days=1)


def date_or_none(value: Any) -> date | None:
    text = str(value)[:10]
    if text in {"", "None", "NaT", "nan", "0000-00-00"}:
        return None
    return date.fromisoformat(text)


def run(through: date) -> dict[str, Any]:
    descriptor = acquire_lock()
    staging: Path | None = None
    try:
        watermark = updater_watermark()
        if through <= watermark:
            return {
                "status": "NO_CHANGE__WATERMARK_CURRENT__NO_NETWORK",
                "watermark": watermark.isoformat(),
                "through": through.isoformat(),
            }
        if str(RQ_SITE) not in sys.path:
            sys.path.insert(0, str(RQ_SITE))
        import rqdatac

        rqdatac.init(enable_bjse=False)
        calendar_end = through + timedelta(days=14)
        calendar = sorted(set(rqdatac.get_trading_dates(watermark.isoformat(), calendar_end.isoformat())))
        delta_dates = [day for day in calendar if watermark < day <= through]
        if not delta_dates:
            return {"status": "NO_CHANGE", "watermark": watermark.isoformat(), "through": through.isoformat()}
        if len(delta_dates) > MAX_TRADING_DATES:
            raise UpdateError(f"gap has {len(delta_dates)} trading dates; maximum is {MAX_TRADING_DATES}")
        next_dates: dict[date, date] = {}
        for day in delta_dates:
            later = [candidate for candidate in calendar if candidate > day]
            if not later:
                raise UpdateError(f"next trading date unavailable after {day}")
            next_dates[day] = later[0]

        run_id = f"rqdata-shsz-daily-{delta_dates[0]:%Y%m%d}-{delta_dates[-1]:%Y%m%d}-v1"
        candidate_id = f"delta-{delta_dates[0]:%Y%m%d}-{delta_dates[-1]:%Y%m%d}-v1"
        final = CANDIDATE_PARENT / candidate_id
        if final.exists():
            return {"status": "NO_CHANGE__CANDIDATE_EXISTS", "candidate": str(final)}
        CANDIDATE_PARENT.mkdir(parents=True, exist_ok=True)
        staging = CANDIDATE_PARENT / f".{candidate_id}.staging-{uuid.uuid4().hex}"
        staging.mkdir(parents=False, exist_ok=False)
        raw = staging / "raw/rqdata"
        raw.mkdir(parents=True)

        quota_before = rqdatac.user.get_quota()
        universe = rqdatac.all_instruments(type="CS")
        if not isinstance(universe, pd.DataFrame):
            raise UpdateError("all_instruments did not return a DataFrame")
        universe = universe[universe["order_book_id"].astype(str).map(is_shsz_a_share)].copy()
        universe["order_book_id"] = universe["order_book_id"].astype(str)
        universe["listed_date"] = universe["listed_date"].map(date_or_none)
        universe["de_listed_date"] = universe["de_listed_date"].map(date_or_none)
        start, end = delta_dates[0], delta_dates[-1]
        selected = universe[
            universe["listed_date"].notna() & (universe["listed_date"] <= end)
            & (universe["de_listed_date"].isna() | (universe["de_listed_date"] >= start))
        ].copy()
        if not MIN_UNIVERSE_ROWS <= len(selected) <= MAX_UNIVERSE_ROWS:
            raise UpdateError(f"unexpected SH/SZ universe size: {len(selected)}")
        ids = sorted(selected["order_book_id"].unique().tolist())
        price = normalize_price(rqdatac.get_price(
            ids, start_date=start.isoformat(), end_date=end.isoformat(), frequency="1d",
            fields=list(PRICE_FIELDS), adjust_type="none", skip_suspended=False, expect_df=True,
        ))
        factors = normalize_factors(rqdatac.get_ex_factor(ids, start.isoformat(), end.isoformat()))
        st = normalize_boolean(rqdatac.is_st_stock(ids, start.isoformat(), end.isoformat()), "is_st")
        suspended = normalize_boolean(rqdatac.is_suspended(ids, start.isoformat(), end.isoformat()), "is_suspended")
        quota_after = rqdatac.user.get_quota()
        before_used = quota_number(quota_before, "bytes_used")
        after_used = quota_number(quota_after, "bytes_used")
        if before_used is None or after_used is None:
            raise UpdateError("quota bytes_used unavailable")
        quota_used = max(0, int(round(after_used - before_used)))
        if quota_used > MAX_QUOTA_BYTES:
            raise UpdateError(f"quota cap exceeded: {quota_used} > {MAX_QUOTA_BYTES}")

        write_csv(raw / "universe.csv", selected)
        write_csv(raw / "price.csv", price)
        write_csv(raw / "ex_factor.csv", factors)
        write_csv(raw / "st.csv", st)
        write_csv(raw / "suspended.csv", suspended)
        panel = build_panel(price, selected, factors, st, suspended, next_dates, run_id)
        quality = quality_summary(panel, delta_dates, quota_used)
        if quality["mechanical_status"] != "PASS":
            raise UpdateError("mechanical quality gate failed: " + json.dumps(quality["checks"], sort_keys=True))

        curated_files: list[Path] = []
        connection = duckdb.connect()
        connection.register("panel_frame", panel)
        try:
            for (year, month), _group in panel.groupby(["year", "month"]):
                target = staging / f"curated/schema={PANEL_SCHEMA}/year={int(year)}/month={int(month)}/part-0.parquet"
                target.parent.mkdir(parents=True, exist_ok=True)
                escaped = target.resolve().as_posix().replace("'", "''")
                connection.execute(
                    f"COPY (SELECT * FROM panel_frame WHERE year={int(year)} AND month={int(month)} ORDER BY trade_date, order_book_id) "
                    f"TO '{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)"
                )
                curated_files.append(target)
        finally:
            connection.close()

        acquisition = {
            "schema_version": 1, "run_id": run_id, "observed_at_utc": datetime.now(timezone.utc),
            "source": "RQDATA", "dates": [day.isoformat() for day in delta_dates],
            "next_trading_dates": {day.isoformat(): value.isoformat() for day, value in next_dates.items()},
            "requested_security_count": len(ids), "returned_price_rows": len(price),
            "quota_before_numeric_only": numeric_only(quota_before),
            "quota_after_numeric_only": numeric_only(quota_after),
            "quota_bytes_used": quota_used, "quota_cap_bytes": MAX_QUOTA_BYTES,
            "credential_value_read": False, "credential_value_captured": False,
            "formal_effect": "NONE__QUARANTINE_ONLY",
        }
        write_json(staging / "ACQUISITION_RECEIPT.json", acquisition)
        write_json(staging / "quality/summary.json", quality)
        artifacts = [
            file_ref(path, staging)
            for path in sorted(
                [*raw.glob("*.csv"), *curated_files, staging / "ACQUISITION_RECEIPT.json", staging / "quality/summary.json"],
                key=lambda item: item.as_posix(),
            )
        ]
        manifest = {
            "schema_version": 1, "producer": PRODUCER_ID, "run_id": run_id,
            "candidate_id": candidate_id, "status": "PASS__SEALED_DELTA_CANDIDATE",
            "purpose_ceiling": "INSPECTION_ONLY", "source_admission": False,
            "formal_catalog_write": False, "research_ready": False,
            "date_start": start.isoformat(), "date_end": end.isoformat(),
            "base_watermark": watermark.isoformat(), "rows": len(panel),
            "securities": int(panel["order_book_id"].nunique()), "artifacts": artifacts,
            "content_hash": hashlib.sha256(canonical_bytes(artifacts)).hexdigest(),
        }
        write_json(staging / "manifest.json", manifest)
        os.replace(staging, final)
        staging = None
        verified = verify_candidate(final)
        transaction = {
            "schema_version": 1, "status": "PASS__DELTA_CANDIDATE_SEALED",
            "run_id": run_id, "candidate": final.resolve().relative_to(ROOT).as_posix(),
            "manifest_sha256": sha256_file(final / "manifest.json"),
            "date_start": verified["date_start"], "date_end": verified["date_end"],
            "rows": verified["rows"], "quota_bytes_used": quota_used,
            "formal_effect": "NONE", "factor_runs": 0,
        }
        write_json(TRANSACTION_PARENT / f"{run_id}.json", transaction)
        return transaction
    except Exception as exc:
        if staging is not None and staging.exists():
            write_json(staging / "FAILURE.json", {
                "status": "FAIL_CLOSED", "observed_at_utc": datetime.now(timezone.utc),
                "error": safe_error(exc), "formal_effect": "NONE", "factor_runs": 0,
            })
            failed = CANDIDATE_PARENT / f"failed-{datetime.now(SHANGHAI):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
            os.replace(staging, failed)
        raise
    finally:
        release_lock(descriptor)


def plan(through: date) -> dict[str, Any]:
    watermark = updater_watermark()
    return {
        "schema_version": 1, "status": "PLAN__NO_NETWORK_NO_WRITE",
        "producer": PRODUCER_ID, "formal_watermark": formal_watermark().isoformat(),
        "effective_watermark": watermark.isoformat(), "through": through.isoformat(),
        "maximum_trading_dates_per_run": MAX_TRADING_DATES,
        "maximum_quota_bytes": MAX_QUOTA_BYTES,
        "candidate_root": CANDIDATE_PARENT.resolve().relative_to(ROOT).as_posix(),
        "formal_catalog_write": False, "factor_runs": 0,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--through", type=date.fromisoformat)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    through = args.through or default_through()
    try:
        result = plan(through) if args.plan else run(through)
    except Exception as exc:
        print(json.dumps({"status": "FAIL_CLOSED", "error": safe_error(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(json_safe(result), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
