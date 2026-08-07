from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
TOOLS = WORKSPACE / "control/tools"
MODULE_PATH = TOOLS / "build_ricequant_r0_pv_tail_delta.py"
CONTRACT_PATH = (
    WORKSPACE / "data/catalog/contracts/ricequant_r0_pv_tail_delta_v1/contract.json"
)


def load_module():
    sys.path.insert(0, str(TOOLS))
    spec = importlib.util.spec_from_file_location("build_ricequant_r0_pv_tail_delta", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RicequantR0PVTailDeltaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_contract_is_strictly_bounded_and_non_formal(self):
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(contract["request_budget"]["retry_count"], 0)
        self.assertEqual(contract["request_budget"]["maximum_quota_bytes"], 30 * 1024 * 1024)
        self.assertEqual(contract["request_budget"]["maximum_price_dates"], 5)
        self.assertEqual(contract["request_budget"]["maximum_price_fields"], 9)
        self.assertFalse(contract["persistence"]["formal_catalog_write"])
        self.assertFalse(contract["persistence"]["source_registry_write"])
        self.assertFalse(contract["persistence"]["roadmap_write"])
        self.assertFalse(contract["persistence"]["factor_run"])
        self.assertFalse(contract["formal_boundaries"]["source_admission"])
        self.assertFalse(contract["formal_boundaries"]["research_ready"])

    def test_scope_is_exact_and_bjse_excluded(self):
        self.assertEqual(len(self.module.DELTA_DATES), 4)
        self.assertEqual(len(self.module.REQUEST_DATES), 5)
        self.assertEqual(len(self.module.PRICE_FIELDS), 9)
        self.assertTrue(self.module.is_shsz_a_share_id("600000.XSHG"))
        self.assertTrue(self.module.is_shsz_a_share_id("300750.XSHE"))
        self.assertFalse(self.module.is_shsz_a_share_id("920001.BJSE"))
        self.assertFalse(self.module.is_shsz_a_share_id("900901.XSHG"))
        self.assertFalse(self.module.is_shsz_a_share_id("200001.XSHE"))

    def test_risk_warning_marker_is_mechanical_and_unknown_safe(self):
        self.assertTrue(self.module.risk_warning_marker("*ST样本"))
        self.assertTrue(self.module.risk_warning_marker("SST样本"))
        self.assertTrue(self.module.risk_warning_marker("PT样本"))
        self.assertFalse(self.module.risk_warning_marker("正常简称"))
        self.assertIsNone(self.module.risk_warning_marker(None))
        self.assertIsNone(self.module.risk_warning_marker(""))

    def test_safe_error_redacts_credentials_and_uris(self):
        error = self.module.safe_error(
            RuntimeError("tcp://secret@example:123 license=abc token=xyz")
        )["message"]
        self.assertNotIn("secret@example", error)
        self.assertNotIn("license=abc", error)
        self.assertNotIn("token=xyz", error)

    def test_offline_check_is_read_only_and_hash_bound(self):
        result = self.module.offline_check()
        self.assertEqual(result["status"], "OFFLINE_CHECK_PASS")
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(result["writes"], 0)
        self.assertTrue(all(result["checks"].values()))


if __name__ == "__main__":
    unittest.main()
