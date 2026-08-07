from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT = WORKSPACE / "control" / "tools" / "build_stage_a_closure_control.py"
SPEC = importlib.util.spec_from_file_location("stage_a_closure_control", SCRIPT)
assert SPEC and SPEC.loader
CONTROL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROL)


class StageAClosureControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.measurements = json.loads(
            (WORKSPACE / CONTROL.DEFAULT_MEASUREMENTS).read_text(encoding="utf-8")
        )
        cls.stage_machine = json.loads(
            (WORKSPACE / CONTROL.DEFAULT_STAGE_MACHINE).read_text(encoding="utf-8")
        )
        cls.supplemental = json.loads(
            (WORKSPACE / CONTROL.DEFAULT_SUPPLEMENTAL).read_text(encoding="utf-8")
        )
        cls.rights_package, rights_receipt = (
            CONTROL.load_verified_report_package(
                workspace=WORKSPACE,
                package_value=Path(CONTROL.DEFAULT_RIGHTS_PACKAGE),
                package_id="CSMAR_USER_ATTESTED_RIGHTS",
                required_artifact="RIGHTS_RECEIPT.json",
            )
        )
        cls.core02_package, core02_receipt = (
            CONTROL.load_verified_report_package(
                workspace=WORKSPACE,
                package_value=Path(CONTROL.DEFAULT_CORE02_PACKAGE),
                package_id="CORE02_CALENDAR_SESSION_CLOSURE",
                required_artifact="CORE02_GATE_RECEIPT.json",
            )
        )
        cls.rules_package, rules_receipt = (
            CONTROL.load_verified_report_package(
                workspace=WORKSPACE,
                package_value=Path(CONTROL.DEFAULT_RULES_PACKAGE),
                package_id="CORE03_CORE06_OFFICIAL_RULES_CLOSURE",
                required_artifact="GATE_RECEIPT.json",
            )
        )
        cls.evidence_packages = [
            cls.rights_package,
            cls.core02_package,
            cls.rules_package,
        ]
        cls.evidence_projection = CONTROL.build_evidence_projection(
            rights_receipt=rights_receipt,
            core02_receipt=core02_receipt,
            rules_receipt=rules_receipt,
        )

    def build_queue(self):
        return CONTROL.build_queue(
            measurements=self.measurements,
            stage_machine=self.stage_machine,
            supplemental=self.supplemental,
            inputs={},
            evidence_packages=self.evidence_packages,
            evidence_projection=self.evidence_projection,
        )

    def test_every_current_nonpass_clause_is_mapped_once(self) -> None:
        rows = CONTROL.extract_nonpass(self.measurements)
        self.assertEqual(len(rows), 25)
        self.assertEqual(len({row["queue_id"] for row in rows}), 25)
        self.assertEqual(
            {row["check_id"] for row in rows},
            set(CONTROL.CHECK_ACTIONS),
        )

    def test_current_queue_fails_closed_without_formal_mutation(self) -> None:
        queue = self.build_queue()
        self.assertEqual(queue["formal_state"]["stage_a_ready"], "0/4")
        self.assertFalse(queue["formal_state"]["formal_catalog_mutated"])
        self.assertFalse(queue["formal_state"]["snapshot_published"])
        self.assertFalse(queue["formal_state"]["quality_promoted"])
        self.assertEqual(queue["summary"]["nonpass_clause_count"], 25)
        self.assertEqual(queue["summary"]["total_queue_items"], 26)

    def test_rights_and_portal_items_cannot_be_silently_automated(self) -> None:
        queue = self.build_queue()
        protected = [
            row
            for row in queue["queue"]
            if row["category"]
            in {
                "REQUIRES_RIGHTS_CONFIRMATION",
                "REQUIRES_PORTAL_MANUAL_DATA",
            }
        ]
        self.assertEqual(len(protected), 3)
        self.assertTrue(all(row["portal_or_user_needed"] for row in protected))
        self.assertTrue(
            all(row["execution_state"].startswith("BLOCKED__") for row in protected)
        )

    def test_hash_bound_evidence_preserves_exact_fail_closed_statuses(self) -> None:
        queue = self.build_queue()
        self.assertEqual(
            queue["evidence_projection"]["official_evidence_gate_statuses"],
            CONTROL.EXPECTED_EVIDENCE_GATE_STATUSES,
        )
        self.assertEqual(
            queue["evidence_projection"]["rights"]["global_gate_status"],
            "FAIL",
        )
        self.assertFalse(
            queue["evidence_projection"]["rights"][
                "independent_contract_verification"
            ]
        )
        rights = next(
            row for row in queue["queue"] if row["core_id"] == "GLOBAL"
        )
        self.assertEqual(rights["current_status"], "FAIL")
        self.assertIn(
            "CAMPUS_INTERNAL_NONCOMMERCIAL_ACADEMIC_RESEARCH",
            rights["hash_bound_evidence"]["allowed"],
        )
        self.assertIn(
            "API_OR_AUTOMATED_BULK_COLLECTION",
            rights["hash_bound_evidence"]["not_covered"],
        )

    def test_review_only_publish_model_does_not_fake_stage_a_closure(self) -> None:
        rows = CONTROL.extract_nonpass(self.measurements)
        publish = [
            row
            for row in rows
            if row["category"] == "REQUIRES_STAGE_B_PUBLISH"
        ]
        self.assertEqual(len(publish), 1)
        self.assertFalse(publish[0]["blocks_stage_a"])


if __name__ == "__main__":
    unittest.main()
