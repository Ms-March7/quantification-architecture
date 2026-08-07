from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_flow_attribution.accounting import (  # noqa: E402
    decompose_etf_aum,
    implied_margin_repayment,
    reconcile_margin,
    validate_point_in_time,
)


class AccountingTests(unittest.TestCase):
    def test_margin_identity_passes(self) -> None:
        result = reconcile_margin(1000, 1030, 80, 50)
        self.assertEqual(result.quality, "OK")
        self.assertAlmostEqual(result.residual or 0.0, 0.0)

    def test_margin_conflict_is_visible(self) -> None:
        result = reconcile_margin(1000, 1200, 80, 50)
        self.assertEqual(result.quality, "CONFLICT")

    def test_implied_repayment(self) -> None:
        self.assertEqual(implied_margin_repayment(1000, 1030, 80), 50)

    def test_etf_decomposition(self) -> None:
        result = decompose_etf_aum(100, 110, 2.0, 2.1)
        self.assertEqual(result["quality"], "OK")
        self.assertAlmostEqual(result["delta_aum"], 31.0)
        self.assertAlmostEqual(result["flow_component"], 21.0)
        self.assertAlmostEqual(result["market_component"], 10.0)

    def test_etf_missing_nav_refuses_decomposition(self) -> None:
        result = decompose_etf_aum(100, 110, 2.0, None)
        self.assertEqual(result["quality"], "MISSING")
        self.assertEqual(result["evidence_state"], "NOT_IDENTIFIED")

    def test_future_available_at_is_conflict(self) -> None:
        self.assertEqual(validate_point_in_time("2026-07-16T09:00:00+08:00", "2026-07-15T09:00:00+08:00"), "CONFLICT")


if __name__ == "__main__":
    unittest.main()
