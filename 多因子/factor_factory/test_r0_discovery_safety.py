"""Safety gates for action-aware R0 single-factor Discovery."""

from __future__ import annotations

import unittest

import numpy as np

from factor_factory.evaluation import (
    forward_total_returns,
    minimum_trading_age_mask,
    winsorized_cross_sectional_rank,
)
from factor_factory.expressions import Expression
from factor_factory.factor_safety import assess_factor_safety
from factor_factory.schema import PanelData
from factor_factory.total_return_projection import build_total_return_signal_fields


class R0DiscoverySafetyTests(unittest.TestCase):
    @staticmethod
    def panel() -> PanelData:
        dates = tuple(f"2026-01-{day:02d}" for day in range(1, 9))
        opens = np.asarray([[100.0], [90.0], [92.0], [85.0], [86.0], [87.0], [88.0], [89.0]])
        closes = np.asarray([[100.0], [91.0], [93.0], [86.0], [87.0], [88.0], [89.0], [90.0]])
        total_returns = np.asarray([
            [0.0],
            [0.01],  # entry-day cash action must not be credited after the open
            [93.0 / 91.0 - 1.0],
            [96.0 / 93.0 - 1.0],  # exit-day action is carried overnight
            [87.0 / 86.0 - 1.0],
            [88.0 / 87.0 - 1.0],
            [89.0 / 88.0 - 1.0],
            [90.0 / 89.0 - 1.0],
        ])
        fields = {
            "open": opens,
            "close": closes,
            "volume": np.ones_like(opens),
            "realized_total_return": total_returns,
        }
        return PanelData(
            dates,
            ("TEST.XSHG",),
            fields,
            eligible=np.ones_like(opens, dtype=bool),
            buyable=np.ones_like(opens, dtype=bool),
            sellable=np.zeros_like(opens, dtype=bool),
        )

    def test_total_return_target_handles_entry_and_exit_actions(self) -> None:
        panel = self.panel()
        target = forward_total_returns(panel, 2)
        expected = (91.0 / 90.0) * (93.0 / 91.0) * (96.0 / 93.0) * (85.0 / 86.0) - 1.0
        self.assertAlmostEqual(float(target[0, 0]), expected, places=6)
        self.assertGreater(float(target[0, 0]), 0.0)
        self.assertLess(85.0 / 90.0 - 1.0, 0.0)

    def test_one_session_target_and_future_sell_mask(self) -> None:
        panel = self.panel()
        target = forward_total_returns(panel, 1)
        self.assertTrue(np.isfinite(target[0, 0]))
        self.assertFalse(panel.sellable[2, 0])

    def test_decision_filter_does_not_mutate_panel_eligible(self) -> None:
        panel = self.panel()
        before = panel.eligible.copy()
        supplied = np.ones(panel.shape, dtype=bool)
        supplied[0, 0] = False
        forward_total_returns(panel, 1, decision_eligible=supplied)
        np.testing.assert_array_equal(panel.eligible, before)
        self.assertTrue(panel.eligible[0, 0])

    def test_missing_holding_return_stays_missing_without_stale_close_carry(self) -> None:
        panel = self.panel()
        panel.metadata["terminal_event_mask_status"] = (
            "UNAVAILABLE__R0_NARROW_PANEL_HAS_NO_VERIFIED_DELISTED_OR_INVALIDATED_MASK"
        )
        panel.fields["realized_total_return"][2, 0] = np.nan
        target = forward_total_returns(panel, 2)
        self.assertTrue(np.isnan(target[0, 0]))
        diagnostics = panel.metadata["forward_total_return_diagnostics"]["2"]
        self.assertGreater(diagnostics["missing_outcome_count"], 0)
        self.assertEqual(diagnostics["terminal_writeoff_count"], 0)
        self.assertEqual(
            diagnostics["missing_outcome_policy"],
            "NAN__NO_STALE_CLOSE_CARRY_FORWARD",
        )

    def test_missing_exit_open_uses_only_same_session_observed_close(self) -> None:
        panel = self.panel()
        panel.fields["open"][3, 0] = np.nan
        target = forward_total_returns(panel, 2)
        expected = (91.0 / 90.0) * (93.0 / 91.0) * (96.0 / 93.0) - 1.0
        self.assertAlmostEqual(float(target[0, 0]), expected, places=6)
        diagnostics = panel.metadata["forward_total_return_diagnostics"]["2"]
        self.assertGreater(diagnostics["same_session_exit_close_mark_count"], 0)

        panel = self.panel()
        panel.fields["open"][3, 0] = np.nan
        panel.fields["close"][3, 0] = np.nan
        target = forward_total_returns(panel, 2)
        self.assertTrue(np.isnan(target[0, 0]))

    def test_minimum_trading_age_is_causal(self) -> None:
        panel = self.panel()
        panel.eligible[2, 0] = False
        mask = minimum_trading_age_mask(panel, 3)
        self.assertFalse(mask[2, 0])
        self.assertTrue(mask[3, 0])

    def test_winsorized_rank_keeps_missing_and_creates_tail_ties(self) -> None:
        values = np.asarray([[0.0, 1.0, 2.0, 3.0, 1000.0, np.nan]])
        ranked = winsorized_cross_sectional_rank(
            values, lower_quantile=0.25, upper_quantile=0.75
        )
        self.assertTrue(np.isnan(ranked[0, 5]))
        self.assertAlmostEqual(float(ranked[0, 0]), float(ranked[0, 1]))
        self.assertAlmostEqual(float(ranked[0, 3]), float(ranked[0, 4]))

    def test_factor_action_sensitivity_is_mechanical(self) -> None:
        close = Expression.terminal("close")
        open_price = Expression.terminal("open")
        same_day = Expression("safe_div", (close, open_price))
        momentum = Expression(
            "safe_div",
            (
                Expression("delta", (close,), window=20),
                Expression("delay", (close,), window=20),
            ),
        )
        safe = assess_factor_safety(Expression("ts_mean", (same_day,), window=20))
        unsafe = assess_factor_safety(momentum)
        self.assertTrue(safe.survivor_eligible)
        self.assertFalse(unsafe.survivor_eligible)
        self.assertIn("RAW_PRICE_LEVEL_CROSSES_SESSIONS", unsafe.reasons)

        projected = assess_factor_safety(
            momentum, price_convention="CAUSAL_TOTAL_RETURN_PROJECTION"
        )
        self.assertTrue(projected.survivor_eligible)
        self.assertEqual(
            projected.tier, "CAUSAL_TOTAL_RETURN_PRICE_SURVIVOR_ELIGIBLE"
        )

    def test_total_return_projection_removes_ex_rights_price_jump(self) -> None:
        dates = ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04")
        close = np.asarray([[100.0], [90.0], [91.0], [92.0]])
        panel = PanelData(
            dates,
            ("TEST.XSHG",),
            {
                "open": close.copy(),
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": np.ones_like(close),
                "amount": close.copy(),
                "realized_total_return": np.asarray(
                    [[0.0], [0.01], [91.0 / 90.0 - 1.0], [92.0 / 91.0 - 1.0]]
                ),
            },
        )
        result = build_total_return_signal_fields(panel)
        projected = result.fields["close"][:, 0]
        self.assertAlmostEqual(float(projected[1] / projected[0] - 1.0), 0.01, places=6)
        self.assertGreater(float(projected[1]), float(projected[0]))
        self.assertEqual(result.diagnostics["raw_vs_total_return_divergence_rows"], 1)
        self.assertEqual(result.diagnostics["ohlc_order_violations"], 0)
        np.testing.assert_array_equal(panel.fields["close"][:, 0], close[:, 0])

    def test_projection_is_causal_and_resets_after_missing_gap(self) -> None:
        close = np.asarray([[10.0], [11.0], [np.nan], [20.0], [21.0]])
        fields = {
            "open": close.copy(),
            "high": close.copy(),
            "low": close.copy(),
            "close": close.copy(),
            "volume": np.ones_like(close),
            "realized_total_return": np.asarray([[0.0], [0.1], [np.nan], [0.5], [0.05]]),
        }
        panel = PanelData(
            tuple(f"2026-02-{day:02d}" for day in range(1, 6)),
            ("TEST.XSHG",),
            fields,
        )
        before = build_total_return_signal_fields(panel)
        panel.fields["realized_total_return"][4, 0] = 0.5
        after = build_total_return_signal_fields(panel)
        np.testing.assert_allclose(
            before.fields["close"][:4], after.fields["close"][:4], equal_nan=True
        )
        self.assertAlmostEqual(float(before.fields["close"][3, 0]), 100.0)
        self.assertEqual(before.diagnostics["episode_starts"], 2)


if __name__ == "__main__":
    unittest.main()
