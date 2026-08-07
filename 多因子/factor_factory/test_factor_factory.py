#!/usr/bin/env python3
"""Unit and integration tests for the factor research factory."""

from __future__ import annotations

import json
import hashlib
import socket
import sqlite3
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

from factor_factory.backtest import BacktestResult, CostModel, run_a_share_backtest
from factor_factory.attempts import ResearchAttemptGovernance
from factor_factory.evaluation import FactorMetrics, benjamini_hochberg, evaluate_factor, forward_returns
from factor_factory.ensemble import build_ensemble, equal_weight_definition
from factor_factory.expressions import (
    Expression,
    evaluate_expression,
    expression_price_scale_exponent,
    expression_unit,
)
from factor_factory.generation import FactorGenerator
from factor_factory.generation import make_candidate
from factor_factory.governance import HoldoutGovernance
from factor_factory.pipeline import FactorFactory, FactorFactoryConfig
from factor_factory.qlib_store import (
    QlibBinaryStore,
    causal_adjustment_quarantine,
    detect_adjustment_anomalies,
)
from factor_factory.registry import ResearchRegistry, ResearchRunLock, file_sha256
from factor_factory.selection import SelectionGates, gate_failures, select_diversified
from factor_factory.selection import factor_similarity
from factor_factory.schema import PanelData
from factor_factory.synthetic import planted_signal_panel
from factor_factory.validation import three_way_time_split


class ExpressionTests(unittest.TestCase):
    def test_future_changes_cannot_rewrite_past_values(self) -> None:
        panel = planted_signal_panel(dates=120, assets=10)
        close = Expression.terminal("close")
        expression = Expression("ts_mean", (Expression("delta", (close,), window=1),), window=5)
        baseline = evaluate_expression(expression, panel.fields)
        changed = {name: values.copy() for name, values in panel.fields.items()}
        changed["close"][90:] *= 3.0
        perturbed = evaluate_expression(expression, changed)
        np.testing.assert_allclose(baseline[:90], perturbed[:90], equal_nan=True)

    def test_generator_is_deterministic_and_deduplicated(self) -> None:
        left = FactorGenerator(("open", "close", "volume"), seed=7).initial_population(40)
        right = FactorGenerator(("open", "close", "volume"), seed=7).initial_population(40)
        self.assertEqual([item.formula for item in left], [item.formula for item in right])
        self.assertEqual(len({item.candidate_id for item in left}), len(left))
        self.assertTrue(all(expression_unit(item.expression) == (0, 0) for item in left))
        self.assertTrue(all(expression_price_scale_exponent(item.expression) == 0 for item in left))
        self.assertTrue(all(item.expression.depth <= 3 for item in left))

    def test_unit_system_rejects_price_plus_volume(self) -> None:
        invalid = Expression("add", (Expression.terminal("close"), Expression.terminal("volume")))
        valid = Expression("safe_div", (Expression.terminal("amount"), Expression.terminal("volume")))
        self.assertIsNone(expression_unit(invalid))
        self.assertEqual(expression_unit(valid), (1, 0))
        self.assertIsNone(expression_unit(Expression.terminal("mystery_field")))

    def test_cross_sectional_price_level_is_rejected_but_return_is_allowed(self) -> None:
        close = Expression.terminal("close")
        raw_rank = Expression("cs_rank", (close,))
        returns = Expression("safe_div", (close, Expression("delay", (close,), window=5)))
        return_rank = Expression("cs_rank", (returns,))
        self.assertIsNone(expression_unit(raw_rank))
        self.assertIsNone(expression_price_scale_exponent(raw_rank))
        self.assertEqual(expression_unit(return_rank), (0, 0))
        self.assertEqual(expression_price_scale_exponent(return_rank), 0)

    def test_generated_candidates_are_invariant_to_per_asset_price_rescaling(self) -> None:
        rng = np.random.default_rng(41)
        base_close = np.exp(np.cumsum(rng.normal(0.0, 0.01, size=(90, 12)), axis=0)).astype(np.float32)
        fields = {
            "open": base_close * 0.999,
            "high": base_close * 1.01,
            "low": base_close * 0.99,
            "close": base_close,
            "volume": rng.lognormal(12.0, 0.5, size=(90, 12)).astype(np.float32),
            "amount": rng.lognormal(15.0, 0.5, size=(90, 12)).astype(np.float32),
        }
        scale = np.linspace(0.2, 5.0, 12, dtype=np.float32)[None, :]
        scaled = {name: values.copy() for name, values in fields.items()}
        for name in ("open", "high", "low", "close"):
            scaled[name] *= scale
        for candidate in FactorGenerator(tuple(fields), seed=41, max_depth=3).initial_population(24):
            baseline = evaluate_expression(candidate.expression, fields)
            changed = evaluate_expression(candidate.expression, scaled)
            np.testing.assert_allclose(
                baseline,
                changed,
                rtol=3e-3,
                atol=1e-4,
                equal_nan=True,
                err_msg=candidate.formula,
            )

    def test_cross_sectional_rank_does_not_invent_order_for_ties(self) -> None:
        fields = {"close": np.ones((12, 8), dtype=np.float32)}
        ranked = evaluate_expression(Expression("cs_rank", (Expression.terminal("close"),)), fields)
        self.assertTrue(np.isnan(ranked).all())

    def test_cross_sectional_rank_ignores_non_pit_members(self) -> None:
        fields = {"close": np.asarray([[1.0, 2.0, 1_000_000.0]], dtype=np.float32)}
        eligible = np.asarray([[True, True, False]])
        ranked = evaluate_expression(
            Expression("cs_rank", (Expression.terminal("close"),)), fields, eligible=eligible
        )
        np.testing.assert_allclose(ranked[0, :2], [-0.5, 0.5])
        self.assertTrue(np.isnan(ranked[0, 2]))

    def test_constant_factor_cannot_receive_false_ic(self) -> None:
        values = np.ones((20, 8), dtype=np.float32)
        targets = np.tile(np.arange(8, dtype=np.float32), (20, 1))
        metrics = evaluate_factor(values, targets, np.arange(20), minimum_assets=8)
        self.assertEqual(metrics.n_dates, 0)
        self.assertEqual(metrics.mean_ic, 0.0)

    def test_tail_membership_never_replaces_a_missing_future_outcome(self) -> None:
        values = np.tile(np.arange(4, dtype=np.float64), (5, 1))
        targets = np.tile(np.asarray([0.0, 0.1, 0.2, np.nan]), (5, 1))
        eligible = np.ones_like(values, dtype=bool)
        eligible[:, 2] = False
        metrics = evaluate_factor(
            values,
            targets,
            np.arange(5),
            eligible=eligible,
            minimum_assets=3,
            tail_count=1,
            permutations=0,
        )
        self.assertEqual(metrics.tail_selection_dates, 5)
        self.assertEqual(metrics.top_return_dates, 0)
        self.assertEqual(metrics.bottom_return_dates, 5)
        self.assertEqual(metrics.decision_observations, 15)
        self.assertEqual(metrics.realized_outcome_observations, 10)
        self.assertEqual(metrics.missing_outcome_observations, 5)
        self.assertAlmostEqual(metrics.outcome_coverage, 2.0 / 3.0)
        self.assertAlmostEqual(metrics.top_tail_missing_outcome_weight_mean, 1.0)
        self.assertEqual(
            metrics.outcome_return_policy,
            "FIXED_FACTOR_VISIBLE_MEMBERSHIP__OBSERVED_OUTCOMES_RENORMALIZED__NO_REPLACEMENT",
        )

    def test_top_and_bottom_tail_turnover_are_reported_separately(self) -> None:
        values = np.asarray(
            [
                [4.0, 3.0, 2.0, 1.0],
                [4.0, 1.0, 2.0, 3.0],
            ]
        )
        targets = np.asarray(
            [
                [0.04, 0.03, 0.02, 0.01],
                [0.04, 0.01, 0.02, 0.03],
            ]
        )
        metrics = evaluate_factor(
            values,
            targets,
            np.arange(2),
            minimum_assets=4,
            tail_count=1,
            round_trip_cost_bps=10.0,
        )
        self.assertAlmostEqual(metrics.turnover, 0.5)
        self.assertAlmostEqual(metrics.top_tail_turnover, 0.5)
        self.assertAlmostEqual(metrics.bottom_tail_turnover, 1.0)
        self.assertAlmostEqual(
            metrics.cost_adjusted_top_excess,
            metrics.top_excess_mean - 0.001 * metrics.top_tail_turnover,
        )
        self.assertAlmostEqual(
            metrics.cost_adjusted_bottom_excess,
            metrics.bottom_excess_mean - 0.001 * metrics.bottom_tail_turnover,
        )

    def test_expression_does_not_mutate_read_only_field(self) -> None:
        close = np.asarray([[1.0, np.inf], [2.0, 3.0]], dtype=np.float64)
        close.setflags(write=False)
        before = close.copy()
        result = evaluate_expression(Expression.terminal("close"), {"close": close})
        np.testing.assert_array_equal(close, before)
        self.assertTrue(np.isnan(result[0, 1]))

    def test_rolling_std_is_stable_for_large_offset(self) -> None:
        close = (1e12 + np.arange(20, dtype=np.float64))[:, None]
        expression = Expression("ts_std", (Expression.terminal("close"),), window=20)
        result = evaluate_expression(expression, {"close": close})
        self.assertAlmostEqual(float(result[-1, 0]), float(np.std(close[:, 0], ddof=1)), places=6)

    def test_ts_corr_requires_eighty_percent_pairs(self) -> None:
        left = np.full((60, 1), np.nan, dtype=np.float64)
        right = np.full((60, 1), np.nan, dtype=np.float64)
        left[-3:, 0] = (1.0, 2.0, 3.0)
        right[-3:, 0] = (1.0, 2.0, 4.0)
        expression = Expression(
            "ts_corr", (Expression.terminal("close"), Expression.terminal("volume")), window=60
        )
        result = evaluate_expression(expression, {"close": left, "volume": right})
        self.assertTrue(np.isnan(result[-1, 0]))

    def test_invalid_expression_shape_fails_fast(self) -> None:
        invalid = Expression("add", (Expression.terminal("close"),))
        with self.assertRaises(ValueError):
            evaluate_expression(invalid, {"close": np.ones((3, 2))})

    def test_generator_honors_small_max_depth(self) -> None:
        for depth in (1, 2):
            generated = FactorGenerator(("open", "close", "volume"), seed=depth, max_depth=depth).initial_population(8)
            self.assertTrue(all(candidate.expression.depth <= depth for candidate in generated))


class EnsembleTests(unittest.TestCase):
    def test_equal_weight_ensemble_is_pit_and_masked(self) -> None:
        first = np.asarray([[1.0, 2.0, 100.0], [3.0, 2.0, 1.0]], dtype=np.float32)
        second = np.asarray([[2.0, 1.0, -100.0], [1.0, 2.0, 3.0]], dtype=np.float32)
        eligible = np.asarray([[True, True, False], [True, True, True]])
        definition = equal_weight_definition(("a", "b"))
        result = build_ensemble((first, second), definition.weights, eligible)
        self.assertTrue(np.isnan(result[0, 2]))
        self.assertAlmostEqual(float(result[0, 0] + result[0, 1]), 0.0, places=6)
        changed = second.copy()
        changed[1] *= 1000.0
        perturbed = build_ensemble((first, changed), definition.weights, eligible)
        np.testing.assert_allclose(result[0], perturbed[0], equal_nan=True)

    def test_ensemble_requires_all_components_for_each_stock(self) -> None:
        first = np.asarray([[1.0, 2.0, 3.0]], dtype=np.float32)
        second = np.asarray([[1.0, np.nan, 3.0]], dtype=np.float32)
        result = build_ensemble((first, second), (0.5, 0.5), np.ones((1, 3), dtype=bool))
        self.assertTrue(np.isnan(result[0, 1]))

    def test_ensemble_standardizes_on_shared_common_mask(self) -> None:
        first = np.asarray([[-1.0, 0.0, 1.0, 1000.0]], dtype=np.float64)
        second = np.asarray([[1.0, 0.0, -1.0, np.nan]], dtype=np.float64)
        eligible = np.ones((1, 4), dtype=bool)
        result = build_ensemble((first, second), (0.5, 0.5), eligible)
        np.testing.assert_allclose(result[0, :3], 0.0, atol=1e-7)
        self.assertTrue(np.isnan(result[0, 3]))

    def test_ensemble_rejects_zero_weight_component(self) -> None:
        values = np.ones((2, 3), dtype=np.float64)
        with self.assertRaises(ValueError):
            build_ensemble((values, values), (1.0, 0.0), np.ones((2, 3), dtype=bool))

    def test_similarity_ignores_non_pit_members(self) -> None:
        left = np.tile(np.asarray([[1.0, 2.0, 3.0, 10.0, 20.0]], dtype=np.float32), (10, 1))
        right = np.tile(np.asarray([[1.0, 2.0, 3.0, 20.0, 10.0]], dtype=np.float32), (10, 1))
        eligible = np.tile(np.asarray([[True, True, True, False, False]]), (10, 1))
        similarity = factor_similarity(left, right, np.arange(10), eligible)
        self.assertAlmostEqual(similarity, 1.0, places=6)

    def test_rank_equivalent_nonlinear_clone_is_redundant(self) -> None:
        base = np.tile(np.linspace(-1.0, 1.0, 12), (20, 1))
        clone = np.exp(12.0 * base)
        eligible = np.ones_like(base, dtype=bool)
        self.assertAlmostEqual(
            factor_similarity(base, clone, np.arange(20), eligible), 1.0, places=6
        )

    def test_subset_search_recovers_joint_coverage_pair(self) -> None:
        rng = np.random.default_rng(53)
        candidates = [
            make_candidate(Expression("ts_rank", (Expression.terminal("close"),), window=window))
            for window in (3, 5, 10)
        ]
        matrices = {
            candidates[0].candidate_id: rng.normal(size=(20, 10)),
            candidates[1].candidate_id: rng.normal(size=(20, 10)),
            candidates[2].candidate_id: rng.normal(size=(20, 10)),
        }
        matrices[candidates[0].candidate_id][:, 8:] = np.nan
        matrices[candidates[1].candidate_id][:, 8:] = np.nan
        matrices[candidates[2].candidate_id][:, :2] = np.nan
        base = FactorMetrics(
            20, 160, 0.8, 0.1, 0.1, 1.0, 0.8, 1.0, 0.1,
            0.01, 0.02, 0.2, 0.009, 0.008, 0.01,
            evaluable_date_ratio=1.0, minimum_fold_evaluable_ratio=1.0,
        )
        metrics = {candidate.candidate_id: base for candidate in candidates}
        selected, _ = select_diversified(
            candidates,
            metrics,
            matrices,
            np.arange(20),
            gates=SelectionGates(
                minimum_evaluation_dates=10,
                minimum_coverage=0.0,
                minimum_evaluable_date_ratio=0.0,
                minimum_fold_evaluable_ratio=0.0,
                minimum_mean_ic=-1.0,
                minimum_icir=-1.0,
                minimum_positive_fold_ratio=0.0,
                minimum_worst_fold_ic=-1.0,
                maximum_q_value=1.0,
            ),
            maximum_selected=2,
            minimum_selected=2,
            minimum_joint_coverage=0.7,
            rank_scores={
                candidates[0].candidate_id: 2.0,
                candidates[1].candidate_id: 1.0,
                candidates[2].candidate_id: 3.0,
            },
            eligible_mask=np.ones((20, 10), dtype=bool),
        )
        self.assertEqual(
            {candidate.candidate_id for candidate in selected},
            {candidates[0].candidate_id, candidates[1].candidate_id},
        )


class ValidationTests(unittest.TestCase):
    def test_constant_late_folds_fail_evaluable_coverage(self) -> None:
        assets = 10
        dates = 180
        targets = np.tile(np.arange(assets, dtype=np.float64), (dates, 1))
        values = targets.copy()
        values[60:] = 1.0
        metrics = evaluate_factor(
            values, targets, np.arange(dates), minimum_assets=assets,
            fold_count=3, permutations=999, permutation_block_length=5,
        )
        self.assertLess(metrics.evaluable_date_ratio, 0.5)
        self.assertEqual(metrics.minimum_fold_evaluable_ratio, 0.0)
        self.assertIn("evaluable_dates", gate_failures(metrics, SelectionGates()))

    def test_open_execution_mask_is_not_overwritten_by_same_day_close_research_mask(self) -> None:
        dates = tuple(f"2026-01-{day:02d}" for day in range(1, 4))
        fields = {
            "open": np.ones((3, 2), dtype=np.float32),
            "close": np.ones((3, 2), dtype=np.float32),
            "volume": np.ones((3, 2), dtype=np.float32),
        }
        eligible = np.ones((3, 2), dtype=bool)
        eligible[1, 0] = False
        buyable = np.ones((3, 2), dtype=bool)
        panel = PanelData(dates, ("A", "B"), fields, eligible=eligible, buyable=buyable)
        self.assertTrue(panel.buyable[1, 0])

    def test_adjustment_jump_contradicting_raw_change_is_quarantined(self) -> None:
        close = np.asarray([[1.0, 1.0], [1.18, 1.1], [1.19, 1.2]], dtype=np.float32)
        change = np.asarray([[np.nan, np.nan], [0.01, 0.10], [0.01, 0.09]], dtype=np.float32)
        anomaly = detect_adjustment_anomalies(close, change)
        self.assertTrue(anomaly[1, 0])
        self.assertFalse(anomaly[1, 1])

    def test_future_adjustment_anomaly_cannot_rewrite_earlier_universe(self) -> None:
        baseline = np.zeros((12, 2), dtype=bool)
        changed = baseline.copy()
        changed[9, 0] = True
        baseline_quarantine, _ = causal_adjustment_quarantine(baseline)
        changed_quarantine, invalidated = causal_adjustment_quarantine(changed)
        np.testing.assert_array_equal(baseline_quarantine[:9], changed_quarantine[:9])
        self.assertFalse(invalidated[9, 0])
        self.assertTrue(invalidated[10, 0])
        self.assertTrue(changed_quarantine[11, 0])

    def test_purge_separates_all_segments(self) -> None:
        split = three_way_time_split(240, purge=5)
        self.assertGreater(split.validation[0] - split.discovery[-1], 5)
        self.assertGreater(split.holdout[0] - split.validation[-1], 5)

    def test_forward_return_matches_next_open_holding_window(self) -> None:
        panel = planted_signal_panel(dates=120, assets=10)
        target = forward_returns(panel, 5)
        expected = panel.fields["open"][16] / panel.fields["open"][11] - 1.0
        np.testing.assert_allclose(target[10], expected, atol=2e-7)

    def test_terminal_listing_event_gets_conservative_loss(self) -> None:
        panel = planted_signal_panel(dates=120, assets=10)
        panel.delisted[14, 0] = True
        target = forward_returns(panel, 5)
        self.assertEqual(float(target[10, 0]), -1.0)

    def test_entry_day_invalidation_gets_conservative_loss(self) -> None:
        panel = planted_signal_panel(dates=120, assets=10)
        panel.invalidated[11, 0] = True
        target = forward_returns(panel, 5)
        self.assertEqual(float(target[10, 0]), -1.0)

    def test_suspended_exit_keeps_position_in_label_sample(self) -> None:
        panel = planted_signal_panel(dates=120, assets=10)
        expected = panel.fields["close"][15, 0] / panel.fields["open"][11, 0] - 1.0
        panel.fields["open"][16, 0] = np.nan
        panel.fields["close"][16, 0] = np.nan
        target = forward_returns(panel, 5)
        self.assertTrue(np.isfinite(target[10, 0]))
        self.assertAlmostEqual(float(target[10, 0]), float(expected), places=6)

    def test_bh_adjustment_is_monotone_in_rank(self) -> None:
        base = FactorMetrics(10, 100, 1.0, 0.1, 0.1, 1.0, 0.8, 1.0, 0.1, 0.01, 0.02, 0.2, 0.009, 0.008, 1.0)
        values = [replace(base, p_value=value) for value in (0.001, 0.02, 0.2)]
        adjusted = benjamini_hochberg(values)
        self.assertLessEqual(adjusted[0].q_value, adjusted[1].q_value)
        self.assertLessEqual(adjusted[1].q_value, adjusted[2].q_value)


class BacktestTests(unittest.TestCase):
    def test_backtest_rejects_zero_top_k(self) -> None:
        panel = planted_signal_panel(dates=120, assets=10)
        with self.assertRaises(ValueError):
            run_a_share_backtest(
                panel, np.ones(panel.shape), start_index=70, end_index=90, top_k=0
            )

    def test_minimum_commission_never_creates_negative_cash(self) -> None:
        dates = tuple(f"2026-01-{day:02d}" for day in range(1, 21))
        fields = {
            name: np.ones((20, 2), dtype=np.float64)
            for name in ("open", "close", "volume")
        }
        panel = PanelData(
            dates, ("A", "B"), fields,
            metadata={"price_scale": "Qlib normalized adjusted prices"},
        )
        result = run_a_share_backtest(
            panel,
            np.tile(np.asarray([[1.0, 2.0]]), (20, 1)),
            start_index=0,
            end_index=15,
            rebalance_every=5,
            top_k=2,
            initial_cash=10_000.0,
            cost=CostModel(lot_size=1, slippage_bps=0.0, minimum_trade_notional=1_000.0),
        )
        self.assertGreaterEqual(result.ending_cash, 0.0)
        self.assertTrue(all(record["cash"] >= 0.0 for record in result.daily_records))
        self.assertEqual(result.trade_count, 2)
        self.assertAlmostEqual(result.ending_equity, 9_990.0, places=6)

    def test_equal_weight_rebalance_trims_winner_drift(self) -> None:
        dates = tuple(f"2026-02-{day:02d}" for day in range(1, 9))
        open_price = np.full((8, 2), 10.0, dtype=np.float64)
        open_price[6, 0] = 20.0
        close = open_price.copy()
        close[7, 0] = 10.0
        panel = PanelData(
            dates,
            ("A", "B"),
            {"open": open_price, "close": close, "volume": np.ones((8, 2))},
            metadata={"price_scale": "Qlib normalized adjusted prices"},
        )
        result = run_a_share_backtest(
            panel,
            np.tile(np.asarray([[1.0, 2.0]]), (8, 1)),
            start_index=0,
            end_index=7,
            rebalance_every=5,
            top_k=2,
            initial_cash=1_000.0,
            cost=CostModel(
                commission_bps=0.0,
                minimum_commission=0.0,
                sell_stamp_tax_bps=0.0,
                slippage_bps=0.0,
                lot_size=1,
                minimum_trade_notional=0.0,
            ),
        )
        self.assertEqual(result.trade_count, 4)
        self.assertAlmostEqual(result.ending_equity, 1_120.0, places=6)

    def test_rank_retention_buffer_reduces_unnecessary_churn(self) -> None:
        dates = tuple(f"d{index}" for index in range(10))
        prices = np.full((10, 2), 10.0, dtype=np.float64)
        panel = PanelData(
            dates,
            ("A", "B"),
            {"open": prices.copy(), "close": prices.copy(), "volume": np.ones((10, 2))},
            metadata={"price_scale": "Qlib normalized adjusted prices"},
        )
        factor = np.tile(np.asarray([[2.0, 1.0]]), (10, 1))
        factor[5:] = np.asarray([1.0, 2.0])
        cost = CostModel(
            commission_bps=0.0,
            minimum_commission=0.0,
            sell_stamp_tax_bps=0.0,
            slippage_bps=0.0,
            lot_size=1,
            minimum_trade_notional=0.0,
        )
        churn = run_a_share_backtest(
            panel, factor, start_index=0, end_index=9, rebalance_every=5,
            top_k=1, cost=cost,
        )
        buffered = run_a_share_backtest(
            panel, factor, start_index=0, end_index=9, rebalance_every=5,
            top_k=1, retention_k=2, cost=cost,
        )
        self.assertEqual(churn.trade_count, 3)
        self.assertEqual(buffered.trade_count, 1)
        self.assertLess(buffered.average_turnover, churn.average_turnover)

    def test_inverse_volatility_weights_are_causal_and_downweight_noisy_asset(self) -> None:
        dates = tuple(f"d{index:02d}" for index in range(30))
        stable = 10.0 + 0.01 * np.arange(30, dtype=np.float64)
        noisy = 10.0 * (1.0 + 0.10 * np.where(np.arange(30) % 2 == 0, 1.0, -1.0))
        close = np.column_stack((stable, noisy))
        panel = PanelData(
            dates,
            ("STABLE", "NOISY"),
            {"open": close.copy(), "close": close.copy(), "volume": np.ones((30, 2))},
            metadata={"price_scale": "Qlib normalized adjusted prices"},
        )
        factor = np.ones(panel.shape, dtype=np.float64)
        cost = CostModel(
            commission_bps=0.0,
            minimum_commission=0.0,
            sell_stamp_tax_bps=0.0,
            slippage_bps=0.0,
            lot_size=1,
            minimum_trade_notional=0.0,
        )
        baseline = run_a_share_backtest(
            panel, factor, start_index=20, end_index=29, rebalance_every=5,
            top_k=2, weighting="inverse_volatility", volatility_lookback=20,
            cost=cost,
        )
        first_weights = baseline.daily_records[1]["target_weights"]
        self.assertGreater(first_weights["STABLE"], first_weights["NOISY"])

        changed_close = close.copy()
        changed_close[22:, 1] *= 5.0
        changed = PanelData(
            dates,
            ("STABLE", "NOISY"),
            {"open": changed_close.copy(), "close": changed_close, "volume": np.ones((30, 2))},
            metadata={"price_scale": "Qlib normalized adjusted prices"},
        )
        replay = run_a_share_backtest(
            changed, factor, start_index=20, end_index=29, rebalance_every=5,
            top_k=2, weighting="inverse_volatility", volatility_lookback=20,
            cost=cost,
        )
        self.assertEqual(first_weights, replay.daily_records[1]["target_weights"])

    def test_blocked_risk_exit_remains_pending_after_signal_recovers(self) -> None:
        dates = tuple(f"2026-03-{day:02d}" for day in range(1, 9))
        fields = {
            "open": np.full((8, 1), 10.0, dtype=np.float64),
            "close": np.full((8, 1), 10.0, dtype=np.float64),
            "volume": np.ones((8, 1), dtype=np.float64),
        }
        sellable = np.ones((8, 1), dtype=bool)
        sellable[2, 0] = False
        panel = PanelData(
            dates,
            ("A",),
            fields,
            sellable=sellable,
            metadata={"price_scale": "Qlib normalized adjusted prices"},
        )
        risk_on = np.ones(8, dtype=bool)
        risk_on[1] = False
        result = run_a_share_backtest(
            panel,
            np.ones(panel.shape, dtype=np.float64),
            start_index=0,
            end_index=7,
            rebalance_every=5,
            execution_phase=0,
            top_k=1,
            initial_cash=1_000.0,
            risk_on_mask=risk_on,
            cost=CostModel(
                commission_bps=0.0,
                minimum_commission=0.0,
                sell_stamp_tax_bps=0.0,
                slippage_bps=0.0,
                lot_size=1,
                minimum_trade_notional=0.0,
            ),
        )
        blocked_day = result.daily_records[2]
        retry_day = result.daily_records[3]
        self.assertTrue(blocked_day["forced_risk_exit"])
        self.assertTrue(blocked_day["liquidation_pending"])
        self.assertEqual(blocked_day["positions"], 1)
        self.assertTrue(retry_day["forced_risk_exit"])
        self.assertFalse(retry_day["liquidation_pending"])
        self.assertEqual(retry_day["positions"], 0)
        self.assertEqual(result.daily_records[4]["positions"], 0)
        self.assertEqual(result.daily_records[5]["positions"], 0)
        self.assertEqual(result.daily_records[6]["positions"], 1)
        self.assertEqual(result.trade_count, 3)

    def test_one_catastrophic_phase_blocks_aggregate_pass(self) -> None:
        panel = planted_signal_panel(dates=120, assets=10)
        daily = tuple({"traded_value": 0.0} for _ in range(4))
        def result(curve: tuple[float, ...], drawdown: float) -> BacktestResult:
            total = curve[-1] / curve[0] - 1.0
            return BacktestResult(
                "2026-01-01", "2026-01-04", total, total, 0.1,
                1.0 if total > 0 else -1.0, drawdown, 0.0, 1, 0, 0, 0, 0,
                0, 0.0, 0, 0.0, 0.0, curve[-1], curve, daily,
            )
        outcomes = [
            result((20_000.0, 20_000.0, 0.0, 0.0), -1.0),
            *[result((20_000.0, 22_000.0, 24_000.0, 26_000.0), 0.0) for _ in range(4)],
        ]
        config = FactorFactoryConfig(
            initial_cash=100_000.0,
            horizon=5,
            rebalance_phase_count=5,
            minimum_phase_pass_ratio=0.8,
            maximum_backtest_drawdown=-0.25,
            require_positive_stressed_backtest_return=False,
        )
        with patch("factor_factory.pipeline.run_a_share_backtest", side_effect=outcomes):
            summary = FactorFactory._phase_backtest_summary(
                panel, np.ones(panel.shape), np.arange(70, 81), config
            )
        self.assertFalse(summary["passed"])
        self.assertFalse(summary["all_phase_drawdowns_pass"])

    def test_stressed_catastrophic_phase_blocks_aggregate_pass(self) -> None:
        panel = planted_signal_panel(dates=120, assets=10)
        daily = tuple({"traded_value": 0.0} for _ in range(4))
        def result(curve: tuple[float, ...], drawdown: float) -> BacktestResult:
            total = curve[-1] / curve[0] - 1.0
            return BacktestResult(
                "2026-01-01", "2026-01-04", total, total, 0.1,
                1.0 if total > 0 else -1.0, drawdown, 0.0, 1, 0, 0, 0, 0,
                0, 0.0, 0, 0.0, 0.0, curve[-1], curve, daily,
            )
        good = result((20_000.0, 22_000.0, 24_000.0, 26_000.0), 0.0)
        bad = result((20_000.0, 20_000.0, 0.0, 0.0), -1.0)
        outcomes: list[BacktestResult] = []
        for phase in range(5):
            outcomes.extend((good, bad if phase == 0 else good))
        config = FactorFactoryConfig(
            initial_cash=100_000.0,
            horizon=5,
            rebalance_phase_count=5,
            minimum_phase_pass_ratio=0.8,
            maximum_backtest_drawdown=-0.25,
            require_positive_stressed_backtest_return=True,
        )
        with patch("factor_factory.pipeline.run_a_share_backtest", side_effect=outcomes):
            summary = FactorFactory._phase_backtest_summary(
                panel, np.ones(panel.shape), np.arange(70, 81), config
            )
        self.assertFalse(summary["passed"])
        self.assertFalse(summary["stressed_passed"])
        self.assertEqual(summary["double_cost_stress"]["worst_phase_drawdown"], -1.0)

    def test_backtest_uses_integer_lots_and_respects_blocked_buys(self) -> None:
        panel = planted_signal_panel(dates=120, assets=10)
        panel.buyable[71:, 9] = False
        factor = np.tile(np.arange(10, dtype=np.float64), (120, 1))
        result = run_a_share_backtest(
            panel,
            factor,
            start_index=70,
            end_index=100,
            rebalance_every=5,
            top_k=1,
            initial_cash=100_000,
            cost=CostModel(),
        )
        self.assertGreater(result.blocked_buy_count, 0)
        self.assertGreater(len(result.equity_curve), 0)

    def test_held_delisting_is_written_off(self) -> None:
        panel = planted_signal_panel(dates=120, assets=10)
        factor = np.tile(np.arange(10, dtype=np.float64), (120, 1))
        panel.delisted[78, 9] = True
        panel.sellable[78:, 9] = False
        result = run_a_share_backtest(
            panel,
            factor,
            start_index=70,
            end_index=90,
            rebalance_every=5,
            top_k=1,
            initial_cash=100_000,
            cost=CostModel(),
        )
        self.assertEqual(result.delisting_writeoff_count, 1)
        self.assertGreater(result.delisting_writeoff_value, 0.0)

    def test_absolute_execution_phase_keeps_common_sample_window(self) -> None:
        panel = planted_signal_panel(dates=120, assets=10)
        factor = np.tile(np.arange(10, dtype=np.float64), (120, 1))
        first = run_a_share_backtest(
            panel, factor, start_index=70, end_index=90, rebalance_every=5,
            execution_phase=0, top_k=1, cost=CostModel(),
        )
        second = run_a_share_backtest(
            panel, factor, start_index=70, end_index=90, rebalance_every=5,
            execution_phase=1, top_k=1, cost=CostModel(),
        )
        self.assertEqual(first.start_date, second.start_date)
        self.assertEqual(first.end_date, second.end_date)
        self.assertEqual(len(first.equity_curve), len(second.equity_curve))


class MarketTimingTests(unittest.TestCase):
    @staticmethod
    def panel(*, buyable: bool = True) -> PanelData:
        dates = tuple(f"d{index:03d}" for index in range(90))
        trend = np.power(1.002, np.arange(90, dtype=np.float64))[:, None]
        close = np.repeat(10.0 * trend, 8, axis=1)
        fields = {
            "open": close.copy(),
            "close": close,
            "volume": np.ones((90, 8), dtype=np.float64),
        }
        return PanelData(
            dates,
            tuple(f"A{index}" for index in range(8)),
            fields,
            buyable=np.full((90, 8), buyable, dtype=bool),
            metadata={"price_scale": "Qlib normalized adjusted prices"},
        )

    @staticmethod
    def config() -> FactorFactoryConfig:
        return FactorFactoryConfig(
            minimum_assets=8,
            horizon=5,
            rebalance_phase_count=5,
            top_k=1,
            market_timing_enabled=True,
            minimum_risk_on_ratio=0.10,
            minimum_risk_on_rebalances_per_phase=1,
            require_positive_stressed_backtest_return=False,
            cost_model=CostModel(
                commission_bps=0.0,
                minimum_commission=0.0,
                sell_stamp_tax_bps=0.0,
                slippage_bps=0.0,
                lot_size=1,
                minimum_trade_notional=0.0,
            ),
        )

    def test_future_prices_cannot_change_past_market_state(self) -> None:
        panel = self.panel()
        baseline, _ = FactorFactory._market_risk_on(panel, self.config())
        changed = self.panel()
        changed.fields["close"][75:] *= 0.5
        replay, _ = FactorFactory._market_risk_on(changed, self.config())
        np.testing.assert_array_equal(baseline[:75], replay[:75])

    def test_activity_gate_requires_actual_investment(self) -> None:
        panel = self.panel(buyable=False)
        summary = FactorFactory._phase_backtest_summary(
            panel,
            np.ones(panel.shape, dtype=np.float64),
            np.arange(60, 90),
            self.config(),
        )
        timing = summary["market_timing"]
        self.assertTrue(timing["signal_activity_passed"])
        self.assertFalse(timing["actual_activity_passed"])
        self.assertFalse(timing["activity_passed"])
        self.assertEqual(timing["invested_day_ratio"], 0.0)

    def test_activity_report_uses_sample_length_invariant_phase_ratios(self) -> None:
        panel = self.panel()
        config = replace(
            self.config(),
            minimum_risk_on_rebalances_per_phase=0,
            minimum_risk_on_rebalance_ratio_per_phase=0.10,
        )
        summary = FactorFactory._phase_backtest_summary(
            panel,
            np.ones(panel.shape, dtype=np.float64),
            np.arange(60, 90),
            config,
        )
        timing = summary["market_timing"]
        for count, scheduled, ratio in zip(
            timing["risk_on_rebalances_per_phase"],
            timing["scheduled_rebalances_per_phase"],
            timing["risk_on_rebalance_ratios_per_phase"],
        ):
            self.assertAlmostEqual(ratio, count / scheduled)
        for count, scheduled, ratio in zip(
            timing["actual_invested_rebalances_per_phase"],
            timing["scheduled_rebalances_per_phase"],
            timing["actual_invested_rebalance_ratios_per_phase"],
        ):
            self.assertAlmostEqual(ratio, count / scheduled)

    def test_unverified_terminal_event_can_be_a_hard_data_quality_gate(self) -> None:
        panel = planted_signal_panel(dates=120, assets=10)
        factor = np.tile(np.arange(10, dtype=np.float64), (120, 1))
        panel.delisted[78, 9] = True
        panel.sellable[78:, 9] = False
        config = FactorFactoryConfig(
            horizon=5,
            rebalance_phase_count=5,
            top_k=1,
            minimum_phase_pass_ratio=0.2,
            minimum_backtest_sharpe=-999.0,
            maximum_backtest_drawdown=-1.0,
            require_positive_backtest_return=False,
            require_positive_stressed_backtest_return=False,
            require_no_unverified_terminal_events=True,
        )
        summary = FactorFactory._phase_backtest_summary(
            panel, factor, np.arange(70, 91), config
        )
        self.assertGreater(
            summary["data_quality_audit"]["unverified_terminal_event_count"], 0
        )
        self.assertFalse(summary["data_quality_audit"]["passed"])
        self.assertFalse(summary["passed"])

    def test_phase_backtest_rejects_non_contiguous_indices(self) -> None:
        panel = self.panel()
        with self.assertRaisesRegex(ValueError, "contiguous"):
            FactorFactory._phase_backtest_summary(
                panel,
                np.ones(panel.shape, dtype=np.float64),
                np.asarray([60, 61, 70, 71]),
                self.config(),
            )

    def test_numeric_risk_mask_is_rejected_fail_closed(self) -> None:
        panel = self.panel()
        risk = np.ones(len(panel.dates), dtype=np.float64)
        risk[1] = np.nan
        with self.assertRaisesRegex(ValueError, "boolean dtype"):
            run_a_share_backtest(
                panel,
                np.ones(panel.shape, dtype=np.float64),
                start_index=60,
                end_index=80,
                risk_on_mask=risk,
                cost=self.config().cost_model,
            )


class AttemptGovernanceTests(unittest.TestCase):
    @staticmethod
    def payload(directory: str) -> dict[str, object]:
        return {
            "protocol_id": "protocol-v3-test",
            "validation_start": "2025-01-01",
            "validation_end": "2025-03-31",
            "holdout_start": "2025-04-01",
            "holdout_end": "2025-06-30",
            "run_directory": str(Path(directory) / "research"),
            "engine_fingerprint": "a" * 64,
        }

    def test_attempt_state_and_evidence_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            governance = ResearchAttemptGovernance(Path(directory) / "attempts.sqlite3")
            registered = governance.register(self.payload(directory))
            evidence = {"seal": "2025-03-31", "raw_tree": "b" * 64}
            consumed = governance.transition(
                registered.protocol_id,
                registered.protocol_hash,
                "VALIDATION_CONSUMED",
                evidence,
            )
            recovered = governance.transition(
                registered.protocol_id,
                registered.protocol_hash,
                "VALIDATION_CONSUMED",
                evidence,
            )
            self.assertEqual(consumed.state, "VALIDATION_CONSUMED")
            self.assertTrue(recovered.recovered)
            self.assertEqual(
                recovered.state_evidence["VALIDATION_CONSUMED"], evidence
            )
            with self.assertRaises(RuntimeError):
                governance.transition(
                    registered.protocol_id,
                    registered.protocol_hash,
                    "VALIDATION_CONSUMED",
                    {"seal": "changed"},
                )

    def test_attempt_detects_direct_state_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "attempts.sqlite3"
            governance = ResearchAttemptGovernance(database)
            record = governance.register(self.payload(directory))
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "UPDATE attempts SET state='NO_FACTOR' WHERE protocol_id=?",
                    (record.protocol_id,),
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(RuntimeError):
                governance.require_state(
                    record.protocol_id, record.protocol_hash, {"NO_FACTOR"}
                )

    def test_attempt_detects_row_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "attempts.sqlite3"
            governance = ResearchAttemptGovernance(database)
            record = governance.register(self.payload(directory))
            connection = sqlite3.connect(database)
            try:
                connection.execute("DELETE FROM attempts")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(RuntimeError):
                governance.require_state(
                    record.protocol_id, record.protocol_hash, {"PROTOCOL_REGISTERED"}
                )

    def test_attempt_recovers_commit_before_anchor_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "attempts.sqlite3"
            governance = ResearchAttemptGovernance(database)
            with patch.object(governance, "_write_anchor", side_effect=OSError("fault")):
                with self.assertRaises(OSError):
                    governance.register(self.payload(directory))
            self.assertTrue(governance.anchor_pending_path.exists())
            recovered = ResearchAttemptGovernance(database).register(self.payload(directory))
            self.assertEqual(recovered.state, "PROTOCOL_REGISTERED")
            self.assertTrue(recovered.recovered)
            self.assertFalse(governance.anchor_pending_path.exists())

    def test_no_factor_successor_requires_exact_committed_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            governance = ResearchAttemptGovernance(root / "attempts.sqlite3")
            predecessor_payload = self.payload(directory)
            predecessor_payload["protocol_version"] = 3
            predecessor = governance.register(predecessor_payload)
            governance.transition(
                predecessor.protocol_id, predecessor.protocol_hash, "VALIDATION_CONSUMED"
            )
            governance.transition(
                predecessor.protocol_id, predecessor.protocol_hash, "NO_FACTOR"
            )
            research = Path(str(predecessor_payload["run_directory"]))
            research.mkdir(parents=True)
            manifest = {
                "attempt_terminal_state": "NO_FACTOR",
                "research_attempt": {"protocol_id": predecessor.protocol_id},
                "ensemble": {"qualified": False, "holdout_was_opened": False},
            }
            manifest_path = research / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True), encoding="utf-8"
            )
            manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            (research / "COMMITTED.json").write_text(
                json.dumps({"manifest_sha256": manifest_sha}), encoding="utf-8"
            )
            successor = dict(predecessor_payload)
            successor.update({
                "protocol_id": "protocol-v4-gpu-test",
                "protocol_version": 4,
                "run_directory": str(root / "gpu-research"),
                "lineage_manifest_sha256": manifest_sha,
                "lineage": {
                    "run_directory": str(research.resolve()),
                    "manifest_sha256": manifest_sha,
                    "qualified": False,
                    "holdout_was_opened": False,
                },
            })
            registered = governance.register(successor)
            self.assertEqual(registered.state, "PROTOCOL_REGISTERED")
            self.assertEqual(
                registered.state_evidence["PROTOCOL_REGISTERED"]["supersedes_terminal_attempt"]["protocol_id"],
                predecessor.protocol_id,
            )

    def test_no_factor_successor_rejects_unbound_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            governance = ResearchAttemptGovernance(root / "attempts.sqlite3")
            predecessor_payload = self.payload(directory)
            predecessor_payload["protocol_version"] = 3
            predecessor = governance.register(predecessor_payload)
            governance.transition(
                predecessor.protocol_id, predecessor.protocol_hash, "VALIDATION_CONSUMED"
            )
            governance.transition(
                predecessor.protocol_id, predecessor.protocol_hash, "NO_FACTOR"
            )
            successor = dict(predecessor_payload)
            successor.update({
                "protocol_id": "protocol-v4-gpu-test",
                "protocol_version": 4,
                "run_directory": str(root / "gpu-research"),
            })
            with self.assertRaisesRegex(RuntimeError, "exact failed-run lineage"):
                governance.register(successor)

    def test_technical_abort_can_only_be_superseded_by_sealed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            governance = ResearchAttemptGovernance(root / "attempts.sqlite3")
            predecessor_payload = self.payload(directory)
            predecessor_payload["protocol_version"] = 4
            predecessor = governance.register(predecessor_payload)
            governance.transition(
                predecessor.protocol_id, predecessor.protocol_hash, "VALIDATION_CONSUMED"
            )
            research = Path(str(predecessor_payload["run_directory"]))
            research.mkdir(parents=True)
            aborted = {
                "state": "ABORTED_TECHNICAL",
                "protocol_id": predecessor.protocol_id,
                "protocol_hash": predecessor.protocol_hash,
                "holdout_was_opened": False,
                "result_committed": False,
            }
            aborted_path = research / "ABORTED.json"
            aborted_path.write_text(
                json.dumps(aborted, sort_keys=True), encoding="utf-8"
            )
            aborted_sha = hashlib.sha256(aborted_path.read_bytes()).hexdigest()
            governance.transition(
                predecessor.protocol_id,
                predecessor.protocol_hash,
                "ABORTED_TECHNICAL",
                {"abort_artifact_sha256": aborted_sha},
            )
            successor = dict(predecessor_payload)
            successor.update({
                "protocol_id": "protocol-v5-batched-gpu-test",
                "protocol_version": 5,
                "run_directory": str(root / "v5-research"),
                "lineage_manifest_sha256": aborted_sha,
                "lineage": {
                    "run_directory": str(research.resolve()),
                    "manifest_sha256": aborted_sha,
                    "qualified": False,
                    "holdout_was_opened": False,
                },
            })
            registered = governance.register(successor)
            superseded = registered.state_evidence["PROTOCOL_REGISTERED"][
                "supersedes_terminal_attempt"
            ]
            self.assertEqual(superseded["state"], "ABORTED_TECHNICAL")


class PipelineTests(unittest.TestCase):
    @staticmethod
    def qlib_store_from_panel(root: Path, panel: PanelData) -> Path:
        store = root / "qlib"
        (store / "calendars").mkdir(parents=True)
        (store / "instruments").mkdir(parents=True)
        (store / "calendars" / "day.txt").write_text(
            "\n".join(panel.dates) + "\n", encoding="utf-8"
        )
        symbols = tuple(
            f"SH{600000 + index:06d}"
            if index < len(panel.symbols) // 2
            else f"SZ{index - len(panel.symbols) // 2 + 1:06d}"
            for index in range(len(panel.symbols))
        )
        (store / "instruments" / "all.txt").write_text(
            "".join(
                f"{symbol}\t{panel.dates[0]}\t{panel.dates[-1]}\n"
                for symbol in symbols
            ),
            encoding="utf-8",
        )
        change = np.zeros(panel.shape, dtype=np.float32)
        change[1:] = (
            panel.fields["close"][1:] / panel.fields["close"][:-1] - 1.0
        ).astype(np.float32)
        for column, symbol in enumerate(symbols):
            directory = store / "features" / symbol.lower()
            directory.mkdir(parents=True)
            for field in ("open", "high", "low", "close", "volume", "amount", "change"):
                values = change[:, column] if field == "change" else panel.fields[field][:, column]
                np.concatenate((
                    np.array([0.0], dtype="<f4"),
                    np.asarray(values, dtype="<f4"),
                )).tofile(directory / f"{field}.day.bin")
        (root / "PROVENANCE.json").write_text(
            json.dumps({"sha256": "b" * 64, "snapshot_date": panel.dates[-1]}),
            encoding="utf-8",
        )
        return store

    @staticmethod
    def governance_record(directory: str, run_id: str, **changes) -> dict[str, object]:
        value: dict[str, object] = {
            "run_id": run_id,
            "holdout_key": "period-a",
            "protocol_id": "protocol-v3-a",
            "protocol_hash": "f" * 64,
            "market": "A_SHARE",
            "holdout_start": "2025-01-01",
            "holdout_end": "2025-12-31",
            "horizon": 5,
            "freeze_hash": "a" * 64,
            "training_fingerprint": "b" * 64,
            "family_hash": "c" * 64,
            "snapshot_fingerprint": "d" * 64,
            "engine_fingerprint": "e" * 64,
            "run_directory": str(Path(directory) / run_id),
        }
        value.update(changes)
        return value

    def test_v3_qlib_panel_without_consumed_attempt_fails_before_expression_read(self) -> None:
        panel = planted_signal_panel(dates=180, assets=10)
        panel.metadata.update({
            "source": "qlib_binary_store",
            "promotion_ceiling": "RESEARCH_ONLY",
        })
        config = FactorFactoryConfig(
            protocol_version=3,
            protocol_id="v3-missing-attempt",
            lineage_manifest_sha256="a" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch("factor_factory.pipeline.evaluate_expression") as evaluate:
                with self.assertRaisesRegex(RuntimeError, "no pre-consumption attempt"):
                    FactorFactory(config).run(panel, Path(directory) / "run")
                evaluate.assert_not_called()

    def test_v3_research_attempt_commits_exactly_one_terminal_state(self) -> None:
        panel = planted_signal_panel(dates=320, assets=10)
        config = FactorFactoryConfig(
            protocol_version=3,
            protocol_id="v3-governed-integration",
            lineage_manifest_sha256="a" * 64,
            population_size=8,
            generations=0,
            elite_size=4,
            max_depth=2,
            minimum_assets=8,
            permutations=0,
            initial_cash=10_000.0,
            top_k=2,
            cost_model=CostModel(
                commission_bps=0.0,
                minimum_commission=0.0,
                sell_stamp_tax_bps=0.0,
                slippage_bps=0.0,
                lot_size=1,
                minimum_trade_notional=0.0,
            ),
        )
        factory = FactorFactory(config)
        splits = three_way_time_split(
            len(panel.dates),
            purge=config.horizon + 1,
            discovery_fraction=config.discovery_fraction,
            validation_fraction=config.validation_fraction,
        )
        split_manifest = factory._split_manifest(panel, splits)
        seal_index = int(splits.validation[-1]) + config.horizon + 1
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "research"
            attempt_path = root / "attempts.sqlite3"
            panel.metadata.update({
                "source": "qlib_binary_store",
                "promotion_ceiling": "RESEARCH_ONLY",
                "store": str((root / "qlib").resolve()),
                "snapshot_sha256": "b" * 64,
                "raw_tree_fingerprint": "c" * 64,
                "snapshot_date": panel.dates[-1],
                "universe": "all",
                "exchanges": ["SH", "SZ"],
                "minimum_history_dates": 1,
                "stock_only": True,
                "content_loaded_through": panel.dates[seal_index],
                "holdout_content_loaded": False,
            })
            snapshot_identity = {
                "store": panel.metadata["store"],
                "start": panel.dates[0],
                "end": panel.dates[-1],
                "calendar_dates": len(panel.dates),
                "symbols": len(panel.symbols),
                "universe": "all",
                "exchanges": ["SH", "SZ"],
                "fields": sorted(set(panel.fields) | {"change"}),
                "snapshot_sha256": "b" * 64,
                "snapshot_date": panel.dates[-1],
                "raw_tree_fingerprint": "c" * 64,
            }
            payload = {
                "schema_version": 1,
                "protocol_version": 3,
                "protocol_id": config.protocol_id,
                "validation_start": split_manifest["validation"]["start"],
                "validation_end": split_manifest["validation"]["end"],
                "holdout_start": split_manifest["holdout"]["start"],
                "holdout_end": split_manifest["holdout"]["end"],
                "run_directory": str(output.resolve()),
                "config": asdict(config),
                "engine_fingerprint": factory._engine_fingerprint(),
                "runtime_signature": factory._runtime_signature(),
                "lineage_manifest_sha256": config.lineage_manifest_sha256,
                "snapshot_identity": snapshot_identity,
                "panel_request": {
                    "start_date": panel.dates[0],
                    "end_date": panel.dates[-1],
                    "universe": "all",
                    "exchanges": ["SH", "SZ"],
                    "minimum_history_dates": 1,
                },
                "splits": split_manifest,
                "content_seal": {
                    "loaded_through": panel.dates[seal_index],
                    "holdout_content_start": split_manifest["holdout"]["start"],
                },
            }
            governance = ResearchAttemptGovernance(attempt_path)
            registered = governance.register(payload)
            validation_evidence = {"seal": panel.dates[seal_index]}
            governance.transition(
                registered.protocol_id,
                registered.protocol_hash,
                "VALIDATION_CONSUMED",
                validation_evidence,
            )
            panel.metadata["research_attempt"] = {
                "database": str(attempt_path.resolve()),
                "protocol_id": registered.protocol_id,
                "protocol_hash": registered.protocol_hash,
                "validation_evidence": validation_evidence,
            }
            result = FactorFactory(
                config, attempt_governance_path=attempt_path
            ).run(panel, output)
            terminal = governance.require_state(
                registered.protocol_id,
                registered.protocol_hash,
                {"NO_FACTOR", "FROZEN_AWAITING_HOLDOUT"},
            )
            self.assertTrue((output / "COMMITTED.json").is_file())
            self.assertIn(
                result.status, {"NO_FACTOR_PROMOTED", "FROZEN_AWAITING_HOLDOUT"}
            )
            self.assertIn(terminal.state, {"NO_FACTOR", "FROZEN_AWAITING_HOLDOUT"})

    def test_v3_qlib_holdout_opens_governance_before_full_panel_load(self) -> None:
        source_panel = planted_signal_panel(dates=220, assets=16, seed=23)
        relaxed = SelectionGates(
            minimum_evaluation_dates=20,
            minimum_coverage=0.40,
            minimum_evaluable_date_ratio=0.0,
            minimum_fold_evaluable_ratio=0.0,
            minimum_mean_ic=-1.0,
            minimum_icir=-10.0,
            minimum_positive_fold_ratio=0.0,
            minimum_worst_fold_ic=-1.0,
            maximum_q_value=1.0,
            require_positive_stressed_return=False,
        )
        config = FactorFactoryConfig(
            protocol_version=3,
            protocol_id="v3-holdout-ordering",
            lineage_manifest_sha256="a" * 64,
            seed=23,
            population_size=18,
            generations=1,
            elite_size=5,
            permutations=19,
            maximum_selected=3,
            minimum_assets=8,
            selection_gates=relaxed,
            minimum_backtest_sharpe=-999.0,
            maximum_backtest_drawdown=-1.0,
            require_positive_backtest_return=False,
            require_positive_stressed_backtest_return=False,
            minimum_ensemble_coverage=0.0001,
            cost_model=CostModel(
                commission_bps=0.0,
                minimum_commission=0.0,
                sell_stamp_tax_bps=0.0,
                slippage_bps=0.0,
                lot_size=1,
                minimum_trade_notional=0.0,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store_root = self.qlib_store_from_panel(root / "snapshot", source_panel)
            store = QlibBinaryStore(store_root)
            splits = three_way_time_split(
                len(store.calendar),
                purge=config.horizon + 1,
                discovery_fraction=config.discovery_fraction,
                validation_fraction=config.validation_fraction,
            )
            seal_index = int(splits.validation[-1]) + config.horizon + 1
            seal_date = store.calendar[seal_index]
            research = root / "research"
            holdout = root / "holdout"
            attempts = root / "attempts.sqlite3"
            holdout_governance = root / "holdout.sqlite3"
            panel = store.load_panel(
                start_date=store.calendar[0],
                end_date=store.calendar[-1],
                minimum_history_dates=1,
                seal_after_date=seal_date,
            )
            factory = FactorFactory(config, attempt_governance_path=attempts)
            split_manifest = factory._split_manifest(panel, splits)
            snapshot_identity = store.research_snapshot_identity(
                start_date=store.calendar[0], end_date=store.calendar[-1]
            )
            payload = {
                "schema_version": 1,
                "protocol_version": 3,
                "protocol_id": config.protocol_id,
                "validation_start": split_manifest["validation"]["start"],
                "validation_end": split_manifest["validation"]["end"],
                "holdout_start": split_manifest["holdout"]["start"],
                "holdout_end": split_manifest["holdout"]["end"],
                "run_directory": str(research.resolve()),
                "config": asdict(config),
                "engine_fingerprint": factory._engine_fingerprint(),
                "runtime_signature": factory._runtime_signature(),
                "lineage_manifest_sha256": config.lineage_manifest_sha256,
                "snapshot_identity": snapshot_identity,
                "panel_request": {
                    "start_date": store.calendar[0],
                    "end_date": store.calendar[-1],
                    "universe": "all",
                    "exchanges": ["SH", "SZ"],
                    "minimum_history_dates": 1,
                },
                "splits": split_manifest,
                "content_seal": {
                    "loaded_through": seal_date,
                    "holdout_content_start": split_manifest["holdout"]["start"],
                },
            }
            attempt_governance = ResearchAttemptGovernance(attempts)
            registered = attempt_governance.register(payload)
            validation_evidence = {"seal": seal_date}
            attempt_governance.transition(
                registered.protocol_id,
                registered.protocol_hash,
                "VALIDATION_CONSUMED",
                validation_evidence,
            )
            panel.metadata["research_attempt"] = {
                "database": str(attempts.resolve()),
                "protocol_id": registered.protocol_id,
                "protocol_hash": registered.protocol_hash,
                "validation_evidence": validation_evidence,
            }
            stage_one = factory.run(panel, research)
            self.assertEqual(stage_one.status, "FROZEN_AWAITING_HOLDOUT")
            freeze = json.loads((research / "freeze.json").read_text(encoding="utf-8"))

            original_load = QlibBinaryStore.load_panel

            def guarded_load(store_instance, *args, **kwargs):
                if kwargs.get("seal_after_date") is None:
                    attempt_governance.require_state(
                        registered.protocol_id,
                        registered.protocol_hash,
                        {"HOLDOUT_OPENED"},
                    )
                    connection = sqlite3.connect(holdout_governance)
                    try:
                        state = connection.execute(
                            "SELECT status FROM holdout_runs"
                        ).fetchone()[0]
                    finally:
                        connection.close()
                    self.assertIn(state, {"OPENED", "RESULT_COMMITTED"})
                return original_load(store_instance, *args, **kwargs)

            with patch.object(QlibBinaryStore, "load_panel", new=guarded_load):
                stage_two = FactorFactory.open_frozen_qlib_holdout(
                    store_root,
                    research,
                    holdout,
                    confirm_freeze_hash=freeze["freeze_hash"],
                    governance_path=holdout_governance,
                    attempt_governance_path=attempts,
                )
            self.assertEqual(stage_two.run_directory, str(holdout.resolve()))
            attempt_governance.require_state(
                registered.protocol_id,
                registered.protocol_hash,
                {"RESULT_COMMITTED"},
            )

    def test_holdout_state_machine_recovers_only_same_frozen_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            governance = HoldoutGovernance(Path(directory) / "governance.sqlite3")
            record = self.governance_record(directory, "run-a")
            self.assertEqual(governance.reserve(record).status, "RESERVED")
            self.assertEqual(governance.mark_opened("run-a", "a" * 64).status, "OPENED")
            committed = governance.commit_result(
                "run-a", "a" * 64, {"qualified": False, "reason": "test"}
            )
            self.assertEqual(committed.status, "RESULT_COMMITTED")
            recovered = governance.reserve(record)
            self.assertTrue(recovered.recovered)
            self.assertEqual(recovered.result, {"qualified": False, "reason": "test"})

    def test_governance_rejects_overlap_from_different_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            governance = HoldoutGovernance(Path(directory) / "governance.sqlite3")
            governance.reserve(self.governance_record(directory, "run-a"))
            conflicting = self.governance_record(
                directory,
                "run-b",
                holdout_key="period-b",
                holdout_start="2025-06-01",
                holdout_end="2026-03-01",
                run_directory=str(Path(directory) / "different-output"),
            )
            with self.assertRaises(RuntimeError):
                governance.reserve(conflicting)

    def test_governance_detects_state_table_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "governance.sqlite3"
            governance = HoldoutGovernance(database)
            governance.reserve(self.governance_record(directory, "run-a"))
            connection = sqlite3.connect(database)
            try:
                connection.execute("DELETE FROM holdout_runs")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(RuntimeError):
                governance.reserve(self.governance_record(directory, "run-b"))

    def test_governance_recovers_committed_database_before_anchor_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "governance.sqlite3"
            governance = HoldoutGovernance(database)
            record = self.governance_record(directory, "run-a")
            with patch.object(governance, "_write_anchor", side_effect=OSError("fault")):
                with self.assertRaises(OSError):
                    governance.reserve(record)
            self.assertTrue(governance.anchor_pending_path.exists())
            recovered = HoldoutGovernance(database).reserve(record)
            self.assertEqual(recovered.status, "RESERVED")
            self.assertTrue(recovered.recovered)
            self.assertFalse(governance.anchor_pending_path.exists())

    def test_governance_discards_journal_when_database_commit_did_not_happen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "governance.sqlite3"
            governance = HoldoutGovernance(database)
            record = self.governance_record(directory, "run-a")
            governance.reserve(record)
            governance._prepare_anchor("f" * 64)
            recovered = governance.reserve(record)
            self.assertEqual(recovered.status, "RESERVED")
            self.assertTrue(recovered.recovered)
            self.assertFalse(governance.anchor_pending_path.exists())

    def test_registry_manifest_is_last_commit_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ResearchRegistry(directory)
            with patch("factor_factory.registry.atomic_write_jsonl", side_effect=OSError("fault")):
                with self.assertRaises(OSError):
                    registry.write_run(
                        manifest={"engine": "test"}, candidates=[], selected=[], backtests={}
                    )
            self.assertFalse((Path(directory) / "manifest.json").exists())

    def test_registry_detects_manifest_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ResearchRegistry(directory)
            registry.write_run(
                manifest={"engine": "test", "effective_promotion_ceiling": "RESEARCH_ONLY"},
                candidates=[], selected=[], backtests={},
            )
            path = Path(directory) / "manifest.json"
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["effective_promotion_ceiling"] = "LIVE"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                registry.verify_completed()

    def test_run_directory_lock_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with ResearchRunLock(directory):
                with self.assertRaises(RuntimeError):
                    with ResearchRunLock(directory):
                        pass

    def test_stale_lock_recovery_requires_hash_and_dead_local_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".run.lock"
            path.write_text(
                json.dumps({
                    "pid": 2_147_483_647,
                    "host": socket.gethostname(),
                    "created_at_utc": "2026-07-15T00:00:00+00:00",
                }),
                encoding="utf-8",
            )
            digest = file_sha256(path)
            with self.assertRaisesRegex(RuntimeError, "confirmation does not match"):
                ResearchRunLock.recover_stale(directory, confirm_lock_sha256="0" * 64)
            with patch.object(ResearchRunLock, "_pid_is_running", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "still running"):
                    ResearchRunLock.recover_stale(directory, confirm_lock_sha256=digest)
            with patch.object(ResearchRunLock, "_pid_is_running", return_value=False):
                result = ResearchRunLock.recover_stale(
                    directory, confirm_lock_sha256=digest
                )
            self.assertEqual(result["status"], "STALE_LOCK_ARCHIVED")
            self.assertFalse(path.exists())
            self.assertTrue(Path(result["archive"]).exists())

    def test_training_fingerprint_does_not_read_holdout(self) -> None:
        panel = planted_signal_panel(dates=220, assets=16, seed=31)
        baseline = FactorFactory._training_fingerprint(panel, decision_end=150, evidence_end=156)
        panel.fields["close"][180:] *= 100.0
        panel.eligible[180:] = False
        changed = FactorFactory._training_fingerprint(panel, decision_end=150, evidence_end=156)
        self.assertEqual(baseline, changed)

    def test_training_fingerprint_ignores_holdout_only_symbol(self) -> None:
        panel = planted_signal_panel(dates=220, assets=16, seed=37)
        baseline = FactorFactory._training_fingerprint(panel, decision_end=150, evidence_end=156)
        fields = {
            name: np.column_stack((values, np.full(len(panel.dates), np.nan, dtype=values.dtype)))
            for name, values in panel.fields.items()
        }
        for name in fields:
            fields[name][180:, -1] = 1.0
        def extended(mask: np.ndarray) -> np.ndarray:
            result = np.column_stack((mask, np.zeros(len(panel.dates), dtype=bool)))
            result[180:, -1] = True
            return result
        expanded = PanelData(
            panel.dates,
            panel.symbols + ("ZZ_FUTURE",),
            fields,
            eligible=extended(panel.eligible),
            buyable=extended(panel.buyable),
            sellable=extended(panel.sellable),
            delisted=np.column_stack((panel.delisted, np.zeros(len(panel.dates), dtype=bool))),
            invalidated=np.column_stack((panel.invalidated, np.zeros(len(panel.dates), dtype=bool))),
        )
        changed = FactorFactory._training_fingerprint(expanded, decision_end=150, evidence_end=156)
        self.assertEqual(baseline, changed)

    def test_pipeline_writes_auditable_registry(self) -> None:
        panel = planted_signal_panel(dates=220, assets=16, seed=23)
        relaxed = SelectionGates(
            minimum_evaluation_dates=20,
            minimum_coverage=0.40,
            minimum_evaluable_date_ratio=0.0,
            minimum_fold_evaluable_ratio=0.0,
            minimum_mean_ic=-1.0,
            minimum_icir=-10.0,
            minimum_positive_fold_ratio=0.0,
            minimum_worst_fold_ic=-1.0,
            maximum_q_value=1.0,
            require_positive_stressed_return=False,
        )
        config = FactorFactoryConfig(
            seed=23,
            population_size=18,
            generations=1,
            elite_size=5,
            permutations=19,
            maximum_selected=3,
            minimum_assets=8,
            selection_gates=relaxed,
            minimum_backtest_sharpe=-999.0,
            maximum_backtest_drawdown=-1.0,
            require_positive_backtest_return=False,
            require_positive_stressed_backtest_return=False,
            minimum_ensemble_coverage=0.0001,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = FactorFactory(
                config, governance_path=Path(directory) / "governance.sqlite3"
            ).run(panel, directory)
            self.assertGreaterEqual(result.generated_count, 18)
            self.assertTrue((Path(directory) / "manifest.json").exists())
            manifest = json.loads((Path(directory) / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["safety_ceiling"], "SHADOW_ONLY")
            self.assertIsNotNone(manifest["ensemble"]["definition"])
            self.assertIn("holdout_was_opened", manifest["ensemble"])
            candidate_rows = (Path(directory) / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(candidate_rows), result.generated_count)
            self.assertEqual(ResearchRegistry(directory).verify_completed()["registry_schema"], 2)

    def test_v6_discovery_first_freezes_before_validation_without_reselection(self) -> None:
        panel = planted_signal_panel(dates=220, assets=16, seed=43)
        relaxed = SelectionGates(
            minimum_evaluation_dates=20,
            minimum_coverage=0.0,
            minimum_evaluable_date_ratio=0.0,
            minimum_fold_evaluable_ratio=0.0,
            minimum_mean_ic=-1.0,
            minimum_icir=-10.0,
            minimum_positive_fold_ratio=0.0,
            minimum_worst_fold_ic=-1.0,
            maximum_q_value=1.0,
            require_positive_stressed_return=False,
        )
        config = FactorFactoryConfig(
            protocol_version=6,
            protocol_id="v6-discovery-first-test",
            seed=43,
            population_size=16,
            generations=0,
            elite_size=4,
            maximum_selected=3,
            maximum_ensemble_components=3,
            discovery_first_selection=True,
            maximum_discovery_event_candidates=16,
            minimum_assets=8,
            permutations=9,
            selection_gates=relaxed,
            correlation_cap=0.999999,
            minimum_ensemble_coverage=0.0001,
            minimum_backtest_sharpe=-999.0,
            maximum_backtest_drawdown=-1.0,
            require_positive_backtest_return=False,
            require_positive_stressed_backtest_return=False,
            cost_model=CostModel(
                commission_bps=0.0,
                minimum_commission=0.0,
                sell_stamp_tax_bps=0.0,
                slippage_bps=0.0,
                lot_size=1,
                minimum_trade_notional=0.0,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            FactorFactory(config).run(panel, directory)
            manifest = json.loads(
                (Path(directory) / "manifest.json").read_text(encoding="utf-8")
            )
            protocol = manifest["selection_protocol"]
            self.assertTrue(protocol["discovery_first_selection"])
            self.assertFalse(protocol["validation_reselection_allowed"])
            self.assertLessEqual(protocol["validation_candidate_count"], 3)
            rows = [
                json.loads(line)
                for line in (Path(directory) / "candidates.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            evaluated = [row for row in rows if row["validation"] is not None]
            self.assertEqual(len(evaluated), protocol["validation_candidate_count"])

    def test_panel_ceiling_prevents_shadow_promotion(self) -> None:
        panel = planted_signal_panel(dates=220, assets=16, seed=29)
        panel.metadata["promotion_ceiling"] = "RESEARCH_ONLY"
        relaxed = SelectionGates(
            minimum_evaluation_dates=20,
            minimum_coverage=0.0,
            minimum_evaluable_date_ratio=0.0,
            minimum_fold_evaluable_ratio=0.0,
            minimum_mean_ic=-1.0,
            minimum_icir=-10.0,
            minimum_positive_fold_ratio=0.0,
            minimum_worst_fold_ic=-1.0,
            maximum_q_value=1.0,
            require_positive_stressed_return=False,
        )
        config = FactorFactoryConfig(
            seed=29,
            population_size=12,
            generations=0,
            elite_size=4,
            permutations=9,
            maximum_selected=2,
            minimum_assets=8,
            selection_gates=relaxed,
            minimum_backtest_sharpe=-999.0,
            maximum_backtest_drawdown=-1.0,
            require_positive_backtest_return=False,
            require_positive_stressed_backtest_return=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = FactorFactory(
                config, governance_path=Path(directory) / "governance.sqlite3"
            ).run(panel, directory)
            manifest = json.loads((Path(directory) / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["effective_promotion_ceiling"], "RESEARCH_ONLY")
            self.assertEqual(result.shadow_count, 0)
            if result.selected_ids:
                self.assertEqual(result.status, "FROZEN_AWAITING_HOLDOUT")
                self.assertFalse(manifest["ensemble"]["holdout_was_opened"])
                self.assertTrue(manifest["ensemble"]["awaiting_explicit_holdout_open"])

    def test_two_stage_holdout_requires_exact_confirmation_and_separate_output(self) -> None:
        panel = planted_signal_panel(dates=220, assets=16, seed=23)
        panel.metadata["promotion_ceiling"] = "RESEARCH_ONLY"
        relaxed = SelectionGates(
            minimum_evaluation_dates=20,
            minimum_coverage=0.40,
            minimum_evaluable_date_ratio=0.0,
            minimum_fold_evaluable_ratio=0.0,
            minimum_mean_ic=-1.0,
            minimum_icir=-10.0,
            minimum_positive_fold_ratio=0.0,
            minimum_worst_fold_ic=-1.0,
            maximum_q_value=1.0,
            require_positive_stressed_return=False,
        )
        config = FactorFactoryConfig(
            seed=23,
            population_size=18,
            generations=1,
            elite_size=5,
            permutations=19,
            maximum_selected=3,
            minimum_assets=8,
            selection_gates=relaxed,
            minimum_backtest_sharpe=-999.0,
            maximum_backtest_drawdown=-1.0,
            require_positive_backtest_return=False,
            require_positive_stressed_backtest_return=False,
            minimum_ensemble_coverage=0.0001,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            research = root / "research"
            holdout = root / "holdout"
            governance = root / "governance.sqlite3"
            stage_one = FactorFactory(config, governance_path=governance).run(panel, research)
            self.assertEqual(stage_one.status, "FROZEN_AWAITING_HOLDOUT")
            self.assertFalse(governance.exists())
            freeze = json.loads((research / "freeze.json").read_text(encoding="utf-8"))
            freeze_hash = freeze["freeze_hash"]

            with self.assertRaisesRegex(RuntimeError, "confirmation does not match"):
                FactorFactory.open_frozen_holdout(
                    panel,
                    research,
                    holdout,
                    confirm_freeze_hash="0" * 64,
                    governance_path=governance,
                )
            self.assertFalse(governance.exists())

            panel.metadata["promotion_ceiling"] = "SHADOW_ONLY"
            with self.assertRaisesRegex(RuntimeError, "promotion policy changed"):
                FactorFactory.open_frozen_holdout(
                    panel,
                    research,
                    holdout,
                    confirm_freeze_hash=freeze_hash,
                    governance_path=governance,
                )
            self.assertFalse(governance.exists())
            panel.metadata["promotion_ceiling"] = "RESEARCH_ONLY"

            stage_two = FactorFactory.open_frozen_holdout(
                panel,
                research,
                holdout,
                confirm_freeze_hash=freeze_hash,
                governance_path=governance,
            )
            self.assertTrue(governance.exists())
            holdout_manifest = ResearchRegistry(holdout).verify_completed()
            self.assertTrue(holdout_manifest["ensemble"]["holdout_was_opened"])
            self.assertEqual(stage_two.run_directory, str(holdout.resolve()))

    def test_engineering_only_freeze_cannot_open_holdout(self) -> None:
        panel = planted_signal_panel(dates=220, assets=16, seed=23)
        panel.metadata["promotion_ceiling"] = "ENGINEERING_ONLY"
        relaxed = SelectionGates(
            minimum_evaluation_dates=20,
            minimum_coverage=0.0,
            minimum_evaluable_date_ratio=0.0,
            minimum_fold_evaluable_ratio=0.0,
            minimum_mean_ic=-1.0,
            minimum_icir=-10.0,
            minimum_positive_fold_ratio=0.0,
            minimum_worst_fold_ic=-1.0,
            maximum_q_value=1.0,
        )
        config = FactorFactoryConfig(
            seed=23,
            population_size=12,
            generations=0,
            elite_size=4,
            permutations=9,
            maximum_selected=2,
            minimum_assets=8,
            selection_gates=relaxed,
            minimum_backtest_sharpe=-999.0,
            maximum_backtest_drawdown=-1.0,
            require_positive_backtest_return=False,
            require_positive_stressed_backtest_return=False,
            minimum_ensemble_coverage=0.0001,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            research = root / "research"
            governance = root / "governance.sqlite3"
            result = FactorFactory(config, governance_path=governance).run(panel, research)
            self.assertEqual(result.status, "FROZEN_ENGINEERING_ONLY")
            freeze = json.loads((research / "freeze.json").read_text(encoding="utf-8"))
            with self.assertRaisesRegex(RuntimeError, "ENGINEERING_ONLY"):
                FactorFactory.open_frozen_holdout(
                    panel,
                    research,
                    root / "holdout",
                    confirm_freeze_hash=freeze["freeze_hash"],
                    governance_path=governance,
                )
            self.assertFalse(governance.exists())


if __name__ == "__main__":
    unittest.main()
