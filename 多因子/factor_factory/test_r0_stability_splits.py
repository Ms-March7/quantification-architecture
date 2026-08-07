from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import numpy as np


SCRIPT = Path(__file__).resolve().parents[1] / "tools/run_r0_c_v1_1_stability.py"
SPEC = importlib.util.spec_from_file_location("r0_stability", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class R0StabilitySplitTests(unittest.TestCase):
    def test_rolling_split_applies_purge_and_test_start_embargo(self) -> None:
        dates = tuple(
            f"{year:04d}-{month:02d}-{day:02d}"
            for year in range(2005, 2011)
            for month in range(1, 13)
            for day in range(1, 21)
        )
        indices = np.arange(len(dates), dtype=np.int64)
        splits = MODULE.build_splits(
            dates, indices, train_years=3,
            first_test_year=2009, last_test_year=2010,
            purge_sessions=20, embargo_sessions=5,
        )
        self.assertEqual(len(splits), 2)
        first = splits[0]
        self.assertEqual(first["test_start"], "2009-01-06")
        self.assertLess(first["train_end"], first["test_start"])
        self.assertGreaterEqual(len(first["train"]), 400)

    def test_summary_reports_cost_survival(self) -> None:
        folds = [
            {
                "test_year": 2020,
                "test_mean_ic_oriented": 0.02,
                "orientation": 1,
                "evaluable_date_ratio": 1.0,
                "cost_adjusted_top_excess": {"15": 0.001, "30": 0.0005},
            },
            {
                "test_year": 2021,
                "test_mean_ic_oriented": -0.01,
                "orientation": -1,
                "evaluable_date_ratio": 0.9,
                "cost_adjusted_top_excess": {"15": -0.001, "30": -0.002},
            },
        ]
        summary = MODULE.summarize_folds(folds, [15.0, 30.0])
        self.assertEqual(summary["folds"], 2)
        self.assertEqual(summary["positive_test_ic_ratio"], 0.5)
        self.assertEqual(summary["costs"]["15"]["positive_fold_ratio"], 0.5)


if __name__ == "__main__":
    unittest.main()
