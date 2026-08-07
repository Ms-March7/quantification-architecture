from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from capital_flow_attribution.open_source_adapters import AKShareAdapter, TushareAdapter  # noqa: E402
from capital_flow_attribution.reconciliation import reconcile_records  # noqa: E402


class FakeAKShare:
    @staticmethod
    def stock_info_a_code_name() -> pd.DataFrame:
        return pd.DataFrame([{"code": "600000", "name": "浦发银行"}, {"code": "430047", "name": "诺思兰德"}])


class FakeTushareClient:
    @staticmethod
    def stock_basic(**_: object) -> pd.DataFrame:
        return pd.DataFrame([{"ts_code": "600000.SH", "symbol": "600000", "name": "浦发银行"}])


class FakeFallbackAKShare:
    @staticmethod
    def stock_info_a_code_name() -> pd.DataFrame:
        raise ConnectionError("primary source unavailable")

    @staticmethod
    def stock_zh_a_spot_em() -> pd.DataFrame:
        raise ConnectionError("secondary source unavailable")

    @staticmethod
    def stock_zh_a_spot() -> pd.DataFrame:
        return pd.DataFrame([{"garbled_code": "bj920000", "garbled_name": "安徽凤凰"}])


class OptionalAdapterTests(unittest.TestCase):
    def test_akshare_security_master_normalizes_exchange(self) -> None:
        result = AKShareAdapter(FakeAKShare).security_master()
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.normalized["symbol"].tolist(), ["600000.SH", "430047.BJ"])
        self.assertTrue((result.normalized["evidence_state"] == "PROXY").all())

    def test_tushare_without_token_is_explicit_skip(self) -> None:
        result = TushareAdapter(token="").security_master()
        self.assertEqual(result.status, "SKIP")
        self.assertIn("no API request", result.note)
        self.assertTrue(result.normalized.empty)

    def test_akshare_fallback_uses_positional_code_and_name(self) -> None:
        result = AKShareAdapter(FakeFallbackAKShare).security_master()
        self.assertEqual(result.normalized.loc[0, "symbol"], "920000.BJ")
        self.assertEqual(result.source_reference, "akshare.stock_zh_a_spot")
        self.assertIn("Fallbacks used", result.note)

    def test_tushare_client_normalizes_without_persisting_token(self) -> None:
        result = TushareAdapter(token="test-only", client=FakeTushareClient()).security_master()
        self.assertEqual(result.normalized.loc[0, "symbol"], "600000.SH")
        self.assertNotIn("test-only", repr(result.manifest()))


class ReconciliationTests(unittest.TestCase):
    def test_conflict_remains_visible(self) -> None:
        rows, counts = reconcile_records(
            [{"symbol": "600000.SH", "name": "浦发银行", "value": 10.0}],
            [{"symbol": "600000.SH", "name": "浦发银行", "value": 12.0}],
            key_fields=("symbol",),
            value_fields=("name", "value"),
            numeric_tolerances={"value": 0.1},
        )
        self.assertEqual(rows[0]["quality"], "CONFLICT")
        self.assertEqual(rows[0]["conflict_fields"], "value")
        self.assertEqual(counts["CONFLICT"], 1)

    def test_secondary_only_does_not_become_canonical(self) -> None:
        rows, counts = reconcile_records(
            [],
            [{"symbol": "600000.SH", "name": "浦发银行"}],
            key_fields=("symbol",),
            value_fields=("name",),
        )
        self.assertEqual(rows[0]["comparison_state"], "ONLY_SECONDARY")
        self.assertEqual(rows[0]["quality"], "MISSING")
        self.assertEqual(counts["ONLY_SECONDARY"], 1)


if __name__ == "__main__":
    unittest.main()
