"""Causal action-aware signal-price projection for the narrow R0 panel.

The admitted R0_SHSZ_PV input stores unadjusted executable prices and a
one-period realised total return.  This module never changes those execution
prices and never exposes corporate-action rows as signal fields.  It builds an
ephemeral, per-security total-return price view that is available at the same
close as the realised return and may therefore only drive next-session (or
later) execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from .schema import PanelData


PRICE_FIELDS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class TotalReturnProjection:
    """Ephemeral factor-input fields plus mechanical verification evidence."""

    fields: Mapping[str, np.ndarray]
    diagnostics: Mapping[str, Any]


def _require_field(panel: PanelData, name: str) -> np.ndarray:
    if name not in panel.fields:
        raise ValueError(f"total-return projection requires field {name!r}")
    return np.asarray(panel.fields[name], dtype=np.float64)


def build_total_return_signal_fields(
    panel: PanelData,
    *,
    base_value: float = 100.0,
    output_dtype: np.dtype[Any] = np.dtype(np.float32),
) -> TotalReturnProjection:
    """Build a causal total-return price view without mutating ``panel``.

    Each contiguous observed security episode is rebased independently.  For
    date ``t`` after the episode start, ``tr_close[t]`` equals
    ``tr_close[t-1] * (1 + realised_total_return[t])``.  All same-session OHLC
    levels are multiplied by ``tr_close[t] / raw_close[t]``.  Consequently the
    projected close-to-close return exactly follows the admitted total return,
    while same-session price geometry is preserved.

    Missing rows break an episode rather than being filled.  The arbitrary
    per-episode base means absolute projected price levels must not be compared
    cross-sectionally unless a factor first removes the per-security scale.
    """

    if not np.isfinite(base_value) or base_value <= 0.0:
        raise ValueError("base_value must be finite and positive")
    raw = {name: _require_field(panel, name) for name in PRICE_FIELDS}
    returns = _require_field(panel, "realized_total_return")
    shape = panel.shape
    if any(values.shape != shape for values in (*raw.values(), returns)):
        raise ValueError("projection inputs must match the panel shape")
    if np.any(np.isfinite(returns) & (returns <= -1.0)):
        raise ValueError("realized_total_return must be greater than -100%")

    observed = (
        np.isfinite(raw["open"])
        & np.isfinite(raw["high"])
        & np.isfinite(raw["low"])
        & np.isfinite(raw["close"])
        & np.isfinite(returns)
        & (raw["open"] > 0.0)
        & (raw["high"] > 0.0)
        & (raw["low"] > 0.0)
        & (raw["close"] > 0.0)
    )
    projected_close = np.full(shape, np.nan, dtype=np.float64)
    scale = np.full(shape, np.nan, dtype=np.float64)
    episode_starts = 0
    verified_links = 0
    max_reconstruction_error = 0.0

    for column in range(shape[1]):
        previous = -2
        for row in np.flatnonzero(observed[:, column]):
            if row != previous + 1:
                projected_close[row, column] = base_value
                episode_starts += 1
            else:
                projected_close[row, column] = projected_close[row - 1, column] * (
                    1.0 + returns[row, column]
                )
                reconstructed = (
                    projected_close[row, column] / projected_close[row - 1, column] - 1.0
                )
                error = abs(reconstructed - returns[row, column])
                max_reconstruction_error = max(max_reconstruction_error, float(error))
                verified_links += 1
            scale[row, column] = projected_close[row, column] / raw["close"][row, column]
            previous = int(row)

    if np.any(np.isfinite(scale) & (scale <= 0.0)):
        raise RuntimeError("non-positive total-return projection scale")
    fields: dict[str, np.ndarray] = {
        name: (raw[name] * scale).astype(output_dtype, copy=False) for name in PRICE_FIELDS
    }
    fields["realized_total_return"] = returns.astype(output_dtype, copy=True)
    for name in ("volume", "amount"):
        if name in panel.fields:
            fields[name] = np.asarray(panel.fields[name], dtype=output_dtype).copy()
    if "amount" in fields and "volume" in fields:
        with np.errstate(divide="ignore", invalid="ignore"):
            raw_vwap = np.where(fields["volume"] > 0.0, fields["amount"] / fields["volume"], np.nan)
            fields["vwap"] = (raw_vwap * scale).astype(output_dtype, copy=False)

    raw_close_return = np.full(shape, np.nan, dtype=np.float64)
    contiguous = observed[1:] & observed[:-1]
    raw_close_return[1:] = np.where(
        contiguous,
        raw["close"][1:] / raw["close"][:-1] - 1.0,
        np.nan,
    )
    action_divergence = (
        np.isfinite(raw_close_return)
        & np.isfinite(returns)
        & (np.abs(raw_close_return - returns) > 1e-8)
    )
    order_violations = int(
        np.count_nonzero(
            observed
            & (
                (fields["high"] < fields["low"])
                | (fields["high"] < fields["open"])
                | (fields["high"] < fields["close"])
                | (fields["low"] > fields["open"])
                | (fields["low"] > fields["close"])
            )
        )
    )
    diagnostics: dict[str, Any] = {
        "projection_id": "causal-chained-total-return-signal-view-v1",
        "source_dataset_id": panel.metadata.get("dataset_id"),
        "source_snapshot_id": panel.metadata.get("snapshot_id"),
        "source_content_hash": panel.metadata.get("content_hash"),
        "shape": list(shape),
        "observed_rows": int(np.count_nonzero(observed)),
        "episode_starts": int(episode_starts),
        "verified_contiguous_links": int(verified_links),
        "max_total_return_reconstruction_error": float(max_reconstruction_error),
        "raw_vs_total_return_divergence_rows": int(np.count_nonzero(action_divergence)),
        "ohlc_order_violations": order_violations,
        "missing_policy": "NO_FILL__MISSING_ROW_STARTS_NEW_EPISODE",
        "signal_available_at": "TRADE_DATE_CLOSE",
        "earliest_execution": "NEXT_OBSERVED_TRADING_SESSION_OPEN",
        "execution_prices_modified": False,
        "corporate_action_rows_exposed_as_signals": False,
        "persistent_factor_value_rows": 0,
        "cross_sectional_absolute_level_allowed": False,
    }
    if max_reconstruction_error > 1e-10 or order_violations:
        raise RuntimeError("total-return projection mechanical verification failed")
    return TotalReturnProjection(fields=fields, diagnostics=diagnostics)
