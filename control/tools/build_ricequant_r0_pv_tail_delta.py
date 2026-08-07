from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


WORKSPACE = Path(__file__).resolve().parents[2]
RUN_ID = "ricequant-r0-pv-tail-delta-20260804-v1"
CANDIDATE_ID = "candidate-20260804-v1"
CONTRACT_ID = "ricequant-r0-pv-tail-delta-v1"
CONTRACT_PATH = (
    WORKSPACE
    / "data/catalog/contracts/ricequant_r0_pv_tail_delta_v1/contract.json"
)
BASE_ROOT = (
    WORKSPACE
    / "tmp/local-dataset-build/csmar-trd-dalyr-pilot/"
    "d2a04b-csmar-trd-dalyr-2022-2026-20260728-v1"
)
BASE_MANIFEST_SHA256 = (
    "2b4af1d492979ea4d6243b33da7bc2414041e2815779806662fec649c8c33396"
)
ST_ROOT = (
    WORKSPACE
    / "tmp/local-dataset-build/resset-stkspctrmt/"
    "manual-export-20260802-zero-loss-v2"
)
ST_MANIFEST_SHA256 = (
    "4164f9a1e452890504d6a86b64663d2524a358157de0f5f0ce9238dab9e85e50"
)
CANDIDATE_PARENT = WORKSPACE / "data/market/quarantine/r0-pv-tail-delta"
CANDIDATE_ROOT = CANDIDATE_PARENT / CANDIDATE_ID
STAGING_ROOT = CANDIDATE_PARENT / f".{CANDIDATE_ID}.staging"
REPORT_ROOT = WORKSPACE / "reports/data-governance" / RUN_ID
CALIBRATION_DATE = "2026-07-28"
DELTA_DATES = (
    "2026-07-29",
    "2026-07-30",
    "2026-07-31",
    "2026-08-03",
)
REQUEST_DATES = (CALIBRATION_DATE, *DELTA_DATES)
PRICE_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "prev_close",
    "volume",
    "total_turnover",
    "limit_up",
    "limit_down",
)
MAX_QUOTA_BYTES = 30 * 1024 * 1024
MAX_UNIVERSE_ROWS = 6000
MAX_BUSINESS_API_CALLS = 4
SH_PREFIXES = ("600", "601", "603", "605", "688")
SZ_PREFIXES = ("000", "001", "002", "003", "300", "301")
PROTECTED_PATHS = (
    "data/catalog/datasets.json",
    "control/ROADMAP.md",
    "data/catalog/contracts/source_admission_v1/current_source_registry.json",
)


class BuildError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_ref(path: Path, *, relative_to: Path = WORKSPACE) -> dict[str, Any]:
    return {
        "path": path.resolve().relative_to(relative_to.resolve()).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, canonical_json_bytes(json_safe(value)))


def json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except (AttributeError, ValueError):
            pass
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return value


def protected_state() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for relative in PROTECTED_PATHS:
        path = WORKSPACE / relative
        if not path.is_file():
            raise BuildError(f"protected authority file missing: {relative}")
        result[relative] = {
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    return result


def is_shsz_a_share_id(value: str) -> bool:
    value = str(value)
    if value.endswith(".XSHG"):
        return value[:3] in SH_PREFIXES and bool(re.fullmatch(r"\d{6}\.XSHG", value))
    if value.endswith(".XSHE"):
        return value[:3] in SZ_PREFIXES and bool(re.fullmatch(r"\d{6}\.XSHE", value))
    return False


def risk_warning_marker(value: Any) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().upper().replace(" ", "")
    if not text or text in {"NAN", "NONE", "<NA>"}:
        return None
    return "ST" in text or text.startswith("PT")


def safe_error(exc: Exception) -> dict[str, str]:
    message = str(exc)
    message = re.sub(r"(?:tcp|rqdata|rqdatac)://\S+", "<redacted>", message)
    message = re.sub(
        r"(?i)\b(license|token|password|secret|api[_-]?key)\b\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        message,
    )
    return {"type": type(exc).__name__, "message": message[:400]}


def _selected_base_inventory(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    pattern = re.compile(r"/year=(2025|2026)/month=(\d{2})/")
    for item in manifest.get("payload_inventory", []):
        relative = str(item.get("relative_path", ""))
        match = pattern.search("/" + relative)
        if not match or item.get("layer") != "curated":
            continue
        year, month = int(match.group(1)), int(match.group(2))
        if year == 2025 or (year == 2026 and month <= 7):
            selected.append(dict(item))
    selected.sort(key=lambda item: item["relative_path"])
    if len(selected) != 19:
        raise BuildError(f"expected 19 base monthly partitions, got {len(selected)}")
    return selected


def validate_bound_sources() -> dict[str, Any]:
    manifest_path = BASE_ROOT / "manifest.json"
    if sha256_file(manifest_path) != BASE_MANIFEST_SHA256:
        raise BuildError("CSMAR base manifest hash mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected = _selected_base_inventory(manifest)
    for item in selected:
        path = BASE_ROOT / item["relative_path"]
        if not path.is_file() or path.stat().st_size != int(item["bytes"]):
            raise BuildError(f"base partition size mismatch: {path}")
        if sha256_file(path) != item["sha256"]:
            raise BuildError(f"base partition hash mismatch: {path}")

    st_manifest_path = ST_ROOT / "manifest.json"
    if sha256_file(st_manifest_path) != ST_MANIFEST_SHA256:
        raise BuildError("RESSET ST manifest hash mismatch")
    st_manifest = json.loads(st_manifest_path.read_text(encoding="utf-8"))
    st_artifact = st_manifest["artifacts"]["candidate_csv"]
    st_path = ST_ROOT / st_artifact["relative_path"]
    if not st_path.is_file() or st_path.stat().st_size != int(st_artifact["bytes"]):
        raise BuildError("RESSET ST candidate size mismatch")
    if sha256_file(st_path) != st_artifact["sha256"]:
        raise BuildError("RESSET ST candidate hash mismatch")

    return {
        "csmar_base": {
            "root": BASE_ROOT.resolve().relative_to(WORKSPACE).as_posix(),
            "manifest": file_ref(manifest_path),
            "manifest_content_hash": manifest.get("content_hash"),
            "selected_partitions": [
                {
                    "path": item["relative_path"],
                    "bytes": item["bytes"],
                    "sha256": item["sha256"],
                }
                for item in selected
            ],
            "selection": {
                "date_start": "2025-01-01",
                "date_end": CALIBRATION_DATE,
                "market_types": [1, 4, 16, 32],
                "copy_mode": "NONE__LOGICAL_REFERENCE_ONLY",
            },
        },
        "resset_st_history": {
            "root": ST_ROOT.resolve().relative_to(WORKSPACE).as_posix(),
            "manifest": file_ref(st_manifest_path),
            "manifest_content_hash": st_manifest.get("content_hash"),
            "candidate_csv": file_ref(st_path),
            "purpose_ceiling": "INSPECTION_ONLY",
        },
    }


def _numeric_only(value: Any) -> Any:
    from numbers import Number

    if isinstance(value, Mapping):
        return {
            str(key): cleaned
            for key, item in value.items()
            if (cleaned := _numeric_only(item)) is not None
        }
    if isinstance(value, Number) and not isinstance(value, complex):
        return value
    return None


def _find_numeric_key(value: Any, key_name: str) -> float | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() == key_name.lower() and isinstance(item, (int, float)):
                return float(item)
        for item in value.values():
            found = _find_numeric_key(item, key_name)
            if found is not None:
                return found
    return None


def _write_frame_csv(path: Path, frame: Any) -> None:
    payload = frame.to_csv(index=False, lineterminator="\n", float_format="%.12g")
    atomic_write(path, payload.encode("utf-8"))


def _normalize_price_frame(frame: Any) -> Any:
    import pandas as pd

    if not isinstance(frame, pd.DataFrame):
        raise BuildError(f"get_price returned {type(frame).__name__}, expected DataFrame")
    value = frame.reset_index()
    columns = {str(item).lower(): str(item) for item in value.columns}
    id_column = columns.get("order_book_id")
    date_column = columns.get("date") or columns.get("datetime") or columns.get("trading_date")
    if id_column is None or date_column is None:
        raise BuildError(f"price index columns missing: {list(value.columns)}")
    value = value.rename(columns={id_column: "order_book_id", date_column: "trade_date"})
    missing = [field for field in PRICE_FIELDS if field not in value.columns]
    if missing:
        raise BuildError(f"get_price missing fields: {missing}")
    value = value[["order_book_id", "trade_date", *PRICE_FIELDS]].copy()
    value["order_book_id"] = value["order_book_id"].astype(str)
    value["trade_date"] = pd.to_datetime(value["trade_date"], errors="raise").dt.strftime(
        "%Y-%m-%d"
    )
    for field in PRICE_FIELDS:
        value[field] = pd.to_numeric(value[field], errors="coerce")
    value = value.sort_values(["trade_date", "order_book_id"], kind="mergesort")
    if value.duplicated(["order_book_id", "trade_date"]).any():
        raise BuildError("RQData returned duplicate security-date keys")
    returned_dates = set(value["trade_date"].unique())
    unexpected = returned_dates - set(REQUEST_DATES)
    if unexpected:
        raise BuildError(f"RQData returned unexpected dates: {sorted(unexpected)}")
    return value


def acquire() -> dict[str, Any]:
    if CANDIDATE_ROOT.exists() or STAGING_ROOT.exists():
        raise BuildError("candidate or staging path already exists")
    bindings = validate_bound_sources()
    state_before = protected_state()
    CANDIDATE_PARENT.mkdir(parents=True, exist_ok=True)
    STAGING_ROOT.mkdir(parents=False, exist_ok=False)
    raw_root = STAGING_ROOT / "raw/rqdata"
    raw_root.mkdir(parents=True, exist_ok=False)
    preflight = {
        "run_id": RUN_ID,
        "contract": file_ref(CONTRACT_PATH),
        "bound_sources": bindings,
        "protected_state_before": state_before,
        "api_calls_planned": MAX_BUSINESS_API_CALLS,
        "retry_count": 0,
        "credential_value_capture": False,
    }
    write_json(STAGING_ROOT / "INPUT_BINDINGS.json", preflight)

    calls = 0
    try:
        import pandas as pd
        import rqdatac

        rqdatac.init()
        quota_before = rqdatac.user.get_quota()
        calls += 1
        universe = rqdatac.all_instruments(type="CS")
        calls += 1
        if not isinstance(universe, pd.DataFrame):
            raise BuildError("all_instruments did not return a DataFrame")
        if len(universe) > MAX_UNIVERSE_ROWS:
            raise BuildError(f"universe row bound exceeded: {len(universe)}")
        required_universe = {"order_book_id", "listed_date", "de_listed_date"}
        if not required_universe.issubset(universe.columns):
            raise BuildError("all_instruments missing listing-boundary fields")
        selected = universe[universe["order_book_id"].map(is_shsz_a_share_id)].copy()
        selected["listed_date"] = pd.to_datetime(selected["listed_date"], errors="coerce")
        selected["de_listed_date"] = pd.to_datetime(
            selected["de_listed_date"], errors="coerce"
        )
        request_start = pd.Timestamp(CALIBRATION_DATE)
        request_end = pd.Timestamp(DELTA_DATES[-1])
        selected = selected[
            selected["listed_date"].notna()
            & selected["listed_date"].le(request_end)
            & (selected["de_listed_date"].isna() | selected["de_listed_date"].ge(request_start))
        ].copy()
        if not 4500 <= len(selected) <= MAX_UNIVERSE_ROWS:
            raise BuildError(f"unexpected SH/SZ A-share universe size: {len(selected)}")
        order_book_ids = sorted(selected["order_book_id"].astype(str).unique())
        price = rqdatac.get_price(
            order_book_ids,
            start_date=CALIBRATION_DATE,
            end_date=DELTA_DATES[-1],
            frequency="1d",
            fields=list(PRICE_FIELDS),
            adjust_type="none",
            skip_suspended=False,
            expect_df=True,
        )
        calls += 1
        quota_after = rqdatac.user.get_quota()
        calls += 1
        if calls != MAX_BUSINESS_API_CALLS:
            raise BuildError(f"unexpected business API call count: {calls}")
        before_used = _find_numeric_key(quota_before, "bytes_used")
        after_used = _find_numeric_key(quota_after, "bytes_used")
        if before_used is None or after_used is None:
            raise BuildError("quota bytes_used unavailable; cannot enforce 30 MiB cap")
        used_delta = max(0, int(round(after_used - before_used)))
        if used_delta > MAX_QUOTA_BYTES:
            raise BuildError(f"quota byte cap exceeded: {used_delta} > {MAX_QUOTA_BYTES}")

        normalized = _normalize_price_frame(price)
        universe_fields = [
            field
            for field in (
                "order_book_id",
                "symbol",
                "listed_date",
                "de_listed_date",
                "status",
                "board_type",
                "type",
            )
            if field in selected.columns
        ]
        universe_capture = selected[universe_fields].copy()
        for field in ("listed_date", "de_listed_date"):
            if field in universe_capture.columns:
                universe_capture[field] = universe_capture[field].dt.strftime("%Y-%m-%d")
        universe_capture = universe_capture.sort_values("order_book_id", kind="mergesort")
        _write_frame_csv(raw_root / "universe.csv", universe_capture)
        _write_frame_csv(raw_root / "price_calibration_and_delta.csv", normalized)

        observed_at = datetime.now(timezone.utc).isoformat()
        receipt = {
            "schema_version": 1,
            "run_id": RUN_ID,
            "contract_id": CONTRACT_ID,
            "observed_at": observed_at,
            "initialization": "PASS",
            "business_api_calls_made": calls,
            "retry_count": 0,
            "quota_before_numeric_only": _numeric_only(quota_before),
            "quota_after_numeric_only": _numeric_only(quota_after),
            "quota_bytes_used_delta": used_delta,
            "quota_cap_bytes": MAX_QUOTA_BYTES,
            "quota_cap_pass": used_delta <= MAX_QUOTA_BYTES,
            "universe_rows_returned": int(len(universe)),
            "universe_rows_selected": int(len(universe_capture)),
            "price_rows_returned": int(len(normalized)),
            "price_dates_returned": sorted(normalized["trade_date"].unique().tolist()),
            "request": {
                "start_date": CALIBRATION_DATE,
                "end_date": DELTA_DATES[-1],
                "frequency": "1d",
                "fields": list(PRICE_FIELDS),
                "adjust_type": "none",
                "skip_suspended": False,
                "requested_order_book_ids": len(order_book_ids),
            },
            "raw_capture": {
                "universe": file_ref(raw_root / "universe.csv", relative_to=STAGING_ROOT),
                "price": file_ref(
                    raw_root / "price_calibration_and_delta.csv",
                    relative_to=STAGING_ROOT,
                ),
            },
            "credential_value_read": False,
            "credential_value_captured": False,
            "credential_value_persisted": False,
            "formal_effect": "NONE__STAGING_QUARANTINE_ONLY",
        }
        write_json(STAGING_ROOT / "ACQUISITION_RECEIPT.json", receipt)
        if protected_state() != state_before:
            raise BuildError("protected authority state changed during acquisition")
        return {
            "status": "ACQUISITION_PASS__READY_TO_FINALIZE",
            "staging_root": STAGING_ROOT.relative_to(WORKSPACE).as_posix(),
            "price_rows": len(normalized),
            "quota_bytes_used_delta": used_delta,
        }
    except Exception as exc:
        failure = {
            "status": "FAIL_CLOSED__NO_RETRY",
            "run_id": RUN_ID,
            "business_api_calls_made": calls,
            "retry_count": 0,
            "error": safe_error(exc),
            "credential_value_captured": False,
            "formal_effect": "NONE",
        }
        write_json(STAGING_ROOT / "FAILURE.json", failure)
        raise


def _duckdb_list(paths: Iterable[Path]) -> str:
    escaped = ["'" + path.resolve().as_posix().replace("'", "''") + "'" for path in paths]
    return "[" + ",".join(escaped) + "]"


def _one_row(connection: Any, sql: str) -> dict[str, Any]:
    cursor = connection.execute(sql)
    columns = [item[0] for item in cursor.description]
    row = cursor.fetchone()
    if row is None:
        raise BuildError("expected one aggregate row")
    return json_safe(dict(zip(columns, row)))


def _rows(connection: Any, sql: str) -> list[dict[str, Any]]:
    cursor = connection.execute(sql)
    columns = [item[0] for item in cursor.description]
    return [json_safe(dict(zip(columns, row))) for row in cursor.fetchall()]


def _build_st_intervals(connection: Any, staging: Path) -> dict[str, Any]:
    import pandas as pd

    source = ST_ROOT / "curated/candidate.csv"
    events = pd.read_csv(
        source,
        usecols=["nobs", "StkCd", "InfoPubDt", "SecuAbbr", "ImpDt"],
        dtype={"StkCd": "string", "SecuAbbr": "string"},
    )
    events["local_code"] = events["StkCd"].str.zfill(6)
    events = events[
        events["local_code"].str[:3].isin((*SH_PREFIXES, *SZ_PREFIXES))
    ].copy()
    events["effective_from"] = pd.to_datetime(events["ImpDt"], errors="coerce")
    events["published_date"] = pd.to_datetime(events["InfoPubDt"], errors="coerce")
    events["risk_warning_state"] = events["SecuAbbr"].map(risk_warning_marker)
    events = events[events["effective_from"].notna()].copy()
    events["venue_mic"] = events["local_code"].map(
        lambda value: "XSHG" if value[:3] in SH_PREFIXES else "XSHE"
    )
    events["order_book_id"] = events["local_code"] + "." + events["venue_mic"]

    collapsed_rows: list[dict[str, Any]] = []
    for (order_book_id, effective_from), group in events.groupby(
        ["order_book_id", "effective_from"], sort=True
    ):
        states = {bool(item) for item in group["risk_warning_state"].dropna().tolist()}
        state: bool | None = next(iter(states)) if len(states) == 1 else None
        collapsed_rows.append(
            {
                "order_book_id": order_book_id,
                "effective_from": effective_from,
                "risk_warning_state": state,
                "state_known": state is not None,
                "same_day_event_rows": int(len(group)),
                "same_day_state_conflict": len(states) > 1,
                "published_date_min": group["published_date"].min(),
                "published_date_max": group["published_date"].max(),
                "source_nobs_min": int(group["nobs"].min()),
                "source_nobs_max": int(group["nobs"].max()),
                "mapping_basis": "SecuAbbr marker contains ST or begins PT",
            }
        )
    intervals = pd.DataFrame(collapsed_rows)
    intervals = intervals.sort_values(["order_book_id", "effective_from"], kind="mergesort")
    intervals["effective_to_exclusive"] = intervals.groupby("order_book_id")[
        "effective_from"
    ].shift(-1)
    connection.register("st_intervals_frame", intervals)
    reference_root = staging / "reference"
    reference_root.mkdir(parents=True, exist_ok=True)
    output = reference_root / "st_risk_warning_intervals.parquet"
    output_sql = output.resolve().as_posix().replace("'", "''")
    connection.execute(
        f"COPY (SELECT * FROM st_intervals_frame ORDER BY order_book_id, effective_from) "
        f"TO '{output_sql}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    return {
        "source_rows": int(len(events)),
        "interval_rows": int(len(intervals)),
        "security_count": int(intervals["order_book_id"].nunique()),
        "state_unknown_rows": int((~intervals["state_known"]).sum()),
        "same_day_conflict_rows": int(intervals["same_day_state_conflict"].sum()),
        "date_min": None
        if intervals.empty
        else intervals["effective_from"].min().date().isoformat(),
        "date_max": None
        if intervals.empty
        else intervals["effective_from"].max().date().isoformat(),
        "artifact": file_ref(output, relative_to=staging),
    }


def _create_views(connection: Any, bindings: dict[str, Any]) -> None:
    base_paths = [BASE_ROOT / item["path"] for item in bindings["csmar_base"]["selected_partitions"]]
    price_path = STAGING_ROOT / "raw/rqdata/price_calibration_and_delta.csv"
    universe_path = STAGING_ROOT / "raw/rqdata/universe.csv"
    base_list = _duckdb_list(base_paths)
    price_sql = price_path.resolve().as_posix().replace("'", "''")
    universe_sql = universe_path.resolve().as_posix().replace("'", "''")
    connection.execute(
        f"""
        CREATE VIEW base_rows AS
        SELECT
          CASE WHEN Markettype IN (1, 32) THEN Stkcd || '.XSHG'
               ELSE Stkcd || '.XSHE' END AS order_book_id,
          Trddt AS trade_date,
          Opnprc AS open,
          Hiprc AS high,
          Loprc AS low,
          Clsprc AS close,
          PreClosePrice AS prev_close,
          Dnshrtrd AS volume,
          Dnvaltrd AS total_turnover,
          LimitUp AS limit_up,
          LimitDown AS limit_down,
          Trdsta AS vendor_trade_status,
          Markettype AS market_type
        FROM read_parquet({base_list}, hive_partitioning=false)
        WHERE Trddt BETWEEN DATE '2025-01-01' AND DATE '{CALIBRATION_DATE}'
          AND Markettype IN (1, 4, 16, 32)
          AND (
            substr(Stkcd, 1, 3) IN ('600','601','603','605','688')
            OR substr(Stkcd, 1, 3) IN ('000','001','002','003','300','301')
          )
        """
    )
    connection.execute(
        f"""
        CREATE VIEW rq_rows AS
        SELECT
          CAST(order_book_id AS VARCHAR) AS order_book_id,
          CAST(trade_date AS DATE) AS trade_date,
          {','.join(f'CAST({field} AS DOUBLE) AS {field}' for field in PRICE_FIELDS)}
        FROM read_csv_auto('{price_sql}', header=true, all_varchar=true)
        """
    )
    connection.execute(
        f"""
        CREATE VIEW universe_rows AS
        SELECT
          CAST(order_book_id AS VARCHAR) AS order_book_id,
          CAST(symbol AS VARCHAR) AS symbol,
          TRY_CAST(listed_date AS DATE) AS listed_date,
          TRY_CAST(de_listed_date AS DATE) AS de_listed_date
        FROM read_csv_auto('{universe_sql}', header=true, all_varchar=true)
        """
    )
    st_path = STAGING_ROOT / "reference/st_risk_warning_intervals.parquet"
    st_sql = st_path.resolve().as_posix().replace("'", "''")
    connection.execute(
        f"CREATE VIEW st_intervals AS SELECT * FROM read_parquet('{st_sql}')"
    )


def _base_statistics(connection: Any) -> dict[str, Any]:
    return _one_row(
        connection,
        """
        SELECT
          COUNT(*) AS rows,
          COUNT(DISTINCT (order_book_id, trade_date)) AS unique_keys,
          COUNT(DISTINCT order_book_id) AS securities,
          COUNT(DISTINCT trade_date) AS trading_dates,
          MIN(trade_date) AS date_min,
          MAX(trade_date) AS date_max,
          COUNT(*) FILTER (WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL)
            AS ohlc_null_rows,
          COUNT(*) FILTER (WHERE volume IS NULL OR total_turnover IS NULL)
            AS activity_null_rows
        FROM base_rows
        """,
    )


def _calibration_statistics(connection: Any) -> dict[str, Any]:
    connection.execute(
        f"""
        CREATE VIEW overlap AS
        SELECT
          b.order_book_id,
          {','.join(f'b.{field} AS c_{field}, r.{field} AS r_{field}' for field in PRICE_FIELDS)}
        FROM base_rows b
        JOIN rq_rows r USING (order_book_id, trade_date)
        WHERE b.trade_date = DATE '{CALIBRATION_DATE}'
        """
    )
    fields: dict[str, Any] = {}
    price_fields = {"open", "high", "low", "close", "prev_close", "limit_up", "limit_down"}
    for field in PRICE_FIELDS:
        tolerance = (
            f"greatest(0.0001, abs(c_{field}) * 0.000001)"
            if field in price_fields
            else f"greatest(1.0, abs(c_{field}) * 0.000001)"
        )
        fields[field] = _one_row(
            connection,
            f"""
            SELECT
              COUNT(*) AS overlap_rows,
              COUNT(*) FILTER (WHERE c_{field} IS NOT NULL AND r_{field} IS NOT NULL)
                AS comparable_rows,
              COUNT(*) FILTER (WHERE c_{field} IS NULL AND r_{field} IS NOT NULL)
                AS csmar_null_rq_present,
              COUNT(*) FILTER (WHERE c_{field} IS NOT NULL AND r_{field} IS NULL)
                AS csmar_present_rq_null,
              MEDIAN(abs(r_{field} - c_{field})) FILTER (
                WHERE c_{field} IS NOT NULL AND r_{field} IS NOT NULL
              ) AS median_absolute_difference,
              quantile_cont(abs(r_{field} - c_{field}), 0.99) FILTER (
                WHERE c_{field} IS NOT NULL AND r_{field} IS NOT NULL
              ) AS p99_absolute_difference,
              MEDIAN(r_{field} / nullif(c_{field}, 0)) FILTER (
                WHERE c_{field} IS NOT NULL AND r_{field} IS NOT NULL
              ) AS median_rq_to_csmar_ratio,
              COUNT(*) FILTER (
                WHERE c_{field} IS NOT NULL AND r_{field} IS NOT NULL
                  AND abs(r_{field} - c_{field}) <= {tolerance}
              ) AS within_tolerance_rows
            FROM overlap
            """,
        )
        comparable = int(fields[field]["comparable_rows"] or 0)
        within = int(fields[field]["within_tolerance_rows"] or 0)
        fields[field]["within_tolerance_rate"] = None if comparable == 0 else within / comparable
    key_stats = _one_row(
        connection,
        f"""
        SELECT
          (SELECT COUNT(*) FROM base_rows WHERE trade_date=DATE '{CALIBRATION_DATE}')
            AS csmar_rows,
          (SELECT COUNT(*) FROM rq_rows WHERE trade_date=DATE '{CALIBRATION_DATE}')
            AS rqdata_rows,
          (SELECT COUNT(*) FROM overlap) AS joined_rows
        """,
    )
    return {
        "calibration_date": CALIBRATION_DATE,
        "key_alignment": key_stats,
        "field_alignment": fields,
        "decision": "RETAIN_BOTH__NO_SILENT_CANONICAL_OVERRIDE",
    }


def _tail_quality(connection: Any) -> dict[str, Any]:
    date_values = ",".join(f"(DATE '{item}')" for item in DELTA_DATES)
    connection.execute(f"CREATE TEMP TABLE target_dates(trade_date DATE); INSERT INTO target_dates VALUES {date_values}")
    coverage = _rows(
        connection,
        """
        WITH expected AS (
          SELECT d.trade_date, COUNT(*) AS expected_rows
          FROM target_dates d
          JOIN universe_rows u
            ON u.listed_date <= d.trade_date
           AND (u.de_listed_date IS NULL OR u.de_listed_date >= d.trade_date)
          GROUP BY 1
        ), actual AS (
          SELECT trade_date, COUNT(*) AS actual_rows,
                 COUNT(DISTINCT order_book_id) AS actual_securities
          FROM rq_rows WHERE trade_date IN (SELECT trade_date FROM target_dates)
          GROUP BY 1
        )
        SELECT e.trade_date, e.expected_rows,
               coalesce(a.actual_rows, 0) AS actual_rows,
               coalesce(a.actual_securities, 0) AS actual_securities,
               coalesce(a.actual_rows, 0) = e.expected_rows AS exact_row_coverage
        FROM expected e LEFT JOIN actual a USING (trade_date)
        ORDER BY e.trade_date
        """,
    )
    summary = _one_row(
        connection,
        """
        SELECT
          COUNT(*) AS rows,
          COUNT(DISTINCT (order_book_id, trade_date)) AS unique_keys,
          COUNT(DISTINCT order_book_id) AS securities,
          COUNT(DISTINCT trade_date) AS trading_dates,
          MIN(trade_date) AS date_min,
          MAX(trade_date) AS date_max,
          COUNT(*) FILTER (WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL
                            OR prev_close IS NULL) AS core_price_null_rows,
          COUNT(*) FILTER (WHERE volume IS NULL OR total_turnover IS NULL)
            AS activity_null_rows,
          COUNT(*) FILTER (WHERE limit_up IS NULL OR limit_down IS NULL)
            AS limit_null_rows,
          COUNT(*) FILTER (WHERE volume < 0 OR total_turnover < 0)
            AS negative_activity_rows,
          COUNT(*) FILTER (WHERE high < greatest(open, close, low)
                            OR low > least(open, close, high)) AS ohlc_violation_rows,
          COUNT(*) FILTER (WHERE volume = 0 AND total_turnover = 0)
            AS zero_activity_candidate_rows
        FROM rq_rows WHERE trade_date IN (SELECT trade_date FROM target_dates)
        """,
    )
    summary["date_coverage"] = coverage
    summary["all_dates_present"] = len(coverage) == len(DELTA_DATES)
    summary["all_expected_rows_present"] = len(coverage) == len(DELTA_DATES) and all(
        bool(item["exact_row_coverage"]) for item in coverage
    )
    return summary


def _write_curated_delta(connection: Any, staging: Path, observed_at: str) -> list[dict[str, Any]]:
    observed = observed_at.replace("'", "''")
    connection.execute(
        f"""
        CREATE VIEW tail_enriched AS
        SELECT
          r.*,
          u.listed_date,
          u.de_listed_date,
          (u.listed_date <= r.trade_date
            AND (u.de_listed_date IS NULL OR u.de_listed_date >= r.trade_date))
            AS within_observed_listing_boundary,
          CASE WHEN upper(replace(u.symbol, ' ', '')) LIKE '%ST%'
                 OR upper(replace(u.symbol, ' ', '')) LIKE 'PT%'
               THEN TRUE ELSE FALSE END AS rqdata_current_symbol_risk_marker,
          s.risk_warning_state AS resset_risk_warning_state,
          coalesce(s.state_known, FALSE) AS resset_risk_warning_state_known,
          CASE WHEN s.state_known
                    AND s.risk_warning_state =
                        (CASE WHEN upper(replace(u.symbol, ' ', '')) LIKE '%ST%'
                                    OR upper(replace(u.symbol, ' ', '')) LIKE 'PT%'
                              THEN TRUE ELSE FALSE END)
               THEN s.risk_warning_state ELSE NULL END AS st_consensus_state,
          CASE WHEN s.state_known
                    AND s.risk_warning_state =
                        (CASE WHEN upper(replace(u.symbol, ' ', '')) LIKE '%ST%'
                                    OR upper(replace(u.symbol, ' ', '')) LIKE 'PT%'
                              THEN TRUE ELSE FALSE END)
               THEN TRUE ELSE FALSE END AS st_consensus_state_known,
          (r.volume = 0 AND r.total_turnover = 0) AS zero_activity_candidate,
          FALSE AS suspension_state_known,
          (r.limit_up IS NOT NULL AND r.limit_down IS NOT NULL) AS limit_fields_observed,
          '{observed}' AS ingested_at,
          CAST(NULL AS TIMESTAMPTZ) AS vendor_available_at,
          'ricequant_rqdata' AS source_id,
          'none' AS source_adjust_type,
          FALSE AS source_skip_suspended,
          FALSE AS research_eligible
        FROM rq_rows r
        JOIN universe_rows u USING (order_book_id)
        LEFT JOIN st_intervals s
          ON s.order_book_id = r.order_book_id
         AND s.effective_from <= r.trade_date
         AND (s.effective_to_exclusive IS NULL OR r.trade_date < s.effective_to_exclusive)
        WHERE r.trade_date IN ({','.join(f"DATE '{item}'" for item in DELTA_DATES)})
        """
    )
    artifacts: list[dict[str, Any]] = []
    for month, dates in (("07", DELTA_DATES[:3]), ("08", DELTA_DATES[3:])):
        output = (
            staging
            / "curated/schema=r0-shsz-pv-tail-delta-v1"
            / "year=2026"
            / f"month={month}"
            / "part-000.parquet"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output_sql = output.resolve().as_posix().replace("'", "''")
        date_sql = ",".join(f"DATE '{item}'" for item in dates)
        connection.execute(
            f"COPY (SELECT * FROM tail_enriched WHERE trade_date IN ({date_sql}) "
            f"ORDER BY trade_date, order_book_id) TO '{output_sql}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)"
        )
        reference = file_ref(output, relative_to=staging)
        reference["trade_dates"] = list(dates)
        artifacts.append(reference)
    return artifacts


def _core06_assessment(connection: Any) -> dict[str, Any]:
    metrics = _one_row(
        connection,
        """
        SELECT
          COUNT(*) AS rows,
          COUNT(*) FILTER (WHERE within_observed_listing_boundary) AS listing_boundary_known_rows,
          COUNT(*) FILTER (WHERE st_consensus_state_known) AS st_consensus_known_rows,
          COUNT(*) FILTER (WHERE suspension_state_known) AS suspension_known_rows,
          COUNT(*) FILTER (WHERE limit_fields_observed) AS limit_fields_observed_rows,
          COUNT(*) FILTER (
            WHERE within_observed_listing_boundary
              AND st_consensus_state_known
              AND suspension_state_known
              AND limit_fields_observed
          ) AS strict_core06_known_rows
        FROM tail_enriched
        """,
    )
    return {
        "core03": {
            "mechanical_tail_fields": "PASS_IF_QUALITY_CHECKS_PASS",
            "formal_closure": False,
            "remaining_blockers": [
                "RQData retention, derivation and immutable-export rights are not formally admitted.",
                "vendor first-publication and revision-chain available_at is not established.",
                "skip_suspended=false can forward-fill suspended observations; rows are not tradability facts.",
                "CSMAR/RQData differences are retained and not canonically adjudicated.",
            ],
        },
        "core06": {
            "metrics": metrics,
            "mechanical_listing_boundary": metrics["listing_boundary_known_rows"]
            == metrics["rows"],
            "mechanical_st_interval_reference": True,
            "formal_closure": False,
            "unknown_values_fail_closed": True,
            "remaining_blockers": [
                "RESSET STKSPCTRMT is INSPECTION_ONLY/NOT_ADMITTED and has no complete revision/rights closure.",
                "No exact tail-date suspension endpoint was requested; zero activity is not equated with suspension.",
                "Limit fields do not prove complete historical rule versions or exception reasons.",
                "RQData listing boundaries are observed vendor episodes, not a proven permanent-identity/code-reuse chain.",
            ],
        },
    }


def _artifact_inventory(root: Path, excluded: set[str] | None = None) -> list[dict[str, Any]]:
    excluded = excluded or set()
    result: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        result.append(file_ref(path, relative_to=root))
    return result


def finalize() -> dict[str, Any]:
    if CANDIDATE_ROOT.exists() or not STAGING_ROOT.is_dir():
        raise BuildError("expected unfinished staging and absent final candidate")
    if (STAGING_ROOT / "FAILURE.json").exists():
        raise BuildError("acquisition staging contains failure receipt")
    receipt_path = STAGING_ROOT / "ACQUISITION_RECEIPT.json"
    if not receipt_path.is_file():
        raise BuildError("acquisition receipt missing")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("quota_cap_pass") is not True:
        raise BuildError("quota cap not proven")
    if int(receipt.get("business_api_calls_made", -1)) != MAX_BUSINESS_API_CALLS:
        raise BuildError("unexpected API call count in receipt")
    bindings = validate_bound_sources()
    input_bindings = json.loads((STAGING_ROOT / "INPUT_BINDINGS.json").read_text(encoding="utf-8"))
    state_before = input_bindings["protected_state_before"]
    if protected_state() != state_before:
        raise BuildError("protected authority state changed before finalization")

    import duckdb

    connection = duckdb.connect(database=":memory:")
    connection.execute("PRAGMA threads=4")
    st_statistics = _build_st_intervals(connection, STAGING_ROOT)
    _create_views(connection, bindings)
    base_statistics = _base_statistics(connection)
    if base_statistics["rows"] != base_statistics["unique_keys"]:
        raise BuildError("base logical reference contains duplicate keys")
    if base_statistics["date_min"] != "2025-01-02" or base_statistics["date_max"] != CALIBRATION_DATE:
        raise BuildError(f"unexpected base logical date range: {base_statistics}")
    calibration = _calibration_statistics(connection)
    tail_quality = _tail_quality(connection)
    if not tail_quality["all_dates_present"]:
        raise BuildError("one or more target delta dates are absent")
    if tail_quality["rows"] != tail_quality["unique_keys"]:
        raise BuildError("tail delta contains duplicate keys")
    curated_partitions = _write_curated_delta(
        connection, STAGING_ROOT, str(receipt["observed_at"])
    )
    core_assessment = _core06_assessment(connection)
    connection.close()

    quality = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "mechanical_status": "PASS"
        if tail_quality["all_expected_rows_present"]
        and tail_quality["ohlc_violation_rows"] == 0
        and tail_quality["negative_activity_rows"] == 0
        else "REVIEW",
        "admission_decision": "NOT_ADMITTED",
        "purpose_ceiling": "INSPECTION_ONLY",
        "research_ready": False,
        "base_logical_reference": base_statistics,
        "tail_delta": tail_quality,
        "cross_source_calibration": calibration,
        "st_interval_reference": st_statistics,
        "core03_core06": core_assessment,
        "unknown_values_fail_closed": True,
    }
    write_json(STAGING_ROOT / "quality/summary.json", quality)
    write_json(STAGING_ROOT / "CORE03_CORE06_ASSESSMENT.json", core_assessment)
    request = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "contract_id": CONTRACT_ID,
        "scope": {
            "venues": ["XSHG", "XSHE"],
            "base_logical_dates": ["2025-01-01", CALIBRATION_DATE],
            "calibration_date": CALIBRATION_DATE,
            "delta_dates": list(DELTA_DATES),
            "fields": list(PRICE_FIELDS),
            "bjse_included": False,
        },
        "formal_effect": "NONE__QUARANTINE_ONLY",
    }
    write_json(STAGING_ROOT / "request.json", request)
    provenance = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "contract": file_ref(CONTRACT_PATH),
        "bound_sources": bindings,
        "api_acquisition_receipt": file_ref(receipt_path, relative_to=STAGING_ROOT),
        "transformations": [
            "CSMAR 2025-01-01..2026-07-28 filtered logical reference; no partition copied.",
            "RQData 2026-07-28 retained only for cross-source calibration.",
            "RQData 2026-07-29,30,31 and 2026-08-03 persisted as unadjusted tail delta.",
            "RESSET ST events collapsed to effective intervals; conflicts and missing states remain unknown.",
            "No vendor-adjusted price, imputation, canonical conflict override or factor computation.",
        ],
        "credential_value_captured": False,
        "formal_catalog_write": False,
        "roadmap_write": False,
        "source_registry_write": False,
    }
    write_json(STAGING_ROOT / "provenance.json", provenance)
    partition_manifest = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "logical_base_partitions": bindings["csmar_base"]["selected_partitions"],
        "physical_delta_partitions": curated_partitions,
        "st_interval_reference": st_statistics["artifact"],
        "copy_base_partitions": False,
    }
    write_json(STAGING_ROOT / "partition-manifest.json", partition_manifest)
    readme = f"""# 沪深 A 股 R0 价量尾部增量候选

状态：`QUARANTINE_ONLY__NOT_ADMITTED`

本候选没有把 2005–2024 或 2025–2026 基座再复制一遍。它以 19 个已哈希 CSMAR
月分区作为 2025-01-02 至 2026-07-28 的逻辑引用，只物理保存 RQData 的
2026-07-29、07-30、07-31、08-03 四个增量交易日。2026-07-28 的 RQData 返回仅用于
跨源重叠校准。

- 基座逻辑行数：{base_statistics['rows']:,}
- 尾部增量行数：{tail_quality['rows']:,}
- 尾部证券数：{tail_quality['securities']:,}
- API 流量：{receipt['quota_bytes_used_delta']:,} B / {MAX_QUOTA_BYTES:,} B
- 四日逐日预期覆盖：{'PASS' if tail_quality['all_expected_rows_present'] else 'REVIEW'}
- 尾部机械质量：`{quality['mechanical_status']}`
- 正式研究可用：`false`

CORE-03 的尾部字段、主键、日期、OHLC、非负活动量与跨源差异已经机械化；但来源权利、
真实 available_at、修订链和停牌填充语义尚未正式闭合。CORE-06 复用了已哈希的 RESSET
ST 事件并生成机械有效区间，同时保存 RQData 上市边界和涨跌停字段；停牌仍为未知，
ST 无证据或冲突行仍为未知，所有未知均失败关闭。因此本包不能单独关闭 R0。
"""
    atomic_write(STAGING_ROOT / "README.md", readme.encode("utf-8"))
    manifest = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "candidate_id": CANDIDATE_ID,
        "dataset_id": "r0-shsz-pv-tail-delta-candidate",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "lifecycle": "candidate",
        "mutability": "immutable_after_seal",
        "purpose_ceiling": "INSPECTION_ONLY",
        "quality_status": quality["mechanical_status"],
        "source_admission": False,
        "catalog_registered": False,
        "research_ready": False,
        "formal_effect": "NONE__QUARANTINE_ONLY",
        "base_copy_performed": False,
        "statistics": {
            "base_logical": base_statistics,
            "tail_delta": tail_quality,
            "st_intervals": st_statistics,
        },
        "artifacts": _artifact_inventory(STAGING_ROOT, {"manifest.json"}),
    }
    write_json(STAGING_ROOT / "manifest.json", manifest)
    if protected_state() != state_before:
        raise BuildError("protected authority state changed during finalization")
    os.replace(STAGING_ROOT, CANDIDATE_ROOT)

    if REPORT_ROOT.exists():
        raise BuildError("report root already exists")
    REPORT_ROOT.mkdir(parents=True, exist_ok=False)
    state_after = protected_state()
    result = {
        "status": "PASS__QUARANTINE_TAIL_DELTA_VERIFIED__FORMAL_ADMISSION_FALSE",
        "run_id": RUN_ID,
        "candidate_manifest": file_ref(CANDIDATE_ROOT / "manifest.json"),
        "base_logical_rows": base_statistics["rows"],
        "tail_delta_rows": tail_quality["rows"],
        "tail_dates": list(DELTA_DATES),
        "tail_securities": tail_quality["securities"],
        "quota_bytes_used_delta": receipt["quota_bytes_used_delta"],
        "quota_cap_bytes": MAX_QUOTA_BYTES,
        "mechanical_quality": quality["mechanical_status"],
        "all_expected_rows_present": tail_quality["all_expected_rows_present"],
        "formal_effect": "NONE",
    }
    write_json(REPORT_ROOT / "RESULT.json", result)
    write_json(
        REPORT_ROOT / "PROTECTED_STATE.json",
        {"before": state_before, "after": state_after, "unchanged": state_before == state_after},
    )
    atomic_write(REPORT_ROOT / "README.md", readme.encode("utf-8"))
    report_manifest = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "candidate_manifest": result["candidate_manifest"],
        "files": _artifact_inventory(REPORT_ROOT, {"REPORT_MANIFEST.json"}),
        "formal_effect": "NONE__REPORT_ONLY",
    }
    write_json(REPORT_ROOT / "REPORT_MANIFEST.json", report_manifest)
    return verify()


def verify() -> dict[str, Any]:
    manifest_path = CANDIDATE_ROOT / "manifest.json"
    report_manifest_path = REPORT_ROOT / "REPORT_MANIFEST.json"
    if not manifest_path.is_file() or not report_manifest_path.is_file():
        raise BuildError("candidate/report manifest missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("purpose_ceiling") != "INSPECTION_ONLY":
        raise BuildError("purpose ceiling changed")
    if manifest.get("source_admission") is not False or manifest.get("research_ready") is not False:
        raise BuildError("formal safety flags changed")
    if manifest.get("base_copy_performed") is not False:
        raise BuildError("base-copy boundary changed")
    for item in manifest.get("artifacts", []):
        path = CANDIDATE_ROOT / item["path"]
        if not path.is_file() or path.stat().st_size != int(item["bytes"]):
            raise BuildError(f"candidate artifact size mismatch: {path}")
        if sha256_file(path) != item["sha256"]:
            raise BuildError(f"candidate artifact hash mismatch: {path}")
    report_manifest = json.loads(report_manifest_path.read_text(encoding="utf-8"))
    for item in report_manifest.get("files", []):
        path = REPORT_ROOT / item["path"]
        if not path.is_file() or path.stat().st_size != int(item["bytes"]):
            raise BuildError(f"report artifact size mismatch: {path}")
        if sha256_file(path) != item["sha256"]:
            raise BuildError(f"report artifact hash mismatch: {path}")
    protected = json.loads((REPORT_ROOT / "PROTECTED_STATE.json").read_text(encoding="utf-8"))
    if protected.get("unchanged") is not True or protected.get("after") != protected_state():
        raise BuildError("protected authority state drifted")
    validate_bound_sources()
    quality = json.loads((CANDIDATE_ROOT / "quality/summary.json").read_text(encoding="utf-8"))
    return {
        "status": "PASS__QUARANTINE_TAIL_DELTA_VERIFIED__FORMAL_ADMISSION_FALSE",
        "candidate_manifest": file_ref(manifest_path),
        "report_manifest": file_ref(report_manifest_path),
        "base_logical_rows": quality["base_logical_reference"]["rows"],
        "tail_delta_rows": quality["tail_delta"]["rows"],
        "tail_dates": list(DELTA_DATES),
        "tail_securities": quality["tail_delta"]["securities"],
        "quota_bytes_used_delta": json.loads(
            (CANDIDATE_ROOT / "ACQUISITION_RECEIPT.json").read_text(encoding="utf-8")
        )["quota_bytes_used_delta"],
        "mechanical_quality": quality["mechanical_status"],
        "formal_effect": "NONE",
    }


def offline_check() -> dict[str, Any]:
    bindings = validate_bound_sources()
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    checks = {
        "contract_id": contract.get("contract_id") == CONTRACT_ID,
        "retry_zero": contract["request_budget"]["retry_count"] == 0,
        "quota_cap": contract["request_budget"]["maximum_quota_bytes"] == MAX_QUOTA_BYTES,
        "base_manifest_hash": bindings["csmar_base"]["manifest"]["sha256"]
        == BASE_MANIFEST_SHA256,
        "st_manifest_hash": bindings["resset_st_history"]["manifest"]["sha256"]
        == ST_MANIFEST_SHA256,
        "selected_base_partitions": len(bindings["csmar_base"]["selected_partitions"]) == 19,
    }
    return {
        "status": "OFFLINE_CHECK_PASS" if all(checks.values()) else "OFFLINE_CHECK_FAIL",
        "checks": checks,
        "network_calls": 0,
        "writes": 0,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build bounded RQData R0 SH/SZ price-volume tail delta")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline-check", action="store_true")
    mode.add_argument("--acquire", action="store_true")
    mode.add_argument("--finalize", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.offline_check:
            result = offline_check()
        elif args.acquire:
            result = acquire()
        elif args.finalize:
            result = finalize()
        else:
            result = verify()
    except Exception as exc:
        print(json.dumps({"status": "FAIL_CLOSED", "error": safe_error(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(json_safe(result), ensure_ascii=False, sort_keys=True))
    return 0 if "FAIL" not in str(result.get("status", "")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
