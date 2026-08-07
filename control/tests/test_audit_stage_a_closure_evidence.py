import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "audit_stage_a_closure_evidence.py"
)
SPEC = importlib.util.spec_from_file_location(
    "audit_stage_a_closure_evidence",
    SCRIPT,
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ConsolidationStatusTests(unittest.TestCase):
    def test_all_required_and_fail_closed_projection_pass(self):
        passed, status = MODULE.consolidation_status(
            required_pass=True,
            projections_fail_closed=True,
            guards_unchanged=True,
        )
        self.assertTrue(passed)
        self.assertIn("5_REQUIRED_PACKAGES_AUDITED", status)

    def test_projection_status_mismatch_fails_closed(self):
        passed, status = MODULE.consolidation_status(
            required_pass=True,
            projections_fail_closed=False,
            guards_unchanged=True,
        )
        self.assertFalse(passed)
        self.assertTrue(status.startswith("FAIL__"))

    def test_required_failure_fails_closed(self):
        passed, status = MODULE.consolidation_status(
            required_pass=False,
            projections_fail_closed=True,
            guards_unchanged=True,
        )
        self.assertFalse(passed)
        self.assertTrue(status.startswith("FAIL__"))

    def test_guard_change_fails_closed(self):
        passed, status = MODULE.consolidation_status(
            required_pass=True,
            projections_fail_closed=True,
            guards_unchanged=False,
        )
        self.assertFalse(passed)
        self.assertTrue(status.startswith("FAIL__"))


class BindingContractTests(unittest.TestCase):
    def test_manifest_payload_accepts_one_files_root(self):
        root_format, payload = MODULE.manifest_payload({"files": []})
        self.assertEqual(root_format, "dict.files")
        self.assertEqual(payload, [])

    def test_manifest_payload_rejects_two_roots(self):
        with self.assertRaises(ValueError):
            MODULE.manifest_payload({"files": [], "artifacts": []})

    def test_incomplete_binding_is_rejected(self):
        with self.assertRaises(ValueError):
            MODULE.collect_manifest_bindings(
                {"path": "x.json", "bytes": 1}
            )

    def test_complete_binding_is_collected(self):
        digest = "a" * 64
        records = MODULE.collect_manifest_bindings(
            {"path": "x.json", "bytes": 1, "sha256": digest}
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["sha256"], digest)

    def test_historical_guard_binding_is_not_treated_as_live_evidence(self):
        digest = "a" * 64
        records = MODULE.collect_embedded_bindings(
            {
                "before_guards": {
                    "files": {
                        "control/ROADMAP.md": {
                            "path": "control/ROADMAP.md",
                            "bytes": 1,
                            "sha256": digest,
                        }
                    }
                },
                "evidence": {
                    "path": "evidence.json",
                    "bytes": 2,
                    "sha256": digest,
                },
            }
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["path"], "evidence.json")

    def test_workspace_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            outside = Path(temporary) / "outside.json"
            outside.write_text(json.dumps({}), encoding="utf-8")
            resolved, basis, errors = MODULE.resolve_binding_path(
                str(outside),
                workspace=workspace,
                local_base=workspace,
            )
            self.assertIsNone(resolved)
            self.assertEqual(basis, "absolute")
            self.assertIn("binding escapes workspace", errors)


class ProjectionContractTests(unittest.TestCase):
    def test_rights_projection_closes_only_attested_subscope(self):
        workspace = Path(__file__).resolve().parents[2]
        receipt = MODULE.load_json(
            workspace
            / MODULE.DEFAULT_RIGHTS_PACKAGE
            / "RIGHTS_RECEIPT.json"
        )
        propositions, gates = MODULE.extract_rights_projection(receipt)
        self.assertEqual(gates[0]["declared_gate_status"], "FAIL")
        self.assertEqual(propositions[0]["audit_status"], "PARTIAL")
        self.assertIn(
            "API_OR_AUTOMATED_BULK_COLLECTION",
            propositions[0]["not_covered"],
        )

    def test_current_package_projections_match_fail_closed_contract(self):
        workspace = Path(__file__).resolve().parents[2]
        reports_root = workspace / "reports" / "data-governance"
        specs = {
            "CORE02": MODULE.DEFAULT_CORE02_PACKAGE,
            "CORE03_CORE06_RULES": MODULE.DEFAULT_RULES_PACKAGE,
            "CSMAR_RIGHTS": MODULE.DEFAULT_RIGHTS_PACKAGE,
        }
        contexts = {}
        for package_id, relative in specs.items():
            ledger, context = MODULE.audit_package(
                package_id=package_id,
                package_root=workspace / relative,
                workspace=workspace,
                required=True,
            )
            self.assertEqual(
                ledger["package_audit"]["status"],
                "AUDITED_PASS__MANIFEST_AND_RECURSIVE_BINDINGS",
            )
            self.assertTrue(
                (workspace / relative).resolve().is_relative_to(
                    reports_root.resolve()
                )
            )
            contexts[package_id] = context
        passed, detail = MODULE.validate_fail_closed_projections(contexts)
        self.assertTrue(passed, detail)


if __name__ == "__main__":
    unittest.main()
