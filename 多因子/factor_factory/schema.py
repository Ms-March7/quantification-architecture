"""Shared point-in-time data contracts for factor research."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np


REQUIRED_FIELDS = ("open", "close", "volume")


@dataclass
class PanelData:
    """Dense date-by-instrument panel with explicit execution masks.

    Prices must use one consistent adjustment convention.  Membership and
    tradability masks must reflect what was known on each historical date.
    Missing observations are represented by NaN, never silently forward-filled.
    """

    dates: tuple[str, ...]
    symbols: tuple[str, ...]
    fields: dict[str, np.ndarray]
    eligible: np.ndarray | None = None
    buyable: np.ndarray | None = None
    sellable: np.ndarray | None = None
    delisted: np.ndarray | None = None
    invalidated: np.ndarray | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        shape = (len(self.dates), len(self.symbols))
        if not self.dates or not self.symbols:
            raise ValueError("panel dates and symbols must be non-empty")
        if tuple(sorted(self.dates)) != self.dates:
            raise ValueError("panel dates must be strictly chronological")
        if len(set(self.dates)) != len(self.dates) or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("panel dates and symbols must be unique")
        for name in REQUIRED_FIELDS:
            if name not in self.fields:
                raise ValueError(f"missing required field: {name}")
        normalized: dict[str, np.ndarray] = {}
        for name, values in self.fields.items():
            array = np.asarray(values)
            if array.dtype.kind not in "fc":
                array = array.astype(np.float32)
            elif array.dtype == np.float16:
                array = array.astype(np.float32)
            if array.shape != shape:
                raise ValueError(f"field {name} has shape {array.shape}; expected {shape}")
            normalized[name] = array
        self.fields = normalized

        valid_price = (
            np.isfinite(self.fields["open"])
            & np.isfinite(self.fields["close"])
            & (self.fields["open"] > 0.0)
            & (self.fields["close"] > 0.0)
        )
        valid_open = np.isfinite(self.fields["open"]) & (self.fields["open"] > 0.0)
        if self.eligible is None:
            self.eligible = valid_price.copy()
        else:
            self.eligible = self._validate_mask("eligible", self.eligible, shape) & valid_price
        if self.buyable is None:
            self.buyable = valid_open & self.eligible
        else:
            # Execution at the session open consumes the prior close signal.
            # Do not force the current close-time research mask onto it: an
            # anomaly first observed at today's close is only actionable next
            # session.
            self.buyable = self._validate_mask("buyable", self.buyable, shape) & valid_open
        if self.sellable is None:
            self.sellable = valid_open.copy()
        else:
            self.sellable = self._validate_mask("sellable", self.sellable, shape) & valid_open
        if self.delisted is None:
            self.delisted = np.zeros(shape, dtype=bool)
        else:
            self.delisted = self._validate_mask("delisted", self.delisted, shape)
        if self.invalidated is None:
            self.invalidated = np.zeros(shape, dtype=bool)
        else:
            self.invalidated = self._validate_mask("invalidated", self.invalidated, shape)

    @staticmethod
    def _validate_mask(name: str, values: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
        array = np.asarray(values, dtype=bool)
        if array.shape != shape:
            raise ValueError(f"mask {name} has shape {array.shape}; expected {shape}")
        return array

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.dates), len(self.symbols)


def _finite_number(value: Any, fallback: float = np.nan) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if np.isfinite(result) else fallback


def panel_from_bars(
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    minimum_dates: int = 80,
    metadata: Mapping[str, Any] | None = None,
) -> PanelData:
    """Build an aligned panel from source-stamped OHLCV rows.

    This convenience adapter uses the common-date intersection and is suitable
    for engineering smoke tests.  Production research should provide a proper
    point-in-time universe panel so delisted and suspended names are retained.
    """

    if len(bars_by_symbol) < 3:
        raise ValueError("at least three instruments are required")
    normalized: dict[str, dict[str, Mapping[str, Any]]] = {}
    for symbol, rows in bars_by_symbol.items():
        by_date: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            timestamp = str(row.get("time") or row.get("date") or "").strip()
            if timestamp:
                by_date[timestamp[:10]] = row
        normalized[str(symbol)] = by_date
    symbols = tuple(sorted(normalized))
    common_dates = set(normalized[symbols[0]])
    for symbol in symbols[1:]:
        common_dates &= set(normalized[symbol])
    dates = tuple(sorted(common_dates))
    if len(dates) < minimum_dates:
        raise ValueError(f"at least {minimum_dates} common dates are required; found {len(dates)}")

    optional_fields = ("high", "low", "amount")
    fields: dict[str, np.ndarray] = {}
    for name in REQUIRED_FIELDS + optional_fields:
        matrix = np.asarray(
            [[_finite_number(normalized[symbol][date].get(name)) for symbol in symbols] for date in dates],
            dtype=np.float32,
        )
        if name == "amount" and not np.isfinite(matrix).any():
            matrix = np.asarray(
                [[
                    _finite_number(normalized[symbol][date].get("close"))
                    * _finite_number(normalized[symbol][date].get("volume"))
                    for symbol in symbols
                ] for date in dates],
                dtype=np.float32,
            )
        if name in REQUIRED_FIELDS or np.isfinite(matrix).any():
            fields[name] = matrix
    return PanelData(
        dates=dates,
        symbols=symbols,
        fields=fields,
        metadata={**dict(metadata or {}), "alignment": "common_date_intersection_smoke_only"},
    )
