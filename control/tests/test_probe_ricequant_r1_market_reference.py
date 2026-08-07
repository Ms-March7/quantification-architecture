from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = WORKSPACE_ROOT / "control" / "tools"
MODULE_PATH = TOOLS_ROOT / "probe_ricequant_r1_market_reference.py"
CONTRACT_PATH = (
    WORKSPACE_ROOT
    / "data"
    / "catalog"
    / "contracts"
    / "ricequant_r1_market_reference_probe_v1"
    / "contract.json"
)


def load_module():
    sys.path.insert(0, str(TOOLS_ROOT))
    spec = importlib.util.spec_from_file_location(
        "probe_ricequant_r1_market_reference", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ProbeRicequantR1MarketReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_contract_is_strictly_bounded_and_non_formal(self):
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        budget = contract["request_budget"]
        self.assertEqual(budget["maximum_total_business_api_calls"], 11)
        self.assertEqual(budget["fresh_process_quota_calls"], 2)
        self.assertEqual(budget["capability_calls"], 9)
        self.assertEqual(budget["retry_count"], 0)
        self.assertLessEqual(budget["maximum_order_book_ids_per_call"], 1)
        self.assertFalse(contract["formal_admission"])
        self.assertFalse(contract["formal_catalog_write"])
        self.assertFalse(contract["research_use"])
        self.assertFalse(contract["bulk_download"])
        self.assertFalse(contract["security"]["credential_value_capture"])
        self.assertFalse(contract["security"]["api_payload_persistence"])

    def test_exact_call_plan(self):
        self.assertEqual(len(self.module.CAPABILITY_NAMES), 9)
        self.assertEqual(self.module.MAX_CAPABILITY_CALLS, 9)
        self.assertEqual(len(self.module.EXPOSURE_FACTORS), 3)

    def test_frame_summary_contains_no_raw_values(self):
        import pandas as pd

        frame = pd.DataFrame(
            {"value": [123.456], "label": ["raw-payload-marker"]},
            index=pd.DatetimeIndex(["2026-08-03"], name="date"),
        )
        summary = self.module.frame_summary(frame)
        encoded = json.dumps(summary, ensure_ascii=False)
        self.assertEqual(summary["rows"], 1)
        self.assertEqual(summary["fields"], ["value", "label"])
        self.assertNotIn("123.456", encoded)
        self.assertNotIn("raw-payload-marker", encoded)
        self.assertEqual(len(summary["canonical_sha256"]), 64)

    def test_permission_denied_continues_but_unexpected_error_halts(self):
        class PermissionDenied(Exception):
            pass

        runner = self.module.BoundedProbeRunner()
        first = runner.call(
            "core07_shares", lambda: (_ for _ in ()).throw(PermissionDenied("permission denied"))
        )
        self.assertIsNone(first)
        self.assertFalse(runner.halted)
        self.assertEqual(runner.results["core07_shares"]["permission"], "DENIED")
        runner.call(
            "core08_turnover", lambda: (_ for _ in ()).throw(ValueError("bad request"))
        )
        self.assertTrue(runner.halted)
        runner.call("core10_index_components", lambda: ["must-not-run"])
        self.assertEqual(
            runner.results["core10_index_components"]["status"], "SKIPPED_AFTER_HALT"
        )
        self.assertEqual(runner.calls, 2)

    def test_offline_check_never_calls_api(self):
        result = self.module.offline_check()
        self.assertEqual(result["network_requests"], 0)
        self.assertEqual(result["business_api_calls"], 0)
        self.assertFalse(result["credential_value_captured"])
        self.assertTrue(all(result["entrypoints"].values()))


if __name__ == "__main__":
    unittest.main()
