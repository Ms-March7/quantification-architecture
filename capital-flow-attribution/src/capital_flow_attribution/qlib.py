from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Interval:
    symbol: str
    start: str
    end: str


def canonical_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if symbol.startswith("SH"):
        return f"{symbol[2:]}.SH"
    if symbol.startswith("SZ"):
        return f"{symbol[2:]}.SZ"
    if symbol.startswith("BJ"):
        return f"{symbol[2:]}.BJ"
    return symbol


def is_a_share(symbol: str) -> bool:
    code = symbol[2:]
    if symbol.startswith("SH"):
        return code.startswith(("600", "601", "603", "605", "688", "689"))
    if symbol.startswith("SZ"):
        return code.startswith(("000", "001", "002", "003", "300", "301"))
    return symbol.startswith("BJ") and code.isdigit()


class QlibContextReader:
    """Small dependency-free reader; the source remains read-only and RESEARCH_ONLY."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.calendar = tuple(
            line.strip()
            for line in (self.root / "calendars" / "day.txt").read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        self.calendar_index = {date: index for index, date in enumerate(self.calendar)}
        self.features = self.root / "features"
        self.intervals = self._load_intervals()

    def _load_intervals(self) -> tuple[Interval, ...]:
        rows: list[Interval] = []
        path = self.root / "instruments" / "all.txt"
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split("\t")
            if len(parts) == 3:
                rows.append(Interval(parts[0].upper(), parts[1], parts[2]))
        return tuple(rows)

    def pilot_dates(self, end_date: str, count: int) -> tuple[str, ...]:
        if end_date not in self.calendar_index:
            raise ValueError(f"pilot end date is not in Qlib calendar: {end_date}")
        end = self.calendar_index[end_date]
        start = end - count + 1
        if start < 0:
            raise ValueError("not enough Qlib dates for pilot")
        return self.calendar[start : end + 1]

    def _feature_slice(self, symbol: str, field: str, dates: Iterable[str]) -> np.ndarray:
        selected = tuple(dates)
        start = self.calendar_index[selected[0]]
        end = self.calendar_index[selected[-1]]
        result = np.full(end - start + 1, np.nan, dtype=np.float32)
        path = self.features / symbol.lower() / f"{field}.day.bin"
        if not path.exists():
            return result
        raw = np.memmap(path, dtype="<f4", mode="r")
        if len(raw) <= 1 or not np.isfinite(raw[0]):
            return result
        feature_start = int(raw[0])
        values = raw[1:]
        feature_end = feature_start + len(values) - 1
        left = max(start, feature_start)
        right = min(end, feature_end)
        if left <= right:
            result[left - start : right - start + 1] = values[left - feature_start : right - feature_start + 1]
        return result

    def universe_rows(self) -> list[dict[str, object]]:
        return [
            {
                "symbol": canonical_symbol(row.symbol),
                "source_symbol": row.symbol,
                "exchange": row.symbol[:2],
                "availability_start": row.start,
                "availability_end": row.end,
                "is_a_share_heuristic": is_a_share(row.symbol),
                "point_in_time_membership": False,
                "quality": "OK",
                "evidence_state": "PROXY",
                "notes": "vendor price-availability interval; not official listing lifecycle",
            }
            for row in self.intervals
        ]

    def market_context(self, dates: tuple[str, ...]) -> list[dict[str, object]]:
        accum: dict[tuple[str, str], dict[str, float]] = {}
        for date in dates:
            for exchange in ("SH", "SZ", "BJ"):
                accum[(date, exchange)] = {
                    "valid_count": 0.0,
                    "advancers": 0.0,
                    "decliners": 0.0,
                    "unchanged": 0.0,
                    "amount": 0.0,
                }
        for interval in self.intervals:
            if not is_a_share(interval.symbol) or interval.end < dates[0] or interval.start > dates[-1]:
                continue
            changes = self._feature_slice(interval.symbol, "change", dates)
            amounts = self._feature_slice(interval.symbol, "amount", dates)
            exchange = interval.symbol[:2]
            for offset, date in enumerate(dates):
                if not (interval.start <= date <= interval.end) or not np.isfinite(changes[offset]):
                    continue
                state = accum[(date, exchange)]
                state["valid_count"] += 1
                if changes[offset] > 0:
                    state["advancers"] += 1
                elif changes[offset] < 0:
                    state["decliners"] += 1
                else:
                    state["unchanged"] += 1
                if np.isfinite(amounts[offset]):
                    state["amount"] += float(amounts[offset])
        rows: list[dict[str, object]] = []
        for (date, exchange), state in sorted(accum.items()):
            rows.append(
                {
                    "date": date,
                    "exchange": exchange,
                    **{key: int(value) if key != "amount" else value for key, value in state.items()},
                    "source": "registered_qlib_snapshot_2026-07-14",
                    "quality": "OK" if state["valid_count"] else "MISSING",
                    "evidence_state": "PROXY",
                    "promotion_ceiling": "RESEARCH_ONLY",
                }
            )
        return rows

    def market_return_series(self, start_date: str, end_date: str) -> pd.DataFrame:
        """Build a survivorship-aware equal-weight A-share proxy from read-only Qlib files."""

        if start_date not in self.calendar_index or end_date not in self.calendar_index:
            raise ValueError("market-return boundaries must exist in the Qlib calendar")
        left, right = self.calendar_index[start_date], self.calendar_index[end_date]
        if left > right:
            raise ValueError("market-return start is after end")
        dates = self.calendar[left : right + 1]
        length = len(dates)
        return_sum = np.zeros(length, dtype=np.float64)
        valid_count = np.zeros(length, dtype=np.int64)
        advancers = np.zeros(length, dtype=np.int64)
        decliners = np.zeros(length, dtype=np.int64)
        amount_sum = np.zeros(length, dtype=np.float64)
        for interval in self.intervals:
            if not is_a_share(interval.symbol) or interval.end < start_date or interval.start > end_date:
                continue
            changes = self._feature_slice(interval.symbol, "change", dates)
            amounts = self._feature_slice(interval.symbol, "amount", dates)
            mask = np.isfinite(changes)
            return_sum[mask] += changes[mask]
            valid_count[mask] += 1
            advancers[mask] += changes[mask] > 0
            decliners[mask] += changes[mask] < 0
            amount_mask = np.isfinite(amounts)
            amount_sum[amount_mask] += amounts[amount_mask]
        market_return = np.divide(
            return_sum,
            valid_count,
            out=np.full(length, np.nan, dtype=np.float64),
            where=valid_count > 0,
        )
        breadth = np.divide(
            advancers - decliners,
            valid_count,
            out=np.full(length, np.nan, dtype=np.float64),
            where=valid_count > 0,
        )
        return pd.DataFrame(
            {
                "date": dates,
                "market_return": market_return,
                "breadth": breadth,
                "advancers": advancers,
                "decliners": decliners,
                "valid_count": valid_count,
                "amount_proxy": amount_sum,
                "evidence_state": "PROXY",
                "promotion_ceiling": "RESEARCH_ONLY",
            }
        )


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    names = fieldnames or (list(rows[0]) if rows else [])
    if not names:
        raise ValueError(f"fieldnames required for empty CSV: {path}")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=names, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
