from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "control" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from classify_schema_evolution import (  # noqa: E402
    ACCEPT_DECISION,
    CLASS_BREAKING,
    CLASS_COMPATIBLE_ADD,
    CLASS_NO_CHANGE,
    CLASS_UNKNOWN,
    REJECT_DECISION,
    canonical_bytes,
    classify_schema_evolution,
)


def base_schema() -> dict:
    return {
        "schema_contract_version": 1,
        "dataset_id": "synthetic-daily-prices",
        "schema_version": "1.0.0",
        "dataset_coverage_start": "1990-12-19",
        "primary_key": ["security_id", "trade_date"],
        "fields": [
            {
                "name": "security_id",
                "logical_type": "VARCHAR",
                "nullable": False,
                "field_coverage_start": "1990-12-19",
            },
            {
                "name": "trade_date",
                "logical_type": "DATE",
                "nullable": False,
                "field_coverage_start": "1990-12-19",
            },
            {
                "name": "close",
                "logical_type": "DOUBLE",
                "nullable": True,
                "field_coverage_start": "1990-12-19",
            },
        ],
    }


class SchemaEvolutionClassifierTests(unittest.TestCase):
    def classify(self, new_schema: dict) -> dict:
        return classify_schema_evolution(base_schema(), new_schema)

    def test_no_change_ignores_field_order_and_version_label(self) -> None:
        new = base_schema()
        new["schema_version"] = "1.0.1"
        new["fields"] = list(reversed(new["fields"]))
        receipt = self.classify(new)
        self.assertEqual(receipt["classification"], CLASS_NO_CHANGE)
        self.assertEqual(receipt["decision"], ACCEPT_DECISION)
        self.assertEqual(receipt["changes"]["added_fields"], [])

    def test_nullable_field_addition_with_coverage_start_is_compatible(self) -> None:
        new = base_schema()
        new["schema_version"] = "1.1.0"
        new["fields"].append(
            {
                "name": "turnover",
                "logical_type": "DOUBLE",
                "nullable": True,
                "field_coverage_start": "2003-01-02",
            }
        )
        receipt = self.classify(new)
        self.assertEqual(receipt["classification"], CLASS_COMPATIBLE_ADD)
        self.assertEqual(receipt["decision"], ACCEPT_DECISION)
        self.assertEqual(
            receipt["changes"]["added_fields"][0]["field_coverage_start"],
            "2003-01-02",
        )

    def test_deleted_field_is_breaking(self) -> None:
        new = base_schema()
        new["fields"] = [
            field for field in new["fields"] if field["name"] != "close"
        ]
        receipt = self.classify(new)
        self.assertEqual(receipt["classification"], CLASS_BREAKING)
        self.assertEqual(receipt["decision"], REJECT_DECISION)
        self.assertEqual(receipt["changes"]["removed_fields"], ["close"])

    def test_type_change_is_breaking(self) -> None:
        new = base_schema()
        new["fields"][2]["logical_type"] = "BIGINT"
        receipt = self.classify(new)
        self.assertEqual(receipt["classification"], CLASS_BREAKING)
        self.assertEqual(receipt["changes"]["type_changes"][0]["field"], "close")

    def test_primary_key_change_is_breaking(self) -> None:
        new = base_schema()
        new["primary_key"] = ["security_id"]
        receipt = self.classify(new)
        self.assertEqual(receipt["classification"], CLASS_BREAKING)
        self.assertEqual(
            receipt["changes"]["primary_key_change"]["old"],
            ["security_id", "trade_date"],
        )

    def test_existing_field_coverage_start_change_is_breaking(self) -> None:
        new = base_schema()
        new["fields"][2]["field_coverage_start"] = "2000-01-04"
        receipt = self.classify(new)
        self.assertEqual(receipt["classification"], CLASS_BREAKING)
        self.assertEqual(
            receipt["changes"]["field_coverage_start_changes"][0]["field"],
            "close",
        )

    def test_required_field_addition_is_breaking(self) -> None:
        new = base_schema()
        new["fields"].append(
            {
                "name": "vendor_flag",
                "logical_type": "BOOLEAN",
                "nullable": False,
                "field_coverage_start": "2026-07-30",
            }
        )
        receipt = self.classify(new)
        self.assertEqual(receipt["classification"], CLASS_BREAKING)
        self.assertEqual(
            receipt["breaking_reasons"]["non_nullable_added_fields"],
            ["vendor_flag"],
        )

    def test_missing_field_coverage_start_is_unknown_fail_closed(self) -> None:
        new = base_schema()
        new["fields"].append(
            {
                "name": "turnover",
                "logical_type": "DOUBLE",
                "nullable": True,
            }
        )
        receipt = self.classify(new)
        self.assertEqual(receipt["classification"], CLASS_UNKNOWN)
        self.assertEqual(receipt["decision"], REJECT_DECISION)
        self.assertTrue(
            any(
                "field_coverage_start" in message
                for message in receipt["validation_errors"]
            )
        )

    def test_unknown_type_and_property_fail_closed(self) -> None:
        unknown_type = base_schema()
        unknown_type["fields"][2]["logical_type"] = "MAGIC_FLOAT"
        type_receipt = self.classify(unknown_type)
        self.assertEqual(type_receipt["classification"], CLASS_UNKNOWN)

        unknown_property = base_schema()
        unknown_property["migration_hint"] = "silently-cast"
        property_receipt = self.classify(unknown_property)
        self.assertEqual(property_receipt["classification"], CLASS_UNKNOWN)

        non_scalar_type = base_schema()
        non_scalar_type["fields"][2]["logical_type"] = ["DOUBLE"]
        non_scalar_receipt = self.classify(non_scalar_type)
        self.assertEqual(non_scalar_receipt["classification"], CLASS_UNKNOWN)

    def test_cross_dataset_comparison_fails_closed(self) -> None:
        new = base_schema()
        new["dataset_id"] = "another-dataset"
        receipt = self.classify(new)
        self.assertEqual(receipt["classification"], CLASS_UNKNOWN)
        self.assertIn("dataset_id mismatch", receipt["validation_errors"][0])

    def test_primary_key_contract_requires_full_coverage_and_non_null(self) -> None:
        new = base_schema()
        new["fields"][0]["field_coverage_start"] = "1991-01-02"
        new["fields"][1]["nullable"] = True
        receipt = self.classify(new)
        self.assertEqual(receipt["classification"], CLASS_UNKNOWN)
        joined = "\n".join(receipt["validation_errors"])
        self.assertIn("must cover dataset start", joined)
        self.assertIn("is nullable", joined)

    def test_version_regression_and_unversioned_change_are_breaking(self) -> None:
        regression = base_schema()
        regression["schema_version"] = "0.9.0"
        regression_receipt = self.classify(regression)
        self.assertEqual(regression_receipt["classification"], CLASS_BREAKING)
        self.assertTrue(
            regression_receipt["breaking_reasons"]["schema_version_regression"]
        )

        unchanged_version = base_schema()
        unchanged_version["fields"].append(
            {
                "name": "turnover",
                "logical_type": "DOUBLE",
                "nullable": True,
                "field_coverage_start": "2003-01-02",
            }
        )
        unchanged_version_receipt = self.classify(unchanged_version)
        self.assertEqual(
            unchanged_version_receipt["classification"],
            CLASS_BREAKING,
        )
        self.assertTrue(
            unchanged_version_receipt["breaking_reasons"][
                "schema_version_not_advanced_for_semantic_change"
            ]
        )

    def test_receipt_is_deterministic_and_has_no_runtime_path_or_time(self) -> None:
        old = base_schema()
        new = copy.deepcopy(old)
        new["schema_version"] = "1.1.0"
        new["fields"].append(
            {
                "name": "turnover",
                "logical_type": "DOUBLE",
                "nullable": True,
                "field_coverage_start": "2003-01-02",
            }
        )
        first = classify_schema_evolution(old, new)
        second = classify_schema_evolution(old, new)
        self.assertEqual(canonical_bytes(first), canonical_bytes(second))
        serialized = canonical_bytes(first).decode("utf-8")
        self.assertNotIn("generated_at", serialized)
        self.assertNotIn(str(ROOT), serialized)
        self.assertEqual(len(first["receipt_fingerprint"]), 64)

    def test_cli_invalid_json_emits_fail_closed_receipt_and_exit_two(self) -> None:
        with tempfile.TemporaryDirectory(prefix="schema-evolution-cli-") as temp:
            temp_root = Path(temp)
            old_path = temp_root / "old.json"
            new_path = temp_root / "new.json"
            old_path.write_text("{bad json", encoding="utf-8")
            new_path.write_text(
                json.dumps(base_schema(), ensure_ascii=False),
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(TOOLS / "classify_schema_evolution.py"),
                "--old",
                str(old_path),
                "--new",
                str(new_path),
            ]
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(completed.returncode, 2)
            receipt = json.loads(completed.stdout)
            self.assertEqual(receipt["classification"], CLASS_UNKNOWN)
            self.assertEqual(receipt["decision"], REJECT_DECISION)


if __name__ == "__main__":
    unittest.main()
