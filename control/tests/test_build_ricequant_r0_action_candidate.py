from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "control/tools/build_ricequant_r0_action_candidate.py"
SPEC = importlib.util.spec_from_file_location("rq_action_candidate", SCRIPT)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)


class RicequantR0ActionCandidateTests(unittest.TestCase):
    def test_batch_plan_is_deterministic_bounded_and_exact(self) -> None:
        ids = [f"{index:06d}.XSHE" for index in range(1000)]
        first = MOD.build_batches(ids)
        second = MOD.build_batches(reversed(ids))
        self.assertEqual(first, second)
        self.assertEqual(len(first[0]), MOD.SAMPLE_SIZE)
        self.assertTrue(all(len(batch) <= MOD.MAX_BATCH_SIZE for batch in first[1:]))
        flattened = [item for batch in first for item in batch]
        self.assertEqual(len(flattened), len(set(flattened)))
        self.assertEqual(set(flattened), set(ids))

    def test_dividend_normalization_retains_minimum_dates_and_hash(self) -> None:
        index = pd.MultiIndex.from_tuples(
            [("000001.XSHE", pd.Timestamp("2024-06-06"))],
            names=["order_book_id", "declaration_announcement_date"],
        )
        frame = pd.DataFrame(
            {
                "book_closure_date": [pd.Timestamp("2024-06-13")],
                "ex_dividend_date": [pd.Timestamp("2024-06-14")],
                "payable_date": [pd.Timestamp("2024-06-14")],
                "dividend_cash_before_tax": [7.19],
                "round_lot": [10.0],
                "quarter": ["2023q4"],
            },
            index=index,
        )
        rows, rejected = MOD.normalize_frame("dividend", frame)
        self.assertEqual(rejected, 0)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["event_date"], "2024-06-14")
        self.assertEqual(row["announcement_date"], "2024-06-06")
        self.assertEqual(row["registration_date"], "2024-06-13")
        self.assertAlmostEqual(row["cash_per_share"], 0.719)
        self.assertEqual(len(row["source_row_sha256"]), 64)
        self.assertFalse(row["research_eligible"])

    def test_ex_factor_normalization_uses_registration_not_announcement(self) -> None:
        frame = pd.DataFrame(
            {
                "order_book_id": ["600000.XSHG"],
                "ex_factor": [0.95],
                "ex_cum_factor": [2.5],
                "announcement_date": [pd.Timestamp("2025-06-10")],
                "ex_end_date": [pd.Timestamp("2026-01-01")],
            },
            index=pd.Index([pd.Timestamp("2025-06-11")], name="ex_date"),
        )
        rows, _ = MOD.normalize_frame("ex_factor", frame)
        self.assertEqual(rows[0]["announcement_date"], None)
        self.assertEqual(rows[0]["registration_date"], "2025-06-10")
        self.assertEqual(rows[0]["segment"], "TAIL_2025_20260803")

    def test_limits_and_redaction_are_fail_closed(self) -> None:
        self.assertEqual(MOD.HARD_LIMIT_BYTES, 40 * 1024 * 1024)
        self.assertLess(MOD.SOFT_STOP_BYTES, MOD.HARD_LIMIT_BYTES)
        error = MOD.safe_error(RuntimeError("tcp://secret-host:1"))
        self.assertNotIn("secret-host", error["message"])


if __name__ == "__main__":
    unittest.main()
