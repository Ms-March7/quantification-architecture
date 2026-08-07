from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from capital_flow_attribution.source_policy import (  # noqa: E402
    adapter_status,
    candidates_for_family,
    load_source_policy,
)
from capital_flow_attribution.storage import create_read_only_catalog, csv_to_parquet  # noqa: E402
from capital_flow_attribution.validation import validate_security_master, validate_unified_daily_state  # noqa: E402


class SourcePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = load_source_policy(PROJECT_ROOT / "configs" / "source_policy.json")

    def test_official_source_is_first(self) -> None:
        candidates = candidates_for_family(self.policy, "margin")
        self.assertEqual(candidates[0]["source_id"], "official_direct")
        self.assertEqual(candidates[1]["source_id"], "tushare_pro")

    def test_token_value_is_not_exposed(self) -> None:
        status = adapter_status(self.policy, {"TUSHARE_TOKEN": "secret-value"})
        tushare = next(item for item in status if item["source_id"] == "tushare_pro")
        self.assertTrue(tushare["credential_configured"])
        self.assertNotIn("secret-value", repr(status))


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.frame = pd.DataFrame(
            [
                {
                    "date": "2026-07-14",
                    "family": "margin",
                    "market": "SSE",
                    "metric": "financing_balance",
                    "value": 1.0,
                    "unit": "CNY",
                    "evidence_state": "OBSERVED",
                    "quality": "OK",
                    "source": "SSE official",
                }
            ]
        )

    def test_good_daily_state_passes(self) -> None:
        checked = validate_unified_daily_state(self.frame)
        self.assertEqual(len(checked), 1)

    def test_duplicate_key_is_rejected(self) -> None:
        duplicate = pd.concat([self.frame, self.frame], ignore_index=True)
        with self.assertRaises(ValueError):
            validate_unified_daily_state(duplicate)

    def test_invalid_quality_is_rejected(self) -> None:
        invalid = self.frame.copy()
        invalid.loc[0, "quality"] = "AVAILABLE"
        with self.assertRaises(Exception):
            validate_unified_daily_state(invalid)

    def test_invalid_security_symbol_is_rejected(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "symbol": "600000",
                    "source_symbol": "600000",
                    "name": "浦发银行",
                    "exchange": "SSE",
                    "security_type": "STOCK",
                    "snapshot_date": "2026-07-19",
                    "available_at": "2026-07-19T03:00:00+08:00",
                    "ingested_at": "2026-07-19T03:00:00+08:00",
                    "source_name": "test",
                    "source_reference": "test",
                    "evidence_state": "PROXY",
                    "quality": "OK",
                    "license_status": "UNVERIFIED",
                }
            ]
        )
        with self.assertRaises(Exception):
            validate_security_master(frame)


class DuckDBStorageTests(unittest.TestCase):
    def test_csv_parquet_catalog_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "input.csv"
            parquet_path = root / "parquet" / "daily_state.parquet"
            database_path = root / "catalog.duckdb"
            pd.DataFrame([{"date": "2026-07-14", "value": 1.0}]).to_csv(csv_path, index=False)
            manifest = csv_to_parquet(csv_path, parquet_path)
            counts = create_read_only_catalog(database_path, {"daily_state": parquet_path})
            self.assertEqual(manifest["row_count"], 1)
            self.assertEqual(counts, {"daily_state": 1})
            self.assertTrue(parquet_path.is_file())


if __name__ == "__main__":
    unittest.main()
