from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = WORKSPACE_ROOT / "control" / "tools"
MODULE_PATH = TOOLS_ROOT / "probe_ricequant_r1_financial_pit.py"
CONTRACT_PATH = (
    WORKSPACE_ROOT
    / "data"
    / "catalog"
    / "contracts"
    / "ricequant_r1_financial_pit_probe_v1"
    / "contract.json"
)


def load_module():
    sys.path.insert(0, str(TOOLS_ROOT))
    spec = importlib.util.spec_from_file_location(
        "probe_ricequant_r1_financial_pit", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ProbeRicequantR1FinancialPitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_contract_is_strictly_bounded_and_non_formal(self):
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(contract["request_budget"]["maximum_total_business_api_calls"], 12)
        self.assertEqual(contract["request_budget"]["financial_capability_calls"], 10)
        self.assertEqual(contract["request_budget"]["maximum_order_book_ids_per_call"], 2)
        self.assertEqual(contract["request_budget"]["maximum_quarters_per_call"], 1)
        self.assertFalse(contract["formal_admission"])
        self.assertFalse(contract["formal_catalog_write"])
        self.assertFalse(contract["research_use"])
        self.assertFalse(contract["bulk_download"])
        self.assertFalse(contract["security"]["credential_value_capture"])
        self.assertFalse(contract["security"]["api_payload_persistence"])

    def test_exact_call_design(self):
        self.assertEqual(len(self.module.CORE13_DATES), 4)
        self.assertEqual(len(self.module.CORE15_EXPRESS_DATES), 2)
        self.assertEqual(len(self.module.CORE15_FORECAST_DATES), 2)
        self.assertEqual(len(self.module.CORE16_DATES), 2)
        self.assertEqual(
            len(self.module.CORE13_DATES)
            + len(self.module.CORE15_EXPRESS_DATES)
            + len(self.module.CORE15_FORECAST_DATES)
            + len(self.module.CORE16_DATES),
            self.module.MAX_FINANCIAL_CALLS,
        )

    def test_temporal_summary_persists_only_counts_and_hash_differences(self):
        import pandas as pd

        first = pd.DataFrame({"value": [1.0]}, index=pd.Index(["a"], name="id"))
        second = pd.DataFrame(
            {"value": [1.0, 2.0]}, index=pd.Index(["a", "b"], name="id")
        )
        result = self.module.temporal_sequence_summary(
            {"2020-01-01": first, "2020-01-02": second}
        )
        self.assertEqual(result["row_counts"], {"2020-01-01": 1, "2020-01-02": 2})
        self.assertTrue(result["row_counts_monotonic"])
        self.assertEqual(result["pairwise_hash_differences"][0]["added_row_hashes"], 1)
        self.assertNotIn("value", json.dumps(result))

    def test_offline_check_never_calls_api(self):
        result = self.module.offline_check()
        self.assertEqual(result["network_requests"], 0)
        self.assertEqual(result["business_api_calls"], 0)
        self.assertFalse(result["credential_value_captured"])
        self.assertTrue(all(result["entrypoints"].values()))


if __name__ == "__main__":
    unittest.main()
