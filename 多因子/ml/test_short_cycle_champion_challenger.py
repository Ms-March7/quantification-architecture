#!/usr/bin/env python3
"""Unit tests for the short-cycle champion-challenger research gate."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import numpy as np

import short_cycle_champion_challenger as lab


def synthetic_bars(days: int = 180, assets: int = 5) -> dict[str, list[dict[str, float | str]]]:
    result: dict[str, list[dict[str, float | str]]] = {}
    for asset in range(assets):
        rows = []
        price = 1.0 + asset * 0.05
        for day in range(days):
            drift = 0.0003 * (asset - 2) + 0.002 * np.sin((day + asset) / 9.0)
            open_price = price * (1.0 + 0.0002 * np.cos(day / 7.0))
            price = open_price * (1.0 + drift)
            rows.append({
                "time": (datetime(2025, 1, 1) + timedelta(days=day)).strftime("%Y-%m-%d 15:00"),
                "open": open_price,
                "close": price,
                "volume": 1_000_000 + asset * 20_000 + day * 100,
                "amount": price * (1_000_000 + asset * 20_000 + day * 100),
            })
        result[f"{510000 + asset}"] = rows
    return result


class ShortCycleLabTests(unittest.TestCase):
    def test_holding_period_enforces_t_plus_one(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 2"):
            lab.build_panel(synthetic_bars(), benchmark="510000", holding_days=1)

    def test_forward_target_uses_next_open_and_later_close(self) -> None:
        bars = synthetic_bars()
        panel = lab.build_panel(bars, benchmark="510000", holding_days=3)
        symbols, dates, fields = lab.align_bars(bars)
        source_index = int(panel.decision_source_indexes[0])
        entry = source_index + 1
        exit_index = source_index + 3
        raw = fields["close"][exit_index] / fields["open"][entry] - 1.0
        np.testing.assert_allclose(panel.asset_returns[0], raw, atol=1e-12)
        benchmark_index = symbols.index("510000")
        np.testing.assert_allclose(
            panel.excess_returns[0], raw - raw[benchmark_index], atol=1e-12
        )

    def test_feature_contract_and_shapes(self) -> None:
        panel = lab.build_panel(synthetic_bars(), benchmark="510000", holding_days=3)
        self.assertEqual(panel.features.shape[1], 5)
        self.assertEqual(panel.features.shape[2], len(lab.FEATURE_NAMES))
        self.assertEqual(panel.excess_returns.shape[:2], panel.features.shape[:2])
        self.assertTrue(np.isfinite(panel.features).all())

    def test_diversified_selection_rejects_correlated_duplicate(self) -> None:
        predictions = np.asarray([0.06, 0.05, 0.04, 0.03])
        correlations = np.eye(4)
        correlations[0, 1] = correlations[1, 0] = 0.97
        selected = lab.select_diversified(predictions, correlations, top_k=3, correlation_cap=0.90)
        self.assertEqual(selected, [0, 2, 3])

    def test_benchmark_can_be_excluded_from_selection(self) -> None:
        predictions = np.asarray([0.10, 0.06, 0.04])
        selected = lab.select_diversified(
            predictions, np.eye(3), top_k=2, correlation_cap=0.9, excluded_indexes=(0,)
        )
        self.assertEqual(selected, [1, 2])

    def test_validation_boundary_is_purged_by_full_holding_period(self) -> None:
        fit, validation = lab.purged_fit_validation_indexes(
            np.arange(100), validation_dates=20, holding_days=3
        )
        self.assertLess(fit[-1] + 3, validation[0])
        self.assertEqual(len(validation), 20)

    def test_ridge_recovers_linear_signal(self) -> None:
        rng = np.random.default_rng(23)
        x = rng.normal(size=(300, 4))
        y = x @ np.asarray([0.4, -0.2, 0.1, 0.3]) + rng.normal(scale=0.01, size=300)
        model = lab.fit_ridge(x[:250], y[:250], l2=0.1)
        prediction = lab.predict_ridge(model, x[250:])
        self.assertGreater(np.corrcoef(prediction, y[250:])[0, 1], 0.98)

    def test_drawdown(self) -> None:
        value = lab.max_drawdown([0.10, -0.20, 0.05])
        self.assertAlmostEqual(value, -0.20, places=8)


if __name__ == "__main__":
    unittest.main()
