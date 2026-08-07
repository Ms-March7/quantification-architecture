from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock


WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT = (
    WORKSPACE
    / "control"
    / "tools"
    / "audit_selected_core_two_stage_admission.py"
)
SPEC = importlib.util.spec_from_file_location("two_stage_admission", SCRIPT)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def pass_check() -> dict:
    return {
        "status": "PASS",
        "assertion": "hash-bound evidence passed",
        "evidence": [{"path": "evidence.json", "sha256": "1" * 64}],
        "blocks_admission": False,
    }


def fail_check() -> dict:
    return {
        "status": "FAIL",
        "assertion": "post-publish state is absent",
        "evidence": [{"path": "catalog.json", "sha256": "2" * 64}],
        "blocks_admission": True,
    }


def ready_legacy_target(core_id: str, dataset_id: str) -> dict:
    checks = {name: pass_check() for name in AUDIT.STAGE_A_CHECKS}
    checks.update(
        {name: fail_check() for name in AUDIT.LEGACY_POST_ADMISSION_CHECKS}
    )
    return {
        "core_id": core_id,
        "dataset_id": dataset_id,
        "candidate_support": "COMPLETE_EVIDENCE_CANDIDATE",
        "missing": [],
        "generic_gaps": {
            "rights": "",
            "schema": "",
            "pit": "",
            "revision": "",
            "quality": "",
        },
        "open_contract_blockers": [],
        "admission_checks": checks,
        "quality_gate_evaluations": [
            {
                "gate_id": f"{core_id}-Q1",
                "status": "PASS",
                "severity": "BLOCK",
                "blocks_admission": True,
                "evidence": [{"path": "quality.json", "sha256": "3" * 64}],
            }
        ],
        "blocking_reasons": [
            "check:formal_catalog_identity:FAIL",
            "check:qdata_research_resolution:FAIL",
        ],
        "gate_status": "BLOCKED__FAIL_CLOSED__NOT_ADMITTED",
        "formally_admitted": False,
    }


def ready_legacy_decision() -> dict:
    return {
        "schema_version": 2,
        "status": "BLOCKED__0_OF_4_ADMITTED__FAIL_CLOSED",
        "targets": [
            ready_legacy_target("CORE-01", "security-master"),
            ready_legacy_target("CORE-02", "calendar"),
            ready_legacy_target("CORE-03", "daily-raw"),
            ready_legacy_target("CORE-06", "tradability"),
        ],
    }


def valid_authorization(phase_a: dict) -> dict:
    return {
        "schema_version": 1,
        "action": AUDIT.AUTHORIZATION_ACTION,
        "scope": list(AUDIT.TARGETS),
        "phase_a_bundle_sha256": phase_a["bundle_sha256"],
        "authorized_by": "USER",
        "authorized_at_utc": "2026-07-30T00:00:00Z",
        "statement": "Authorize this exact bounded selected-CORE publication.",
    }


class TwoStageAdmissionTests(unittest.TestCase):
    def test_current_legacy_evidence_is_zero_of_four_stage_a_ready(self) -> None:
        path = (
            WORKSPACE
            / "reports"
            / "data-governance"
            / "d2b-selected-core-formal-admission-gate-20260730-v1"
            / "GATE_DECISION.json"
        )
        before = AUDIT.sha256_file(path)
        legacy = json.loads(path.read_text(encoding="utf-8"))
        result = AUDIT.evaluate_phase_a(legacy)
        after = AUDIT.sha256_file(path)
        self.assertEqual(result["ready_count"], 0)
        self.assertFalse(result["all_ready"])
        self.assertEqual(before, after)

    def test_pre_admission_stage_excludes_catalog_and_qdata(self) -> None:
        result = AUDIT.detect_pre_admission_cycle(AUDIT.STAGE_A_CHECKS)
        self.assertFalse(result["cycle_risk_detected"])
        self.assertEqual(result["offending_checks"], [])

    def test_legacy_combined_gate_has_the_exact_cycle(self) -> None:
        combined = (
            *AUDIT.STAGE_A_CHECKS,
            *AUDIT.LEGACY_POST_ADMISSION_CHECKS,
        )
        result = AUDIT.detect_pre_admission_cycle(combined)
        self.assertTrue(result["cycle_risk_detected"])
        self.assertEqual(
            result["offending_checks"],
            sorted(AUDIT.LEGACY_POST_ADMISSION_CHECKS),
        )

    def test_all_stage_a_passes_even_when_catalog_and_qdata_are_absent(self) -> None:
        result = AUDIT.evaluate_phase_a(ready_legacy_decision())
        self.assertTrue(result["all_ready"])
        self.assertEqual(result["ready_count"], 4)
        for target in result["targets"]:
            self.assertEqual(
                target["stage_a"]["status"],
                "PASS__PRE_ADMISSION_EVIDENCE_READY",
            )
            self.assertEqual(
                target["stage_b_legacy_observations"]["checks"][
                    "formal_catalog_identity"
                ]["status"],
                "FAIL",
            )
            self.assertEqual(
                target["stage_b_legacy_observations"]["checks"][
                    "formal_closure"
                ]["status"],
                "FAIL",
            )

    def test_stage_b_waits_for_explicit_authorization(self) -> None:
        phase_a = AUDIT.evaluate_phase_a(ready_legacy_decision())
        result = AUDIT.evaluate_phase_b(
            phase_a=phase_a,
            authorization=None,
            authorization_record=None,
            publish_receipt=None,
            publish_receipt_record=None,
        )
        self.assertEqual(
            result["status"], "WAITING__EXPLICIT_PHASE_B_AUTHORIZATION"
        )

    def test_valid_authorization_does_not_auto_publish(self) -> None:
        phase_a = AUDIT.evaluate_phase_a(ready_legacy_decision())
        result = AUDIT.evaluate_phase_b(
            phase_a=phase_a,
            authorization=valid_authorization(phase_a),
            authorization_record={"sha256": "a" * 64},
            publish_receipt=None,
            publish_receipt_record=None,
        )
        self.assertEqual(
            result["status"],
            "READY_FOR_SEPARATE_ATOMIC_PUBLISH__NOT_EXECUTED",
        )
        self.assertEqual(
            result["publication"]["status"],
            "AUTHORIZED__NO_PUBLISH_RECEIPT",
        )

    def test_post_publish_pass_requires_exact_catalog_and_qdata(self) -> None:
        phase_a = AUDIT.evaluate_phase_a(ready_legacy_decision())
        authorization = valid_authorization(phase_a)
        authorization_record = {"sha256": "a" * 64}
        publish_targets = [
            {
                "core_id": target["core_id"],
                "dataset_id": target["dataset_id"],
                "snapshot_id": f"20260730-{target['core_id'].lower()}",
                "content_hash": str(index) * 64,
            }
            for index, target in enumerate(phase_a["targets"], start=1)
        ]
        publish_receipt = {
            "schema_version": 1,
            "status": "PUBLISHED",
            "phase_a_bundle_sha256": phase_a["bundle_sha256"],
            "authorization_receipt_sha256": authorization_record["sha256"],
            "targets": deepcopy(publish_targets),
        }
        catalog = {
            "datasets": [
                {
                    "dataset_id": target["dataset_id"],
                    "snapshot_id": target["snapshot_id"],
                    "content_hash": target["content_hash"],
                    "mutability": "immutable",
                }
                for target in publish_targets
            ]
        }
        qdata_validation = {
            "status": "PASS",
            "targets": [
                {**target, "status": "PASS"} for target in publish_targets
            ],
        }
        result = AUDIT.evaluate_phase_b(
            phase_a=phase_a,
            authorization=authorization,
            authorization_record=authorization_record,
            publish_receipt=publish_receipt,
            publish_receipt_record={"sha256": "b" * 64},
            catalog=catalog,
            qdata_validation=qdata_validation,
        )
        self.assertEqual(
            result["status"],
            "PASS__PUBLISHED_AND_POST_PUBLISH_VALIDATED",
        )

    def test_post_publish_catalog_hash_mismatch_fails_closed(self) -> None:
        target = {
            "core_id": "CORE-01",
            "dataset_id": "security-master",
            "snapshot_id": "20260730-core01",
            "content_hash": "1" * 64,
        }
        result = AUDIT.evaluate_catalog_post_publish(
            publish_targets=[target],
            catalog={
                "datasets": [
                    {
                        "dataset_id": target["dataset_id"],
                        "snapshot_id": target["snapshot_id"],
                        "content_hash": "2" * 64,
                        "mutability": "immutable",
                    }
                ]
            },
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn(
            "content_hash",
            " ".join(result["targets"][0]["errors"]),
        )

    def test_post_publish_qdata_identity_mismatch_fails_closed(self) -> None:
        target = {
            "core_id": "CORE-01",
            "dataset_id": "security-master",
            "snapshot_id": "20260730-core01",
            "content_hash": "1" * 64,
        }
        result = AUDIT.evaluate_post_publish_validation(
            publish_targets=[target],
            catalog_validation={"status": "PASS", "targets": [target]},
            qdata_validation={
                "status": "PASS",
                "targets": [
                    {
                        **target,
                        "snapshot_id": "wrong-snapshot",
                        "status": "PASS",
                    }
                ],
            },
        )
        self.assertFalse(result["valid"])
        self.assertIn(
            "identity mismatch",
            " ".join(result["qdata"]["exact_identity_errors"]),
        )

    def test_publish_receipt_rejects_shell_metacharacter_snapshot(self) -> None:
        phase_a = AUDIT.evaluate_phase_a(ready_legacy_decision())
        targets = [
            {
                "core_id": target["core_id"],
                "dataset_id": target["dataset_id"],
                "snapshot_id": f"20260730-{target['core_id'].lower()}",
                "content_hash": "1" * 64,
            }
            for target in phase_a["targets"]
        ]
        targets[0]["snapshot_id"] = "snap&echo_INJECTED"
        result = AUDIT.validate_publish_receipt_value(
            {
                "schema_version": 1,
                "status": "PUBLISHED",
                "phase_a_bundle_sha256": phase_a["bundle_sha256"],
                "authorization_receipt_sha256": "a" * 64,
                "targets": targets,
            },
            phase_a_bundle_sha256=phase_a["bundle_sha256"],
            authorization_receipt_sha256="a" * 64,
            phase_a_targets=phase_a["targets"],
        )
        self.assertFalse(result["valid"])
        self.assertIn(
            "CORE-01 needs an exact non-latest snapshot_id",
            result["errors"],
        )

        command = AUDIT.qdata_command(
            WORKSPACE / "data" / "catalog" / "qdata.cmd",
            ["resolve", "security-master", "snap&echo_INJECTED"],
        )
        self.assertNotIn("/c", [item.lower() for item in command])
        self.assertEqual(command[-1], "snap&echo_INJECTED")
        self.assertTrue(command[3].endswith("dataset_cli.py"))

    def test_report_replace_rejects_hardlinked_allowed_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="two-stage-hardlink-") as value:
            root = Path(value)
            output = root / "report"
            output.mkdir()
            protected = root / "datasets.json"
            protected.write_text('{"datasets":[]}\n', encoding="utf-8")
            os.link(protected, output / "README.md")
            with self.assertRaisesRegex(ValueError, "hardlink_count"):
                AUDIT.reject_unexpected_report_residuals(output)
            self.assertEqual(
                protected.read_text(encoding="utf-8"),
                '{"datasets":[]}\n',
            )

    def test_report_replace_rejects_reparse_allowed_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="two-stage-reparse-") as value:
            output = Path(value) / "report"
            output.mkdir()
            readme = output / "README.md"
            readme.write_text("existing\n", encoding="utf-8")
            original = AUDIT.has_reparse_flag

            def mark_readme_as_reparse(path: Path) -> bool:
                if path == readme:
                    return True
                return original(path)

            with mock.patch.object(
                AUDIT,
                "has_reparse_flag",
                side_effect=mark_readme_as_reparse,
            ):
                with self.assertRaisesRegex(ValueError, "reparse_or_symlink"):
                    AUDIT.reject_unexpected_report_residuals(output)

    def test_current_publisher_is_not_misrepresented_as_real_data_capable(
        self,
    ) -> None:
        publisher = WORKSPACE / "control" / "tools" / "atomic_snapshot_publish.py"
        result = AUDIT.assess_current_publisher(publisher)
        self.assertTrue(result["synthetic_only"])
        self.assertEqual(
            result["status"],
            "REAL_DATA_UNSUPPORTED__SYNTHETIC_FIXTURE_ONLY",
        )


if __name__ == "__main__":
    unittest.main()
