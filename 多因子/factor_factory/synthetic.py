"""Synthetic panels with a planted, causal cross-sectional signal."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from .schema import PanelData


def planted_signal_panel(
    *,
    dates: int = 320,
    assets: int = 30,
    seed: int = 20260715,
) -> PanelData:
    if dates < 100 or assets < 8:
        raise ValueError("synthetic demo needs at least 100 dates and 8 assets")
    rng = np.random.default_rng(seed)
    open_price = np.zeros((dates, assets), dtype=np.float64)
    close = np.zeros((dates, assets), dtype=np.float64)
    high = np.zeros((dates, assets), dtype=np.float64)
    low = np.zeros((dates, assets), dtype=np.float64)
    volume = np.zeros((dates, assets), dtype=np.float64)
    close[0] = 10.0 + rng.uniform(0.0, 20.0, size=assets)
    open_price[0] = close[0]
    returns = rng.normal(0.0, 0.006, size=assets)
    persistent_quality = rng.normal(0.0, 1.0, size=assets)
    for index in range(1, dates):
        cross_rank = np.argsort(np.argsort(returns)).astype(np.float64)
        cross_rank = cross_rank / max(1, assets - 1) - 0.5
        shock = rng.normal(0.0, 0.007, size=assets)
        returns = 0.0015 * cross_rank + 0.00015 * persistent_quality + shock
        overnight = rng.normal(0.0, 0.0015, size=assets)
        open_price[index] = close[index - 1] * np.exp(overnight)
        close[index] = open_price[index] * np.exp(returns)
    intraday_range = np.abs(rng.normal(0.006, 0.002, size=(dates, assets)))
    high[:] = np.maximum(open_price, close) * (1.0 + intraday_range)
    low[:] = np.minimum(open_price, close) * np.maximum(0.5, 1.0 - intraday_range)
    volume[:] = np.exp(rng.normal(14.0, 0.45, size=(dates, assets)))
    amount = volume * close
    start = date(2025, 1, 1)
    date_labels = tuple((start + timedelta(days=index)).isoformat() for index in range(dates))
    symbols = tuple(f"{600000 + index:06d}" for index in range(assets))
    return PanelData(
        dates=date_labels,
        symbols=symbols,
        fields={
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        },
        metadata={
            "source": "synthetic_planted_signal",
            "purpose": "engineering_validation_only",
            "planted_signal": "weak one-day cross-sectional momentum",
        },
    )
