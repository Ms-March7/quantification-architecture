"""Memory-bounded columnar loader for the exact formal R0 narrow snapshot."""

from __future__ import annotations

import re
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .r0_shsz_panel_input import (
    DATASET_ID,
    EXPECTED_RESEARCH_INPUT_CONTRACT,
    SNAPSHOT_ID,
    SOURCE_COLUMNS,
    SOURCE_DUCKDB_TYPES,
    TIMEZONE,
    AvailabilityRecord,
    R0SHSZPVPanelAdapter,
    R0SHSZPanelInputError,
    _iso_date,
    _path_key,
    _safe_workspace_relative,
    _sha256_file,
    _timestamp,
    enforce_signal_field_isolation,
)
from .schema import PanelData


FETCH_COLUMNS = (
    "order_book_id",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume_shares",
    "turnover_cny",
    "realized_total_return",
    "listed_episode_eligible",
    "had_trade",
    "open_execution_eligible",
    "upper_limit_price",
    "lower_limit_price",
)

_CURATED_MONTH_PARTITION = re.compile(
    r"^schema=r0-shsz-pv-panel-v2/year=(?P<year>[0-9]{4})/"
    r"month=(?P<month>[1-9]|1[0-2])/part-[^/]+\.parquet$"
)


def _select_curated_payloads(
    *,
    adapter: R0SHSZPVPanelAdapter,
    documents: Mapping[str, Mapping[str, Any]],
    validated_paths: Sequence[Path],
    start: date,
    end: date,
) -> tuple[tuple[Path, ...], tuple[dict[str, Any], ...]]:
    """Select and hash only manifest partitions intersecting the request.

    ``validated_paths`` must come from ``_payload_paths(..., False)``.  That
    gate binds every path and byte size to the sealed partition manifest but
    deliberately does not read Parquet contents.  This function then uses the
    manifest's verified canonical year/month path as partition metadata,
    selects the minimum physical file set, and hashes only that selected set
    before DuckDB can inspect any footer.
    """

    files = documents["partition-manifest.json"].get("files")
    if not isinstance(files, list) or not files:
        raise R0SHSZPanelInputError("partition-manifest.files must be non-empty")
    validated_by_key = {_path_key(Path(path)): Path(path) for path in validated_paths}
    if len(validated_by_key) != len(validated_paths):
        raise R0SHSZPanelInputError("validated curated payload paths are not unique")

    declared_curated: set[str] = set()
    selected: list[tuple[Path, dict[str, Any]]] = []
    for index, item in enumerate(files):
        if not isinstance(item, Mapping) or item.get("layer") != "curated":
            continue
        relative = item.get("relative_path")
        match = _CURATED_MONTH_PARTITION.fullmatch(str(relative))
        if match is None:
            raise R0SHSZPanelInputError(
                f"partition files[{index}] lacks verifiable curated month metadata"
            )
        target = _safe_workspace_relative(
            item.get("workspace_relative_path"),
            adapter.workspace_root,
            f"partition files[{index}].workspace_relative_path",
        )
        target_key = _path_key(target)
        if target_key not in validated_by_key:
            raise R0SHSZPanelInputError(
                "partition manifest curated entry is absent from validated payload paths"
            )
        if target_key in declared_curated:
            raise R0SHSZPanelInputError("duplicate curated partition path")
        declared_curated.add(target_key)

        year = int(match.group("year"))
        month = int(match.group("month"))
        partition_start = date(year, month, 1)
        partition_end = (
            date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        )
        if partition_start <= end and partition_end > start:
            expected_hash = item.get("sha256")
            if _sha256_file(validated_by_key[target_key]) != expected_hash:
                raise R0SHSZPanelInputError(
                    f"selected formal payload hash mismatch: {item.get('workspace_relative_path')}"
                )
            selected.append(
                (
                    validated_by_key[target_key],
                    {
                        "relative_path": str(relative),
                        "sha256": str(expected_hash),
                        "partition_start": partition_start.isoformat(),
                        "partition_end_exclusive": partition_end.isoformat(),
                    },
                )
            )

    if declared_curated != set(validated_by_key):
        raise R0SHSZPanelInputError(
            "validated curated payload paths disagree with partition manifest"
        )
    if not selected:
        raise R0SHSZPanelInputError("requested slice has no manifest-declared Parquet partition")
    selected.sort(key=lambda item: item[0].as_posix())
    return (
        tuple(item[0] for item in selected),
        tuple(item[1] for item in selected),
    )


def _validate_bounded_availability(
    records: Sequence[AvailabilityRecord],
) -> dict[date, datetime]:
    """Validate only the requested calendar without opening adjacent files."""

    if not records:
        raise R0SHSZPanelInputError("requested payload has no availability calendar")
    normalized: list[tuple[date, datetime]] = []
    seen: set[date] = set()
    for index, record in enumerate(records):
        trade_date = _iso_date(f"availability[{index}].trade_date", record.trade_date)
        if trade_date in seen:
            raise R0SHSZPanelInputError("duplicate availability trade_date")
        seen.add(trade_date)
        if (
            not isinstance(record.row_count, int)
            or not isinstance(record.nonnull_count, int)
            or record.row_count < 1
            or record.nonnull_count != record.row_count
        ):
            raise R0SHSZPanelInputError(
                f"available_at is null or row count is invalid on {trade_date}"
            )
        minimum = _timestamp(
            f"availability[{index}].minimum_available_at",
            record.minimum_available_at,
        )
        maximum = _timestamp(
            f"availability[{index}].maximum_available_at",
            record.maximum_available_at,
        )
        if minimum != maximum:
            raise R0SHSZPanelInputError(
                f"available_at is not unique within trade_date {trade_date}"
            )
        normalized.append((trade_date, minimum))

    normalized.sort(key=lambda item: item[0])
    result: dict[date, datetime] = {}
    for index, (trade_date, actual) in enumerate(normalized):
        if actual.date() <= trade_date or actual.timetz().replace(tzinfo=None) != time(9, 30):
            raise R0SHSZPanelInputError(
                f"available_at for {trade_date} must be a later session at 09:30"
            )
        if index + 1 < len(normalized):
            expected = datetime.combine(normalized[index + 1][0], time(9, 30), tzinfo=TIMEZONE)
            if actual != expected:
                raise R0SHSZPanelInputError(
                    f"available_at for {trade_date} is not the next requested observed session"
                )
        result[trade_date] = actual
    return result


def _plain_array(value: Any, *, fill: float | bool | None = None) -> np.ndarray:
    if np.ma.isMaskedArray(value):
        if fill is None:
            return np.asarray(value.filled())
        return np.asarray(value.filled(fill))
    return np.asarray(value)


def _price_equal(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    tolerance = 1e-8 * np.maximum.reduce((np.ones_like(left), np.abs(left), np.abs(right)))
    return np.abs(left - right) <= tolerance


def load_r0_shsz_panel_dense(
    *,
    start_date: str,
    end_date: str,
    symbols: Sequence[str] | None = None,
    workspace_root: str | Path | None = None,
    max_panel_cells: int = 30_000_000,
) -> PanelData:
    """Load one large formal slice without materializing per-row Python dicts.

    Authority, qdata, schema, availability and row-validity gates are the same
    fail-closed gates as the bounded adapter.  DuckDB returns column arrays and
    the loader scatters them into a dense float32 panel, reducing the transient
    memory cost of multi-year full-market Discovery.
    """

    adapter = R0SHSZPVPanelAdapter(workspace_root=workspace_root)
    adapter._exact_identity(DATASET_ID, SNAPSHOT_ID)
    start = _iso_date("start_date", start_date)
    end = _iso_date("end_date", end_date)
    coverage_start = date.fromisoformat(
        EXPECTED_RESEARCH_INPUT_CONTRACT["coverage_start_date"]
    )
    coverage_end = date.fromisoformat(
        EXPECTED_RESEARCH_INPUT_CONTRACT["coverage_end_date"]
    )
    if start > end or start < coverage_start or end > coverage_end:
        raise R0SHSZPanelInputError("requested slice escapes the frozen R0 coverage")
    if isinstance(max_panel_cells, bool) or not isinstance(max_panel_cells, int) or max_panel_cells < 1:
        raise R0SHSZPanelInputError("max_panel_cells must be a positive integer")
    normalized_symbols = adapter._symbols(symbols)
    adapter._validate_workspace_contract()
    resolved = adapter.resolver.resolve(DATASET_ID, SNAPSHOT_ID)
    documents = adapter._validate_formal_resolution(resolved)
    validated_paths = adapter._payload_paths(documents, verify_payload_hashes=False)
    parquet_paths, selected_payloads = _select_curated_payloads(
        adapter=adapter,
        documents=documents,
        validated_paths=validated_paths,
        start=start,
        end=end,
    )

    try:
        import duckdb  # type: ignore[import-not-found]
    except ImportError as error:
        raise R0SHSZPanelInputError("DuckDB is required for the dense R0 loader") from error

    connection = duckdb.connect(database=":memory:")
    try:
        relation = connection.read_parquet(
            [str(path) for path in parquet_paths],
            hive_partitioning=True,
            union_by_name=True,
        )
        relation.create_view("r0_shsz_payload", replace=True)
        described = connection.execute("DESCRIBE r0_shsz_payload").fetchall()
        types = {str(row[0]): str(row[1]).upper() for row in described}
        missing = sorted(set(SOURCE_COLUMNS) - set(types))
        wrong_types = {
            name: {"expected": expected, "actual": types.get(name)}
            for name, expected in SOURCE_DUCKDB_TYPES.items()
            if types.get(name) != expected
        }
        if missing or wrong_types:
            raise R0SHSZPanelInputError(
                f"formal Parquet schema mismatch: missing={missing}, wrong_types={wrong_types}"
            )

        availability_rows = connection.execute(
            'SELECT "trade_date", MIN("available_at"), MAX("available_at"), '
            'COUNT(*), COUNT("available_at") FROM r0_shsz_payload '
            'WHERE "trade_date" BETWEEN ? AND ? '
            'GROUP BY "trade_date" ORDER BY "trade_date"',
            [start, end],
        ).fetchall()
        availability = _validate_bounded_availability(
            tuple(AvailabilityRecord(*row) for row in availability_rows)
        )

        where = ['"trade_date" BETWEEN ? AND ?']
        parameters: list[Any] = [start, end]
        if normalized_symbols is not None:
            placeholders = ",".join("?" for _ in normalized_symbols)
            where.append(f'"order_book_id" IN ({placeholders})')
            parameters.extend(normalized_symbols)
        predicate = " AND ".join(where)
        row_count, date_count, symbol_count = (
            int(value)
            for value in connection.execute(
                'SELECT COUNT(*), COUNT(DISTINCT "trade_date"), '
                f'COUNT(DISTINCT "order_book_id") FROM r0_shsz_payload WHERE {predicate}',
                parameters,
            ).fetchone()
        )
        if row_count < 1:
            raise R0SHSZPanelInputError("requested formal panel slice is empty")
        if date_count * symbol_count > max_panel_cells:
            raise R0SHSZPanelInputError(
                "requested dense PanelData exceeds max_panel_cells before payload fetch"
            )

        invalid = int(
            connection.execute(
                "SELECT COUNT(*) FROM r0_shsz_payload WHERE " + predicate + " AND ("
                "NOT regexp_full_match(order_book_id, '^[0-9]{6}\\.(XSHG|XSHE)$') OR "
                "exchange_mic NOT IN ('XSHG','XSHE') OR "
                "NOT ends_with(order_book_id, '.' || exchange_mic) OR "
                "previous_close <= 0 OR open <= 0 OR high <= 0 OR low <= 0 OR close <= 0 OR "
                "high < greatest(open, close, low) OR low > least(open, close, high) OR "
                "volume_shares < 0 OR turnover_cny < 0 OR "
                "NOT isfinite(realized_total_return) OR realized_total_return <= -1 OR "
                "(open_execution_eligible AND NOT (listed_episode_eligible AND had_trade)) OR "
                "(upper_limit_price IS NOT NULL AND upper_limit_price < 0) OR "
                "(lower_limit_price IS NOT NULL AND lower_limit_price < 0) OR "
                "(upper_limit_price IS NOT NULL AND lower_limit_price IS NOT NULL "
                " AND upper_limit_price > 0 AND lower_limit_price > 0 "
                " AND lower_limit_price >= upper_limit_price))",
                parameters,
            ).fetchone()[0]
        )
        if invalid:
            raise R0SHSZPanelInputError(
                f"formal dense slice contains {invalid} invalid rows"
            )

        selected = ",".join(f'"{name}"' for name in FETCH_COLUMNS)
        columns = connection.execute(
            f"SELECT {selected} FROM r0_shsz_payload WHERE {predicate}", parameters
        ).fetchnumpy()
    except R0SHSZPanelInputError:
        raise
    except Exception as error:
        raise R0SHSZPanelInputError(f"cannot read formal dense panel: {error}") from error
    finally:
        connection.close()

    symbol_rows = _plain_array(columns["order_book_id"]).astype(str)
    date_rows = _plain_array(columns["trade_date"]).astype("datetime64[D]")
    ordered_symbols = np.unique(symbol_rows)
    ordered_dates = np.unique(date_rows)
    if normalized_symbols is not None:
        missing_symbols = sorted(set(normalized_symbols) - set(ordered_symbols.tolist()))
        if missing_symbols:
            raise R0SHSZPanelInputError(
                f"requested symbols have no rows in the bounded slice: {missing_symbols}"
            )
    date_index = np.searchsorted(ordered_dates, date_rows)
    symbol_index = np.searchsorted(ordered_symbols, symbol_rows)
    linear = date_index.astype(np.int64) * len(ordered_symbols) + symbol_index.astype(np.int64)
    if len(np.unique(linear)) != row_count:
        raise R0SHSZPanelInputError("formal dense slice has duplicate primary keys")
    shape = (len(ordered_dates), len(ordered_symbols))
    fields = {
        name: np.full(shape, np.nan, dtype=np.float32)
        for name in ("open", "close", "high", "low", "volume", "amount", "realized_total_return")
    }
    mappings = {
        "open": "open",
        "close": "close",
        "high": "high",
        "low": "low",
        "volume": "volume_shares",
        "amount": "turnover_cny",
        "realized_total_return": "realized_total_return",
    }
    for target, source in mappings.items():
        fields[target][date_index, symbol_index] = _plain_array(columns[source]).astype(np.float32)

    listed = _plain_array(columns["listed_episode_eligible"]).astype(bool)
    traded = _plain_array(columns["had_trade"]).astype(bool)
    source_execution = _plain_array(columns["open_execution_eligible"]).astype(bool)
    row_eligible = listed & traded
    opens = _plain_array(columns["open"]).astype(np.float64)
    highs = _plain_array(columns["high"]).astype(np.float64)
    lows = _plain_array(columns["low"]).astype(np.float64)
    upper = _plain_array(columns["upper_limit_price"], fill=np.nan).astype(np.float64)
    lower = _plain_array(columns["lower_limit_price"], fill=np.nan).astype(np.float64)
    at_upper = np.isfinite(upper) & (upper > 0.0) & _price_equal(opens, upper)
    at_lower = np.isfinite(lower) & (lower > 0.0) & _price_equal(opens, lower)
    sealed_upper = at_upper & _price_equal(highs, upper) & _price_equal(lows, upper)
    sealed_lower = at_lower & _price_equal(highs, lower) & _price_equal(lows, lower)
    reopened = (at_upper & (lows < upper) & ~sealed_upper) | (
        at_lower & (highs > lower) & ~sealed_lower
    )
    row_buyable = np.where(
        upper == 0.0,
        row_eligible,
        np.where(np.isfinite(upper), row_eligible & ~sealed_upper, source_execution),
    )
    row_sellable = np.where(
        lower == 0.0,
        row_eligible,
        np.where(np.isfinite(lower), row_eligible & ~sealed_lower, source_execution),
    )
    eligible = np.zeros(shape, dtype=bool)
    buyable = np.zeros(shape, dtype=bool)
    sellable = np.zeros(shape, dtype=bool)
    reopened_mask = np.zeros(shape, dtype=bool)
    eligible[date_index, symbol_index] = row_eligible
    buyable[date_index, symbol_index] = row_buyable
    sellable[date_index, symbol_index] = row_sellable
    reopened_mask[date_index, symbol_index] = reopened
    reopened_mask.setflags(write=False)

    date_strings = tuple(np.datetime_as_string(value, unit="D") for value in ordered_dates)
    selected_availability = {
        value: availability[date.fromisoformat(value)].isoformat() for value in date_strings
    }
    signal_fields = tuple(fields)
    enforce_signal_field_isolation(tuple(fields), signal_fields)
    return PanelData(
        dates=date_strings,
        symbols=tuple(ordered_symbols.tolist()),
        fields=fields,
        eligible=eligible,
        buyable=buyable,
        sellable=sellable,
        metadata={
            "dataset_id": DATASET_ID,
            "snapshot_id": SNAPSHOT_ID,
            "content_hash": resolved.content_hash,
            "contract_id": "r0_shsz_pv_v2",
            "research_tier": "R0_SHSZ_PV",
            "loader": "DUCKDB_COLUMNAR_NUMPY_DENSE_V1",
            "numeric_dtype": "float32",
            "bounded_slice": {
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "requested_symbols": list(normalized_symbols) if normalized_symbols is not None else None,
                "max_panel_cells": max_panel_cells,
                "source_rows": row_count,
                "selected_payload_count": len(parquet_paths),
                "selected_payloads": [dict(item) for item in selected_payloads],
            },
            "available_at_by_trade_date": selected_availability,
            "decision_time": "TRADE_DATE_CLOSE",
            "execution_time": "NEXT_OBSERVED_TRADING_SESSION_OPEN",
            "signal_lag_sessions_minimum": 1,
            "execution_mask_semantics": (
                "simulation-only one-price-side block; opened limit board allowed with "
                "extra slippage stress; missing limits fall back to source open mask"
            ),
            "execution_mask_counts": {
                "buyable": int(buyable.sum()),
                "sellable": int(sellable.sum()),
                "reopened_limit_board": int(reopened_mask.sum()),
            },
            "execution_masks": {
                "reopened_limit_board": reopened_mask,
            },
            "execution_mask_field_policy": (
                "MASKS_ARE_METADATA_ONLY__EXCLUDED_FROM_FACTOR_FIELDS_AND_SIGNAL_FIELDS"
            ),
            "opened_limit_board_slippage_policy": "EXTRA_STRESS_REQUIRED",
            "opened_limit_board_extra_stress_bps": 20.0,
            "terminal_event_mask_status": (
                "UNAVAILABLE__R0_NARROW_PANEL_HAS_NO_VERIFIED_DELISTED_OR_INVALIDATED_MASK"
            ),
            "terminal_event_return_policy": (
                "NO_SYNTHETIC_WRITE_OFF__MISSING_OUTCOME_REMAINS_NAN"
            ),
            "raw_price_convention": "UNADJUSTED",
            "return_convention": "ONE_PERIOD_REALIZED_TOTAL_RETURN_WITH_CASH_REINVESTMENT",
            "signal_fields": signal_fields,
            "factor_runs": 0,
            "ic_runs": 0,
            "backtest_runs": 0,
            "validation_consumed": False,
            "holdout_opened": False,
        },
    )
