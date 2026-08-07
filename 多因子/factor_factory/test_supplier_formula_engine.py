from __future__ import annotations

import unittest

import numpy as np

from factor_factory.supplier_formula_engine import SupplierFormulaEvaluator


class SupplierFormulaEngineTests(unittest.TestCase):
    def fields(self) -> dict[str, np.ndarray]:
        dates, assets = 30, 4
        base = np.arange(dates * assets, dtype=np.float32).reshape(dates, assets) + 10.0
        return {
            "open": base,
            "high": base + 2.0,
            "low": base - 2.0,
            "close": base + 1.0,
            "volume": base * 100.0,
            "amount": base * base * 100.0,
            "vwap": base,
            "realized_total_return": np.full_like(base, 0.01),
        }

    def test_alpha101_101_formula(self) -> None:
        engine = SupplierFormulaEvaluator(self.fields())
        values = engine.evaluate("((close - open) / ((high - low) + .001))")
        expected = 1.0 / 4.001
        self.assertAlmostEqual(float(values[-1, -1]), expected, places=6)

    def test_alpha101_003_formula_is_finite_after_window(self) -> None:
        fields = self.fields()
        # Make each asset's cross-sectional rank vary through time while volume
        # has the exact inverse rank.  Static per-asset ranks have zero time-
        # series variance, for which correlation is correctly undefined.
        ranks = np.asarray(
            [np.roll(np.arange(4, dtype=np.float32), date % 4) for date in range(30)]
        )
        fields["open"] = 10.0 + ranks
        fields["volume"] = 1000.0 - 10.0 * ranks
        engine = SupplierFormulaEvaluator(fields)
        values = engine.evaluate("(-1*correlation(rank(open), rank(volume), 10))")
        self.assertTrue(np.isfinite(values[20:]).all())
        self.assertTrue((values[20:] > 0.99).all())

    def test_power_and_rolling_sum_are_whitelisted(self) -> None:
        engine = SupplierFormulaEvaluator(self.fields())
        values = engine.evaluate("rank((-1*((1-(open/close))^1)))")
        self.assertEqual(values.shape, self.fields()["close"].shape)
        self.assertTrue(np.isfinite(values).all())
        summed = engine.evaluate("sum(returns, 5)")
        self.assertTrue(np.isnan(summed[:4]).all())
        self.assertAlmostEqual(float(summed[-1, 0]), 0.05, places=6)

    def test_arbitrary_python_and_conditionals_are_rejected(self) -> None:
        engine = SupplierFormulaEvaluator(self.fields())
        with self.assertRaises(ValueError):
            engine.evaluate("__import__('os').system('whoami')")
        with self.assertRaises(ValueError):
            engine.evaluate("close if open > 0 else low")


if __name__ == "__main__":
    unittest.main()
