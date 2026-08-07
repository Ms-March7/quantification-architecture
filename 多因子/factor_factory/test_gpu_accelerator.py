"""CPU/GPU equivalence gates for the optional CuPy backend."""

from __future__ import annotations

import unittest
from dataclasses import asdict

import numpy as np

from factor_factory.accelerator import accelerator_signature, rolling_corr_gpu
from factor_factory.evaluation import evaluate_factor
from factor_factory.expressions import Expression, _rolling_corr, evaluate_expression
from factor_factory.pipeline import FactorFactoryConfig
from factor_factory.selection import SelectionGates, gate_failures


class GPUAcceleratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        signature = accelerator_signature("gpu")
        if "RTX 3060" not in signature["device_name"]:
            raise AssertionError(f"unexpected test device: {signature}")

    def test_config_requires_explicit_backend_and_positive_chunk(self) -> None:
        with self.assertRaisesRegex(ValueError, "accelerator"):
            FactorFactoryConfig(accelerator="auto")
        with self.assertRaisesRegex(ValueError, "gpu_column_chunk"):
            FactorFactoryConfig(gpu_column_chunk=0)
        with self.assertRaisesRegex(ValueError, "evaluation_top_k"):
            FactorFactoryConfig(evaluation_top_k=-1)
        with self.assertRaisesRegex(ValueError, "minimum_risk_on_rebalance_ratio"):
            FactorFactoryConfig(minimum_risk_on_rebalance_ratio_per_phase=1.1)

    def test_all_gpu_rolling_operators_match_cpu_with_missing_values_and_ties(self) -> None:
        rng = np.random.default_rng(20260716)
        left = rng.normal(size=(96, 24)).astype(np.float32)
        right = rng.normal(size=(96, 24)).astype(np.float32)
        left[rng.random(left.shape) < 0.10] = np.nan
        right[rng.random(right.shape) < 0.10] = np.nan
        left[40:45, :3] = 1.0
        fields = {"close": left, "volume": right}
        expressions = [
            Expression(op, (Expression.terminal("close"),), window=20)
            for op in ("ts_mean", "ts_std", "ts_min", "ts_max", "ts_rank")
        ]
        expressions.append(
            Expression(
                "ts_corr",
                (Expression.terminal("close"), Expression.terminal("volume")),
                window=20,
            )
        )
        for expression in expressions:
            cpu = evaluate_expression(expression, fields, accelerator="cpu")
            gpu = evaluate_expression(
                expression, fields, accelerator="gpu", gpu_column_chunk=8
            )
            np.testing.assert_allclose(
                cpu, gpu, rtol=2e-6, atol=2e-7, equal_nan=True,
                err_msg=expression.canonical(),
            )

    def test_gpu_correlation_is_stable_for_large_nearly_constant_levels(self) -> None:
        rng = np.random.default_rng(20260806)
        dates, assets = 90, 11
        common = rng.normal(0.0, 0.025, size=(dates, assets))
        left = 1.0e8 + common + rng.normal(0.0, 0.004, size=(dates, assets))
        right = -7.0e7 + 1.7 * common + rng.normal(0.0, 0.003, size=(dates, assets))
        left[31, 2] = np.nan
        right[48, 7] = np.nan
        left[:, -1] = 2.0e8
        right[:, -1] = -3.0e8
        cpu = _rolling_corr(left, right, 20)
        gpu = rolling_corr_gpu(left, right, 20, column_chunk=4)
        np.testing.assert_array_equal(np.isnan(cpu), np.isnan(gpu))
        np.testing.assert_allclose(cpu, gpu, rtol=2e-10, atol=2e-10, equal_nan=True)

    def test_gpu_permutation_uses_identical_null_calendar_and_tie_policy(self) -> None:
        rng = np.random.default_rng(11)
        values = rng.normal(size=(90, 80))
        targets = 0.03 * values + rng.normal(size=values.shape)
        values[rng.random(values.shape) < 0.04] = np.nan
        targets[rng.random(targets.shape) < 0.04] = np.nan
        arguments = dict(
            minimum_assets=30,
            permutations=19_999,
            fold_count=3,
            seed=20260716,
        )
        cpu = evaluate_factor(
            values, targets, np.arange(90), accelerator="cpu", **arguments
        )
        gpu = evaluate_factor(
            values, targets, np.arange(90), accelerator="gpu", **arguments
        )
        for name, cpu_value in asdict(cpu).items():
            gpu_value = getattr(gpu, name)
            if isinstance(cpu_value, float):
                self.assertAlmostEqual(cpu_value, gpu_value, places=12, msg=name)
            else:
                self.assertEqual(cpu_value, gpu_value, name)
        self.assertEqual(cpu.p_value, gpu.p_value)

    def test_gpu_tied_tail_weights_and_skipped_dates_match_cpu_gates(self) -> None:
        rng = np.random.default_rng(73)
        levels = np.repeat(np.arange(10, dtype=np.float64), 8)
        values = np.tile(levels, (75, 1))
        targets = rng.normal(size=values.shape)
        for date in range(len(values)):
            rng.shuffle(values[date])
        values[20] = 1.0
        values[35, :12] = np.nan
        targets[35, 12:20] = np.nan
        arguments = dict(
            minimum_assets=30,
            quantile_fraction=0.2,
            tail_count=7,
            permutations=999,
            fold_count=3,
            seed=20260716,
        )
        cpu = evaluate_factor(
            values, targets, np.arange(75), accelerator="cpu", **arguments
        )
        gpu = evaluate_factor(
            values, targets, np.arange(75), accelerator="gpu", **arguments
        )
        for name, cpu_value in asdict(cpu).items():
            gpu_value = getattr(gpu, name)
            if isinstance(cpu_value, float):
                self.assertAlmostEqual(cpu_value, gpu_value, places=11, msg=name)
            else:
                self.assertEqual(cpu_value, gpu_value, name)
        gates = SelectionGates(
            minimum_evaluation_dates=50,
            minimum_coverage=0.5,
            minimum_evaluable_date_ratio=0.5,
            minimum_fold_evaluable_ratio=0.5,
            minimum_mean_ic=-1.0,
            minimum_icir=-1.0,
            minimum_positive_fold_ratio=0.0,
            minimum_worst_fold_ic=-1.0,
            maximum_q_value=1.0,
        )
        self.assertEqual(gate_failures(cpu, gates), gate_failures(gpu, gates))

    def test_gpu_tail_membership_does_not_replace_missing_outcomes(self) -> None:
        values = np.tile(np.arange(6, dtype=np.float64), (12, 1))
        values[1::2, 0] = 1.0
        values[1::2, 1] = 0.0
        targets = 0.01 * values
        targets[:, -1] = np.nan
        eligible = np.ones_like(values, dtype=bool)
        eligible[:, 4] = False
        arguments = dict(
            eligible=eligible,
            minimum_assets=5,
            tail_count=1,
            permutations=0,
            fold_count=3,
        )
        cpu = evaluate_factor(
            values, targets, np.arange(12), accelerator="cpu", **arguments
        )
        gpu = evaluate_factor(
            values, targets, np.arange(12), accelerator="gpu", **arguments
        )
        for name, cpu_value in asdict(cpu).items():
            gpu_value = getattr(gpu, name)
            if isinstance(cpu_value, float):
                self.assertAlmostEqual(cpu_value, gpu_value, places=12, msg=name)
            else:
                self.assertEqual(cpu_value, gpu_value, name)
        self.assertGreater(cpu.top_tail_missing_outcome_weight_mean, 0.0)
        self.assertNotEqual(cpu.top_tail_turnover, cpu.bottom_tail_turnover)


if __name__ == "__main__":
    unittest.main()
