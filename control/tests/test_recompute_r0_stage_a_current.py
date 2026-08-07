from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
MODULE_PATH = WORKSPACE / "control/tools/recompute_r0_stage_a_current.py"
SPEC = importlib.util.spec_from_file_location("recompute_r0_stage_a_current", MODULE_PATH)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class CurrentStageARecomputeTests(unittest.TestCase):
    def test_all_upstream_manifests_verify(self) -> None:
        for relative in mod.UPSTREAM_MANIFESTS.values():
            result = mod.verify_manifest(relative)
            self.assertTrue(result["entries_verified"])
            self.assertGreater(result["entry_count"], 0)

    def test_source_decisions_remain_conservative(self) -> None:
        source = mod.validate_source_decisions()
        self.assertEqual(source["identity_gates"]["CORE01-Q1"], "FAIL")
        self.assertFalse(source["identity_history_search"]["permanent_identity_found"])
        self.assertFalse(source["identity_history_search"]["complete_history_found"])
        self.assertEqual(source["calendar_gates"]["CORE02-Q4"], "PASS")
        self.assertFalse(source["calendar_minimum_gap_search"]["formal_admission"])
        self.assertEqual(source["semantic_gates"]["CORE03-Q2"], "UNKNOWN")
        self.assertEqual(source["semantic_gates"]["CORE06-Q4"], "FAIL")

    def test_decision_is_zero_of_four(self) -> None:
        decision = mod.stage_a_decision(mod.validate_source_decisions())
        self.assertFalse(decision["stage_a"]["all_ready"])
        self.assertEqual(decision["stage_a"]["ready_count"], 0)
        self.assertEqual(decision["stage_a"]["target_count"], 4)
        self.assertFalse(decision["research_usable"])

    def test_no_target_is_silently_promoted(self) -> None:
        decision = mod.stage_a_decision(mod.validate_source_decisions())
        self.assertTrue(all(not row["ready"] for row in decision["stage_a"]["targets"]))
        self.assertTrue(
            all(
                any(value != "PASS" for value in row["quality_gates"].values())
                for row in decision["stage_a"]["targets"]
            )
        )

    def test_rights_boundary_is_narrow(self) -> None:
        source = mod.validate_source_decisions()
        boundary = source["rights_boundary"]["strict_interpretation"]
        self.assertIn("LOCAL_RETENTION", boundary)
        self.assertIn("NO_API_AUTOMATION_OR_REDISTRIBUTION_INFERENCE", boundary)

    def test_authority_guard_is_complete_and_current(self) -> None:
        guard = mod.authority_guard()
        self.assertEqual(set(guard["files"]), set(mod.AUTHORITY_PATHS))
        mod.validate_formal_tree_guard(guard)

    def test_company_action_unknowns_remain_blocking(self) -> None:
        source = mod.validate_source_decisions()
        self.assertIn("UNCLOSED", source["action_pit"]["core_04_assessment"])
        self.assertIn("BLOCKED", source["action_pit"]["core_05_assessment"])

    def test_existing_report_verifies_when_present(self) -> None:
        if mod.REPORT.is_dir():
            result = mod.verify_existing()
            self.assertEqual(result["stage_a_ready"], "0/4")
            self.assertEqual(result["formal_effect"], "NONE")


if __name__ == "__main__":
    unittest.main()
