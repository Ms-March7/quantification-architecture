from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from capital_flow_attribution.attribution import (  # noqa: E402
    build_migration_graph,
    partial_attribution,
    switchability_bounds,
)
from capital_flow_attribution.backtest import (  # noqa: E402
    evaluate_backtest,
    prepare_backtest_frame,
    walk_forward_exposure,
)
from capital_flow_attribution.g1_adapters import (  # noqa: E402
    build_etf_aum_ledger,
    nav_bundle_matches,
    parse_hkex_daily,
    parse_jsonp,
)
from capital_flow_attribution.exchange_adapters import sse_hundred_million_hkd  # noqa: E402
from capital_flow_attribution.research_pipeline import _coverage_rows  # noqa: E402


class G1AdapterTests(unittest.TestCase):
    def test_jsonp_parser_does_not_execute_javascript(self) -> None:
        payload = parse_jsonp('callback([[{"rzye": 10}], "2026-01-01"])')
        self.assertEqual(payload[0][0]["rzye"], 10)
        self.assertEqual(parse_jsonp("callback([[{}], '2026-01-01'])")[1], "2026-01-01")
        with self.assertRaises(ValueError):
            parse_jsonp("alert(1); callback({})")

    def test_hkex_units_and_directions(self) -> None:
        payload = [
            {
                "market": "SZSE Northbound",
                "tradingDay": 1,
                "content": [{"table": {"schema": [["Total Turnover", "ETF Turnover"]], "tr": [
                    {"td": [["2.50"]]}, {"td": [["0.25"]]}
                ]}}],
            },
            {
                "market": "SZSE Southbound",
                "tradingDay": 1,
                "content": [{"table": {"schema": [["Total Turnover", "Buy Turnover", "Sell Turnover"]], "tr": [
                    {"td": [["3.00"]]}, {"td": [["1.25"]]}, {"td": [["1.75"]]}
                ]}}],
            },
        ]
        rows = parse_hkex_daily("tabData = " + json.dumps(payload) + ";", "2026-01-02")
        self.assertEqual(rows[0]["northbound_total_amount_cny"], 2_500_000.0)
        self.assertEqual(rows[1]["buy_amount_hkd"], 1_250_000.0)

    def test_etf_decomposition_keeps_secondary_ceiling(self) -> None:
        shares = [
            {"date": "2026-01-01", "symbol": "510010.SH", "shares": 100.0},
            {"date": "2026-01-02", "symbol": "510010.SH", "shares": 110.0},
        ]
        nav = [
            {"date": "2026-01-01", "symbol": "510010.SH", "nav": 1.0},
            {"date": "2026-01-02", "symbol": "510010.SH", "nav": 1.1},
        ]
        rows = build_etf_aum_ledger(shares, nav)
        self.assertEqual(rows[1]["quality"], "OK")
        self.assertAlmostEqual(rows[1]["flow_component_cny"], 11.0)
        self.assertAlmostEqual(rows[1]["market_component_cny"], 10.0)
        self.assertEqual(rows[1]["promotion_ceiling"], "RESEARCH_ONLY")

    def test_sse_southbound_display_unit_becomes_absolute_hkd(self) -> None:
        self.assertEqual(sse_hundred_million_hkd("398.56"), 39_856_000_000.0)

    def test_nav_cache_requires_same_window_and_symbol_set(self) -> None:
        bundle = {"start": "2026-01-01", "end": "2026-01-02", "responses": [{"code": "510010"}]}
        self.assertTrue(nav_bundle_matches(bundle, ["510010"], "2026-01-01", "2026-01-02"))
        self.assertFalse(nav_bundle_matches(bundle, ["510010", "510050"], "2026-01-01", "2026-01-02"))
        self.assertFalse(nav_bundle_matches(bundle, ["510010"], "2026-01-01", "2026-01-03"))

    def test_etf_share_coverage_detects_internal_date_hole(self) -> None:
        dates = ("2026-01-01", "2026-01-02", "2026-01-03")
        etf = [
            {"date": dates[0], "symbol": "510010.SH", "nav": 1.0, "quality": "MISSING"},
            {"date": dates[2], "symbol": "510010.SH", "nav": 1.0, "quality": "OK"},
        ]
        coverage = _coverage_rows(dates, [], etf, [], {"successful_symbols": 1, "requested_symbols": 1})
        shares = next(row for row in coverage if row["dataset"] == "etf_exchange_shares_20d")
        self.assertEqual(shares["observed_rows"], 2)
        self.assertEqual(shares["expected_rows"], 3)
        self.assertEqual(shares["quality"], "MISSING")


class AttributionTests(unittest.TestCase):
    def test_unknown_is_preserved_as_interval(self) -> None:
        rows = partial_attribution(100.0, {"observed": 30.0}, {"proxy": (10.0, 20.0)})
        unknown = next(row for row in rows if row["bucket"] == "NOT_IDENTIFIED")
        self.assertEqual((unknown["lower"], unknown["upper"]), (50.0, 60.0))

    def test_over_attribution_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            partial_attribution(10.0, {"observed": 11.0}, {})

    def test_graph_and_switchability_bounds(self) -> None:
        graph = build_migration_graph([{"source": "TECH", "target": "ETF", "lower": 1, "upper": 2}])
        self.assertEqual(graph["TECH"]["ETF"]["upper"], 2.0)
        bounds = switchability_bounds(100.0, 100.0, 1)
        self.assertLessEqual(bounds["switchable_fraction_lower"], bounds["switchable_fraction_upper"])

    def test_duplicate_migration_edge_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_migration_graph(
                [
                    {"source": "TECH", "target": "ETF", "lower": 1, "upper": 2},
                    {"source": "TECH", "target": "ETF", "lower": 2, "upper": 3},
                ]
            )

    def test_proxy_upper_bound_is_jointly_feasible(self) -> None:
        rows = partial_attribution(100.0, {"observed": 80.0}, {"a": (10.0, 50.0), "b": (5.0, 50.0)})
        proxy = {row["bucket"]: row for row in rows}
        self.assertEqual(proxy["a"]["upper"], 15.0)
        self.assertEqual(proxy["b"]["upper"], 10.0)
        self.assertTrue(all(row["constraint_group"] == "total_conservation" for row in rows))


class BacktestTests(unittest.TestCase):
    def _frame(self) -> pd.DataFrame:
        size = 2200
        dates = pd.bdate_range("2015-01-01", periods=size).strftime("%Y-%m-%d")
        margin = pd.DataFrame(
            {
                "date": dates,
                "financing_balance_cny": 1000 * np.exp(np.linspace(0, 1, size) + 0.01 * np.sin(np.arange(size))),
                "financing_buy_cny": 100 + 10 * np.cos(np.arange(size) / 10),
            }
        )
        market = pd.DataFrame({"date": dates, "market_return": 0.001 * np.sin(np.arange(size) / 7)})
        return prepare_backtest_frame(margin, market)

    def test_walk_forward_has_gap_and_execution_lag(self) -> None:
        frame = self._frame()
        predictions = walk_forward_exposure(frame, n_splits=4, gap=5, test_size=252)
        self.assertTrue((predictions["execution_date"] > predictions["signal_date"]).all())
        self.assertEqual(len(predictions), 1008)

    def test_cost_is_charged_on_exposure_changes(self) -> None:
        frame = pd.DataFrame(
            {
                "date": ["a", "b"],
                "signal_date": ["a", "b"],
                "execution_date": ["b", "c"],
                "fold": [1, 1],
                "exposure": [1.0, 0.0],
                "forward_return": [0.01, 0.01],
            }
        )
        result, _ = evaluate_backtest(frame, one_way_cost_bps=10)
        self.assertAlmostEqual(result["cost"].sum(), 0.002)

    def test_incomplete_exchange_day_does_not_compress_execution_calendar(self) -> None:
        size = 40
        dates = pd.bdate_range("2020-01-01", periods=size).strftime("%Y-%m-%d")
        market = pd.DataFrame({"date": dates, "market_return": np.arange(size) / 10000})
        margin = pd.DataFrame(
            {
                "date": dates,
                "financing_balance_cny": np.arange(size) + 1000.0,
                "financing_buy_cny": np.arange(size) + 100.0,
                "exchange_count": 2,
            }
        )
        margin.loc[25, "exchange_count"] = 1
        frame = prepare_backtest_frame(margin, market)
        expected = dict(zip(dates, pd.Series(dates).shift(-2)))
        comparable = frame.dropna(subset=["execution_date"])
        self.assertTrue(all(expected[row.date] == row.execution_date for row in comparable.itertuples()))
        missing = frame.loc[frame["date"] == dates[25]].iloc[0]
        self.assertFalse(missing["signal_available"])
        self.assertTrue(pd.isna(missing["margin_growth_20"]))

    def test_missing_signal_day_is_flat_and_kept_in_oos_returns(self) -> None:
        frame = self._frame()
        frame.loc[frame.index[-300], ["margin_growth_20", "buy_intensity_20"]] = np.nan
        frame.loc[frame.index[-300], "signal_available"] = False
        missing_date = frame.loc[frame.index[-300], "date"]
        predictions = walk_forward_exposure(frame, n_splits=4, gap=5, test_size=252)
        row = predictions.loc[predictions["date"] == missing_date].iloc[0]
        self.assertFalse(row["signal_available"])
        self.assertEqual(row["exposure"], 0.0)


if __name__ == "__main__":
    unittest.main()
