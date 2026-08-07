from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "audit_core01_02_03_06_local_evidence.py"
)
SPEC = importlib.util.spec_from_file_location("core_local_evidence", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def item(status: str) -> dict[str, str]:
    return {"status": status}


class CoreLocalEvidenceTests(unittest.TestCase):
    def test_gate_all_pass_is_pass(self) -> None:
        self.assertEqual(
            MODULE.aggregate_gate_status([item("PASS"), item("PASS")]),
            "PASS",
        )

    def test_gate_direct_failure_is_fail(self) -> None:
        self.assertEqual(
            MODULE.aggregate_gate_status(
                [item("PASS"), item("UNKNOWN"), item("FAIL")]
            ),
            "FAIL",
        )

    def test_gate_incomplete_without_failure_is_unknown(self) -> None:
        self.assertEqual(
            MODULE.aggregate_gate_status([item("PASS"), item("UNKNOWN")]),
            "UNKNOWN",
        )

    def test_gate_requires_subcheck(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one"):
            MODULE.aggregate_gate_status([])

    def test_subcheck_rejects_invalid_status(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid subcheck status"):
            MODULE.subcheck("X", "BLOCK", "bad", [])

    def test_missing_fields_is_sorted_and_exact(self) -> None:
        self.assertEqual(
            MODULE.missing_fields(["b", "a"], ["c", "a", "d"]),
            ["c", "d"],
        )

    def test_report_path_rejects_governance_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "reports" / "data-governance"
            root.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "must be a child"):
                MODULE.require_report_path(root, workspace)

    def test_report_residual_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "MEASUREMENTS.json").write_text("{}", encoding="utf-8")
            (root / "unexpected.txt").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, "unregistered report residuals"
            ):
                MODULE.reject_unexpected_report_residuals(root)


if __name__ == "__main__":
    unittest.main()
