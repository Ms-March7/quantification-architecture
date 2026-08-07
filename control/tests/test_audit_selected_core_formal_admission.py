from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "audit_selected_core_formal_admission.py"
)
SPEC = importlib.util.spec_from_file_location("formal_admission_audit", SCRIPT)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


class FormalAdmissionAuditTests(unittest.TestCase):
    def test_stale_limit_field_gap_is_corrected_not_removed(self) -> None:
        corrected, corrections = AUDIT.correct_stale_phase_a_missing(
            "CORE-03",
            [AUDIT.STALE_LIMIT_FIELD_GAP, "another open gap"],
        )
        self.assertIn(AUDIT.CORRECTED_LIMIT_FIELD_GAP, corrected)
        self.assertNotIn(AUDIT.STALE_LIMIT_FIELD_GAP, corrected)
        self.assertIn("another open gap", corrected)
        self.assertEqual(len(corrections), 1)
        self.assertIn("semantics", corrections[0]["new"])

    def test_stale_limit_field_gap_is_not_rewritten_for_other_core(self) -> None:
        corrected, corrections = AUDIT.correct_stale_phase_a_missing(
            "CORE-06", [AUDIT.STALE_LIMIT_FIELD_GAP]
        )
        self.assertEqual(corrected, [AUDIT.STALE_LIMIT_FIELD_GAP])
        self.assertEqual(corrections, [])

    def test_block_gate_definition_is_unknown_without_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            contract = {
                "quality_gates": [
                    {
                        "gate_id": "CORE01-Q1",
                        "severity": "BLOCK",
                        "rule": "identity must be unique",
                    }
                ]
            }
            result = AUDIT.evaluate_quality_gates(
                core_id="CORE-01",
                contract_item=contract,
                evidence_targets={},
                workspace=workspace,
                contract_evidence={
                    "path": "contract.json",
                    "sha256": "0" * 64,
                    "assertion": "contract defines the gate",
                },
            )
            self.assertEqual(result[0]["status"], "UNKNOWN")
            self.assertTrue(result[0]["blocks_admission"])
            self.assertEqual(
                result[0]["evidence_source"],
                "no_gate_specific_receipt_supplied",
            )
            self.assertIn(
                "contract definition alone is not failure evidence",
                result[0]["evidence"][0]["assertion"],
            )

    def test_hash_bound_pass_receipt_recomputes_gate_to_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            evidence_file = workspace / "evidence.json"
            evidence_file.write_text('{"passed":true}\n', encoding="utf-8")
            expected_hash = AUDIT.sha256_file(evidence_file)
            contract = {
                "quality_gates": [
                    {
                        "gate_id": "CORE01-Q1",
                        "severity": "BLOCK",
                        "rule": "identity must be unique",
                    }
                ]
            }
            result = AUDIT.evaluate_quality_gates(
                core_id="CORE-01",
                contract_item=contract,
                evidence_targets={
                    "CORE-01": {
                        "CORE01-Q1": {
                            "status": "PASS",
                            "evidence": [
                                {
                                    "path": "evidence.json",
                                    "sha256": expected_hash,
                                    "assertion": "the exact identity test passed",
                                }
                            ],
                        }
                    }
                },
                workspace=workspace,
                contract_evidence={
                    "path": "contract.json",
                    "sha256": "0" * 64,
                    "assertion": "contract defines the gate",
                },
            )
            self.assertEqual(result[0]["status"], "PASS")
            self.assertFalse(result[0]["blocks_admission"])
            self.assertTrue(result[0]["evidence"][0]["hash_verified"])

    def test_pass_or_fail_receipt_rejects_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            evidence_file = workspace / "evidence.json"
            evidence_file.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "evidence hash mismatch"):
                AUDIT.verify_gate_receipt(
                    {
                        "status": "PASS",
                        "evidence": [
                            {
                                "path": "evidence.json",
                                "sha256": "0" * 64,
                                "assertion": "test",
                            }
                        ],
                    },
                    workspace=workspace,
                    core_id="CORE-01",
                    gate_id="CORE01-Q1",
                )

    def test_unknown_receipt_still_requires_explanation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with self.assertRaisesRegex(ValueError, "non-empty evidence"):
                AUDIT.verify_gate_receipt(
                    {"status": "UNKNOWN", "evidence": []},
                    workspace=workspace,
                    core_id="CORE-01",
                    gate_id="CORE01-Q1",
                )

    def test_report_replacement_rejects_unregistered_residual(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "README.md").write_text("ok\n", encoding="utf-8")
            AUDIT.reject_unexpected_report_residuals(output)
            (output / "leftover.tmp").write_text("unexpected\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unregistered residuals"):
                AUDIT.reject_unexpected_report_residuals(output)

    def test_tree_hash_changes_when_payload_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "data" / "published"
            root.mkdir(parents=True)
            payload = root / "README.md"
            payload.write_text("one\n", encoding="utf-8")
            before = AUDIT.tree_state(root, workspace)
            payload.write_text("two\n", encoding="utf-8")
            after = AUDIT.tree_state(root, workspace)
            self.assertNotEqual(before["tree_sha256"], after["tree_sha256"])
            self.assertEqual(before["file_count"], 1)
            self.assertEqual(after["file_count"], 1)

    def test_quality_projection_is_order_independent(self) -> None:
        rows = [
            {
                "dataset_id": "b",
                "snapshot_id": "2",
                "quality_status": "REVIEW",
                "purpose_ceiling": "ENGINEERING_ONLY",
            },
            {
                "dataset_id": "a",
                "snapshot_id": "1",
                "quality_status": "OK",
                "purpose_ceiling": "RESEARCH_ONLY",
            },
        ]
        forward = AUDIT.catalog_quality_projection({"datasets": rows})
        reverse = AUDIT.catalog_quality_projection({"datasets": list(reversed(rows))})
        self.assertEqual(forward, reverse)

    def test_manifest_bundle_digest_is_canonical(self) -> None:
        records = [
            {"path": "a", "bytes": 1, "sha256": "1" * 64},
            {"path": "b", "bytes": 2, "sha256": "2" * 64},
        ]
        first = AUDIT.sha256_bytes(AUDIT.canonical_json_bytes(records))
        second = AUDIT.sha256_bytes(
            AUDIT.canonical_json_bytes(json.loads(json.dumps(records)))
        )
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
