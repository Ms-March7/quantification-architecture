from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT = (
    WORKSPACE
    / "control"
    / "tools"
    / "audit_core01_official_identity_closure.py"
)
SPEC = importlib.util.spec_from_file_location("core01_identity_audit", SCRIPT)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def build_guard_workspace(root: Path) -> None:
    for relative_path in AUDIT.PROTECTED_AUTHORITY_FILES.values():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{relative_path}\n", encoding="utf-8")
    for relative_root in AUDIT.FORMAL_STORE_ROOTS.values():
        path = root / relative_root
        path.mkdir(parents=True, exist_ok=True)
        (path / "README.md").write_text(relative_root, encoding="utf-8")


class Core01OfficialIdentityAuditTests(unittest.TestCase):
    def test_protected_state_records_exact_files_and_inventories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            build_guard_workspace(workspace)
            pre = AUDIT.capture_protected_state(workspace)
            post = AUDIT.capture_protected_state(workspace)
            guard = AUDIT.build_mutation_guard(pre, post)

            self.assertTrue(guard["checks"]["all_pre_equals_post"])
            self.assertEqual(
                set(pre["authority_files"]),
                set(AUDIT.PROTECTED_AUTHORITY_FILES),
            )
            self.assertEqual(
                set(pre["formal_store_inventories"]),
                set(AUDIT.FORMAL_STORE_ROOTS),
            )
            self.assertTrue(
                all(
                    {"path", "bytes", "sha256"} <= set(reference)
                    for reference in pre["authority_files"].values()
                )
            )

    def test_authority_content_change_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            build_guard_workspace(workspace)
            pre = AUDIT.capture_protected_state(workspace)
            registry = (
                workspace
                / AUDIT.PROTECTED_AUTHORITY_FILES["source_registry"]
            )
            registry.write_text("changed-with-same-boundary\n", encoding="utf-8")
            post = AUDIT.capture_protected_state(workspace)
            with self.assertRaisesRegex(
                RuntimeError, "authority_files.source_registry"
            ):
                AUDIT.build_mutation_guard(pre, post)

    def test_inventory_limit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / AUDIT.FORMAL_STORE_ROOTS["market_snapshots"]
            root.mkdir(parents=True)
            (root / "a").write_text("a", encoding="utf-8")
            (root / "b").write_text("b", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "max entries"):
                AUDIT.controlled_inventory(
                    workspace,
                    AUDIT.FORMAL_STORE_ROOTS["market_snapshots"],
                    max_entries=1,
                )

    def test_current_clause_decisions_remain_conservative(self) -> None:
        package = (
            WORKSPACE
            / "reports"
            / "data-governance"
            / AUDIT.RUN_ID
        )
        decisions = json.loads(
            (package / "CLAUSE_DECISIONS.json").read_text(encoding="utf-8")
        )
        baseline = json.loads(
            (
                WORKSPACE
                / "reports/data-governance/"
                "core01-02-03-06-local-evidence-closure-20260730-v1/"
                "MEASUREMENTS.json"
            ).read_text(encoding="utf-8")
        )
        q1_q4 = {
            row["gate_id"]: row
            for row in decisions["current_clause_decisions"]
            if row["gate_id"] in {
                "CORE01-Q1",
                "CORE01-Q2",
                "CORE01-Q3",
                "CORE01-Q4",
            }
        }
        baseline_q1_q4 = {
            row["gate_id"]: row
            for row in baseline["gate_evaluations"]["targets"]["CORE-01"]
            if row["gate_id"] in q1_q4
        }
        for gate_id, gate in q1_q4.items():
            expected = {
                item["check_id"]: item["status"]
                for item in baseline_q1_q4[gate_id]["subchecks"]
            }
            actual = {
                item["subcheck_id"]: item["baseline_status"]
                for item in gate["subchecks"]
            }
            self.assertEqual(actual, expected)
            self.assertNotEqual(gate["strict_gate_status"], "PASS")

        closure_passes = [
            (gate["gate_id"], item["subcheck_id"])
            for gate in q1_q4.values()
            for item in gate["subchecks"]
            if item["closure_assessment"] == "PASS"
        ]
        self.assertEqual(
            closure_passes,
            [("CORE01-Q1", "local_code_is_not_permanent_identity")],
        )
        self.assertEqual(q1_q4["CORE01-Q1"]["strict_gate_status"], "FAIL")
        self.assertEqual(decisions["stage_a"]["total_blocking_reasons"], 11)


if __name__ == "__main__":
    unittest.main()
