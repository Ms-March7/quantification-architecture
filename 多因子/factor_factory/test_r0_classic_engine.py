from __future__ import annotations

import json
from pathlib import Path
import unittest

import numpy as np

from factor_factory.accelerator import AcceleratorError, accelerator_signature
from factor_factory.r0_classic_engine import (
    R0ClassicFactorEngine,
    R0ClassicFactorError,
    SUPPORTED_KINDS,
)
from factor_factory.schema import PanelData
from factor_factory.total_return_projection import build_total_return_signal_fields


def synthetic_panel(dates: int = 320, assets: int = 8, *, missing: bool = False) -> PanelData:
    rng = np.random.default_rng(20260806)
    market = rng.normal(0.0004, 0.011, size=(dates, 1))
    idiosyncratic = rng.normal(0.0, 0.009, size=(dates, assets))
    returns = np.clip(market + idiosyncratic, -0.12, 0.12).astype(np.float32)
    raw_close = (20.0 * np.exp(np.cumsum(returns, axis=0))).astype(np.float32)
    gap = rng.normal(0.0, 0.004, size=(dates, assets)).astype(np.float32)
    raw_open = (raw_close * np.exp(gap)).astype(np.float32)
    spread = rng.uniform(0.003, 0.035, size=(dates, assets)).astype(np.float32)
    high = (np.maximum(raw_open, raw_close) * (1.0 + spread)).astype(np.float32)
    low = (np.minimum(raw_open, raw_close) * (1.0 - spread)).astype(np.float32)
    volume = rng.lognormal(14.0, 0.6, size=(dates, assets)).astype(np.float32)
    amount = (volume * (raw_open + raw_close + high + low) / 4.0).astype(np.float32)
    eligible = np.ones((dates, assets), dtype=bool)
    fields = {
        "open": raw_open,
        "high": high,
        "low": low,
        "close": raw_close,
        "volume": volume,
        "amount": amount,
        "realized_total_return": returns,
    }
    if missing:
        eligible[40, 0] = False
        for values in fields.values():
            values[40, 0] = np.nan
    return PanelData(
        dates=tuple(f"2025-{1 + row // 28:02d}-{1 + row % 28:02d}" for row in range(dates)),
        symbols=tuple(f"{600000 + column:06d}.XSHG" for column in range(assets)),
        fields=fields,
        eligible=eligible,
    )


def spec(kind: str, params: dict[str, object], **extra: object) -> dict[str, object]:
    return {
        "factor_id": f"test:{kind}",
        "kind": kind,
        "params": params,
        "inputs": ["open", "high", "low", "close", "volume", "amount", "realized_total_return"],
        "formula": "",
        **extra,
    }


class R0ClassicFactorEngineTests(unittest.TestCase):
    def test_window_shape_dtype_and_known_total_return(self) -> None:
        panel = synthetic_panel(40, 5)
        engine = R0ClassicFactorEngine(panel, reject_constant=False, max_cache_bytes=8_000_000)
        values = engine.evaluate(spec("total_return", {"window": 5}))
        expected = np.prod(1.0 + panel.fields["realized_total_return"][6:11, 2]) - 1.0
        self.assertEqual(values.shape, panel.shape)
        self.assertEqual(values.dtype, np.float32)
        self.assertTrue(np.isnan(values[:4]).all())
        self.assertAlmostEqual(float(values[10, 2]), float(expected), places=6)

    def test_missing_row_is_hard_episode_boundary(self) -> None:
        panel = synthetic_panel(70, 5, missing=True)
        engine = R0ClassicFactorEngine(panel, reject_constant=False, max_cache_bytes=8_000_000)
        values = engine.evaluate(spec("moving_average_ratio", {"window": 4}))
        self.assertTrue(np.isnan(values[40:44, 0]).all())
        self.assertTrue(np.isfinite(values[44, 0]))
        self.assertTrue(np.isfinite(values[41:, 1]).all())

    def test_research_eligibility_is_cross_sectional_not_a_history_boundary(self) -> None:
        panel = synthetic_panel(140, 5)
        research = np.ones(panel.shape, dtype=bool)
        research[:119, -1] = False

        baseline = R0ClassicFactorEngine(
            panel,
            panel.fields,
            research_eligible=research,
            reject_constant=False,
        )
        changed_fields = {name: values.copy() for name, values in panel.fields.items()}
        changed_fields["close"][:119, -1] *= 100.0
        changed_fields["realized_total_return"][:119, -1] = 0.50
        changed = PanelData(
            dates=panel.dates,
            symbols=panel.symbols,
            fields=changed_fields,
            eligible=panel.eligible.copy(),
        )
        perturbed = R0ClassicFactorEngine(
            changed,
            changed.fields,
            research_eligible=research,
            reject_constant=False,
        )

        rank_spec = spec(
            "supplier_dsl",
            {"vwap": "amount_div_volume"},
            formula="rank(close)",
        )
        baseline_rank = baseline.evaluate(rank_spec)
        perturbed_rank = perturbed.evaluate(rank_spec)
        np.testing.assert_allclose(
            baseline_rank[:119, :-1], perturbed_rank[:119, :-1], equal_nan=True
        )
        self.assertTrue(np.isnan(baseline_rank[:119, -1]).all())
        np.testing.assert_allclose(
            baseline._daily_market_return()[:119],
            perturbed._daily_market_return()[:119],
            equal_nan=True,
        )

        # The first research-eligible date can still use all 120 preceding/current
        # source observations; age eligibility is not a rolling-history break.
        history_factor = baseline.evaluate(spec("total_return", {"window": 120}))
        self.assertTrue(np.isfinite(history_factor[119, -1]))

    def test_research_eligibility_contract_is_strict(self) -> None:
        panel = synthetic_panel(40, 5)
        with self.assertRaisesRegex(R0ClassicFactorError, "shape mismatch"):
            R0ClassicFactorEngine(panel, research_eligible=np.ones((39, 5), dtype=bool))
        invalid = panel.eligible.copy()
        invalid[0, 0] = True
        panel.eligible[0, 0] = False
        with self.assertRaisesRegex(R0ClassicFactorError, "subset"):
            R0ClassicFactorEngine(panel, research_eligible=invalid)

    def test_future_mutation_cannot_change_past_signal(self) -> None:
        panel = synthetic_panel(80, 5)
        before = R0ClassicFactorEngine(panel, reject_constant=False).evaluate(
            spec("return_volatility", {"window": 20})
        )
        changed_fields = {name: values.copy() for name, values in panel.fields.items()}
        changed_fields["realized_total_return"][61:] *= -3.0
        changed = PanelData(
            dates=panel.dates,
            symbols=panel.symbols,
            fields=changed_fields,
            eligible=panel.eligible.copy(),
        )
        after = R0ClassicFactorEngine(changed, reject_constant=False).evaluate(
            spec("return_volatility", {"window": 20})
        )
        np.testing.assert_allclose(before[:61], after[:61], equal_nan=True)

    def test_constant_output_is_rejected(self) -> None:
        panel = synthetic_panel(30, 5)
        with self.assertRaisesRegex(R0ClassicFactorError, "constant"):
            R0ClassicFactorEngine(panel).evaluate(
                spec("supplier_dsl", {"vwap": "amount_div_volume"}, formula="1")
            )

    def test_parameter_and_formula_safety(self) -> None:
        panel = synthetic_panel(40, 5)
        engine = R0ClassicFactorEngine(panel, reject_constant=False)
        with self.assertRaises(R0ClassicFactorError):
            engine.evaluate(spec("total_return", {"window": 0}))
        with self.assertRaises(ValueError):
            engine.evaluate(
                spec(
                    "supplier_dsl",
                    {"vwap": "amount_div_volume"},
                    formula="__import__('os').system('whoami')",
                )
            )

    def test_engine_matches_authoritative_transparent_formulas(self) -> None:
        panel = synthetic_panel(80, 6)
        engine = R0ClassicFactorEngine(
            panel,
            build_total_return_signal_fields(panel),
            reject_constant=False,
            max_cache_bytes=32 * 1024 * 1024,
        )
        row, column, window = 50, 2, 10
        start = row - window + 1
        returns = engine.fields["realized_total_return"][start : row + 1, column].astype(
            np.float64
        )

        tail = engine.evaluate(spec("tail_loss", {"window": window, "quantile": 0.2}))
        threshold = np.quantile(returns, 0.2)
        expected_shortfall = -float(np.mean(returns[returns <= threshold]))
        self.assertAlmostEqual(float(tail[row, column]), expected_shortfall, places=7)
        maximum_drawdown = engine.evaluate(spec("maximum_drawdown", {"window": window}))
        wealth = np.r_[1.0, np.cumprod(1.0 + returns)]
        expected_drawdown = np.min(wealth / np.maximum.accumulate(wealth) - 1.0)
        self.assertAlmostEqual(
            float(maximum_drawdown[row, column]), float(expected_drawdown), places=7
        )

        breakout = engine.evaluate(spec("breakout_position", {"window": window}))
        low = engine.fields["low"][start : row + 1, column]
        high = engine.fields["high"][start : row + 1, column]
        close = engine.fields["close"][row, column]
        self.assertAlmostEqual(
            float(breakout[row, column]),
            float((close - np.min(low)) / (np.max(high) - np.min(low))),
            places=6,
        )

        location = engine.evaluate(
            spec("price_location", {"window": window, "field": "close"})
        )
        close_window = engine.fields["close"][start : row + 1, column]
        self.assertAlmostEqual(
            float(location[row, column]),
            float((close - np.min(close_window)) / (np.max(close_window) - np.min(close_window))),
            places=6,
        )

        amount = engine.fields["amount"][start : row + 1, column].astype(np.float64)
        amount_volatility = engine.evaluate(spec("amount_volatility", {"window": window}))
        self.assertAlmostEqual(
            float(amount_volatility[row, column]),
            float(np.std(np.log1p(amount), ddof=1)),
            places=6,
        )
        correlation = engine.evaluate(spec("return_amount_corr", {"window": window}))
        self.assertAlmostEqual(
            float(correlation[row, column]),
            float(np.corrcoef(returns, np.log1p(amount))[0, 1]),
            places=5,
        )
        amount_mean = engine.evaluate(spec("amount_mean", {"window": window}))
        volume_mean = engine.evaluate(spec("volume_mean", {"window": window}))
        volume = engine.fields["volume"][start : row + 1, column].astype(np.float64)
        self.assertAlmostEqual(
            float(amount_mean[row, column]), float(np.log1p(np.mean(amount))), places=6
        )
        self.assertAlmostEqual(
            float(volume_mean[row, column]), float(np.log1p(np.mean(volume))), places=6
        )

        gains = np.maximum(returns, 0.0)
        losses = np.maximum(-returns, 0.0)
        expected_rsi = 100.0 * np.mean(gains) / (np.mean(gains) + np.mean(losses))
        rsi = engine.evaluate(spec("rsi", {"window": window}))
        self.assertAlmostEqual(float(rsi[row, column]), float(expected_rsi), places=5)

        stochastic = engine.evaluate(
            spec("stochastic", {"window": window, "smooth": 3, "component": "k"})
        )
        expected_stochastic = 100.0 * (close - np.min(low)) / (np.max(high) - np.min(low))
        self.assertAlmostEqual(
            float(stochastic[row, column]), float(expected_stochastic), places=5
        )
        williams = engine.evaluate(spec("williams_r", {"window": window}))
        expected_williams = -100.0 * (np.max(high) - close) / (np.max(high) - np.min(low))
        self.assertAlmostEqual(float(williams[row, column]), float(expected_williams), places=5)

        macd_spec = spec(
            "macd", {"fast": 5, "slow": 20, "signal": 5, "component": "line"}
        )
        macd = engine.evaluate(macd_spec)
        expected_macd = engine._ema(engine.fields["close"], 5, key="close")[row, column] - engine._ema(
            engine.fields["close"], 20, key="close"
        )[row, column]
        self.assertAlmostEqual(float(macd[row, column]), float(expected_macd), places=6)

        mean = float(np.mean(returns))
        centered = returns - mean
        m2 = float(np.mean(centered**2))
        biased_skew = float(np.mean(centered**3)) / m2**1.5
        expected_skew = np.sqrt(window * (window - 1.0)) / (window - 2.0) * biased_skew
        skew = engine.evaluate(spec("realized_skewness", {"window": window}))
        self.assertAlmostEqual(float(skew[row, column]), float(expected_skew), places=5)
        biased_excess = float(np.mean(centered**4)) / m2**2 - 3.0
        expected_kurtosis = (window - 1.0) / ((window - 2.0) * (window - 3.0)) * (
            (window + 1.0) * biased_excess + 6.0
        )
        kurtosis = engine.evaluate(spec("realized_kurtosis", {"window": window}))
        self.assertAlmostEqual(float(kurtosis[row, column]), float(expected_kurtosis), places=4)

        previous_close = engine.fields["close"][start - 1 : row, column]
        high_window = engine.fields["high"][start : row + 1, column]
        low_window = engine.fields["low"][start : row + 1, column]
        true_range = np.maximum.reduce(
            (
                high_window - low_window,
                np.abs(high_window - previous_close),
                np.abs(low_window - previous_close),
            )
        )
        atr = engine.evaluate(spec("atr", {"window": window}))
        self.assertAlmostEqual(
            float(atr[row, column]), float(np.mean(true_range) / close), places=6
        )

    def test_catalog_kind_contract_and_representative_execution(self) -> None:
        root = Path(__file__).resolve().parents[1]
        catalog = json.loads(
            (root / "configs" / "r0_shsz_pv_classic_factor_catalog_v1.json").read_text(
                encoding="utf-8"
            )
        )
        records = catalog["records"]
        self.assertEqual(len(records), int(catalog["record_count"]))
        self.assertGreaterEqual(len(records), 300)
        self.assertEqual({record["kind"] for record in records}, set(SUPPORTED_KINDS))
        panel = synthetic_panel()
        projection = build_total_return_signal_fields(panel)
        engine = R0ClassicFactorEngine(
            panel,
            projection,
            reject_constant=False,
            max_cache_bytes=64 * 1024 * 1024,
        )
        seen: set[str] = set()
        for record in records:
            if record["kind"] in seen:
                continue
            values = engine.evaluate(record)
            self.assertEqual(values.shape, panel.shape)
            self.assertEqual(values.dtype, np.float32)
            seen.add(record["kind"])
        self.assertEqual(seen, set(SUPPORTED_KINDS))

    def test_full_catalog_cpu_gpu_equivalence(self) -> None:
        try:
            accelerator_signature("gpu")
        except AcceleratorError as error:
            self.skipTest(str(error))
        root = Path(__file__).resolve().parents[1]
        records = json.loads(
            (root / "configs" / "r0_shsz_pv_classic_factor_catalog_v1.json").read_text(
                encoding="utf-8"
            )
        )["records"]
        panel = synthetic_panel(520, 12)
        projection = build_total_return_signal_fields(panel)
        cpu = R0ClassicFactorEngine(
            panel,
            projection,
            accelerator="cpu",
            reject_constant=False,
            max_cache_bytes=128 * 1024 * 1024,
        )
        gpu = R0ClassicFactorEngine(
            panel,
            projection,
            accelerator="gpu",
            gpu_column_chunk=8,
            reject_constant=False,
            max_cache_bytes=128 * 1024 * 1024,
        )
        for record in records:
            cpu_values = cpu.evaluate(record)
            gpu_values = gpu.evaluate(record)
            np.testing.assert_array_equal(
                np.isnan(cpu_values),
                np.isnan(gpu_values),
                err_msg=record["factor_id"],
            )
            np.testing.assert_allclose(
                cpu_values,
                gpu_values,
                rtol=2e-5,
                atol=2e-6,
                equal_nan=True,
                err_msg=record["factor_id"],
            )


if __name__ == "__main__":
    unittest.main()
