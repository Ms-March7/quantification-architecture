from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "control/tools/build_ricequant_tail_return_calibration.py"
SPEC = importlib.util.spec_from_file_location("tail_return_calibration", SCRIPT)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)


class TailReturnCalibrationTests(unittest.TestCase):
    def test_exact_target_dates_and_formula_boundary(self) -> None:
        factors = [
            {"event_date": "2026-07-30", "security_id": "000001.XSHE", "ex_factor": 1.1, "ex_cum_factor": 2.0, "registration_date": "2026-07-29", "source_row_sha256": "a" * 64},
            {"event_date": "2026-07-28", "security_id": "000002.XSHE", "ex_factor": 1.2, "ex_cum_factor": 2.0, "registration_date": "2026-07-27", "source_row_sha256": "b" * 64},
        ]
        actions = [{"event_date": "2026-07-30", "event_type": "DIVIDEND"}]
        payload = MOD.build_payload(factors, actions)
        self.assertEqual(payload["factor_row_count"], 1)
        self.assertEqual(payload["dates"]["2026-07-30"]["factor_row_count"], 1)
        self.assertEqual(payload["dates"]["2026-08-03"]["factor_row_count"], 0)
        self.assertIn("ex_factor_t", payload["formula_candidate"])
        self.assertFalse(payload["research_eligible"])


if __name__ == "__main__":
    unittest.main()
