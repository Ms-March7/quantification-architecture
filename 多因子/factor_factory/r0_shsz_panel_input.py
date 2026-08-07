"""Fail-closed adapter for the exact formal ``R0_SHSZ_PV`` snapshot.

This module is intentionally narrower than :mod:`factor_factory.qdata_input`.
It accepts one catalog identity only, verifies the formal snapshot authorities
and payload inventory, then materializes a bounded date/instrument slice as
``PanelData``.  It never generates factors, runs IC/backtests, or touches any
research state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from .qdata_input import (
    QDataInputError,
    QDataResearchResolver,
    ResolvedResearchInput,
)
from .schema import PanelData


DATASET_ID = "cn-a-share-r0-shsz-pv-panel"
SNAPSHOT_ID = "20050104-20260803-v1"
CONTRACT_ID = "r0_shsz_pv_v2"
PAYLOAD_SCHEMA = "r0-shsz-pv-panel-v2"
HASH_ALGORITHM = "sha256-snapshot-contract-v1"
PAYLOAD_HASH_ALGORITHM = "sha256-canonical-file-list-v1"
TIMEZONE = ZoneInfo("Asia/Shanghai")
TAIL_TRADE_DATE = date(2026, 8, 3)
TAIL_AVAILABLE_DATE = date(2026, 8, 4)

AUTHORITY_RELATIVE_PATHS = frozenset(
    {
        "request.json",
        "manifest.json",
        "provenance.json",
        "partition-manifest.json",
        "quality/summary.json",
    }
)

EXPECTED_RESEARCH_INPUT_CONTRACT: dict[str, Any] = {
    "schema_version": 1,
    "research_tier": "R0_SHSZ_PV",
    "gate_id": "R0_SHSZ_PV",
    "contract_id": CONTRACT_ID,
    "dataset_id": DATASET_ID,
    "snapshot_id": SNAPSHOT_ID,
    "market_scope": "XSHG_XSHE_ORDINARY_A",
    "timezone": "Asia/Shanghai",
    "coverage_start_date": "2005-01-04",
    "coverage_end_date": "2026-08-03",
    "decision_time": "TRADE_DATE_CLOSE",
    "available_at_semantics": "NEXT_OBSERVED_TRADING_SESSION_OPEN",
    "earliest_execution_time": "NEXT_OBSERVED_TRADING_SESSION_OPEN",
    "signal_lag_sessions_minimum": 1,
    "raw_price_convention": "UNADJUSTED",
    "return_convention": "ONE_PERIOD_REALIZED_TOTAL_RETURN",
    "adjusted_price_levels_allowed": False,
    "latest_forward_adjusted_history_allowed": False,
    "corporate_action_features_allowed": False,
    "execution_mask_role": "SIMULATION_ONLY__NEVER_SIGNAL_RANK_INPUT",
    "survivorship_policy": (
        "DYNAMIC_LISTING_EPISODES__NO_CURRENT_ACTIVE_UNIVERSE_BACKFILL"
    ),
    "missing_value_policy": "NULL_NEVER_ZERO_FILL",
    "payload_schema": PAYLOAD_SCHEMA,
    "primary_key": ["order_book_id", "trade_date"],
    "payload_format": "PARQUET",
    "payload_discovery": "PARTITION_MANIFEST_CURATED_ENTRIES",
}

SOURCE_COLUMNS = (
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
SOURCE_DUCKDB_TYPES = {
    "order_book_id": "VARCHAR",
    "exchange_mic": "VARCHAR",
    "local_code": "VARCHAR",
    "trade_date": "DATE",
    "previous_close": "DOUBLE",
    "open": "DOUBLE",
    "high": "DOUBLE",
    "low": "DOUBLE",
    "close": "DOUBLE",
    "volume_shares": "BIGINT",
    "turnover_cny": "DOUBLE",
    "realized_total_return": "DOUBLE",
    "trading_status_code": "VARCHAR",
    "upper_limit_price": "DOUBLE",
    "lower_limit_price": "DOUBLE",
    "available_at": "TIMESTAMP WITH TIME ZONE",
    "listed_episode_eligible": "BOOLEAN",
    "had_trade": "BOOLEAN",
    "open_execution_eligible": "BOOLEAN",
    "source_id": "VARCHAR",
    "source_snapshot_id": "VARCHAR",
}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ORDER_BOOK_ID = re.compile(r"^[0-9]{6}\.(XSHG|XSHE)$")
_CURATED_PARQUET = re.compile(
    rf"^schema={re.escape(PAYLOAD_SCHEMA)}/year=[0-9]{{4}}/"
    r"month=(?:[1-9]|1[0-2])/part-[^/]+\.parquet$"
)
FORBIDDEN_SIGNAL_FIELDS = frozenset(
    {
        "listed_episode_eligible",
        "had_trade",
        "open_execution_eligible",
        "eligible",
        "buyable",
        "sellable",
    }
)


class R0SHSZPanelInputError(QDataInputError):
    """The exact formal R0 snapshot or requested slice failed closed."""


def enforce_signal_field_isolation(
    field_names: Sequence[str], signal_field_names: Sequence[str]
) -> None:
    """Reject execution/eligibility masks from every signal-readable namespace."""

    exposed = set(field_names) | set(signal_field_names)
    leaked = sorted(exposed & FORBIDDEN_SIGNAL_FIELDS)
    if leaked:
        raise R0SHSZPanelInputError(
            f"ex-post execution/eligibility masks cannot be signal fields: {leaked}"
        )


@dataclass(frozen=True, slots=True)
class AvailabilityRecord:
    trade_date: Any
    minimum_available_at: Any
    maximum_available_at: Any
    row_count: int
    nonnull_count: int


@dataclass(frozen=True, slots=True)
class PayloadSlice:
    rows: tuple[Mapping[str, Any], ...]
    availability: tuple[AvailabilityRecord, ...]


class ParquetSliceReader(Protocol):
    def read_slice(
        self,
        parquet_paths: Sequence[Path],
        *,
        start_date: date,
        end_date: date,
        symbols: tuple[str, ...] | None,
        max_panel_cells: int,
    ) -> PayloadSlice: ...


def _strict_json(path: Path) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise R0SHSZPanelInputError(
                    f"duplicate authority JSON key {key!r}: {path}"
                )
            result[key] = value
        return result

    def reject_nonfinite(value: str) -> None:
        raise R0SHSZPanelInputError(
            f"non-finite authority JSON number {value!r}: {path}"
        )

    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(
                handle,
                object_pairs_hook=reject_duplicates,
                parse_constant=reject_nonfinite,
            )
    except R0SHSZPanelInputError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise R0SHSZPanelInputError(f"cannot read strict JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise R0SHSZPanelInputError(f"authority JSON must be one object: {path}")
    return value


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise R0SHSZPanelInputError(f"cannot canonicalize snapshot authority: {error}") from error


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.resolve(strict=False))))


def _is_within(path: Path, directory: Path) -> bool:
    try:
        return os.path.commonpath((_path_key(path), _path_key(directory))) == _path_key(
            directory
        )
    except ValueError:
        return False


def _safe_workspace_relative(raw: Any, workspace_root: Path, label: str) -> Path:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise R0SHSZPanelInputError(f"{label} must be a normalized POSIX relative path")
    relative = Path(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise R0SHSZPanelInputError(f"{label} is unsafe: {raw!r}")
    path = workspace_root.joinpath(*relative.parts)
    if not _is_within(path, workspace_root):
        raise R0SHSZPanelInputError(f"{label} escapes the workspace")
    current = workspace_root
    for part in relative.parts:
        current /= part
        is_junction = getattr(current, "is_junction", None)
        if current.is_symlink() or (callable(is_junction) and is_junction()):
            raise R0SHSZPanelInputError(f"{label} traverses a reparse point: {current}")
    return path


def _iso_date(label: str, value: Any) -> date:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise R0SHSZPanelInputError(f"{label} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise R0SHSZPanelInputError(f"{label} must be an ISO date") from error
    if parsed.isoformat() != value:
        raise R0SHSZPanelInputError(f"{label} must use canonical YYYY-MM-DD")
    return parsed


def _timestamp(label: str, value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError as error:
            raise R0SHSZPanelInputError(f"{label} must be RFC 3339") from error
    else:
        raise R0SHSZPanelInputError(f"{label} must be a timezone-aware timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise R0SHSZPanelInputError(f"{label} must include an explicit UTC offset")
    return parsed.astimezone(TIMEZONE)


def _finite_float(label: str, value: Any, *, positive: bool = False) -> float:
    if isinstance(value, (bool, np.bool_)) or value is None:
        raise R0SHSZPanelInputError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise R0SHSZPanelInputError(f"{label} must be a finite number") from error
    if not np.isfinite(result) or (positive and result <= 0.0):
        qualifier = "positive " if positive else ""
        raise R0SHSZPanelInputError(f"{label} must be a finite {qualifier}number")
    return result


def _optional_nonnegative_limit(label: str, value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        numeric = np.nan
    if np.isnan(numeric):
        return None
    if not np.isfinite(numeric) or numeric < 0.0:
        raise R0SHSZPanelInputError(f"{label} must be null or a finite non-negative number")
    return numeric


def _same_price(left: float, right: float) -> bool:
    return abs(left - right) <= 1e-8 * max(1.0, abs(left), abs(right))


def _boolean(label: str, value: Any) -> bool:
    if not isinstance(value, (bool, np.bool_)):
        raise R0SHSZPanelInputError(f"{label} must be boolean; null/numeric is forbidden")
    return bool(value)


class DuckDBParquetSliceReader:
    """Read exact partition-manifest paths; globs and direct user paths are absent."""

    def read_slice(
        self,
        parquet_paths: Sequence[Path],
        *,
        start_date: date,
        end_date: date,
        symbols: tuple[str, ...] | None,
        max_panel_cells: int,
    ) -> PayloadSlice:
        try:
            import duckdb  # type: ignore[import-not-found]
        except ImportError as error:
            raise R0SHSZPanelInputError(
                "DuckDB is required to read the formal Parquet payload; use the central "
                "data-engineering Python runtime"
            ) from error
        if not parquet_paths:
            raise R0SHSZPanelInputError("partition manifest contains no curated Parquet files")
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
            columns = set(types)
            missing = sorted(set(SOURCE_COLUMNS) - columns)
            if missing:
                raise R0SHSZPanelInputError(
                    f"formal Parquet payload is missing required columns: {missing}"
                )
            wrong_types = {
                name: {"expected": expected, "actual": types.get(name)}
                for name, expected in SOURCE_DUCKDB_TYPES.items()
                if types.get(name) != expected
            }
            if wrong_types:
                raise R0SHSZPanelInputError(
                    f"formal Parquet payload has incompatible column types: {wrong_types}"
                )

            availability_rows = connection.execute(
                'SELECT "trade_date", MIN("available_at"), MAX("available_at"), '
                'COUNT(*), COUNT("available_at") FROM r0_shsz_payload '
                'GROUP BY "trade_date" ORDER BY "trade_date"'
            ).fetchall()
            availability = tuple(
                AvailabilityRecord(*row) for row in availability_rows
            )

            where = ['"trade_date" BETWEEN ? AND ?']
            parameters: list[Any] = [start_date, end_date]
            if symbols is not None:
                placeholders = ",".join("?" for _ in symbols)
                where.append(f'"order_book_id" IN ({placeholders})')
                parameters.extend(symbols)
            predicate = " AND ".join(where)
            count_row = connection.execute(
                'SELECT COUNT(*), COUNT(DISTINCT "trade_date"), '
                f'COUNT(DISTINCT "order_book_id") FROM r0_shsz_payload WHERE {predicate}',
                parameters,
            ).fetchone()
            row_count, date_count, symbol_count = (int(value) for value in count_row)
            if row_count < 1:
                raise R0SHSZPanelInputError("requested formal panel slice is empty")
            if date_count * symbol_count > max_panel_cells:
                raise R0SHSZPanelInputError(
                    "requested dense PanelData exceeds max_panel_cells before payload fetch"
                )
            selected = ",".join(f'"{name}"' for name in SOURCE_COLUMNS)
            cursor = connection.execute(
                f"SELECT {selected} FROM r0_shsz_payload WHERE {predicate} "
                'ORDER BY "trade_date", "order_book_id"',
                parameters,
            )
            names = tuple(str(item[0]) for item in cursor.description)
            rows = tuple(dict(zip(names, row)) for row in cursor.fetchall())
            return PayloadSlice(rows=rows, availability=availability)
        except R0SHSZPanelInputError:
            raise
        except Exception as error:
            raise R0SHSZPanelInputError(f"cannot read formal Parquet payload: {error}") from error
        finally:
            connection.close()


class R0SHSZPVPanelAdapter:
    """Resolve and materialize a bounded slice of one exact formal snapshot."""

    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        resolver: QDataResearchResolver | None = None,
        parquet_reader: ParquetSliceReader | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.workspace_root = Path(
            workspace_root or Path(__file__).resolve().parents[2]
        ).resolve(strict=False)
        self.resolver = resolver or QDataResearchResolver(
            workspace_root=self.workspace_root
        )
        self.parquet_reader = parquet_reader or DuckDBParquetSliceReader()
        self._now_provider = now_provider or (lambda: datetime.now(TIMEZONE))

    def load(
        self,
        *,
        start_date: str,
        end_date: str,
        symbols: Sequence[str] | None = None,
        dataset_id: str = DATASET_ID,
        snapshot_id: str = SNAPSHOT_ID,
        max_panel_cells: int = 2_000_000,
    ) -> PanelData:
        """Load one bounded slice after every formal/PIT gate passes.

        ``dataset_id`` and ``snapshot_id`` are exposed only so callers and tests
        can prove that aliases, ``latest``, paths, candidates, and review inputs
        fail before qdata is called.
        """

        self._exact_identity(dataset_id, snapshot_id)
        start = _iso_date("start_date", start_date)
        end = _iso_date("end_date", end_date)
        coverage_start = date.fromisoformat(
            EXPECTED_RESEARCH_INPUT_CONTRACT["coverage_start_date"]
        )
        coverage_end = date.fromisoformat(
            EXPECTED_RESEARCH_INPUT_CONTRACT["coverage_end_date"]
        )
        if start > end:
            raise R0SHSZPanelInputError("start_date must not follow end_date")
        if start < coverage_start or end > coverage_end:
            raise R0SHSZPanelInputError("requested slice escapes the frozen R0 coverage")
        if isinstance(max_panel_cells, bool) or not isinstance(max_panel_cells, int) or max_panel_cells < 1:
            raise R0SHSZPanelInputError("max_panel_cells must be a positive integer")
        normalized_symbols = self._symbols(symbols)
        self._validate_workspace_contract()

        resolved = self.resolver.resolve(dataset_id, snapshot_id)
        documents = self._validate_formal_resolution(resolved)
        parquet_paths = self._payload_paths(
            documents,
            verify_payload_hashes=True,
        )
        payload = self.parquet_reader.read_slice(
            parquet_paths,
            start_date=start,
            end_date=end,
            symbols=normalized_symbols,
            max_panel_cells=max_panel_cells,
        )
        availability = self._validate_availability(payload.availability)
        panel = self._build_panel(
            payload.rows,
            availability=availability,
            start=start,
            end=end,
            requested_symbols=normalized_symbols,
            max_panel_cells=max_panel_cells,
            resolved=resolved,
        )
        return panel

    @staticmethod
    def _exact_identity(dataset_id: str, snapshot_id: str) -> None:
        if dataset_id != DATASET_ID or snapshot_id != SNAPSHOT_ID:
            raise R0SHSZPanelInputError(
                f"only exact {DATASET_ID}@{SNAPSHOT_ID} is accepted; "
                "direct paths, latest, candidates, review identities, and aliases are forbidden"
            )

    @staticmethod
    def _symbols(symbols: Sequence[str] | None) -> tuple[str, ...] | None:
        if symbols is None:
            return None
        if isinstance(symbols, (str, bytes)):
            raise R0SHSZPanelInputError("symbols must be a sequence, not one string")
        normalized: list[str] = []
        for value in symbols:
            if not isinstance(value, str) or value != value.strip() or not _ORDER_BOOK_ID.fullmatch(value):
                raise R0SHSZPanelInputError(f"invalid SH/SZ order_book_id: {value!r}")
            normalized.append(value)
        if not normalized:
            raise R0SHSZPanelInputError("symbols must not be empty when supplied")
        if len(normalized) != len(set(normalized)):
            raise R0SHSZPanelInputError("symbols must be unique")
        return tuple(sorted(normalized))

    def _validate_workspace_contract(self) -> None:
        path = (
            self.workspace_root
            / "data"
            / "catalog"
            / "contracts"
            / CONTRACT_ID
            / "contract.json"
        )
        contract = _strict_json(path)
        checks = {
            "contract_id": CONTRACT_ID,
            "gate_id": "R0_SHSZ_PV",
        }
        for key, expected in checks.items():
            if contract.get(key) != expected:
                raise R0SHSZPanelInputError(f"workspace {CONTRACT_ID} has wrong {key}")
        target = contract.get("formal_target")
        if not isinstance(target, dict) or target.get("dataset_id") != DATASET_ID or target.get("snapshot_id") != SNAPSHOT_ID:
            raise R0SHSZPanelInputError("workspace contract formal_target identity mismatch")
        scope = contract.get("scope")
        if not isinstance(scope, dict) or (
            scope.get("coverage_start_date") != EXPECTED_RESEARCH_INPUT_CONTRACT["coverage_start_date"]
            or scope.get("coverage_end_date") != EXPECTED_RESEARCH_INPUT_CONTRACT["coverage_end_date"]
            or scope.get("venues") != ["XSHG", "XSHE"]
            or scope.get("instrument_scope") != "ORDINARY_A_SHARES_ONLY"
        ):
            raise R0SHSZPanelInputError("workspace contract R0 scope mismatch")
        semantics = contract.get("research_semantics")
        if not isinstance(semantics, dict):
            raise R0SHSZPanelInputError("workspace contract lacks research_semantics")
        semantic_checks = {
            "timezone": "Asia/Shanghai",
            "decision_time": "TRADE_DATE_CLOSE",
            "bar_available_at": "NEXT_OBSERVED_TRADING_SESSION_OPEN",
            "earliest_execution_time": "NEXT_OBSERVED_TRADING_SESSION_OPEN",
            "signal_lag_sessions_minimum": 1,
            "raw_price_convention": "UNADJUSTED",
            "adjusted_price_levels_allowed": False,
            "latest_forward_adjusted_history_allowed": False,
            "corporate_action_features_allowed": False,
            "missing_value_policy": "NULL_NEVER_ZERO_FILL",
            "execution_mask_role": "SIMULATION_ONLY__NEVER_SIGNAL_RANK_INPUT",
        }
        for key, expected in semantic_checks.items():
            if semantics.get(key) != expected:
                raise R0SHSZPanelInputError(
                    f"workspace contract research_semantics.{key} mismatch"
                )
        source_return = semantics.get("return_convention")
        if source_return != "ONE_PERIOD_REALIZED_TOTAL_RETURN_AVAILABLE_AFTER_PERIOD_CLOSE":
            raise R0SHSZPanelInputError("workspace contract return convention mismatch")

    def _validate_formal_resolution(
        self, resolved: ResolvedResearchInput
    ) -> dict[str, dict[str, Any]]:
        if resolved.dataset_id != DATASET_ID or resolved.snapshot_id != SNAPSHOT_ID:
            raise R0SHSZPanelInputError("qdata returned a non-exact R0 identity")
        if resolved.lifecycle != "frozen":
            raise R0SHSZPanelInputError("R0 PanelData requires lifecycle=frozen")
        if resolved.quality_status != "OK":
            raise R0SHSZPanelInputError("R0 PanelData requires quality_status=OK")
        if resolved.license_status not in {"DECLARED", "VERIFIED"}:
            raise R0SHSZPanelInputError(
                "R0 PanelData requires license_status DECLARED or VERIFIED"
            )
        if resolved.purpose_ceiling != "RESEARCH_ONLY":
            raise R0SHSZPanelInputError("R0 PanelData requires purpose_ceiling=RESEARCH_ONLY")
        if resolved.hash_algorithm != HASH_ALGORITHM:
            raise R0SHSZPanelInputError("R0 snapshot hash algorithm mismatch")
        if not isinstance(resolved.content_hash, str) or not _SHA256.fullmatch(resolved.content_hash):
            raise R0SHSZPanelInputError("R0 snapshot lacks a canonical content hash")
        root = resolved.resolved_path.resolve(strict=False)
        expected_paths = {
            relative: root.joinpath(*Path(relative).parts).resolve(strict=False)
            for relative in AUTHORITY_RELATIVE_PATHS
        }
        actual_paths: dict[str, Path] = {}
        for authority in resolved.authority_paths:
            candidate = authority.resolve(strict=False)
            if not _is_within(candidate, root):
                raise R0SHSZPanelInputError("authority path escapes the formal snapshot")
            relative = candidate.relative_to(root).as_posix()
            if relative in actual_paths:
                raise R0SHSZPanelInputError("duplicate formal authority path")
            actual_paths[relative] = candidate
        if set(actual_paths) != AUTHORITY_RELATIVE_PATHS:
            raise R0SHSZPanelInputError(
                "formal snapshot must expose exactly the five authority documents"
            )
        if any(actual_paths[name] != expected_paths[name] for name in expected_paths):
            raise R0SHSZPanelInputError("formal authority path layout mismatch")
        if any(not path.is_file() for path in actual_paths.values()):
            raise R0SHSZPanelInputError("one or more formal authority documents are missing")
        documents = {name: _strict_json(path) for name, path in actual_paths.items()}
        for name, document in documents.items():
            if document.get("dataset_id") != DATASET_ID or document.get("snapshot_id") != SNAPSHOT_ID:
                raise R0SHSZPanelInputError(f"{name} identity mismatch")
            if document.get("schema_version") != 1:
                raise R0SHSZPanelInputError(f"{name} schema_version mismatch")

        request = documents["request.json"]
        manifest = documents["manifest.json"]
        provenance = documents["provenance.json"]
        partitions = documents["partition-manifest.json"]
        quality = documents["quality/summary.json"]
        if manifest.get("status") != "SEALED_SNAPSHOT" or manifest.get("formal_workspace_execution") is not True:
            raise R0SHSZPanelInputError("manifest is not a sealed formal workspace snapshot")
        if str(manifest.get("root_path", "")).replace("\\", "/") != str(root).replace("\\", "/"):
            raise R0SHSZPanelInputError("manifest root_path does not match qdata resolved_path")
        if manifest.get("contract_id") != CONTRACT_ID or provenance.get("contract_id") != CONTRACT_ID:
            raise R0SHSZPanelInputError("formal authorities are not bound to r0_shsz_pv_v2")
        if manifest.get("dataset_schema_version") != PAYLOAD_SCHEMA:
            raise R0SHSZPanelInputError("formal manifest dataset_schema_version mismatch")
        if manifest.get("hash_algorithm") != HASH_ALGORITHM:
            raise R0SHSZPanelInputError("manifest hash_algorithm mismatch")
        if manifest.get("payload_hash_algorithm") != PAYLOAD_HASH_ALGORITHM:
            raise R0SHSZPanelInputError("manifest payload_hash_algorithm mismatch")
        if manifest.get("research_input_contract") != EXPECTED_RESEARCH_INPUT_CONTRACT:
            raise R0SHSZPanelInputError("manifest research_input_contract is not the exact R0_SHSZ_PV map")
        if request.get("license_status") != resolved.license_status:
            raise R0SHSZPanelInputError("request license_status is not catalog-bound")
        if request.get("quality_status") != "OK" or request.get("purpose_ceiling") != "RESEARCH_ONLY":
            raise R0SHSZPanelInputError("request does not bind the formal research policy")
        if quality.get("overall") != "PASS" or quality.get("catalog_quality_status") != "OK":
            raise R0SHSZPanelInputError("quality authority is not PASS/OK")
        if quality.get("purpose_ceiling") != "RESEARCH_ONLY" or quality.get("formal_workspace_execution") is not True:
            raise R0SHSZPanelInputError("quality authority is not formal research-only evidence")
        if provenance.get("mode") != "FORMAL_ATOMIC_WORKSPACE_PUBLICATION":
            raise R0SHSZPanelInputError("candidate/review/synthetic provenance is forbidden")
        expected_available_at = datetime.combine(
            TAIL_AVAILABLE_DATE,
            time(9, 30),
            tzinfo=TIMEZONE,
        )
        if _timestamp("qdata available_at", resolved.available_at) != expected_available_at:
            raise R0SHSZPanelInputError(
                "qdata available_at disagrees with the exact R0 terminal gate"
            )
        if _timestamp("manifest.available_at", manifest.get("available_at")) != expected_available_at:
            raise R0SHSZPanelInputError("formal manifest available_at is not 2026-08-04 09:30 Asia/Shanghai")
        if _timestamp("provenance.available_at", provenance.get("available_at")) != expected_available_at:
            raise R0SHSZPanelInputError("formal provenance available_at disagrees with manifest")
        current_time = _timestamp("research access clock", self._now_provider())
        if current_time < expected_available_at:
            raise R0SHSZPanelInputError(
                "formal R0 snapshot is not yet available for research; "
                f"wait until {expected_available_at.isoformat()}"
            )
        request_hash = _sha256_bytes(_canonical_bytes(request))
        if provenance.get("request_contract_hash") != request_hash:
            raise R0SHSZPanelInputError("provenance request_contract_hash mismatch")
        manifest_without_hash = dict(manifest)
        manifest_without_hash.pop("content_hash", None)
        contract_value = {
            "hash_algorithm": HASH_ALGORITHM,
            "request": request,
            "manifest": manifest_without_hash,
            "provenance": provenance,
            "partition_manifest": partitions,
            "quality": quality,
        }
        content_hash = _sha256_bytes(_canonical_bytes(contract_value))
        if manifest.get("content_hash") != content_hash or resolved.content_hash != content_hash:
            raise R0SHSZPanelInputError("formal snapshot content hash cannot be reconstructed")
        return documents

    def _payload_paths(
        self,
        documents: Mapping[str, Mapping[str, Any]],
        *,
        verify_payload_hashes: bool,
    ) -> tuple[Path, ...]:
        manifest = documents["manifest.json"]
        provenance = documents["provenance.json"]
        partitions = documents["partition-manifest.json"]
        files = partitions.get("files")
        if not isinstance(files, list) or not files:
            raise R0SHSZPanelInputError("partition-manifest.files must be non-empty")
        layer_roots = {
            layer: _safe_workspace_relative(
                manifest.get(f"{layer}_root"),
                self.workspace_root,
                f"manifest.{layer}_root",
            )
            for layer in ("raw", "curated")
        }
        canonical_entries: list[dict[str, Any]] = []
        curated: list[Path] = []
        seen_workspace: set[str] = set()
        seen_layer: set[tuple[str, str]] = set()
        declared_by_layer: dict[str, set[str]] = {"raw": set(), "curated": set()}
        for index, item in enumerate(files):
            if not isinstance(item, dict):
                raise R0SHSZPanelInputError(f"partition files[{index}] must be an object")
            layer = item.get("layer")
            relative = item.get("relative_path")
            workspace_relative = item.get("workspace_relative_path")
            size = item.get("bytes")
            digest = item.get("sha256")
            if layer not in {"raw", "curated"}:
                raise R0SHSZPanelInputError(f"partition files[{index}] has invalid layer")
            if not isinstance(relative, str) or not relative or "\\" in relative:
                raise R0SHSZPanelInputError(f"partition files[{index}] has unsafe relative_path")
            relative_path = Path(relative)
            if relative_path.is_absolute() or any(part in {"", ".", ".."} for part in relative_path.parts):
                raise R0SHSZPanelInputError(f"partition files[{index}] has unsafe relative_path")
            target = _safe_workspace_relative(
                workspace_relative,
                self.workspace_root,
                f"partition files[{index}].workspace_relative_path",
            )
            expected = layer_roots[layer].joinpath(*relative_path.parts)
            if _path_key(target) != _path_key(expected):
                raise R0SHSZPanelInputError(
                    f"partition files[{index}] does not match the declared layer root"
                )
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise R0SHSZPanelInputError(f"partition files[{index}].bytes is invalid")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise R0SHSZPanelInputError(f"partition files[{index}].sha256 is invalid")
            workspace_key = str(workspace_relative).casefold()
            layer_key = (str(layer), relative.casefold())
            if workspace_key in seen_workspace or layer_key in seen_layer:
                raise R0SHSZPanelInputError("duplicate/case-folded partition payload path")
            seen_workspace.add(workspace_key)
            seen_layer.add(layer_key)
            declared_by_layer[layer].add(relative.casefold())
            if not target.is_file():
                raise R0SHSZPanelInputError(f"formal payload file is missing: {workspace_relative}")
            if target.stat().st_size != size:
                raise R0SHSZPanelInputError(f"formal payload byte size mismatch: {workspace_relative}")
            if verify_payload_hashes and _sha256_file(target) != digest:
                raise R0SHSZPanelInputError(f"formal payload hash mismatch: {workspace_relative}")
            canonical_entries.append(
                {
                    "layer": layer,
                    "relative_path": relative,
                    "bytes": size,
                    "sha256": digest,
                }
            )
            if layer == "curated":
                if not _CURATED_PARQUET.fullmatch(relative):
                    raise R0SHSZPanelInputError(
                        "curated payload path is not the canonical R0 Parquet partition layout"
                    )
                curated.append(target)
        for layer, root in layer_roots.items():
            if not root.is_dir():
                raise R0SHSZPanelInputError(f"formal {layer} payload root is missing")
            discovered: set[str] = set()
            for path in root.rglob("*"):
                is_junction = getattr(path, "is_junction", None)
                if path.is_symlink() or (callable(is_junction) and is_junction()):
                    raise R0SHSZPanelInputError(
                        f"formal {layer} payload contains a reparse point: {path}"
                    )
                if path.is_file():
                    discovered.add(path.relative_to(root).as_posix().casefold())
            if discovered != declared_by_layer[layer]:
                raise R0SHSZPanelInputError(
                    f"formal {layer} payload file set differs from partition-manifest"
                )
        canonical_entries.sort(key=lambda value: (value["layer"], value["relative_path"]))
        payload_hash = _sha256_bytes(_canonical_bytes(canonical_entries))
        if (
            partitions.get("payload_hash") != payload_hash
            or manifest.get("payload_hash") != payload_hash
            or provenance.get("payload_hash") != payload_hash
        ):
            raise R0SHSZPanelInputError("formal payload hash bindings disagree")
        if not curated:
            raise R0SHSZPanelInputError("no curated R0 Parquet partitions were declared")
        return tuple(sorted(curated, key=lambda value: value.as_posix()))

    @staticmethod
    def _validate_availability(
        records: Sequence[AvailabilityRecord],
    ) -> dict[date, datetime]:
        if not records:
            raise R0SHSZPanelInputError("formal payload has no availability calendar")
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
        if normalized[0][0] != date(2005, 1, 4) or normalized[-1][0] != TAIL_TRADE_DATE:
            raise R0SHSZPanelInputError("availability calendar does not cover the frozen R0 endpoints")
        result: dict[date, datetime] = {}
        for index, (trade_date, actual) in enumerate(normalized):
            if index + 1 < len(normalized):
                next_date = normalized[index + 1][0]
            elif trade_date == TAIL_TRADE_DATE:
                next_date = TAIL_AVAILABLE_DATE
            else:
                raise R0SHSZPanelInputError("R0 terminal availability date is unbound")
            expected = datetime.combine(next_date, time(9, 30), tzinfo=TIMEZONE)
            if actual != expected:
                raise R0SHSZPanelInputError(
                    f"available_at for {trade_date} must be next observed session 09:30; "
                    f"expected {expected.isoformat()}, found {actual.isoformat()}"
                )
            if actual.date() <= trade_date:
                raise R0SHSZPanelInputError(
                    f"same-day/future leakage guard failed for {trade_date}"
                )
            result[trade_date] = actual
        return result

    @staticmethod
    def _build_panel(
        rows: Sequence[Mapping[str, Any]],
        *,
        availability: Mapping[date, datetime],
        start: date,
        end: date,
        requested_symbols: tuple[str, ...] | None,
        max_panel_cells: int,
        resolved: ResolvedResearchInput,
    ) -> PanelData:
        if not rows:
            raise R0SHSZPanelInputError("requested formal panel slice is empty")
        normalized: list[dict[str, Any]] = []
        primary_keys: set[tuple[str, date]] = set()
        dates: set[date] = set()
        symbols: set[str] = set()
        for row_index, row in enumerate(rows):
            missing = [column for column in SOURCE_COLUMNS if column not in row]
            if missing:
                raise R0SHSZPanelInputError(
                    f"payload row {row_index} lacks required columns: {missing}"
                )
            symbol = row["order_book_id"]
            if not isinstance(symbol, str) or not _ORDER_BOOK_ID.fullmatch(symbol):
                raise R0SHSZPanelInputError(f"payload row {row_index} has invalid identity")
            exchange = row["exchange_mic"]
            if exchange not in {"XSHG", "XSHE"} or not symbol.endswith(f".{exchange}"):
                raise R0SHSZPanelInputError(f"payload row {row_index} exchange identity mismatch")
            trade_date = _iso_date(f"payload row {row_index}.trade_date", row["trade_date"])
            if not start <= trade_date <= end:
                raise R0SHSZPanelInputError("Parquet reader returned a row outside the requested dates")
            key = (symbol, trade_date)
            if key in primary_keys:
                raise R0SHSZPanelInputError(f"duplicate payload primary key: {key}")
            primary_keys.add(key)
            row_available = _timestamp(
                f"payload row {row_index}.available_at", row["available_at"]
            )
            if availability.get(trade_date) != row_available:
                raise R0SHSZPanelInputError(
                    f"payload row {row_index} available_at disagrees with the calendar"
                )
            listed = _boolean(
                f"payload row {row_index}.listed_episode_eligible",
                row["listed_episode_eligible"],
            )
            had_trade = _boolean(
                f"payload row {row_index}.had_trade", row["had_trade"]
            )
            open_execution = _boolean(
                f"payload row {row_index}.open_execution_eligible",
                row["open_execution_eligible"],
            )
            eligible = listed and had_trade
            if open_execution and not eligible:
                raise R0SHSZPanelInputError(
                    "open_execution_eligible cannot be true outside listed/had_trade rows"
                )
            prices = {
                name: _finite_float(
                    f"payload row {row_index}.{name}", row[name], positive=True
                )
                for name in ("previous_close", "open", "high", "low", "close")
            }
            if prices["high"] < max(prices["open"], prices["close"], prices["low"]):
                raise R0SHSZPanelInputError(f"payload row {row_index} has invalid OHLC high")
            if prices["low"] > min(prices["open"], prices["close"], prices["high"]):
                raise R0SHSZPanelInputError(f"payload row {row_index} has invalid OHLC low")
            volume = _finite_float(
                f"payload row {row_index}.volume_shares", row["volume_shares"]
            )
            turnover = _finite_float(
                f"payload row {row_index}.turnover_cny", row["turnover_cny"]
            )
            if volume < 0.0 or turnover < 0.0:
                raise R0SHSZPanelInputError(
                    f"payload row {row_index} has negative volume/turnover"
                )
            realized_return = _finite_float(
                f"payload row {row_index}.realized_total_return",
                row["realized_total_return"],
            )
            upper_limit = _optional_nonnegative_limit(
                f"payload row {row_index}.upper_limit_price",
                row["upper_limit_price"],
            )
            lower_limit = _optional_nonnegative_limit(
                f"payload row {row_index}.lower_limit_price",
                row["lower_limit_price"],
            )
            if (
                upper_limit is not None and upper_limit > 0.0
                and lower_limit is not None and lower_limit > 0.0
                and lower_limit >= upper_limit
            ):
                raise R0SHSZPanelInputError(
                    f"payload row {row_index} has invalid price-limit ordering"
                )
            at_upper_open = upper_limit is not None and upper_limit > 0.0 and _same_price(prices["open"], upper_limit)
            at_lower_open = lower_limit is not None and lower_limit > 0.0 and _same_price(prices["open"], lower_limit)
            sealed_upper = bool(
                at_upper_open
                and _same_price(prices["high"], upper_limit)
                and _same_price(prices["low"], upper_limit)
            )
            sealed_lower = bool(
                at_lower_open
                and _same_price(prices["high"], lower_limit)
                and _same_price(prices["low"], lower_limit)
            )
            reopened_limit_board = bool(
                (at_upper_open and prices["low"] < float(upper_limit) and not sealed_upper)
                or (at_lower_open and prices["high"] > float(lower_limit) and not sealed_lower)
            )
            buyable = (
                eligible and not sealed_upper
                if upper_limit is not None and upper_limit > 0.0
                else eligible
                if upper_limit == 0.0
                else open_execution
            )
            sellable = (
                eligible and not sealed_lower
                if lower_limit is not None and lower_limit > 0.0
                else eligible
                if lower_limit == 0.0
                else open_execution
            )
            normalized.append(
                {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "open": prices["open"],
                    "high": prices["high"],
                    "low": prices["low"],
                    "close": prices["close"],
                    "volume": volume,
                    "amount": turnover,
                    # Never recompute this from close/previous_close: cash and
                    # split actions can make the realized total return differ.
                    "realized_total_return": realized_return,
                    "eligible": eligible,
                    "open_execution_eligible": open_execution,
                    "buyable": buyable,
                    "sellable": sellable,
                    "reopened_limit_board": reopened_limit_board,
                }
            )
            dates.add(trade_date)
            symbols.add(symbol)
        if requested_symbols is not None:
            missing_symbols = sorted(set(requested_symbols) - symbols)
            if missing_symbols:
                raise R0SHSZPanelInputError(
                    f"requested symbols have no rows in the bounded slice: {missing_symbols}"
                )
        ordered_dates = tuple(sorted(dates))
        ordered_symbols = tuple(sorted(symbols))
        if len(ordered_dates) * len(ordered_symbols) > max_panel_cells:
            raise R0SHSZPanelInputError("requested dense PanelData exceeds max_panel_cells")
        date_index = {value: index for index, value in enumerate(ordered_dates)}
        symbol_index = {value: index for index, value in enumerate(ordered_symbols)}
        shape = (len(ordered_dates), len(ordered_symbols))
        fields = {
            name: np.full(shape, np.nan, dtype=np.float64)
            for name in (
                "open",
                "close",
                "high",
                "low",
                "volume",
                "amount",
                "realized_total_return",
            )
        }
        eligible = np.zeros(shape, dtype=bool)
        buyable = np.zeros(shape, dtype=bool)
        sellable = np.zeros(shape, dtype=bool)
        reopened_limit_board = np.zeros(shape, dtype=bool)
        for row in normalized:
            index = (
                date_index[row["trade_date"]],
                symbol_index[row["symbol"]],
            )
            for field in fields:
                fields[field][index] = row[field]
            eligible[index] = bool(row["eligible"])
            buyable[index] = bool(row["buyable"])
            sellable[index] = bool(row["sellable"])
            reopened_limit_board[index] = bool(row["reopened_limit_board"])
        selected_availability = {
            value.isoformat(): availability[value].isoformat()
            for value in ordered_dates
        }
        signal_fields = tuple(fields)
        enforce_signal_field_isolation(tuple(fields), signal_fields)
        return PanelData(
            dates=tuple(value.isoformat() for value in ordered_dates),
            symbols=ordered_symbols,
            fields=fields,
            eligible=eligible,
            # Index d is the d-open execution mask.  The backtest consumes the
            # close-d-1 signal and checks buyable/sellable at index d.
            buyable=buyable,
            sellable=sellable,
            metadata={
                "dataset_id": DATASET_ID,
                "snapshot_id": SNAPSHOT_ID,
                "content_hash": resolved.content_hash,
                "contract_id": CONTRACT_ID,
                "research_tier": "R0_SHSZ_PV",
                "bounded_slice": {
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "requested_symbols": list(requested_symbols)
                    if requested_symbols is not None
                    else None,
                    "max_panel_cells": max_panel_cells,
                },
                "source_field_mapping": {
                    "volume": "volume_shares",
                    "amount": "turnover_cny",
                    "realized_total_return": "realized_total_return",
                },
                "available_at_by_trade_date": selected_availability,
                "decision_time": "TRADE_DATE_CLOSE",
                "bar_availability_semantics": (
                    "row[d] becomes available only at next_observed_session[d+1] 09:30"
                ),
                "execution_time": "NEXT_OBSERVED_TRADING_SESSION_OPEN",
                "signal_lag_sessions_minimum": 1,
                "eligible_semantics": (
                    "listed_episode_eligible AND had_trade observed ex-post for row[d]; "
                    "row[d] is not available before open[d+1]"
                ),
                "execution_mask_semantics": (
                    "buyable/sellable[d] are ex-post simulation-only masks for an "
                    "order from signal[d-1]; observed one-price upper limits block "
                    "buys, observed one-price lower limits block sells, and an "
                    "opened limit board is allowed but requires slippage stress; "
                    "vendor zero denotes no active bound, while null limit prices "
                    "fall back to open_execution_eligible; "
                    "never a PIT signal or ranking feature"
                ),
                "execution_mask_counts": {
                    "buyable": int(buyable.sum()),
                    "sellable": int(sellable.sum()),
                    "reopened_limit_board": int(reopened_limit_board.sum()),
                },
                "opened_limit_board_slippage_policy": "EXTRA_STRESS_REQUIRED",
                "raw_price_convention": "UNADJUSTED",
                "return_convention": "ONE_PERIOD_REALIZED_TOTAL_RETURN_WITH_CASH_REINVESTMENT",
                "signal_fields": signal_fields,
                "forbidden_signal_fields": tuple(sorted(FORBIDDEN_SIGNAL_FIELDS)),
                "factor_runs": 0,
                "ic_runs": 0,
                "backtest_runs": 0,
                "validation_consumed": False,
                "holdout_opened": False,
            },
        )


def load_r0_shsz_panel(
    *,
    start_date: str,
    end_date: str,
    symbols: Sequence[str] | None = None,
    workspace_root: str | Path | None = None,
    max_panel_cells: int = 2_000_000,
) -> PanelData:
    """Convenience wrapper that still performs exact qdata research resolution."""

    return R0SHSZPVPanelAdapter(workspace_root=workspace_root).load(
        start_date=start_date,
        end_date=end_date,
        symbols=symbols,
        max_panel_cells=max_panel_cells,
    )
