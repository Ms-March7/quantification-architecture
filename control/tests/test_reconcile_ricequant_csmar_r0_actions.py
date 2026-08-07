from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "control/tools/reconcile_ricequant_csmar_r0_actions.py"
SPEC = importlib.util.spec_from_file_location("rq_csmar_action_reconcile", SCRIPT)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)


class RicequantCsmarActionReconcileTests(unittest.TestCase):
    def test_action_coverage_conflict_and_unmatched_are_separate(self) -> None:
        rq = pd.DataFrame(
            [
                {"local_security_code": "000001", "event_date": "2024-06-14", "event_type": "DIVIDEND", "cash_per_share": 0.5, "split_increment": None, "allotment_proportion": None, "allotted_proportion": None},
                {"local_security_code": "000002", "event_date": "2024-07-01", "event_type": "SPLIT", "cash_per_share": None, "split_increment": 0.2, "allotment_proportion": None, "allotted_proportion": None},
            ]
        )
        cs = pd.DataFrame(
            [
                {"local_security_code": "000001", "event_date": "2024-06-14", "action_type_candidate": "CASH_DIVIDEND", "csmar_cash_per_share": 0.5, "csmar_bonus_shares": None, "csmar_split_increment": None, "csmar_rights_proportion": None, "csmar_rights_price": None},
                {"local_security_code": "000003", "event_date": "2024-08-01", "action_type_candidate": "RIGHTS", "csmar_cash_per_share": None, "csmar_bonus_shares": None, "csmar_split_increment": None, "csmar_rights_proportion": 0.3, "csmar_rights_price": 5.0},
            ]
        )
        result, samples = MOD.reconcile_actions(rq, cs, "BASE", "2024-01-01", "2024-12-31")
        self.assertEqual(result["matched_security_event_keys"], 1)
        self.assertEqual(result["rqdata_only_security_event_keys"], 1)
        self.assertEqual(result["csmar_only_security_event_keys"], 1)
        self.assertEqual(result["term_reconciliation"]["DIVIDEND"]["numeric_matches"], 1)
        self.assertEqual(len(samples), 2)

    def test_factor_numeric_conflict_is_not_overwritten(self) -> None:
        rq = pd.DataFrame([{"local_security_code": "000001", "event_date": "2024-06-14", "ex_factor": 0.9}])
        cs = pd.DataFrame([{"local_security_code": "000001", "event_date": "2024-06-14", "csmar_forward_factor": 1.25, "csmar_backward_factor": 0.8}])
        result, _ = MOD.reconcile_factors(rq, cs, "BASE", "2024-01-01", "2024-12-31")
        self.assertEqual(result["matched_security_event_keys"], 1)
        self.assertEqual(result["numeric_conflicts"], 1)
        self.assertEqual(result["numeric_matches"], 0)


if __name__ == "__main__":
    unittest.main()
