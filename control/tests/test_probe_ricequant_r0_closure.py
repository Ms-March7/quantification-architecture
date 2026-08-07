from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = WORKSPACE_ROOT / "control" / "tools"
MODULE_PATH = TOOLS_ROOT / "probe_ricequant_r0_closure.py"
CONTRACT_PATH = (
    WORKSPACE_ROOT
    / "data"
    / "catalog"
    / "contracts"
    / "ricequant_r0_closure_probe_v1"
    / "contract.json"
)


def load_module():
    sys.path.insert(0, str(TOOLS_ROOT))
    spec = importlib.util.spec_from_file_location("probe_ricequant_r0_closure", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ProbeRicequantR0ClosureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_contract_is_bounded_and_non_formal(self):
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(contract["request_budget"]["maximum_business_api_calls"], 17)
        self.assertEqual(contract["request_budget"]["maximum_price_order_book_ids"], 4)
        self.assertFalse(contract["formal_admission"])
        self.assertFalse(contract["formal_catalog_write"])
        self.assertFalse(contract["research_use"])
        self.assertFalse(contract["bulk_download"])

    def test_error_redacts_license_uri(self):
        error = ValueError("failed tcp://license:sensitive@example.invalid:16011")
        result = self.module.safe_error(error)
        self.assertEqual(result["type"], "ValueError")
        self.assertNotIn("sensitive", result["message"])
        self.assertIn("<redacted>", result["message"])

    def test_numeric_summary_drops_sensitive_keys(self):
        result = self.module.numeric_summary(
            {"used": 12, "remaining": 34, "license": "secret", "username": "name"}
        )
        self.assertEqual(result, {"used": 12, "remaining": 34})

    def test_call_budget_is_hard(self):
        runner = self.module.ProbeRunner(object())
        runner.calls = self.module.MAX_BUSINESS_API_CALLS
        with self.assertRaisesRegex(RuntimeError, "budget exceeded"):
            runner.call("overflow", lambda: None)

    def test_empty_frame_is_not_reported_as_substantive_pass(self):
        runner = self.module.ProbeRunner(object())
        runner.call("empty", lambda: None)
        self.assertEqual(runner.results["empty"]["status"], "PASS_EMPTY")

    def test_range_index_is_not_coerced_to_1970_date(self):
        import pandas as pd

        result = self.module.frame_summary(pd.DataFrame({"value": [1, 2]}))
        self.assertNotIn("index_date_min", result)
        self.assertNotIn("index_date_max", result)

    def test_offline_check_never_calls_api(self):
        result = self.module.offline_check()
        self.assertEqual(result["network_requests"], 0)
        self.assertEqual(result["business_api_calls"], 0)
        self.assertFalse(result["license_value_captured"])
        self.assertTrue(all(result["entrypoints"].values()))


if __name__ == "__main__":
    unittest.main()
