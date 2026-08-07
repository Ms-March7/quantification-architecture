"""Direct, dependency-free reader for a Qlib daily binary data store."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from .schema import PanelData


def detect_adjustment_anomalies(
    close: np.ndarray,
    change: np.ndarray,
    adjusted_return_threshold: float = 0.10,
    source_change_threshold: float = 0.11,
    disagreement_threshold: float = 0.05,
) -> np.ndarray:
    """Flag adjusted-price jumps contradicted by the source daily change.

    The public snapshot does not include a separately verified corporate-action
    ledger.  A large adjusted-price move is therefore quarantined when it also
    materially disagrees with the source's own daily-change field.  This is a
    deliberately conservative research gate, not a claim that every flagged
    observation is erroneous.
    """

    prices = np.asarray(close)
    raw_change = np.asarray(change)
    if prices.shape != raw_change.shape:
        raise ValueError("close and change must have identical shapes")
    adjusted_return = np.full(prices.shape, np.nan, dtype=np.float32)
    with np.errstate(divide="ignore", invalid="ignore"):
        adjusted_return[1:] = prices[1:] / prices[:-1] - 1.0
    return (
        np.isfinite(adjusted_return)
        & np.isfinite(raw_change)
        & (np.abs(adjusted_return) > adjusted_return_threshold)
        & (np.abs(raw_change) < source_change_threshold)
        & (np.abs(adjusted_return - raw_change) > disagreement_threshold)
    )


def causal_adjustment_quarantine(anomaly: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Quarantine close-time research now; invalidate execution next session."""

    events = np.asarray(anomaly, dtype=bool)
    if events.ndim != 2:
        raise ValueError("anomaly mask must be date-by-instrument")
    quarantine = np.maximum.accumulate(events, axis=0)
    first_events = np.zeros_like(events)
    if len(events):
        first_events[0] = events[0]
        first_events[1:] = events[1:] & ~quarantine[:-1]
    invalidated = np.zeros_like(events)
    if len(events) > 1:
        invalidated[1:] = first_events[:-1]
    return quarantine, invalidated


@dataclass(frozen=True)
class InstrumentInterval:
    symbol: str
    start: str
    end: str


class QlibBinaryStore:
    """Read Qlib's float32 ``*.day.bin`` files without installing Qlib."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.calendar_path = self.root / "calendars" / "day.txt"
        self.features_path = self.root / "features"
        self.instruments_path = self.root / "instruments"
        if not self.calendar_path.exists() or not self.features_path.is_dir():
            raise FileNotFoundError(f"not a Qlib daily store: {self.root}")
        self.calendar = tuple(
            line.strip() for line in self.calendar_path.read_text(encoding="utf-8").splitlines() if line.strip()
        )
        if not self.calendar or tuple(sorted(self.calendar)) != self.calendar or len(set(self.calendar)) != len(self.calendar):
            raise ValueError("Qlib calendar must be non-empty, unique, and strictly increasing")
        self._calendar_index = {date: index for index, date in enumerate(self.calendar)}
        provenance_path = self.root.parent / "PROVENANCE.json"
        self.provenance: dict[str, object] = {}
        if provenance_path.exists():
            try:
                self.provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self.provenance = {}

    def intervals(self, universe: str = "all") -> tuple[InstrumentInterval, ...]:
        """Load exact listing intervals or calendar-aligned index intervals."""

        path = self.instruments_path / f"{universe}.txt"
        if not path.exists():
            raise FileNotFoundError(f"unknown Qlib universe: {universe}")
        rows: list[InstrumentInterval] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split("\t")
            if len(parts) != 3:
                raise ValueError(f"invalid Qlib instrument interval: {line!r}")
            symbol, start, end = parts[0].upper(), parts[1], parts[2]
            if start > end:
                raise ValueError(f"invalid Qlib interval bounds: {line!r}")
            if universe == "all":
                if start not in self._calendar_index or end not in self._calendar_index:
                    raise ValueError(f"invalid Qlib interval bounds: {line!r}")
            else:
                start_index = bisect_left(self.calendar, start)
                end_index = bisect_right(self.calendar, end) - 1
                if (
                    start_index >= len(self.calendar)
                    or end_index < 0
                    or start_index > end_index
                ):
                    raise ValueError(f"invalid Qlib interval bounds: {line!r}")
                start = self.calendar[start_index]
                end = self.calendar[end_index]
            rows.append(InstrumentInterval(symbol, start, end))
        return tuple(rows)

    def available_fields(self, symbol: str) -> tuple[str, ...]:
        directory = self.features_path / symbol.lower()
        return tuple(sorted(path.name.removesuffix(".day.bin") for path in directory.glob("*.day.bin")))

    def _raw_tree_fingerprint(
        self,
        *,
        symbols: Sequence[str],
        fields: Sequence[str],
        universe: str,
    ) -> str:
        """Hash the exact raw tree later consumed by the holdout reader."""

        paths = {self.calendar_path, self.instruments_path / "all.txt", self.instruments_path / f"{universe}.txt"}
        required_fields = set(fields) | {"change"}
        for symbol in symbols:
            directory = self.features_path / symbol.lower()
            for field in required_fields:
                paths.add(directory / f"{field}.day.bin")
        hasher = hashlib.sha256(b"qlib-required-tree-v1")
        for path in sorted(paths, key=lambda item: item.relative_to(self.root).as_posix()):
            if not path.is_file():
                raise FileNotFoundError(f"required Qlib tree file is missing: {path}")
            relative = path.relative_to(self.root).as_posix().encode("utf-8")
            hasher.update(len(relative).to_bytes(8, "big"))
            hasher.update(relative)
            hasher.update(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(block)
        return hasher.hexdigest()

    def research_snapshot_identity(
        self,
        *,
        start_date: str,
        end_date: str,
        universe: str = "all",
        fields: Sequence[str] = ("open", "high", "low", "close", "volume", "amount"),
        exchanges: Iterable[str] = ("SH", "SZ"),
        stock_only: bool = True,
    ) -> dict[str, object]:
        """Hash a research tree without parsing any feature values."""

        if start_date not in self._calendar_index or end_date not in self._calendar_index:
            raise ValueError("start_date and end_date must be exact Qlib trading dates")
        exchange_set = {str(value).upper() for value in exchanges}
        rows = [
            row for row in self.intervals(universe)
            if row.symbol[:2] in exchange_set
            and row.end >= start_date
            and row.start <= end_date
            and (not stock_only or self._is_a_share(row.symbol))
        ]
        symbols = tuple(sorted({row.symbol for row in rows}))
        if not symbols:
            raise ValueError("no instruments intersect the requested snapshot identity")
        return {
            "store": str(self.root.resolve()),
            "start": start_date,
            "end": end_date,
            "calendar_dates": self._calendar_index[end_date] - self._calendar_index[start_date] + 1,
            "symbols": len(symbols),
            "universe": universe,
            "exchanges": sorted(exchange_set),
            "fields": sorted(set(fields) | {"change"}),
            "snapshot_sha256": self.provenance.get("sha256"),
            "snapshot_date": self.provenance.get("snapshot_date"),
            "raw_tree_fingerprint": self._raw_tree_fingerprint(
                symbols=symbols, fields=fields, universe=universe
            ),
        }

    @staticmethod
    def _is_a_share(symbol: str) -> bool:
        code = symbol[2:]
        if symbol.startswith("SH"):
            return code.startswith(("600", "601", "603", "605", "688", "689"))
        if symbol.startswith("SZ"):
            return code.startswith(("000", "001", "002", "003", "300", "301"))
        return symbol.startswith("BJ") and code.isdigit()

    def _read_feature_slice(
        self,
        symbol: str,
        field: str,
        start_index: int,
        end_index: int,
    ) -> np.ndarray:
        result = np.full(end_index - start_index + 1, np.nan, dtype=np.float32)
        path = self.features_path / symbol.lower() / f"{field}.day.bin"
        if not path.exists():
            raise FileNotFoundError(f"missing Qlib field {field!r} for {symbol}")
        raw = np.memmap(path, dtype="<f4", mode="r")
        if len(raw) <= 1 or not np.isfinite(raw[0]):
            raise ValueError(f"invalid Qlib feature header: {path}")
        if float(raw[0]) != float(int(raw[0])):
            raise ValueError(f"non-integer Qlib feature offset: {path}")
        feature_start = int(raw[0])
        values = raw[1:]
        feature_end = feature_start + len(values) - 1
        if feature_start < 0 or feature_end >= len(self.calendar):
            raise ValueError(f"Qlib feature range exceeds calendar: {path}")
        overlap_start = max(start_index, feature_start)
        overlap_end = min(end_index, feature_end)
        if overlap_start <= overlap_end:
            result[overlap_start - start_index:overlap_end - start_index + 1] = values[
                overlap_start - feature_start:overlap_end - feature_start + 1
            ]
        return result

    def load_panel(
        self,
        *,
        start_date: str,
        end_date: str,
        universe: str = "all",
        fields: Sequence[str] = ("open", "high", "low", "close", "volume", "amount"),
        exchanges: Iterable[str] = ("SH", "SZ"),
        minimum_history_dates: int = 60,
        apply_locked_limit_proxy: bool = False,
        stock_only: bool = True,
        quarantine_adjustment_anomalies: bool = True,
        anomaly_adjusted_return_threshold: float = 0.10,
        anomaly_source_change_threshold: float = 0.11,
        anomaly_disagreement_threshold: float = 0.05,
        seal_after_date: str | None = None,
    ) -> PanelData:
        if isinstance(minimum_history_dates, bool) or not isinstance(minimum_history_dates, int) or minimum_history_dates < 1:
            raise ValueError("minimum_history_dates must be a positive integer")
        if apply_locked_limit_proxy:
            raise ValueError(
                "same-day locked-limit proxy is forbidden for next-open execution; "
                "supply point-in-time official tradability states instead"
            )
        if not fields or len(set(fields)) != len(fields) or any(not isinstance(field, str) for field in fields):
            raise ValueError("fields must be a non-empty sequence of unique names")
        if "open" not in fields or "close" not in fields:
            raise ValueError("Qlib factor panels require both open and close fields")
        if start_date not in self._calendar_index or end_date not in self._calendar_index:
            raise ValueError("start_date and end_date must be exact Qlib trading dates")
        start_index = self._calendar_index[start_date]
        end_index = self._calendar_index[end_date]
        if start_index >= end_index:
            raise ValueError("start_date must precede end_date")
        if seal_after_date is not None:
            if seal_after_date not in self._calendar_index:
                raise ValueError("seal_after_date must be an exact Qlib trading date")
            content_end_index = self._calendar_index[seal_after_date]
            if not start_index <= content_end_index < end_index:
                raise ValueError("seal_after_date must be inside the panel and before end_date")
        else:
            content_end_index = end_index
        loaded_count = content_end_index - start_index + 1
        requested_exchanges = tuple(item.upper() for item in exchanges)
        if "BJ" in requested_exchanges:
            raise ValueError("BJ data requires a permanent-security-ID mapping and is not supported")
        if "vwap" in fields:
            raise ValueError("Qlib vwap is disabled until field-level quality gates are implemented")
        dates = self.calendar[start_index:end_index + 1]
        exchange_set = set(requested_exchanges)
        interval_rows = [
            row for row in self.intervals(universe)
            if row.symbol[:2] in exchange_set
            and row.end >= start_date
            and row.start <= end_date
            and (not stock_only or self._is_a_share(row.symbol))
        ]
        symbols = tuple(sorted({row.symbol for row in interval_rows}))
        if not symbols:
            raise ValueError("no instruments intersect the requested panel")
        raw_tree_fingerprint = self._raw_tree_fingerprint(
            symbols=symbols, fields=fields, universe=universe
        )
        research_membership = np.zeros((len(dates), len(symbols)), dtype=bool)
        listing_membership = np.zeros((len(dates), len(symbols)), dtype=bool)
        symbol_index = {symbol: index for index, symbol in enumerate(symbols)}

        def apply_intervals(rows: Iterable[InstrumentInterval], target: np.ndarray) -> None:
            for row in rows:
                if row.symbol not in symbol_index:
                    continue
                first = max(start_date, row.start)
                last = min(end_date, row.end)
                left = int(np.searchsorted(dates, first, side="left"))
                right = int(np.searchsorted(dates, last, side="right"))
                target[left:right, symbol_index[row.symbol]] = True

        apply_intervals(interval_rows, research_membership)
        apply_intervals(self.intervals("all"), listing_membership)
        delisted = np.zeros_like(listing_membership)
        delisted[1:] = listing_membership[:-1] & ~listing_membership[1:]

        matrices = {field: np.full((len(dates), len(symbols)), np.nan, dtype=np.float32) for field in fields}
        change = np.full((len(dates), len(symbols)), np.nan, dtype=np.float32)
        history_before = np.zeros(len(symbols), dtype=np.int32)
        anomaly = np.zeros((len(dates), len(symbols)), dtype=bool)
        quarantine = np.zeros_like(anomaly)
        invalidated = np.zeros_like(anomaly)
        execution_quarantine = np.zeros_like(anomaly)
        global_dates = np.arange(start_index, end_index + 1)
        for column, symbol in enumerate(symbols):
            full_open = self._read_feature_slice(symbol, "open", 0, content_end_index)
            full_close = self._read_feature_slice(symbol, "close", 0, content_end_index)
            full_change = self._read_feature_slice(symbol, "change", 0, content_end_index)
            full_price_valid = (
                np.isfinite(full_open) & (full_open > 0.0)
                & np.isfinite(full_close) & (full_close > 0.0)
            )
            history_before[column] = int(full_price_valid[:start_index].sum())
            for field in fields:
                if field == "open":
                    matrices[field][:loaded_count, column] = full_open[start_index:content_end_index + 1]
                elif field == "close":
                    matrices[field][:loaded_count, column] = full_close[start_index:content_end_index + 1]
                else:
                    matrices[field][:loaded_count, column] = self._read_feature_slice(
                        symbol, field, start_index, content_end_index
                    )
            change[:loaded_count, column] = full_change[start_index:content_end_index + 1]
            if quarantine_adjustment_anomalies:
                full_anomaly = detect_adjustment_anomalies(
                    full_close[:, None],
                    full_change[:, None],
                    adjusted_return_threshold=anomaly_adjusted_return_threshold,
                    source_change_threshold=anomaly_source_change_threshold,
                    disagreement_threshold=anomaly_disagreement_threshold,
                )[:, 0]
                anomaly[:loaded_count, column] = full_anomaly[start_index:content_end_index + 1]
                if np.any(full_anomaly):
                    first_event = int(np.flatnonzero(full_anomaly)[0])
                    quarantine[:, column] = global_dates >= first_event
                    execution_quarantine[:, column] = global_dates > first_event
                    invalidated[:, column] = global_dates == first_event + 1
        open_valid = np.isfinite(matrices["open"]) & (matrices["open"] > 0.0)
        research_price_valid = (
            np.isfinite(matrices["open"])
            & np.isfinite(matrices["close"])
            & (matrices["open"] > 0.0)
            & (matrices["close"] > 0.0)
        )
        research_enough_history = (
            history_before[None, :] + np.cumsum(research_price_valid, axis=0)
        ) >= minimum_history_dates
        completed_before_open = (
            history_before[None, :]
            + np.cumsum(research_price_valid, axis=0)
            - research_price_valid.astype(np.int64)
        )
        execution_enough_history = completed_before_open >= minimum_history_dates
        pre_quarantine_tradable = (
            research_membership & listing_membership & research_price_valid & research_enough_history
        )
        base_tradable = pre_quarantine_tradable.copy()
        if quarantine_adjustment_anomalies:
            base_tradable &= ~quarantine
        locked_up = np.zeros_like(base_tradable)
        locked_down = np.zeros_like(base_tradable)
        return PanelData(
            dates=tuple(dates),
            symbols=symbols,
            fields=matrices,
            eligible=base_tradable,
            buyable=(
                research_membership & listing_membership & open_valid & execution_enough_history
                & ~execution_quarantine
            ),
            sellable=listing_membership & open_valid & ~execution_quarantine,
            delisted=delisted,
            invalidated=invalidated,
            metadata={
                "source": "qlib_binary_store",
                "promotion_ceiling": "RESEARCH_ONLY",
                "promotion_blockers": [
                    "public multi-source snapshot without independently verified corporate actions",
                    "no official historical ST/suspension/price-limit state",
                    "normalized adjusted prices cannot reproduce broker lot execution",
                    "instrument intervals are price-availability, not verified official listing lifecycles",
                ],
                "store": str(self.root.resolve()),
                "snapshot_sha256": self.provenance.get("sha256"),
                "raw_tree_fingerprint": raw_tree_fingerprint,
                "snapshot_date": self.provenance.get("snapshot_date"),
                "universe": universe,
                "point_in_time_membership": False,
                "research_membership_source": universe,
                "listing_membership_source": "all",
                "interval_semantics": "vendor price-availability interval; not official listing master data",
                "delisting_policy": "availability-end stress writeoff; not a verified delisting classification",
                "delisting_events": int(delisted.sum()),
                "exchanges": sorted(exchange_set),
                "minimum_history_dates": minimum_history_dates,
                "stock_only": stock_only,
                "price_scale": "Qlib normalized adjusted prices",
                "execution_warning": "not raw broker prices; lot-size/minimum-commission simulation is not valid",
                "locked_limit_proxy": False,
                "locked_up_observations": int(locked_up.sum()),
                "locked_down_observations": int(locked_down.sum()),
                "limit_warning": "same-day high/low/change proxy disabled because it is unavailable at the execution open; official limit/ST flags are absent",
                "adjustment_anomaly_quarantine": quarantine_adjustment_anomalies,
                "adjustment_anomaly_adjusted_return_threshold": anomaly_adjusted_return_threshold,
                "adjustment_anomaly_source_change_threshold": anomaly_source_change_threshold,
                "adjustment_anomaly_disagreement_threshold": anomaly_disagreement_threshold,
                "adjustment_anomaly_events": int(anomaly.sum()),
                "adjustment_quarantine_observations": int(quarantine.sum()),
                "adjustment_execution_quarantine_observations": int(execution_quarantine.sum()),
                "adjustment_quarantine_policy": (
                    "history prescan through content_loaded_through; "
                    "research at first event close; execution from next session"
                ),
                "adjustment_invalidation_events": int(invalidated.sum()),
                "adjustment_quarantined_symbols": [
                    symbols[int(asset)] for asset in np.flatnonzero(np.any(anomaly, axis=0))
                ],
                "adjustment_anomaly_examples": [
                    {"date": dates[int(date)], "symbol": symbols[int(asset)]}
                    for date, asset in np.argwhere(anomaly)[:100]
                ],
                "content_loaded_through": self.calendar[content_end_index],
                "holdout_content_loaded": seal_after_date is None,
                "content_seal_policy": (
                    "full requested panel loaded"
                    if seal_after_date is None
                    else "feature bytes after content_loaded_through were not materialized"
                ),
            },
        )

    def audit_summary(self) -> dict[str, object]:
        feature_directories = [path for path in self.features_path.iterdir() if path.is_dir()]
        all_intervals = self.intervals("all")
        current = self.calendar[-1]
        active = {row.symbol for row in all_intervals if row.start <= current <= row.end}
        return {
            "root": str(self.root.resolve()),
            "calendar_start": self.calendar[0],
            "calendar_end": self.calendar[-1],
            "calendar_dates": len(self.calendar),
            "feature_instruments": len(feature_directories),
            "all_interval_rows": len(all_intervals),
            "active_on_last_date": len(active),
            "universes": sorted(path.stem for path in self.instruments_path.glob("*.txt")),
        }
