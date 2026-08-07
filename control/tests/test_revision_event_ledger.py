from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = ROOT / "control" / "tools" / "replay_revision_event_ledger.py"
CONTRACT_ROOT = (
    ROOT / "data" / "catalog" / "contracts" / "revision_event_ledger_v1"
)
SPEC = importlib.util.spec_from_file_location("revision_event_ledger", TOOL_PATH)
assert SPEC is not None and SPEC.loader is not None
ledger = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ledger
SPEC.loader.exec_module(ledger)


class RevisionEventLedgerTests(unittest.TestCase):
    def fixture(self, name: str) -> Path:
        return CONTRACT_ROOT / "fixtures" / name

    def valid_values(self) -> list[dict]:
        return [
            json.loads(line)
            for line in self.fixture("valid.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]

    def assert_code(self, expected: str, values: list[dict]) -> None:
        with self.assertRaises(ledger.LedgerError) as context:
            ledger.build_ledger(values)
        self.assertEqual(context.exception.code, expected)

    def test_contract_and_schema_are_strict_inspection_only(self) -> None:
        contract = ledger.load_contract(CONTRACT_ROOT / "contract.json")
        schema = json.loads(
            (CONTRACT_ROOT / "event.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(contract["status"], "INSPECTION_ONLY")
        self.assertIn("no formal catalog write", contract["prohibitions"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["properties"]["event_type"]["enum"]),
            {"UPSERT", "WITHDRAWAL"},
        )

    def test_fixture_replay_is_pit_strict_and_duplicate_idempotent(self) -> None:
        state = ledger.validate_file(self.fixture("valid.jsonl"))
        self.assertEqual(state.input_line_count, 6)
        self.assertEqual(len(state.events), 5)
        self.assertEqual(state.duplicate_event_count, 1)
        self.assertEqual(state.late_arrival_count, 1)
        self.assertEqual(state.out_of_order_available_count, 1)

        before_late = ledger.replay_ledger(
            state, "2026-04-06T12:00:00+08:00"
        )
        self.assertEqual(before_late["active_record_count"], 1)
        self.assertEqual(before_late["active_records"][0]["payload"]["value"], 100)

        after_late = ledger.replay_ledger(
            state, "2026-04-11T12:00:00+08:00"
        )
        by_entity = {
            item["business_key"]["entity"]: item
            for item in after_late["active_records"]
        }
        self.assertEqual(by_entity["A"]["payload"]["value"], 101)
        self.assertEqual(by_entity["B"]["payload"]["value"], 50)

        withdrawn = ledger.replay_ledger(
            state, "2026-04-12T12:00:00+08:00"
        )
        self.assertEqual(withdrawn["active_record_count"], 1)
        self.assertEqual(withdrawn["withdrawn_record_count"], 1)
        self.assertEqual(
            withdrawn["withdrawn_records"][0]["withdraws_revision_id"],
            "a-r2",
        )

        reinstated = ledger.replay_ledger(
            state, "2026-04-13T12:00:00+08:00"
        )
        self.assertEqual(reinstated["active_record_count"], 2)
        self.assertEqual(reinstated["withdrawn_record_count"], 0)

    def test_changed_duplicate_event_fails_closed(self) -> None:
        with self.assertRaises(ledger.LedgerError) as context:
            ledger.validate_file(self.fixture("conflicting_duplicate.jsonl"))
        self.assertEqual(context.exception.code, "EVENT_ID_CONFLICT")

    def test_missing_withdrawal_target_fails_closed(self) -> None:
        with self.assertRaises(ledger.LedgerError) as context:
            ledger.validate_file(self.fixture("missing_withdrawal_target.jsonl"))
        self.assertEqual(context.exception.code, "WITHDRAWAL_TARGET_MISSING")

    def test_timezone_is_required(self) -> None:
        values = self.valid_values()[:1]
        values[0]["available_at"] = "2026-04-01T09:00:00"
        self.assert_code("TIMEZONE_REQUIRED", values)

    def test_revision_number_must_be_contiguous(self) -> None:
        values = self.valid_values()[:3]
        values[2]["revision_no"] = 3
        self.assert_code("REVISION_NOT_MONOTONIC", values)

    def test_supersedes_must_target_current_existing_head(self) -> None:
        values = self.valid_values()[:3]
        values[2]["supersedes_revision_id"] = "does-not-exist"
        self.assert_code("SUPERSEDES_TARGET_MISSING", values)

        values = self.valid_values()[:3]
        values[2]["supersedes_revision_id"] = "b-r1"
        self.assert_code("SUPERSEDES_TARGET_INVALID", values)

    def test_backdated_revision_availability_fails_closed(self) -> None:
        values = self.valid_values()[:3]
        values[2]["available_at"] = "2026-03-31T09:00:00+08:00"
        self.assert_code("REVISION_AVAILABLE_AT_INVALID", values)

    def test_schema_drift_and_multi_dataset_fail_closed(self) -> None:
        values = self.valid_values()[:1]
        values[0]["unexpected"] = True
        self.assert_code("EVENT_SCHEMA_MISMATCH", values)

        values = self.valid_values()[:2]
        values[1]["dataset_id"] = "another-dataset"
        self.assert_code("MULTI_DATASET_LEDGER", values)

    def test_cli_returns_json_and_never_claims_formal_state(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(TOOL_PATH),
                "--events",
                str(self.fixture("valid.jsonl")),
                "--decision-time",
                "2026-04-12T12:00:00+08:00",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "PASS_INSPECTION_ONLY")
        self.assertEqual(result["purpose_ceiling"], "INSPECTION_ONLY")
        self.assertFalse(result["source_admission"])
        self.assertFalse(result["catalog_registered"])
        self.assertFalse(result["quality_promotion"])
        self.assertFalse(result["research_ready"])


if __name__ == "__main__":
    unittest.main()
