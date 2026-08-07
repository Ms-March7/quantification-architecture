#!/usr/bin/env python3
"""Build the quarantine-only R0_SHSZ_PV v2 composite panel candidate.

The builder is intentionally narrow and fail-closed.  It combines the pinned
2005-2024 CSMAR-derived base, the pinned 2025-2026-07-28 local CSMAR tail and
the four-day RQData unadjusted delta.  It does not edit the formal catalog,
Roadmap, source registry, formal snapshots or factor runs.

RQData delta returns are calibrated only on the exact corporate-action keys:

    close * ex_factor / previous_close - 1

Non-event RQData rows use an exact factor of 1.  Historical/local CSMAR rows
retain Dretwd (vendor total return with cash reinvestment) and never consume a
latest forward-adjusted price history.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import duckdb


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = WORKSPACE_ROOT / "data/catalog/contracts/r0_shsz_pv_v2/contract.json"
DEFAULT_OUTPUT = (
    WORKSPACE_ROOT
    / "data/market/quarantine/r0-shsz-pv-panel/candidate-20260804-v1"
)
HISTORICAL_ROOT = (
    WORKSPACE_ROOT / "data/market/quarantine/r0-pv-bounded/candidate-20260801-v1"
)
LOCAL_TAIL_ROOT = (
    WORKSPACE_ROOT
    / "tmp/local-dataset-build/csmar-trd-dalyr-pilot/"
    "d2a04b-csmar-trd-dalyr-2022-2026-20260728-v1"
)
RQ_DELTA_ROOT = (
    WORKSPACE_ROOT
    / "data/market/quarantine/r0-pv-tail-delta/candidate-20260804-v1"
)
MASTER_CALENDAR_ROOT = (
    WORKSPACE_ROOT
    / "data/market/quarantine/rqdata-r0-shsz-core01-core02/"
    "candidate-20260804-v1"
)
ACTION_ROOT = (
    WORKSPACE_ROOT
    / "data/market/quarantine/ricequant-r0-core04-core05/"
    "candidate-20260804-v1"
)
ACTION_CONTRACT_PATH = (
    WORKSPACE_ROOT
    / "data/catalog/contracts/ricequant_r0_core04_core05_candidate_v1/contract.json"
)
RQDATAC_SITE_PACKAGES = (
    WORKSPACE_ROOT / "control/runtime/ricequant-rqsdk/.venv/Lib/site-packages"
)

RUN_ID = "r0-shsz-pv-panel-candidate-20260804-v1"
CANDIDATE_ID = "r0-shsz-pv-panel-candidate-20260804-v1"
DATASET_ID = "cn-a-share-r0-shsz-pv-panel"
SNAPSHOT_ID = "20050104-20260803-v1"
PANEL_SCHEMA = "r0-shsz-pv-panel-v2"
PURPOSE_CEILING = "INSPECTION_ONLY"
START_DATE = date(2005, 1, 4)
HISTORICAL_END = date(2024, 12, 31)
LOCAL_TAIL_START = date(2025, 1, 2)
LOCAL_TAIL_END = date(2026, 7, 28)
END_DATE = date(2026, 8, 3)
TERMINAL_QUERY_END = date(2026, 8, 5)
TARGET_DELTA_DATES = (
    date(2026, 7, 29),
    date(2026, 7, 30),
    date(2026, 7, 31),
    date(2026, 8, 3),
)
EXPECTED_DELTA_ROWS_BY_DATE = {
    "2026-07-29": 5200,
    "2026-07-30": 5201,
    "2026-07-31": 5201,
    "2026-08-03": 5201,
}
EXPECTED_ACTION_KEYS_BY_DATE = {
    "2026-07-29": 12,
    "2026-07-30": 15,
    "2026-07-31": 24,
    "2026-08-03": 7,
}
MARKET_TYPES = (1, 4, 16, 32)
EXCLUDED_ORDER_BOOK_IDS = {"600849.XSHG"}

REQUIRED_FIELDS = (
    "order_book_id",
    "exchange_mic",
    "local_code",
    "trade_date",
    "previous_close",
    "open",
    "high",
    "low",
    "close",
    "volume_shares",
    "turnover_cny",
    "realized_total_return",
    "trading_status_code",
    "upper_limit_price",
    "lower_limit_price",
    "available_at",
    "listed_episode_eligible",
    "had_trade",
    "open_execution_eligible",
    "source_id",
    "source_snapshot_id",
)

PROTECTED_FILES = (
    WORKSPACE_ROOT / "data/catalog/datasets.json",
    WORKSPACE_ROOT / "data/catalog/planned_datasets.json",
    WORKSPACE_ROOT
    / "data/catalog/contracts/source_admission_v1/current_source_registry.json",
    WORKSPACE_ROOT / "control/ROADMAP.md",
)
PROTECTED_TREES = (
    WORKSPACE_ROOT / "data/market/snapshots",
    WORKSPACE_ROOT / "data/published",
)


class BuildError(RuntimeError):
    """Raised when a candidate invariant is not satisfied."""


@dataclass(frozen=True)
class BuildInputs:
    contract_path: Path = CONTRACT_PATH
    historical_root: Path = HISTORICAL_ROOT
    local_tail_root: Path = LOCAL_TAIL_ROOT
    delta_root: Path = RQ_DELTA_ROOT
    master_calendar_root: Path = MASTER_CALENDAR_ROOT
    action_root: Path = ACTION_ROOT
    action_contract_path: Path = ACTION_CONTRACT_PATH


@dataclass(frozen=True)
class BuildExpectations:
    delta_rows_by_date: Mapping[str, int]
    action_keys_by_date: Mapping[str, int]
    coverage_start: date = START_DATE
    coverage_end: date = END_DATE


PRODUCTION_EXPECTATIONS = BuildExpectations(
    delta_rows_by_date=EXPECTED_DELTA_ROWS_BY_DATE,
    action_keys_by_date=EXPECTED_ACTION_KEYS_BY_DATE,
)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(WORKSPACE_ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def file_receipt(path: Path, *, relative_to: Path | None = None) -> dict[str, Any]:
    if not path.is_file() or is_link_or_reparse(path):
        raise BuildError(f"plain file required: {path}")
    if relative_to is None:
        name = display_path(path)
    else:
        name = path.resolve().relative_to(relative_to.resolve()).as_posix()
    return {"relative_path": name, "sha256": sha256_file(path), "bytes": path.stat().st_size}


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, canonical_bytes(value) + b"\n")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BuildError(f"JSON object required: {path}")
    return value


def quote_sql(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def sql_path_list(paths: Sequence[Path]) -> str:
    if not paths:
        raise BuildError("at least one input path is required")
    return "[" + ",".join(quote_sql(path.resolve().as_posix()) for path in paths) + "]"


def discover_parquets(root: Path) -> list[Path]:
    if not root.is_dir() or is_link_or_reparse(root):
        raise BuildError(f"plain input root required: {root}")
    paths = sorted(root.rglob("*.parquet"))
    if not paths:
        raise BuildError(f"no parquet input below: {root}")
    for path in paths:
        if is_link_or_reparse(path):
            raise BuildError(f"linked/reparse payload forbidden: {path}")
    return paths


def _artifact_index(manifest: Mapping[str, Any], path_key: str) -> dict[str, Mapping[str, Any]]:
    records = manifest.get("artifacts")
    if not isinstance(records, list):
        raise BuildError("manifest artifacts must be an array")
    result: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get(path_key), str):
            raise BuildError("malformed manifest artifact")
        key = str(record[path_key]).replace("\\", "/")
        if key in result:
            raise BuildError(f"duplicate manifest artifact: {key}")
        result[key] = record
    return result


def _verify_receipt(path: Path, record: Mapping[str, Any]) -> None:
    expected_bytes = record.get("bytes")
    expected_sha = record.get("sha256")
    if not isinstance(expected_bytes, int) or not isinstance(expected_sha, str):
        raise BuildError(f"invalid hash receipt for {path}")
    actual_bytes = path.stat().st_size if path.is_file() else -1
    actual_sha = sha256_file(path) if path.is_file() else None
    if actual_bytes != expected_bytes or actual_sha != expected_sha:
        raise BuildError(f"input hash/byte mismatch: {path}")


def verify_historical_authority(root: Path, parquets: Sequence[Path]) -> list[dict[str, Any]]:
    manifest_path = root / "CANDIDATE_MANIFEST.json"
    manifest = read_json(manifest_path)
    index = _artifact_index(manifest, "path")
    receipts = [file_receipt(manifest_path)]
    for path in parquets:
        relative = path.relative_to(root).as_posix()
        record = index.get(relative)
        if record is None:
            raise BuildError(f"historical parquet missing from manifest: {relative}")
        _verify_receipt(path, record)
        receipts.append(file_receipt(path))
    return receipts


def local_tail_parquets(root: Path, *, verify: bool) -> tuple[list[Path], list[dict[str, Any]]]:
    manifest_path = root / "manifest.json"
    partition_path = root / "partition-manifest.json"
    partition_manifest = read_json(partition_path)
    records = partition_manifest.get("partitions")
    if not isinstance(records, list):
        raise BuildError("local-tail partition manifest is malformed")
    selected: list[Path] = []
    receipts = [file_receipt(manifest_path), file_receipt(partition_path)]
    for record in records:
        if not isinstance(record, dict):
            raise BuildError("malformed local-tail partition record")
        year = int(record.get("year"))
        month = int(record.get("month"))
        if year < 2025 or (year == 2026 and month > 7):
            continue
        relative = record.get("relative_path")
        if not isinstance(relative, str):
            raise BuildError("local-tail partition path missing")
        path = root / relative
        if verify:
            _verify_receipt(path, record)
        selected.append(path)
        receipts.append(file_receipt(path))
    if len(selected) != 19:
        raise BuildError(f"expected 19 local-tail partitions, found {len(selected)}")
    return selected, receipts


def verify_delta_authority(root: Path, parquets: Sequence[Path]) -> list[dict[str, Any]]:
    manifest_path = root / "manifest.json"
    manifest = read_json(manifest_path)
    index = _artifact_index(manifest, "path")
    receipts = [file_receipt(manifest_path)]
    for path in parquets:
        relative = path.relative_to(root).as_posix()
        record = index.get(relative)
        if record is None:
            raise BuildError(f"delta parquet missing from manifest: {relative}")
        _verify_receipt(path, record)
        receipts.append(file_receipt(path))
    return receipts


def verify_master_calendar_authority(root: Path) -> tuple[Path, Path, list[dict[str, Any]]]:
    manifest_path = root / "CANDIDATE_MANIFEST.json"
    manifest = read_json(manifest_path)
    index = _artifact_index(manifest, "path")
    master = root / "normalized/rqdata_shsz_cs_master.jsonl"
    calendar = root / "normalized/rqdata_trading_dates.jsonl"
    receipts = [file_receipt(manifest_path)]
    for path in (master, calendar):
        relative = path.relative_to(root).as_posix()
        record = index.get(relative)
        if record is None:
            raise BuildError(f"master/calendar artifact missing from manifest: {relative}")
        _verify_receipt(path, record)
        receipts.append(file_receipt(path))
    return master, calendar, receipts


def verify_action_authority(root: Path) -> tuple[Path, Path, list[dict[str, Any]]]:
    reconciliation_path = root / "RECONCILIATION.json"
    reconciliation_manifest_path = root / "RECONCILIATION_MANIFEST.json"
    reconciliation_manifest = read_json(reconciliation_manifest_path)
    entries = reconciliation_manifest.get("entries")
    if not isinstance(entries, list):
        raise BuildError("action reconciliation manifest entries missing")
    entry_index = {
        str(record.get("path")): record for record in entries if isinstance(record, dict)
    }
    reconciliation_key = display_path(reconciliation_path)
    if reconciliation_key not in entry_index:
        raise BuildError("RECONCILIATION.json is not bound")
    _verify_receipt(reconciliation_path, entry_index[reconciliation_key])
    reconciliation = read_json(reconciliation_path)
    bindings = reconciliation.get("input_bindings")
    if not isinstance(bindings, dict):
        raise BuildError("action input bindings missing")
    action = root / "aggregate/ACTION_EVENTS_TAIL_2025_20260803.jsonl"
    factors = root / "aggregate/EX_FACTORS_TAIL_2025_20260803.jsonl"
    receipts = [file_receipt(reconciliation_manifest_path), file_receipt(reconciliation_path)]
    for path in (action, factors):
        key = display_path(path)
        record = bindings.get(key)
        if not isinstance(record, dict):
            raise BuildError(f"action aggregate is not hash-bound: {key}")
        _verify_receipt(path, record)
        receipts.append(file_receipt(path))
    return action, factors, receipts


def bind_inputs(inputs: BuildInputs, *, verify_authorities: bool) -> dict[str, Any]:
    contract = read_json(inputs.contract_path)
    if contract.get("contract_id") != "r0_shsz_pv_v2":
        raise BuildError("unexpected R0 contract identity")
    required = contract.get("normalized_panel_required_fields")
    if required != list(REQUIRED_FIELDS):
        raise BuildError("R0 contract required fields drifted")

    historical_parquets = discover_parquets(inputs.historical_root / "curated")
    delta_parquets = discover_parquets(inputs.delta_root / "curated")
    if verify_authorities:
        historical_receipts = verify_historical_authority(
            inputs.historical_root, historical_parquets
        )
        local_parquets, local_receipts = local_tail_parquets(
            inputs.local_tail_root, verify=True
        )
        delta_receipts = verify_delta_authority(inputs.delta_root, delta_parquets)
        master, calendar, master_receipts = verify_master_calendar_authority(
            inputs.master_calendar_root
        )
        actions, factors, action_receipts = verify_action_authority(inputs.action_root)
    else:
        local_parquets = discover_parquets(inputs.local_tail_root / "curated")
        master = inputs.master_calendar_root / "normalized/rqdata_shsz_cs_master.jsonl"
        calendar = inputs.master_calendar_root / "normalized/rqdata_trading_dates.jsonl"
        actions = inputs.action_root / "aggregate/ACTION_EVENTS_TAIL_2025_20260803.jsonl"
        factors = inputs.action_root / "aggregate/EX_FACTORS_TAIL_2025_20260803.jsonl"
        historical_receipts = [file_receipt(path) for path in historical_parquets]
        local_receipts = [file_receipt(path) for path in local_parquets]
        delta_receipts = [file_receipt(path) for path in delta_parquets]
        master_receipts = [file_receipt(master), file_receipt(calendar)]
        action_receipts = [file_receipt(actions), file_receipt(factors)]

    value: dict[str, Any] = {
        "schema_version": 1,
        "contract": file_receipt(inputs.contract_path),
        "action_contract": file_receipt(inputs.action_contract_path),
        "sources": {
            "historical_base": {
                "root": display_path(inputs.historical_root),
                "payload_files": historical_receipts,
            },
            "local_tail": {
                "root": display_path(inputs.local_tail_root),
                "payload_files": local_receipts,
            },
            "ricequant_delta": {
                "root": display_path(inputs.delta_root),
                "payload_files": delta_receipts,
            },
            "master_calendar": {
                "root": display_path(inputs.master_calendar_root),
                "payload_files": master_receipts,
            },
            "corporate_actions": {
                "root": display_path(inputs.action_root),
                "payload_files": action_receipts,
            },
        },
        "selection": {
            "coverage_start": START_DATE.isoformat(),
            "coverage_end": END_DATE.isoformat(),
            "market_types": list(MARKET_TYPES),
            "venues": ["XSHG", "XSHE"],
            "ordinary_a_share_namespaces": {
                "XSHG_MAIN": ["600", "601", "603", "605"],
                "XSHG_STAR": ["688"],
                "XSHE_MAIN": ["000", "001", "002", "003"],
                "XSHE_CHINEXT": ["300", "301", "302"],
            },
            "explicit_exclusions": sorted(EXCLUDED_ORDER_BOOK_IDS),
            "bjse_in_scope": False,
            "b_shares_in_scope": False,
            "cdr_689_namespace_in_scope": False,
        },
    }
    value["binding_sha256"] = sha256_bytes(canonical_bytes(value))
    value["resolved_paths"] = {
        "historical_parquets": [path.resolve().as_posix() for path in historical_parquets],
        "local_tail_parquets": [path.resolve().as_posix() for path in local_parquets],
        "delta_parquets": [path.resolve().as_posix() for path in delta_parquets],
        "master": master.resolve().as_posix(),
        "calendar": calendar.resolve().as_posix(),
        "actions": actions.resolve().as_posix(),
        "factors": factors.resolve().as_posix(),
    }
    return value


def fetch_terminal_session_receipt() -> dict[str, Any]:
    if str(RQDATAC_SITE_PACKAGES) not in sys.path:
        sys.path.insert(0, str(RQDATAC_SITE_PACKAGES))
    import rqdatac

    observed_at = datetime.now(timezone.utc).isoformat()
    rqdatac.init(enable_bjse=False)
    values = rqdatac.get_trading_dates(END_DATE.isoformat(), TERMINAL_QUERY_END.isoformat())
    returned = sorted({str(value)[:10] for value in values})
    later = [value for value in returned if value > END_DATE.isoformat()]
    if not later or later[0] != "2026-08-04":
        raise BuildError(
            "bounded RQData terminal calendar did not establish 2026-08-04 as the next session"
        )
    value: dict[str, Any] = {
        "schema_version": 1,
        "receipt_kind": "RQDATA_BOUNDED_TERMINAL_SESSION_OBSERVATION",
        "source_id": "RQDATA_TRADING_CALENDAR",
        "observed_at_utc": observed_at,
        "method": "get_trading_dates",
        "parameters": {
            "start_date": END_DATE.isoformat(),
            "end_date": TERMINAL_QUERY_END.isoformat(),
            "enable_bjse": False,
        },
        "returned_trading_dates": returned,
        "next_trade_date_after_panel_end": later[0],
        "credential_value_captured": False,
        "license_value_captured": False,
        "business_api_calls": 1,
    }
    value["receipt_sha256"] = sha256_bytes(canonical_bytes(value))
    return value


def test_terminal_session_receipt(
    returned_dates: Sequence[str], *, observed_at_utc: str = "2026-08-04T00:00:00+00:00"
) -> dict[str, Any]:
    """Construct a deterministic receipt for unit tests only."""
    returned = sorted(set(returned_dates))
    later = [value for value in returned if value > END_DATE.isoformat()]
    if not later:
        raise BuildError("test terminal receipt needs a date after panel end")
    value: dict[str, Any] = {
        "schema_version": 1,
        "receipt_kind": "TEST_FIXTURE_TERMINAL_SESSION_OBSERVATION",
        "source_id": "TEST_FIXTURE",
        "observed_at_utc": observed_at_utc,
        "method": "fixture",
        "parameters": {},
        "returned_trading_dates": returned,
        "next_trade_date_after_panel_end": later[0],
        "credential_value_captured": False,
        "license_value_captured": False,
        "business_api_calls": 0,
    }
    value["receipt_sha256"] = sha256_bytes(canonical_bytes(value))
    return value


def validate_terminal_receipt(value: Mapping[str, Any], *, allow_test_fixture: bool) -> None:
    expected_sha = value.get("receipt_sha256")
    unsigned = dict(value)
    unsigned.pop("receipt_sha256", None)
    if expected_sha != sha256_bytes(canonical_bytes(unsigned)):
        raise BuildError("terminal session receipt hash mismatch")
    if value.get("credential_value_captured") is not False:
        raise BuildError("terminal session receipt captured a credential value")
    if value.get("next_trade_date_after_panel_end") != "2026-08-04":
        raise BuildError("terminal next trading session is not 2026-08-04")
    if value.get("source_id") == "TEST_FIXTURE" and not allow_test_fixture:
        raise BuildError("test terminal receipt is forbidden for production output")


def protected_state() -> dict[str, Any]:
    files = {}
    for path in PROTECTED_FILES:
        files[display_path(path)] = file_receipt(path)
    trees: dict[str, Any] = {}
    for root in PROTECTED_TREES:
        if not root.exists():
            trees[display_path(root)] = {
                "exists": False,
                "file_count": 0,
                "bytes": 0,
                "entries_sha256": sha256_bytes(b"[]"),
            }
            continue
        if is_link_or_reparse(root):
            raise BuildError(f"protected tree is linked/reparse: {root}")
        entries = [
            file_receipt(path, relative_to=root)
            for path in sorted(item for item in root.rglob("*") if item.is_file())
        ]
        trees[display_path(root)] = {
            "exists": True,
            "file_count": len(entries),
            "bytes": sum(item["bytes"] for item in entries),
            "entries_sha256": sha256_bytes(canonical_bytes(entries)),
        }
    state = {"files": files, "trees": trees}
    state["state_sha256"] = sha256_bytes(canonical_bytes(state))
    return state


def _ordinary_scope_sql(code: str, market_type: str) -> str:
    return f"""
    (
      ({market_type}=1 AND regexp_matches({code}, '^(600|601|603|605)[0-9]{{3}}$'))
      OR ({market_type}=4 AND regexp_matches({code}, '^(000|001|002|003)[0-9]{{3}}$'))
      OR ({market_type}=16 AND regexp_matches({code}, '^(300|301|302)[0-9]{{3}}$'))
      OR ({market_type}=32 AND regexp_matches({code}, '^688[0-9]{{3}}$'))
    )
    """


def _date_values(values: Iterable[date]) -> str:
    return ",".join(f"DATE '{value.isoformat()}'" for value in values)


def configure_connection(connection: duckdb.DuckDBPyConnection, temporary: Path) -> None:
    temporary.mkdir(parents=True, exist_ok=True)
    connection.execute("SET TimeZone='Asia/Shanghai'")
    connection.execute("SET preserve_insertion_order=false")
    connection.execute(f"SET threads={max(1, min(os.cpu_count() or 4, 12))}")
    connection.execute("SET memory_limit='4GB'")
    connection.execute(f"SET temp_directory={quote_sql(temporary.resolve().as_posix())}")


def create_source_views(
    connection: duckdb.DuckDBPyConnection,
    bindings: Mapping[str, Any],
    terminal_receipt: Mapping[str, Any],
) -> None:
    paths = bindings["resolved_paths"]
    historical_files = [Path(value) for value in paths["historical_parquets"]]
    local_files = [Path(value) for value in paths["local_tail_parquets"]]
    delta_files = [Path(value) for value in paths["delta_parquets"]]
    connection.execute(
        "CREATE TEMP VIEW historical_source AS SELECT * FROM read_parquet("
        f"{sql_path_list(historical_files)}, union_by_name=true, hive_partitioning=true)"
    )
    connection.execute(
        "CREATE TEMP VIEW local_tail_source AS SELECT * FROM read_parquet("
        f"{sql_path_list(local_files)}, union_by_name=true, hive_partitioning=true)"
    )
    connection.execute(
        "CREATE TEMP VIEW delta_source AS SELECT * FROM read_parquet("
        f"{sql_path_list(delta_files)}, union_by_name=true, hive_partitioning=true)"
    )
    connection.execute(
        "CREATE TEMP VIEW security_master AS SELECT * FROM read_json_auto("
        f"{quote_sql(paths['master'])})"
    )
    connection.execute(
        "CREATE TEMP TABLE calendar_dates AS SELECT DISTINCT CAST(trading_date AS DATE) "
        "AS trade_date FROM read_json_auto("
        f"{quote_sql(paths['calendar'])})"
    )
    returned_dates = terminal_receipt.get("returned_trading_dates")
    if not isinstance(returned_dates, list) or not returned_dates:
        raise BuildError("terminal receipt returned dates missing")
    terminal_values = ",".join(f"(DATE '{value}')" for value in returned_dates)
    connection.execute(
        "INSERT INTO calendar_dates SELECT terminal_trade_date FROM (VALUES "
        f"{terminal_values}) AS terminal(terminal_trade_date) WHERE terminal_trade_date NOT IN "
        "(SELECT trade_date FROM calendar_dates)"
    )
    connection.execute(
        """
        CREATE TEMP VIEW calendar_next AS
        SELECT trade_date, LEAD(trade_date) OVER (ORDER BY trade_date) AS next_trade_date
        FROM (SELECT DISTINCT trade_date FROM calendar_dates)
        """
    )
    target_dates = _date_values(TARGET_DELTA_DATES)
    connection.execute(
        """
        CREATE TEMP VIEW action_keys AS
        SELECT security_id AS order_book_id, CAST(event_date AS DATE) AS trade_date,
               COUNT(*)::BIGINT AS event_count
        FROM read_json_auto("""
        + quote_sql(paths["actions"])
        + f""", union_by_name=true)
        WHERE CAST(event_date AS DATE) IN ({target_dates})
        GROUP BY 1, 2
        """
    )
    connection.execute(
        """
        CREATE TEMP VIEW factor_rows AS
        SELECT security_id AS order_book_id, CAST(event_date AS DATE) AS trade_date,
               CAST(ex_factor AS DOUBLE) AS ex_factor
        FROM read_json_auto("""
        + quote_sql(paths["factors"])
        + f""", union_by_name=true)
        WHERE CAST(event_date AS DATE) IN ({target_dates})
        """
    )

    market_types = ",".join(str(value) for value in MARKET_TYPES)
    base_scope = _ordinary_scope_sql("source_local_code", "market_type")
    tail_scope = _ordinary_scope_sql("Stkcd", "Markettype")
    connection.execute(
        f"""
        CREATE TEMP VIEW historical_market_rows AS
        SELECT * FROM historical_source
        WHERE trade_date BETWEEN DATE '{START_DATE}' AND DATE '{HISTORICAL_END}'
          AND market_type IN ({market_types})
        """
    )
    connection.execute(
        f"""
        CREATE TEMP VIEW local_market_rows AS
        SELECT * FROM local_tail_source
        WHERE Trddt BETWEEN DATE '{LOCAL_TAIL_START}' AND DATE '{LOCAL_TAIL_END}'
          AND Markettype IN ({market_types})
        """
    )
    connection.execute(
        f"""
        CREATE TEMP VIEW delta_market_rows AS
        SELECT *,
          CASE
            WHEN split_part(order_book_id,'.',2)='XSHG'
                 AND left(split_part(order_book_id,'.',1),3)='688' THEN 32
            WHEN split_part(order_book_id,'.',2)='XSHG' THEN 1
            WHEN split_part(order_book_id,'.',2)='XSHE'
                 AND left(split_part(order_book_id,'.',1),3) IN ('300','301','302') THEN 16
            WHEN split_part(order_book_id,'.',2)='XSHE' THEN 4
            ELSE NULL
          END AS inferred_market_type
        FROM delta_source
        WHERE trade_date IN ({target_dates})
        """
    )
    connection.execute(
        f"""
        CREATE TEMP VIEW historical_scoped AS
        SELECT * FROM historical_market_rows
        WHERE {base_scope}
          AND source_local_code || '.' || observed_venue_mic <> '600849.XSHG'
        """
    )
    connection.execute(
        f"""
        CREATE TEMP VIEW local_scoped AS
        SELECT * FROM local_market_rows
        WHERE {tail_scope}
          AND Stkcd || '.' || CASE WHEN Markettype IN (1,32) THEN 'XSHG' ELSE 'XSHE' END
              <> '600849.XSHG'
        """
    )
    delta_scope = _ordinary_scope_sql(
        "split_part(order_book_id,'.',1)", "inferred_market_type"
    )
    connection.execute(
        f"""
        CREATE TEMP VIEW delta_scoped AS
        SELECT * FROM delta_market_rows
        WHERE inferred_market_type IN ({market_types})
          AND {delta_scope}
          AND order_book_id <> '600849.XSHG'
        """
    )

    connection.execute(
        """
        CREATE TEMP VIEW normalized_scoped AS
        SELECT
          source_local_code || '.' || observed_venue_mic AS order_book_id,
          observed_venue_mic AS exchange_mic,
          source_local_code AS local_code,
          trade_date,
          exchange_preclose_price::DOUBLE AS previous_close,
          open_price::DOUBLE AS open,
          high_price::DOUBLE AS high,
          low_price::DOUBLE AS low,
          close_price::DOUBLE AS close,
          traded_shares::BIGINT AS volume_shares,
          traded_value_cny::DOUBLE AS turnover_cny,
          vendor_total_return_with_cash_reinvestment::DOUBLE AS realized_total_return,
          'CSMAR_TRDSTA_' || CAST(vendor_trade_status AS VARCHAR) AS trading_status_code,
          vendor_limit_up_price::DOUBLE AS upper_limit_price,
          vendor_limit_down_price::DOUBLE AS lower_limit_price,
          (traded_shares > 0 AND traded_value_cny > 0) AS had_trade,
          'CSMAR_TRD_DALYR' AS source_id,
          'r0-pv-bounded-candidate-20260801-v1' AS source_snapshot_id,
          market_type::INTEGER AS source_market_type,
          'CSMAR_DRETWD_VENDOR_TOTAL_RETURN_WITH_CASH_REINVESTMENT' AS return_calibration_method,
          NULL::BOOLEAN AS corporate_action_event_flag,
          NULL::BIGINT AS corporate_action_event_count,
          NULL::DOUBLE AS total_return_event_factor,
          'UNKNOWN' AS st_state_code,
          FALSE AS st_state_known,
          'UNKNOWN__CSMAR_TRDSTA_SEMANTICS_UNADMITTED' AS suspension_state_code,
          FALSE AS suspension_state_known,
          'HISTORICAL_BASE_2005_2024' AS source_segment
        FROM historical_scoped

        UNION ALL

        SELECT
          Stkcd || '.' || CASE WHEN Markettype IN (1,32) THEN 'XSHG' ELSE 'XSHE' END,
          CASE WHEN Markettype IN (1,32) THEN 'XSHG' ELSE 'XSHE' END,
          Stkcd,
          Trddt,
          PreClosePrice::DOUBLE,
          Opnprc::DOUBLE,
          Hiprc::DOUBLE,
          Loprc::DOUBLE,
          Clsprc::DOUBLE,
          Dnshrtrd::BIGINT,
          Dnvaltrd::DOUBLE,
          Dretwd::DOUBLE,
          'CSMAR_TRDSTA_' || CAST(Trdsta AS VARCHAR),
          LimitUp::DOUBLE,
          LimitDown::DOUBLE,
          (Dnshrtrd > 0 AND Dnvaltrd > 0),
          'CSMAR_TRD_DALYR',
          'd2a04b-csmar-trd-dalyr-2022-2026-20260728-v1',
          Markettype::INTEGER,
          'CSMAR_DRETWD_VENDOR_TOTAL_RETURN_WITH_CASH_REINVESTMENT',
          NULL::BOOLEAN,
          NULL::BIGINT,
          NULL::DOUBLE,
          'UNKNOWN',
          FALSE,
          'UNKNOWN__CSMAR_TRDSTA_SEMANTICS_UNADMITTED',
          FALSE,
          'LOCAL_TAIL_2025_20260728'
        FROM local_scoped

        UNION ALL

        SELECT
          d.order_book_id,
          split_part(d.order_book_id,'.',2),
          split_part(d.order_book_id,'.',1),
          d.trade_date,
          d.prev_close::DOUBLE,
          d.open::DOUBLE,
          d.high::DOUBLE,
          d.low::DOUBLE,
          d.close::DOUBLE,
          round(d.volume)::BIGINT,
          d.total_turnover::DOUBLE,
          (d.close * COALESCE(f.ex_factor, 1.0) / d.prev_close - 1.0)::DOUBLE,
          CASE WHEN d.zero_activity_candidate
               THEN 'RQDATA_ZERO_ACTIVITY_CANDIDATE__SUSPENSION_UNKNOWN'
               ELSE 'RQDATA_ACTIVITY_OBSERVED__SUSPENSION_UNKNOWN' END,
          d.limit_up::DOUBLE,
          d.limit_down::DOUBLE,
          (d.volume > 0 AND d.total_turnover > 0),
          'RQDATA_UNADJUSTED_DAILY',
          'ricequant-r0-pv-tail-delta-20260804-v1',
          d.inferred_market_type::INTEGER,
          CASE WHEN a.order_book_id IS NOT NULL
               THEN 'RQDATA_CLOSE_TIMES_EX_FACTOR_OVER_PREVIOUS_CLOSE_MINUS_ONE'
               ELSE 'RQDATA_CLOSE_OVER_PREVIOUS_CLOSE_MINUS_ONE' END,
          (a.order_book_id IS NOT NULL),
          COALESCE(a.event_count, 0)::BIGINT,
          COALESCE(f.ex_factor, 1.0)::DOUBLE,
          CASE WHEN d.st_consensus_state_known AND d.st_consensus_state
               THEN 'ST_OR_RISK_WARNING'
               WHEN d.st_consensus_state_known AND NOT d.st_consensus_state
               THEN 'NOT_ST_OR_RISK_WARNING'
               ELSE 'UNKNOWN' END,
          COALESCE(d.st_consensus_state_known, FALSE),
          'UNKNOWN',
          FALSE,
          'RQDATA_DELTA_20260729_20260803'
        FROM delta_scoped d
        LEFT JOIN action_keys a
          ON a.order_book_id=d.order_book_id AND a.trade_date=d.trade_date
        LEFT JOIN factor_rows f
          ON f.order_book_id=d.order_book_id AND f.trade_date=d.trade_date
        """
    )
    connection.execute(
        """
        CREATE TEMP VIEW joined_scoped AS
        SELECT n.*, m.episode_id, m.listed_date, m.de_listed_date,
               c.next_trade_date,
               (m.order_book_id IS NOT NULL
                 AND n.trade_date >= m.listed_date
                 AND (m.de_listed_date IS NULL OR n.trade_date <= m.de_listed_date))
                 AS listed_episode_eligible,
               (c.trade_date IS NOT NULL AND c.next_trade_date IS NOT NULL)
                 AS calendar_eligible
        FROM normalized_scoped n
        LEFT JOIN security_master m ON m.order_book_id=n.order_book_id
        LEFT JOIN calendar_next c ON c.trade_date=n.trade_date
        """
    )
    connection.execute(
        f"""
        CREATE TEMP VIEW panel_rows AS
        SELECT
          order_book_id,
          exchange_mic,
          local_code,
          trade_date,
          previous_close,
          open,
          high,
          low,
          close,
          volume_shares,
          turnover_cny,
          realized_total_return,
          trading_status_code,
          upper_limit_price,
          lower_limit_price,
          make_timestamptz(
            year(next_trade_date), month(next_trade_date), day(next_trade_date),
            9, 30, 0, 'Asia/Shanghai'
          ) AS available_at,
          listed_episode_eligible,
          had_trade,
          (
            listed_episode_eligible
            AND had_trade
            AND previous_close > 0 AND open > 0 AND high > 0 AND low > 0 AND close > 0
            AND high > low
            AND (upper_limit_price IS NULL OR open < upper_limit_price)
            AND (lower_limit_price IS NULL OR open > lower_limit_price)
          ) AS open_execution_eligible,
          source_id,
          source_snapshot_id,
          source_market_type,
          return_calibration_method,
          corporate_action_event_flag,
          corporate_action_event_count,
          total_return_event_factor,
          st_state_code,
          st_state_known,
          suspension_state_code,
          suspension_state_known,
          next_trade_date,
          'SIMULATION_ONLY__NEVER_SIGNAL_RANK_INPUT' AS execution_mask_role,
          source_segment,
          '{PANEL_SCHEMA}' AS schema,
          year(trade_date)::INTEGER AS year,
          month(trade_date)::INTEGER AS month
        FROM joined_scoped
        WHERE listed_episode_eligible AND calendar_eligible
        """
    )


def _one_row(connection: duckdb.DuckDBPyConnection, query: str) -> dict[str, Any]:
    cursor = connection.execute(query)
    columns = [item[0] for item in cursor.description]
    row = cursor.fetchone()
    if row is None:
        raise BuildError("expected one aggregate row")
    return dict(zip(columns, row))


def _rows(connection: duckdb.DuckDBPyConnection, query: str) -> list[dict[str, Any]]:
    cursor = connection.execute(query)
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def validate_actions(
    connection: duckdb.DuckDBPyConnection,
    expectations: BuildExpectations,
) -> dict[str, Any]:
    factor_duplicate_rows = connection.execute(
        """
        SELECT COALESCE(SUM(n-1),0) FROM (
          SELECT order_book_id, trade_date, COUNT(*) n
          FROM factor_rows GROUP BY 1,2 HAVING n>1
        )
        """
    ).fetchone()[0]
    action_stats = _rows(
        connection,
        """
        SELECT trade_date, COUNT(*) AS security_date_keys,
               SUM(event_count)::BIGINT AS source_event_rows
        FROM action_keys GROUP BY trade_date ORDER BY trade_date
        """,
    )
    factor_stats = _rows(
        connection,
        """
        SELECT trade_date, COUNT(*) AS security_date_keys,
               MIN(ex_factor) AS factor_min, MAX(ex_factor) AS factor_max
        FROM factor_rows GROUP BY trade_date ORDER BY trade_date
        """,
    )
    key_mismatch = connection.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT COALESCE(a.order_book_id,f.order_book_id),
                 COALESCE(a.trade_date,f.trade_date)
          FROM action_keys a FULL OUTER JOIN factor_rows f USING(order_book_id,trade_date)
          WHERE a.order_book_id IS NULL OR f.order_book_id IS NULL
        )
        """
    ).fetchone()[0]
    missing_delta = connection.execute(
        """
        SELECT COUNT(*) FROM action_keys a
        LEFT JOIN delta_scoped d
          ON d.order_book_id=a.order_book_id AND d.trade_date=a.trade_date
        WHERE d.order_book_id IS NULL
        """
    ).fetchone()[0]
    invalid_factors = connection.execute(
        "SELECT COUNT(*) FROM factor_rows WHERE ex_factor IS NULL OR NOT isfinite(ex_factor) OR ex_factor<=0"
    ).fetchone()[0]
    actual = {str(row["trade_date"]): row["security_date_keys"] for row in action_stats}
    if actual != dict(expectations.action_keys_by_date):
        raise BuildError(
            f"corporate-action key coverage mismatch: expected={dict(expectations.action_keys_by_date)} actual={actual}"
        )
    if factor_duplicate_rows or key_mismatch or missing_delta or invalid_factors:
        raise BuildError(
            "corporate-action calibration failed: "
            f"factor_duplicate_rows={factor_duplicate_rows}, key_mismatch={key_mismatch}, "
            f"missing_delta={missing_delta}, invalid_factors={invalid_factors}"
        )
    return json_safe(
        {
            "action_by_date": action_stats,
            "factor_by_date": factor_stats,
            "action_security_date_keys": sum(actual.values()),
            "factor_duplicate_rows": factor_duplicate_rows,
            "action_factor_key_mismatch": key_mismatch,
            "action_keys_missing_delta": missing_delta,
            "invalid_factors": invalid_factors,
            "calibration_formula": "close * ex_factor / previous_close - 1",
            "non_event_factor": 1.0,
        }
    )


def input_coverage_statistics(
    connection: duckdb.DuckDBPyConnection,
    expectations: BuildExpectations,
) -> dict[str, Any]:
    segment_rows = _rows(
        connection,
        """
        SELECT source_segment,
               COUNT(*) AS scoped_rows,
               COUNT(*) FILTER (WHERE episode_id IS NULL) AS missing_identity_rows,
               COUNT(*) FILTER (WHERE NOT listed_episode_eligible) AS outside_episode_rows,
               COUNT(*) FILTER (WHERE NOT calendar_eligible) AS calendar_gap_rows,
               COUNT(*) FILTER (WHERE listed_episode_eligible AND calendar_eligible)
                 AS eligible_output_rows
        FROM joined_scoped GROUP BY source_segment ORDER BY source_segment
        """,
    )
    raw_scope = {
        "HISTORICAL_BASE_2005_2024": _one_row(
            connection,
            """
            SELECT COUNT(*) AS market_type_rows,
                   COUNT(*) FILTER (WHERE source_local_code='600849') AS explicit_600849_rows,
                   COUNT(*) FILTER (WHERE left(source_local_code,3)='689') AS excluded_689_cdr_rows,
                   COUNT(*) FILTER (WHERE left(source_local_code,3)='302') AS included_302_rows,
                   (SELECT COUNT(*) FROM historical_scoped) AS ordinary_scope_rows
            FROM historical_market_rows
            """,
        ),
        "LOCAL_TAIL_2025_20260728": _one_row(
            connection,
            """
            SELECT COUNT(*) AS market_type_rows,
                   COUNT(*) FILTER (WHERE Stkcd='600849') AS explicit_600849_rows,
                   COUNT(*) FILTER (WHERE left(Stkcd,3)='689') AS excluded_689_cdr_rows,
                   COUNT(*) FILTER (WHERE left(Stkcd,3)='302') AS included_302_rows,
                   (SELECT COUNT(*) FROM local_scoped) AS ordinary_scope_rows
            FROM local_market_rows
            """,
        ),
        "RQDATA_DELTA_20260729_20260803": _one_row(
            connection,
            """
            SELECT COUNT(*) AS market_type_rows,
                   COUNT(*) FILTER (WHERE order_book_id='600849.XSHG') AS explicit_600849_rows,
                   COUNT(*) FILTER (WHERE left(order_book_id,3)='689') AS excluded_689_cdr_rows,
                   COUNT(*) FILTER (WHERE left(order_book_id,3)='302') AS included_302_rows,
                   COUNT(*) FILTER (WHERE volume<>round(volume)) AS fractional_volume_rows,
                   (SELECT COUNT(*) FROM delta_scoped) AS ordinary_scope_rows
            FROM delta_market_rows
            """,
        ),
    }
    delta_by_date = _rows(
        connection,
        """
        SELECT CAST(trade_date AS VARCHAR) AS trade_date, COUNT(*) AS rows
        FROM delta_scoped GROUP BY trade_date ORDER BY trade_date
        """,
    )
    actual_delta = {row["trade_date"]: row["rows"] for row in delta_by_date}
    delta_raw_vs_scoped = _rows(
        connection,
        """
        SELECT CAST(r.trade_date AS VARCHAR) AS trade_date,
               COUNT(*) AS raw_market_rows,
               COUNT(*) FILTER (WHERE r.order_book_id='689009.XSHG') AS raw_689009_rows,
               (SELECT COUNT(*) FROM delta_scoped s WHERE s.trade_date=r.trade_date)
                 AS scoped_rows
        FROM delta_market_rows r GROUP BY r.trade_date ORDER BY r.trade_date
        """,
    )
    if actual_delta != dict(expectations.delta_rows_by_date):
        raise BuildError(
            f"delta row coverage mismatch: expected={dict(expectations.delta_rows_by_date)} actual={actual_delta}"
        )
    if raw_scope["RQDATA_DELTA_20260729_20260803"]["fractional_volume_rows"] != 0:
        raise BuildError("RQData delta volume cannot be losslessly cast to BIGINT shares")
    output_rows = connection.execute("SELECT COUNT(*) FROM panel_rows").fetchone()[0]
    expected_rows = sum(int(row["eligible_output_rows"]) for row in segment_rows)
    if output_rows != expected_rows:
        raise BuildError(
            f"full row coverage failed: output={output_rows}, eligible_inputs={expected_rows}"
        )
    episode_exclusions = _rows(
        connection,
        """
        SELECT source_segment, order_book_id, MIN(trade_date) AS date_min,
               MAX(trade_date) AS date_max, COUNT(*) AS rows,
               MIN(listed_date) AS master_listed_date,
               MAX(de_listed_date) AS master_de_listed_date
        FROM joined_scoped
        WHERE episode_id IS NOT NULL AND NOT listed_episode_eligible
        GROUP BY source_segment, order_book_id
        ORDER BY rows DESC, order_book_id
        """,
    )
    return json_safe(
        {
            "segments": segment_rows,
            "raw_scope": raw_scope,
            "delta_by_date": delta_by_date,
            "delta_raw_vs_scoped": delta_raw_vs_scoped,
            "episode_exclusions": episode_exclusions,
            "eligible_input_rows": expected_rows,
            "output_rows": output_rows,
            "full_row_coverage": output_rows == expected_rows,
        }
    )


def copy_panel(connection: duckdb.DuckDBPyConnection, curated_root: Path) -> None:
    curated_root.parent.mkdir(parents=True, exist_ok=True)
    if curated_root.exists():
        raise BuildError(f"curated output already exists: {curated_root}")
    connection.execute(
        f"""
        COPY (
          SELECT * FROM panel_rows
        ) TO {quote_sql(curated_root.resolve().as_posix())} (
          FORMAT PARQUET,
          COMPRESSION ZSTD,
          PARTITION_BY (schema, year, month),
          FILENAME_PATTERN 'part-{{i}}',
          PER_THREAD_OUTPUT FALSE,
          ROW_GROUP_SIZE 100000
        )
        """
    )


def output_quality(
    connection: duckdb.DuckDBPyConnection,
    output_files: Sequence[Path],
    coverage: Mapping[str, Any],
    actions: Mapping[str, Any],
    expectations: BuildExpectations,
) -> dict[str, Any]:
    output_list = sql_path_list(output_files)
    connection.execute(
        "CREATE OR REPLACE TEMP VIEW output_panel AS SELECT * FROM read_parquet("
        f"{output_list}, union_by_name=true, hive_partitioning=true)"
    )
    required_null_sql = ",\n".join(
        f"COUNT(*) FILTER (WHERE {field} IS NULL) AS null_{field}"
        for field in REQUIRED_FIELDS
    )
    output_columns = {
        row[0]
        for row in connection.execute("DESCRIBE SELECT * FROM output_panel").fetchall()
    }
    missing_required_columns = sorted(set(REQUIRED_FIELDS) - output_columns)
    summary = _one_row(
        connection,
        f"""
        SELECT
          COUNT(*) AS rows,
          COUNT(DISTINCT (order_book_id, trade_date)) AS unique_primary_keys,
          COUNT(DISTINCT order_book_id) AS securities,
          COUNT(DISTINCT trade_date) AS trading_dates,
          MIN(trade_date) AS date_min,
          MAX(trade_date) AS date_max,
          {required_null_sql},
          COUNT(*) FILTER (
            WHERE previous_close<=0 OR open<=0 OR high<=0 OR low<=0 OR close<=0
               OR high<GREATEST(open,low,close) OR low>LEAST(open,high,close)
          ) AS invalid_ohlc_rows,
          COUNT(*) FILTER (WHERE volume_shares<0 OR turnover_cny<0)
            AS negative_volume_or_turnover_rows,
          COUNT(*) FILTER (WHERE realized_total_return IS NULL OR NOT isfinite(realized_total_return))
            AS missing_or_nonfinite_total_return_rows,
          COUNT(*) FILTER (
            WHERE order_book_id <> local_code || '.' || exchange_mic
               OR exchange_mic NOT IN ('XSHG','XSHE')
               OR source_market_type NOT IN (1,4,16,32)
          ) AS identity_rows_invalid,
          COUNT(*) FILTER (
            WHERE order_book_id='600849.XSHG'
               OR exchange_mic='BJSE'
               OR left(local_code,3) IN ('200','201','900','689')
          ) AS explicitly_out_of_scope_rows,
          COUNT(*) FILTER (WHERE NOT listed_episode_eligible) AS unlisted_episode_rows,
          COUNT(*) FILTER (
            WHERE CAST(timezone('Asia/Shanghai',available_at) AS DATE)<>next_trade_date
               OR CAST(timezone('Asia/Shanghai',available_at) AS TIME)<>TIME '09:30:00'
               OR next_trade_date<=trade_date
          ) AS available_at_mismatch_or_leakage_rows,
          COUNT(*) FILTER (
            WHERE open_execution_eligible AND NOT had_trade
          ) AS execution_mask_inconsistent_rows,
          COUNT(*) FILTER (
            WHERE execution_mask_role<>'SIMULATION_ONLY__NEVER_SIGNAL_RANK_INPUT'
          ) AS execution_mask_role_invalid_rows,
          COUNT(*) FILTER (
            WHERE source_segment='RQDATA_DELTA_20260729_20260803'
              AND corporate_action_event_flag
          ) AS rqdata_action_event_rows,
          COUNT(*) FILTER (
            WHERE source_segment='RQDATA_DELTA_20260729_20260803'
              AND NOT corporate_action_event_flag
              AND total_return_event_factor<>1.0
          ) AS rqdata_non_event_factor_not_one_rows,
          COUNT(*) FILTER (
            WHERE source_segment='RQDATA_DELTA_20260729_20260803'
              AND corporate_action_event_flag
              AND ABS(realized_total_return -
                (close*total_return_event_factor/previous_close-1.0))>1e-12
          ) AS rqdata_action_formula_mismatch_rows,
          COUNT(*) FILTER (
            WHERE source_segment<>'RQDATA_DELTA_20260729_20260803'
              AND return_calibration_method<>
                'CSMAR_DRETWD_VENDOR_TOTAL_RETURN_WITH_CASH_REINVESTMENT'
          ) AS csmar_return_method_mismatch_rows,
          COUNT(*) FILTER (WHERE st_state_code='UNKNOWN') AS st_unknown_rows,
          COUNT(*) FILTER (WHERE suspension_state_code LIKE 'UNKNOWN%')
            AS suspension_unknown_rows
        FROM output_panel
        """,
    )
    segment_stats = _rows(
        connection,
        """
        SELECT source_segment, COUNT(*) AS rows,
               COUNT(DISTINCT order_book_id) AS securities,
               COUNT(DISTINCT trade_date) AS trading_dates,
               MIN(trade_date) AS date_min, MAX(trade_date) AS date_max,
               COUNT(*) FILTER (WHERE had_trade) AS had_trade_rows,
               COUNT(*) FILTER (WHERE open_execution_eligible) AS open_execution_eligible_rows
        FROM output_panel GROUP BY source_segment ORDER BY source_segment
        """,
    )
    by_market = _rows(
        connection,
        """
        SELECT source_market_type, exchange_mic, COUNT(*) AS rows,
               COUNT(DISTINCT order_book_id) AS securities,
               MIN(trade_date) AS date_min, MAX(trade_date) AS date_max
        FROM output_panel GROUP BY 1,2 ORDER BY 1,2
        """,
    )
    by_year = _rows(
        connection,
        """
        SELECT year, COUNT(*) AS rows, COUNT(DISTINCT order_book_id) AS securities,
               COUNT(DISTINCT trade_date) AS trading_dates
        FROM output_panel GROUP BY year ORDER BY year
        """,
    )
    terminal = _one_row(
        connection,
        """
        SELECT COUNT(*) AS rows,
               MIN(available_at) AS available_at_min,
               MAX(available_at) AS available_at_max,
               MIN(CAST(timezone('Asia/Shanghai',available_at) AS VARCHAR)) AS local_min,
               MAX(CAST(timezone('Asia/Shanghai',available_at) AS VARCHAR)) AS local_max
        FROM output_panel WHERE trade_date=DATE '2026-08-03'
        """,
    )
    checks = {
        "full_row_coverage": bool(coverage["full_row_coverage"]),
        "primary_key_unique": summary["rows"] == summary["unique_primary_keys"],
        "coverage_start_exact": str(summary["date_min"]) == expectations.coverage_start.isoformat(),
        "coverage_end_exact": str(summary["date_max"]) == expectations.coverage_end.isoformat(),
        "required_contract_fields_present": not missing_required_columns,
        "required_nonnullable_fields_nonnull": all(
            summary[f"null_{field}"] == 0
            for field in REQUIRED_FIELDS
            if field not in {"upper_limit_price", "lower_limit_price"}
        ),
        "ohlc_valid": summary["invalid_ohlc_rows"] == 0,
        "volume_turnover_nonnegative": summary["negative_volume_or_turnover_rows"] == 0,
        "total_return_complete_and_finite": summary["missing_or_nonfinite_total_return_rows"] == 0,
        "identity_exact": summary["identity_rows_invalid"] == 0,
        "scope_exact": summary["explicitly_out_of_scope_rows"] == 0,
        "listing_episode_exact": summary["unlisted_episode_rows"] == 0,
        "available_at_next_session_0930": summary["available_at_mismatch_or_leakage_rows"] == 0,
        "execution_mask_simulation_only": (
            summary["execution_mask_inconsistent_rows"] == 0
            and summary["execution_mask_role_invalid_rows"] == 0
        ),
        "rqdata_action_keys_exact": (
            summary["rqdata_action_event_rows"] == actions["action_security_date_keys"]
        ),
        "rqdata_non_event_factor_exact_one": summary["rqdata_non_event_factor_not_one_rows"] == 0,
        "rqdata_action_formula_exact": summary["rqdata_action_formula_mismatch_rows"] == 0,
        "csmar_dretwd_only": summary["csmar_return_method_mismatch_rows"] == 0,
        "no_latest_forward_adjusted_history": True,
        "st_and_suspension_unknown_not_fabricated": (
            summary["st_unknown_rows"] > 0 and summary["suspension_unknown_rows"] > 0
        ),
    }
    status = "PASS__CANDIDATE_MECHANICAL_QUALITY__NOT_FORMALLY_ADMITTED" if all(
        checks.values()
    ) else "FAIL_CLOSED__CANDIDATE_QUALITY"
    result = json_safe(
        {
            "schema_version": 1,
            "run_id": RUN_ID,
            "contract_id": "r0_shsz_pv_v2",
            "payload_schema": PANEL_SCHEMA,
            "payload_format": "PARQUET",
            "payload_discovery": "PARTITION_MANIFEST_CURATED_ENTRIES",
            "primary_key": ["order_book_id", "trade_date"],
            "dataset_id": DATASET_ID,
            "snapshot_id": SNAPSHOT_ID,
            "status": status,
            "all_checks_pass": all(checks.values()),
            "checks": checks,
            "summary": summary,
            "missing_required_columns": missing_required_columns,
            "segments": segment_stats,
            "by_market": by_market,
            "by_year": by_year,
            "input_coverage": coverage,
            "corporate_action_calibration": actions,
            "terminal_trade_date_available_at": terminal,
            "unknown_semantics": {
                "st": "Explicit UNKNOWN unless the RQ delta supplied a known consensus state.",
                "suspension": "Explicit UNKNOWN; CSMAR Trdsta is preserved in trading_status_code without claiming admitted suspension semantics.",
            },
            "formal_source_admission": False,
            "formal_catalog_written": False,
            "research_ready": False,
            "factor_runs": 0,
        }
    )
    if not result["all_checks_pass"]:
        failed = [key for key, passed in checks.items() if not passed]
        raise BuildError(f"output quality failed: {failed}")
    return result


def build_partition_manifest(
    connection: duckdb.DuckDBPyConnection,
    candidate_root: Path,
    output_files: Sequence[Path],
) -> dict[str, Any]:
    output_list = sql_path_list(output_files)
    rows = _rows(
        connection,
        """
        SELECT filename, schema, year, month, COUNT(*) AS row_count,
               COUNT(DISTINCT order_book_id) AS securities,
               MIN(trade_date) AS date_min, MAX(trade_date) AS date_max
        FROM read_parquet("""
        + output_list
        + """, union_by_name=true, hive_partitioning=true, filename=true)
        GROUP BY 1,2,3,4 ORDER BY 3,4,1
        """,
    )
    metadata = {Path(row["filename"]).resolve(): row for row in rows}
    partitions: list[dict[str, Any]] = []
    for path in sorted(output_files):
        row = metadata.get(path.resolve())
        if row is None:
            raise BuildError(f"partition metadata missing: {path}")
        receipt = file_receipt(path, relative_to=candidate_root)
        partitions.append(
            json_safe(
                {
                    **receipt,
                    "schema": row["schema"],
                    "year": int(row["year"]),
                    "month": int(row["month"]),
                    "row_count": int(row["row_count"]),
                    "securities": int(row["securities"]),
                    "date_min": row["date_min"],
                    "date_max": row["date_max"],
                }
            )
        )
    payload = {
        "algorithm": "sha256-snapshot-contract-v1",
        "partitions": partitions,
    }
    return {
        "schema_version": 1,
        "dataset_id": DATASET_ID,
        "snapshot_id": SNAPSHOT_ID,
        "partition_scheme": ["schema", "year", "month"],
        "partition_count": len(partitions),
        "total_rows": sum(item["row_count"] for item in partitions),
        "total_bytes": sum(item["bytes"] for item in partitions),
        "payload_hash_algorithm": "sha256-snapshot-contract-v1",
        "payload_contract_sha256": sha256_bytes(canonical_bytes(payload)),
        "partitions": partitions,
    }


def schema_document() -> dict[str, Any]:
    fields = [
        ("order_book_id", "VARCHAR", False),
        ("exchange_mic", "VARCHAR", False),
        ("local_code", "VARCHAR", False),
        ("trade_date", "DATE", False),
        ("previous_close", "DOUBLE", False),
        ("open", "DOUBLE", False),
        ("high", "DOUBLE", False),
        ("low", "DOUBLE", False),
        ("close", "DOUBLE", False),
        ("volume_shares", "BIGINT", False),
        ("turnover_cny", "DOUBLE", False),
        ("realized_total_return", "DOUBLE", False),
        ("trading_status_code", "VARCHAR", False),
        ("upper_limit_price", "DOUBLE", True),
        ("lower_limit_price", "DOUBLE", True),
        ("available_at", "TIMESTAMPTZ", False),
        ("listed_episode_eligible", "BOOLEAN", False),
        ("had_trade", "BOOLEAN", False),
        ("open_execution_eligible", "BOOLEAN", False),
        ("source_id", "VARCHAR", False),
        ("source_snapshot_id", "VARCHAR", False),
        ("source_market_type", "INTEGER", False),
        ("return_calibration_method", "VARCHAR", False),
        ("corporate_action_event_flag", "BOOLEAN", True),
        ("corporate_action_event_count", "BIGINT", True),
        ("total_return_event_factor", "DOUBLE", True),
        ("st_state_code", "VARCHAR", False),
        ("st_state_known", "BOOLEAN", False),
        ("suspension_state_code", "VARCHAR", False),
        ("suspension_state_known", "BOOLEAN", False),
        ("next_trade_date", "DATE", False),
        ("execution_mask_role", "VARCHAR", False),
        ("source_segment", "VARCHAR", False),
    ]
    return {
        "schema_version": 1,
        "schema_id": PANEL_SCHEMA,
        "timezone": "Asia/Shanghai",
        "primary_key": ["order_book_id", "trade_date"],
        "required_contract_fields": list(REQUIRED_FIELDS),
        "fields": [
            {"name": name, "logical_type": logical_type, "nullable": nullable}
            for name, logical_type, nullable in fields
        ],
        "hive_partition_fields": [
            {"name": "schema", "logical_type": "VARCHAR"},
            {"name": "year", "logical_type": "INTEGER"},
            {"name": "month", "logical_type": "INTEGER"},
        ],
        "open_execution_eligible_semantics": (
            "Simulation-only mask: listed episode, observed positive activity, valid non-flat "
            "OHLC and open strictly inside any observed limit prices. It is never a signal "
            "rank input and does not assert admitted ST/suspension semantics."
        ),
    }


def candidate_payload_paths(candidate_root: Path) -> list[Path]:
    paths = [
        candidate_root / "raw/TERMINAL_SESSION_RECEIPT.json",
        candidate_root / "SOURCE_BINDING.json",
        candidate_root / "SCHEMA.json",
        candidate_root / "partition-manifest.json",
        candidate_root / "quality/summary.json",
        candidate_root / "provenance.json",
    ]
    paths.extend(sorted((candidate_root / "curated").rglob("*.parquet")))
    return paths


def exact_candidate_manifest(candidate_root: Path) -> dict[str, Any]:
    artifacts = [
        file_receipt(path, relative_to=candidate_root)
        for path in candidate_payload_paths(candidate_root)
    ]
    if not any("/raw/" in "/" + item["relative_path"] for item in artifacts):
        raise BuildError("candidate publication payload lacks raw layout")
    if not any("/curated/" in "/" + item["relative_path"] for item in artifacts):
        raise BuildError("candidate publication payload lacks curated layout")
    return {
        "schema_version": 1,
        "contract_id": "candidate_manifest_v1",
        "candidate_id": CANDIDATE_ID,
        "dataset_id": DATASET_ID,
        "snapshot_id": SNAPSHOT_ID,
        "purpose_ceiling": PURPOSE_CEILING,
        "formal_source_admission": False,
        "formal_catalog_written": False,
        "research_ready": False,
        "artifacts": artifacts,
    }


def validate_output_root(path: Path, *, allow_test_paths: bool) -> None:
    resolved = path.resolve()
    if resolved.exists():
        raise BuildError(f"immutable output already exists: {resolved}")
    if allow_test_paths:
        return
    parent = DEFAULT_OUTPUT.parent.resolve()
    if resolved.parent != parent or resolved != DEFAULT_OUTPUT.resolve():
        raise BuildError(f"production output must be exactly {DEFAULT_OUTPUT}")
    if is_link_or_reparse(parent):
        raise BuildError("production output parent is linked/reparse")


def build_candidate(
    output: Path = DEFAULT_OUTPUT,
    *,
    inputs: BuildInputs = BuildInputs(),
    expectations: BuildExpectations = PRODUCTION_EXPECTATIONS,
    terminal_receipt: Mapping[str, Any] | None = None,
    verify_authorities: bool = True,
    allow_test_paths: bool = False,
) -> dict[str, Any]:
    validate_output_root(output, allow_test_paths=allow_test_paths)
    before = protected_state() if not allow_test_paths else {"state_sha256": "TEST_SCOPE"}
    bindings = bind_inputs(inputs, verify_authorities=verify_authorities)
    receipt = dict(terminal_receipt) if terminal_receipt is not None else fetch_terminal_session_receipt()
    validate_terminal_receipt(receipt, allow_test_fixture=allow_test_paths)
    bindings["terminal_session_receipt_sha256"] = receipt["receipt_sha256"]
    bindings["binding_sha256"] = sha256_bytes(
        canonical_bytes({key: value for key, value in bindings.items() if key != "resolved_paths"})
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    stage = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    if stage.exists() or is_link_or_reparse(output.parent):
        raise BuildError("unsafe staging state")
    stage.mkdir(parents=False, exist_ok=False)
    temporary = stage / ".duckdb-tmp"
    connection: duckdb.DuckDBPyConnection | None = None
    try:
        write_json(stage / "raw/TERMINAL_SESSION_RECEIPT.json", receipt)
        source_binding = {
            key: value for key, value in bindings.items() if key != "resolved_paths"
        }
        write_json(stage / "SOURCE_BINDING.json", source_binding)
        write_json(stage / "SCHEMA.json", schema_document())

        connection = duckdb.connect(database=":memory:")
        configure_connection(connection, temporary)
        create_source_views(connection, bindings, receipt)
        action_stats = validate_actions(connection, expectations)
        coverage = input_coverage_statistics(connection, expectations)
        copy_panel(connection, stage / "curated")
        output_files = sorted((stage / "curated").rglob("*.parquet"))
        if not output_files:
            raise BuildError("DuckDB produced no output partitions")
        quality = output_quality(
            connection, output_files, coverage, action_stats, expectations
        )
        partition_manifest = build_partition_manifest(connection, stage, output_files)
        write_json(stage / "partition-manifest.json", partition_manifest)
        write_json(stage / "quality/summary.json", quality)

        def future_receipt(path: Path) -> dict[str, Any]:
            relative = path.resolve().relative_to(stage.resolve())
            receipt_value = file_receipt(path, relative_to=stage)
            receipt_value["relative_path"] = display_path(output / relative)
            return receipt_value

        research_input_contract = {
            "schema_version": 1,
            "research_tier": "R0_SHSZ_PV",
            "gate_id": "R0_SHSZ_PV",
            "contract_id": "r0_shsz_pv_v2",
            "dataset_id": DATASET_ID,
            "snapshot_id": SNAPSHOT_ID,
            "market_scope": "XSHG_XSHE_ORDINARY_A",
            "coverage_start_date": START_DATE.isoformat(),
            "coverage_end_date": END_DATE.isoformat(),
            "decision_time": "TRADE_DATE_CLOSE",
            "available_at_semantics": "NEXT_OBSERVED_TRADING_SESSION_OPEN",
            "signal_lag_sessions_minimum": 1,
            "raw_price_convention": "UNADJUSTED",
            "return_convention": "ONE_PERIOD_REALIZED_TOTAL_RETURN",
            "adjusted_price_levels_allowed": False,
            "latest_forward_adjusted_history_allowed": False,
            "corporate_action_features_allowed": False,
            "execution_mask_role": "SIMULATION_ONLY__NEVER_SIGNAL_RANK_INPUT",
            "missing_value_policy": "NULL_NEVER_ZERO_FILL",
            "payload_schema": PANEL_SCHEMA,
            "primary_key": ["order_book_id", "trade_date"],
            "format": "PARQUET",
            "discovery": "PARTITION_MANIFEST_CURATED_ENTRIES",
        }
        provenance = {
            "schema_version": 1,
            "run_id": RUN_ID,
            "contract_id": "r0_shsz_pv_v2",
            "dataset_id": DATASET_ID,
            "snapshot_id": SNAPSHOT_ID,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_binding": future_receipt(stage / "SOURCE_BINDING.json"),
            "terminal_session_receipt": future_receipt(
                stage / "raw/TERMINAL_SESSION_RECEIPT.json"
            ),
            "research_input_contract": research_input_contract,
            "transformations": [
                "Historical 2005-2024 CSMAR Dretwd retained as realized total return with cash reinvestment.",
                "Local 2025-01-02..2026-07-28 CSMAR Dretwd retained under the same convention.",
                "RQData four-day unadjusted rows use close*ex_factor/previous_close-1 on exactly 58 action keys and factor=1 elsewhere.",
                "order_book_id is exact local_code plus XSHG/XSHE MIC; market types are restricted to 1/4/16/32.",
                "600849.XSHG, B-share namespaces, BJSE and 689 CDR namespace are excluded fail-closed.",
                "Rows outside the observed RQData listing episode are excluded fail-closed.",
                "available_at is the next pinned/observed trading session at 09:30 Asia/Shanghai.",
                "Execution masks are simulation-only and ST/suspension unknowns remain explicit.",
            ],
            "price_convention": "UNADJUSTED_ONLY",
            "return_convention": "ONE_PERIOD_REALIZED_TOTAL_RETURN_WITH_CASH_REINVESTMENT",
            "latest_forward_adjusted_history_used": False,
            "corporate_action_features_exposed_as_signal_inputs": False,
            "formal_source_admission": False,
            "formal_catalog_written": False,
            "roadmap_written": False,
            "source_registry_written": False,
            "factor_runs": 0,
            "research_ready": False,
        }
        write_json(stage / "provenance.json", provenance)
        candidate_manifest = exact_candidate_manifest(stage)
        # Candidate artifacts must be workspace-relative for formal preflight.
        for artifact in candidate_manifest["artifacts"]:
            path = stage / artifact["relative_path"]
            artifact["relative_path"] = display_path(output / artifact["relative_path"])
            if not path.is_file():
                raise BuildError(f"candidate payload disappeared: {path}")
        write_json(stage / "CANDIDATE_MANIFEST.json", candidate_manifest)

        manifest = {
            "schema_version": 1,
            "run_id": RUN_ID,
            "candidate_id": CANDIDATE_ID,
            "dataset_id": DATASET_ID,
            "snapshot_id": SNAPSHOT_ID,
            "contract_id": "r0_shsz_pv_v2",
            "lifecycle": "QUARANTINE_CANDIDATE",
            "mutability": "immutable_after_seal",
            "purpose_ceiling": PURPOSE_CEILING,
            "quality_status": quality["status"],
            "source_admission": False,
            "formal_catalog_registered": False,
            "formal_effect": "NONE__QUARANTINE_ONLY",
            "research_ready": False,
            "payload_schema": PANEL_SCHEMA,
            "payload_format": "PARQUET",
            "payload_discovery": "PARTITION_MANIFEST_CURATED_ENTRIES",
            "primary_key": ["order_book_id", "trade_date"],
            "research_input_contract": research_input_contract,
            "candidate_manifest_v1": future_receipt(stage / "CANDIDATE_MANIFEST.json"),
            "partition_manifest": future_receipt(stage / "partition-manifest.json"),
            "quality": future_receipt(stage / "quality/summary.json"),
            "provenance": future_receipt(stage / "provenance.json"),
            "source_binding": future_receipt(stage / "SOURCE_BINDING.json"),
            "payload_contract_sha256": partition_manifest["payload_contract_sha256"],
            "statistics": quality["summary"],
        }
        write_json(stage / "manifest.json", manifest)

        if temporary.exists():
            connection.close()
            connection = None
            shutil.rmtree(temporary)
        os.replace(stage, output)
    except Exception:
        if connection is not None:
            connection.close()
        if stage.exists() and not is_link_or_reparse(stage):
            shutil.rmtree(stage)
        raise

    after = protected_state() if not allow_test_paths else {"state_sha256": "TEST_SCOPE"}
    if before != after:
        raise BuildError("protected catalog/Roadmap/source-registry/formal trees changed")
    build_result = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "status": "PASS__QUARANTINE_COMPOSITE_PANEL_BUILT__NO_FORMAL_EFFECT",
        "candidate_root": display_path(output),
        "manifest": file_receipt(output / "manifest.json"),
        "candidate_manifest": file_receipt(output / "CANDIDATE_MANIFEST.json"),
        "rows": quality["summary"]["rows"],
        "date_min": quality["summary"]["date_min"],
        "date_max": quality["summary"]["date_max"],
        "securities": quality["summary"]["securities"],
        "trading_dates": quality["summary"]["trading_dates"],
        "parquet_files": partition_manifest["partition_count"],
        "payload_contract_sha256": partition_manifest["payload_contract_sha256"],
        "quality_status": quality["status"],
        "formal_source_admission": False,
        "formal_catalog_written": False,
        "roadmap_written": False,
        "source_registry_written": False,
        "factor_runs": 0,
        "research_ready": False,
        "protected_state_unchanged": before == after,
        "protected_state_sha256": after["state_sha256"],
    }
    write_json(output / "BUILD_RESULT.json", build_result)
    return verify_candidate(output, allow_test_paths=allow_test_paths)


def verify_candidate(output: Path = DEFAULT_OUTPUT, *, allow_test_paths: bool = False) -> dict[str, Any]:
    if not output.is_dir() or is_link_or_reparse(output):
        raise BuildError(f"plain candidate output missing: {output}")
    candidate_path = output / "CANDIDATE_MANIFEST.json"
    manifest_path = output / "manifest.json"
    quality_path = output / "quality/summary.json"
    partition_path = output / "partition-manifest.json"
    for path in (candidate_path, manifest_path, quality_path, partition_path):
        if not path.is_file():
            raise BuildError(f"candidate authority file missing: {path}")
    candidate = read_json(candidate_path)
    exact_fields = {
        "schema_version",
        "contract_id",
        "candidate_id",
        "dataset_id",
        "snapshot_id",
        "purpose_ceiling",
        "formal_source_admission",
        "formal_catalog_written",
        "research_ready",
        "artifacts",
    }
    if set(candidate) != exact_fields:
        raise BuildError("candidate_manifest_v1 exact fields mismatch")
    expected_values = {
        "schema_version": 1,
        "contract_id": "candidate_manifest_v1",
        "candidate_id": CANDIDATE_ID,
        "dataset_id": DATASET_ID,
        "snapshot_id": SNAPSHOT_ID,
        "purpose_ceiling": PURPOSE_CEILING,
        "formal_source_admission": False,
        "formal_catalog_written": False,
        "research_ready": False,
    }
    for key, expected in expected_values.items():
        if candidate.get(key) != expected:
            raise BuildError(f"candidate manifest {key} mismatch")
    artifacts = candidate.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise BuildError("candidate artifacts missing")
    seen: set[str] = set()
    raw_layout = False
    curated_layout = False
    parquet_paths: set[Path] = set()
    for record in artifacts:
        if not isinstance(record, dict) or set(record) != {"relative_path", "sha256", "bytes"}:
            raise BuildError("candidate artifact exact fields mismatch")
        relative = record["relative_path"]
        if relative in seen:
            raise BuildError(f"duplicate candidate artifact: {relative}")
        seen.add(relative)
        path = WORKSPACE_ROOT / relative if not allow_test_paths else None
        if allow_test_paths:
            marker = f"/{output.name}/"
            normalized = "/" + str(relative).replace("\\", "/")
            if marker not in normalized:
                raise BuildError(f"test artifact outside candidate: {relative}")
            suffix = normalized.split(marker, 1)[1]
            path = output / suffix
        assert path is not None
        _verify_receipt(path, record)
        normalized_relative = "/" + relative.replace("\\", "/")
        raw_layout = raw_layout or "/raw/" in normalized_relative
        curated_layout = curated_layout or "/curated/" in normalized_relative
        if path.suffix == ".parquet":
            parquet_paths.add(path.resolve())
        if path.name in {"CANDIDATE_MANIFEST.json", "manifest.json"}:
            raise BuildError("candidate manifest listed itself as payload")
    actual_parquets = {path.resolve() for path in output.rglob("*.parquet")}
    if parquet_paths != actual_parquets:
        raise BuildError("candidate manifest parquet coverage mismatch")
    if not raw_layout or not curated_layout:
        raise BuildError("candidate payload needs raw and curated layouts")
    quality = read_json(quality_path)
    if quality.get("all_checks_pass") is not True:
        raise BuildError("quality gate is not PASS")
    partition_manifest = read_json(partition_path)
    if partition_manifest.get("partition_count") != len(actual_parquets):
        raise BuildError("partition manifest count mismatch")
    manifest = read_json(manifest_path)
    if manifest.get("formal_effect") != "NONE__QUARANTINE_ONLY":
        raise BuildError("candidate formal-effect boundary drifted")
    return {
        "status": "PASS__R0_SHSZ_PV_PANEL_CANDIDATE_VERIFIED__NO_FORMAL_EFFECT",
        "candidate_root": display_path(output),
        "rows": quality["summary"]["rows"],
        "date_min": quality["summary"]["date_min"],
        "date_max": quality["summary"]["date_max"],
        "securities": quality["summary"]["securities"],
        "trading_dates": quality["summary"]["trading_dates"],
        "parquet_files": len(actual_parquets),
        "payload_contract_sha256": partition_manifest["payload_contract_sha256"],
        "candidate_manifest_sha256": sha256_file(candidate_path),
        "quality_status": quality["status"],
        "formal_source_admission": False,
        "formal_catalog_written": False,
        "research_ready": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build/verify the quarantine-only R0_SHSZ_PV v2 composite panel."
    )
    parser.add_argument("--verify-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = verify_candidate() if args.verify_only else build_candidate()
    except Exception as exc:
        safe = re.sub(r"(?:tcp|rqdata|rqdatac)://\S+", "<redacted>", str(exc))[:500]
        print(
            json.dumps(
                {"status": "FAIL_CLOSED", "error": safe},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
