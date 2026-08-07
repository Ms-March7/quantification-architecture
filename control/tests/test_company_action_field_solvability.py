from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


TOOL = (
    Path(__file__).resolve().parents[1]
    / "tools/audit_company_action_field_solvability.py"
)
SPEC = importlib.util.spec_from_file_location("company_action_field_solvability", TOOL)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class CompanyActionFieldSolvabilityTests(unittest.TestCase):
    def test_parse_combined_standard_note(self) -> None:
        parsed = MODULE.parse_jq_implementation_note("10送2转增13股派0.5元(含税)")
        self.assertEqual(parsed["bonus_per_10"], Decimal("2"))
        self.assertEqual(parsed["capitalization_per_10"], Decimal("13"))
        self.assertEqual(parsed["cash_per_10"], Decimal("0.5"))

    def test_parse_each_explicit_term(self) -> None:
        self.assertEqual(
            MODULE.parse_jq_implementation_note("每10股转增6股派6元"),
            {
                "capitalization_per_10": Decimal("6"),
                "cash_per_10": Decimal("6"),
            },
        )

    def test_vague_text_is_not_guessed(self) -> None:
        self.assertEqual(MODULE.parse_jq_implementation_note("实施年度利润分配"), {})
        self.assertEqual(MODULE.parse_jq_implementation_note(None), {})

    def test_decimal_parser_rejects_non_numeric(self) -> None:
        self.assertIsNone(MODULE.decimal_or_none("UNKNOWN"))
        self.assertEqual(MODULE.decimal_or_none("1,234.5"), Decimal("1234.5"))

    def test_power_of_ten_conflict_detection(self) -> None:
        self.assertTrue(MODULE.contains_power_of_ten_pair([0.3, 3.0]))
        self.assertTrue(MODULE.contains_power_of_ten_pair([0.4, 40.0]))
        self.assertFalse(MODULE.contains_power_of_ten_pair([0.3, 0.31]))

    def test_observation_value_extraction(self) -> None:
        values = MODULE.extract_observation_values("A=0.3000 | B=3.000000 | C=bad")
        self.assertEqual(values, [0.3, 3.0])

    def test_deterministic_sampling_key(self) -> None:
        first = MODULE.deterministic_rank_key("obs-1")
        second = MODULE.deterministic_rank_key("obs-1")
        self.assertEqual(first, second)
        self.assertNotEqual(first, MODULE.deterministic_rank_key("obs-2"))

    def test_path_guard_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "allowed"
            root.mkdir()
            with self.assertRaises(RuntimeError):
                MODULE.ensure_within(Path(temp) / "outside", root, "test")

    def test_safety_contract_constants(self) -> None:
        self.assertEqual(MODULE.AUDIT_VERSION, "company-action-field-solvability-audit-v1")
        self.assertIn("available_at", MODULE.REQUIRED_LEDGER_COLUMNS)
        self.assertIn("supersedes_revision_id", MODULE.REQUIRED_LEDGER_COLUMNS)


if __name__ == "__main__":
    unittest.main()
