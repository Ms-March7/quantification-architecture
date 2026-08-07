from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tools/build_ricequant_core11_industry_history_candidate.py"
SPEC = importlib.util.spec_from_file_location("rq_core11_candidate", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class FakeFrame:
    pass


class RicequantCore11CandidateTests(unittest.TestCase):
    def test_safe_error_redacts_connection_and_secret(self) -> None:
        error = MODULE.safe_error(RuntimeError("tcp://secret-host password=abc"))
        self.assertNotIn("secret-host", error["message"])
        self.assertNotIn("abc", error["message"])

    def test_quota_numeric_extraction(self) -> None:
        fields = MODULE.numeric_fields({"rqdata": {"bytes_used": 12, "bytes_limit": 20}, "days": 3})
        self.assertEqual(MODULE.quota_value(fields, "bytes_used"), 12.0)
        self.assertEqual(MODULE.quota_value(fields, "bytes_limit"), 20.0)

    def test_overlap_detection_uses_half_open_intervals(self) -> None:
        rows = [
            {"order_book_id": "000001.XSHE", "first_industry_code": "10", "start_date": "2020-01-01", "cancel_date": "2021-01-01"},
            {"order_book_id": "000001.XSHE", "first_industry_code": "20", "start_date": "2021-01-01", "cancel_date": "2200-12-31"},
        ]
        self.assertEqual(MODULE.overlap_count(rows), 0)
        rows[1]["start_date"] = "2020-12-31"
        self.assertEqual(MODULE.overlap_count(rows), 1)

    def test_contract_is_inspection_only(self) -> None:
        import json

        contract = json.loads(MODULE.CONTRACT.read_text(encoding="utf-8"))
        self.assertEqual(contract["output"]["purpose_ceiling"], "INSPECTION_ONLY")
        self.assertFalse(contract["output"]["formal_admission"])
        self.assertFalse(contract["output"]["research_use"])


if __name__ == "__main__":
    unittest.main()
