from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

import pandas as pd


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = WORKSPACE_ROOT / "control/tools"
MODULE_PATH = TOOLS_ROOT / "build_rqdata_r0_shsz_core01_core02_candidate.py"
CONTRACT_PATH = (
    WORKSPACE_ROOT
    / "data/catalog/contracts/rqdata_r0_shsz_core01_core02_candidate_v1/contract.json"
)


def load_module():
    sys.path.insert(0, str(TOOLS_ROOT))
    spec = importlib.util.spec_from_file_location("build_rqdata_core01_core02", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BuildRqdataCore01Core02CandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_contract_is_non_formal_and_two_call_bounded(self):
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(contract["request_budget"]["maximum_business_api_calls"], 2)
        self.assertFalse(contract["catalog_write"])
        self.assertFalse(contract["research_use"])
        self.assertFalse(contract["credential_value_capture"])
        self.assertEqual(contract["scope"]["complete_through"], "2026-08-03")

    def test_master_normalization_is_minimal_deterministic_and_scope_bounded(self):
        frame = pd.DataFrame(
            [
                {
                    "order_book_id": "600000.XSHG",
                    "symbol": "浦发银行",
                    "special_type": "Normal",
                    "exchange": "XSHG",
                    "status": "Active",
                    "type": "CS",
                    "listed_date": "1999-11-10",
                    "de_listed_date": "0000-00-00",
                    "board_type": "MainBoard",
                },
                {
                    "order_book_id": "920001.BJSE",
                    "symbol": "excluded",
                    "special_type": "Normal",
                    "exchange": "BJSE",
                    "status": "Active",
                    "type": "CS",
                    "listed_date": "2024-01-01",
                    "de_listed_date": "0000-00-00",
                    "board_type": "BJS",
                },
                {
                    "order_book_id": "001999.XSHE",
                    "symbol": "future",
                    "special_type": "Normal",
                    "exchange": "XSHE",
                    "status": "Active",
                    "type": "CS",
                    "listed_date": "2026-08-04",
                    "de_listed_date": "0000-00-00",
                    "board_type": "MainBoard",
                },
            ]
        )
        rows, stats = self.module.normalize_master(frame)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["local_code"], "600000")
        self.assertTrue(rows[0]["episode_id"].startswith("rqdata-listing-episode:v1:"))
        self.assertIsNone(rows[0]["de_listed_date"])
        self.assertEqual(stats["rqdata_xshg_xshe_cs_rows_before_window_filter"], 2)

    def test_old_exclusions_are_exact_joined_and_non_a_is_fail_closed(self):
        master = [
            {
                "mic": "XSHG",
                "local_code": "600000",
                "episode_id": "e1",
                "listed_date": "1999-11-10",
                "de_listed_date": None,
                "symbol": "浦发银行",
                "board_type": "MainBoard",
                "special_type": "Normal",
            }
        ]
        ledger = [
            {
                "mic": "XSHG",
                "local_code_observed": "600000",
                "exclusion_reasons": ["OLD_CONFLICT"],
                "pv_row_count": 1,
                "pv_first_date": "2005-01-04",
                "pv_last_date": "2005-01-04",
            },
            {
                "mic": "XSHG",
                "local_code_observed": "900935",
                "exclusion_reasons": ["OUTSIDE"],
                "pv_row_count": 0,
                "pv_first_date": None,
                "pv_last_date": None,
            },
        ]
        joined, remaining, stats = self.module.join_exclusion_ledger(ledger, master)
        self.assertEqual(stats["exact_unique_matches"], 1)
        self.assertEqual(stats["still_unmatched"], 1)
        self.assertEqual(joined[0]["join_status"], "EXACT_UNIQUE_MATCH")
        self.assertEqual(
            remaining[0]["classification"], "NON_A_SHARE__XSHG_B_SHARE_900_NAMESPACE"
        )

    def test_calendar_reconciliation_separates_base_and_increment(self):
        rq_dates = ["2005-01-04", "2024-12-31", "2025-01-02", "2026-08-03"]
        result = self.module.reconcile_calendar(
            rq_dates, ["2005-01-04", "2024-12-31"]
        )
        self.assertTrue(result["baseline_set_equal"])
        self.assertEqual(result["incremental_2025_2026_count"], 2)

    def test_error_redacts_connection_values(self):
        result = self.module.safe_error(
            ValueError("failed tcp://license:sensitive@example.invalid:16011")
        )
        self.assertNotIn("sensitive", result["message"])
        self.assertIn("<redacted>", result["message"])


if __name__ == "__main__":
    unittest.main()
