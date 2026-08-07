from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = WORKSPACE_ROOT / "control" / "tools"
MODULE_PATH = TOOLS_ROOT / "probe_ricequant_r0_coverage.py"
CONTRACT_PATH = (
    WORKSPACE_ROOT
    / "data"
    / "catalog"
    / "contracts"
    / "ricequant_r0_coverage_probe_v1"
    / "contract.json"
)


def load_module():
    sys.path.insert(0, str(TOOLS_ROOT))
    spec = importlib.util.spec_from_file_location("probe_ricequant_r0_coverage", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ProbeRicequantR0CoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_contract_is_bounded_and_non_formal(self):
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(contract["request_budget"]["maximum_business_api_calls"], 17)
        self.assertEqual(contract["request_budget"]["maximum_incremental_flow_bytes"], 8 * 1024 * 1024)
        self.assertFalse(contract["formal_admission"])
        self.assertFalse(contract["formal_catalog_write"])
        self.assertFalse(contract["research_use"])
        self.assertFalse(contract["bulk_download"])
        self.assertFalse(contract["security"]["credential_value_capture"])

    def test_board_quota_totals(self):
        self.assertEqual(sum(self.module.SMALL_BOARD_QUOTAS.values()), 32)
        self.assertEqual(sum(self.module.LARGE_BOARD_QUOTAS.values()), 256)

    def test_deterministic_board_sample(self):
        import pandas as pd

        rows = []
        for board, count in {"MainBoard": 25, "GEM": 12, "KSH": 8, "BJS": 5}.items():
            for index in range(count):
                rows.append({"order_book_id": f"{board}-{index:03d}", "board_type": board})
        frame = pd.DataFrame(rows)
        quotas = {"MainBoard": 4, "GEM": 3, "KSH": 2, "BJS": 1}
        first = self.module.deterministic_board_sample(frame, quotas, "test")
        second = self.module.deterministic_board_sample(frame, quotas, "test")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 10)
        self.assertEqual(len(first), len(set(first)))

    def test_offline_check_never_calls_api(self):
        result = self.module.offline_check()
        self.assertEqual(result["network_requests"], 0)
        self.assertEqual(result["business_api_calls"], 0)
        self.assertFalse(result["credential_value_captured"])
        self.assertTrue(all(result["entrypoints"].values()))


if __name__ == "__main__":
    unittest.main()
