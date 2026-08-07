from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CATALOG = Path(__file__).resolve().parents[2] / "data" / "catalog"
if str(CATALOG) not in sys.path:
    sys.path.insert(0, str(CATALOG))

from adapter_gate import (  # noqa: E402
    AdapterGateError,
    canonical_row_sha256,
    validate_adapter_sample,
)


CONTRACT_PATH = (
    CATALOG
    / "contracts"
    / "adapter_gate_v1"
    / "tencent_daily_inspection.json"
)


class AdapterGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.sample = {
            "schema_version": 1,
            "contract_id": self.contract["contract_id"],
            "declared_units": copy.deepcopy(self.contract["expected_units"]),
            "raw_rows": [
                ["20260726", "10", "11", "9", "10.5", "100", "1", "1050", "x", "y", "z"],
                ["20260726", "20", "21", "19", "20.5", "200", "2", "4100", "x", "y", "z"],
            ],
            "normalized_rows": [
                {
                    "security_id": "XSHG.600000",
                    "trade_date": "2026-07-26",
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.5,
                    "volume": 100,
                    "turnover": 1.0,
                    "amount": 1050.0,
                },
                {
                    "security_id": "XSHE.000001",
                    "trade_date": "2026-07-26",
                    "open": 20.0,
                    "high": 21.0,
                    "low": 19.0,
                    "close": 20.5,
                    "volume": 200,
                    "turnover": 2.0,
                    "amount": 4100.0,
                },
            ],
        }
        self.sample["row_lineage"] = [
            {
                "raw_index": index,
                "normalized_index": index,
                "raw_row_sha256": canonical_row_sha256(row),
            }
            for index, row in enumerate(self.sample["raw_rows"])
        ]

    def result(self) -> dict:
        return validate_adapter_sample(self.contract, self.sample)

    def test_valid_bounded_sample_passes_without_admission(self) -> None:
        result = self.result()
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["source_admission"])
        self.assertEqual(result["purpose_ceiling"], "SMOKE_ONLY")

    def test_raw_width_drift_fails(self) -> None:
        self.sample["raw_rows"][0].pop()
        result = self.result()
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["checks"]["raw_width"])

    def test_unit_mismatch_fails(self) -> None:
        self.sample["declared_units"]["volume"] = "LOT"
        result = self.result()
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["checks"]["declared_units"])

    def test_duplicate_primary_key_fails(self) -> None:
        self.sample["normalized_rows"][1]["security_id"] = "XSHG.600000"
        result = self.result()
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["checks"]["primary_key_unique"])

    def test_critical_null_fails(self) -> None:
        self.sample["normalized_rows"][0]["close"] = None
        result = self.result()
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["checks"]["critical_non_null"])
        self.assertFalse(result["checks"]["numeric_finite"])

    def test_ohlc_inconsistency_fails(self) -> None:
        self.sample["normalized_rows"][0]["high"] = 9.5
        result = self.result()
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["checks"]["ohlc_consistent"])

    def test_negative_amount_fails(self) -> None:
        self.sample["normalized_rows"][0]["amount"] = -1.0
        result = self.result()
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["checks"]["non_negative"])

    def test_row_count_and_lineage_are_one_to_one(self) -> None:
        self.sample["normalized_rows"].pop()
        result = self.result()
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["checks"]["row_count_preserved"])
        self.assertFalse(result["checks"]["row_lineage"])

    def test_raw_row_hash_lineage_mismatch_fails(self) -> None:
        self.sample["row_lineage"][0]["raw_row_sha256"] = "0" * 64
        result = self.result()
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["checks"]["row_lineage"])

    def test_primary_key_type_and_date_format_fail_without_crashing(self) -> None:
        self.sample["normalized_rows"][0]["security_id"] = ["XSHG.600000"]
        self.sample["normalized_rows"][1]["trade_date"] = "not-a-date"
        result = self.result()
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["checks"]["normalized_field_types"])
        self.assertTrue(result["checks"]["primary_key_unique"])

    def test_non_positive_prices_fail(self) -> None:
        for field in ("open", "high", "low", "close"):
            self.sample["normalized_rows"][0][field] = -1.0
        result = self.result()
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["checks"]["positive"])

    def test_primary_key_contract_requires_non_null_fields(self) -> None:
        self.contract["critical_non_null_fields"].remove("security_id")
        with self.assertRaises(AdapterGateError):
            self.result()

    def test_source_admission_cannot_be_enabled(self) -> None:
        self.contract["source_admission"] = True
        with self.assertRaises(AdapterGateError):
            self.result()

    def test_ohlc_roles_must_map_to_four_distinct_fields(self) -> None:
        self.contract["ohlc_fields"] = {
            "open": "close",
            "high": "close",
            "low": "close",
            "close": "close",
        }
        with self.assertRaises(AdapterGateError):
            self.result()

    def test_ohlc_roles_must_map_to_positive_price_fields(self) -> None:
        self.contract["ohlc_fields"]["low"] = "volume"
        with self.assertRaises(AdapterGateError):
            self.result()

    def test_units_must_cover_every_numeric_field(self) -> None:
        self.contract["expected_units"].pop("amount")
        self.sample["declared_units"].pop("amount")
        with self.assertRaises(AdapterGateError):
            self.result()

    def test_cli_returns_stable_json_failure_for_bad_key_type(self) -> None:
        self.sample["normalized_rows"][0]["security_id"] = ["XSHG.600000"]
        validator = Path(__file__).resolve().parents[1] / "tools" / "validate_adapter_sample.py"
        with tempfile.TemporaryDirectory(prefix="adapter-gate-cli-") as temp_dir:
            root = Path(temp_dir)
            contract_path = root / "contract.json"
            sample_path = root / "sample.json"
            contract_path.write_text(
                json.dumps(self.contract),
                encoding="utf-8",
            )
            sample_path.write_text(
                json.dumps(self.sample),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(validator),
                    "--contract",
                    str(contract_path),
                    "--sample",
                    str(sample_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["source_admission"])

    def test_cli_returns_stable_json_failure_for_overflowing_integer(self) -> None:
        self.sample["normalized_rows"][0]["open"] = 10**400
        validator = Path(__file__).resolve().parents[1] / "tools" / "validate_adapter_sample.py"
        with tempfile.TemporaryDirectory(prefix="adapter-gate-overflow-") as temp_dir:
            root = Path(temp_dir)
            contract_path = root / "contract.json"
            sample_path = root / "sample.json"
            contract_path.write_text(
                json.dumps(self.contract),
                encoding="utf-8",
            )
            sample_path.write_text(
                json.dumps(self.sample),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(validator),
                    "--contract",
                    str(contract_path),
                    "--sample",
                    str(sample_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["checks"]["normalized_field_types"])


if __name__ == "__main__":
    unittest.main()
