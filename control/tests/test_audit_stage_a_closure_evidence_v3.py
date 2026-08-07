from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "control/tools/audit_stage_a_closure_evidence_v3.py"
SPEC = importlib.util.spec_from_file_location("stage_a_v3", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class StageAEvidenceV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "tmp")
        self.workspace = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _package(
        self,
        *,
        external_binding: dict[str, object] | None = None,
    ) -> tuple[Path, str]:
        root = self.workspace / "reports/package"
        root.mkdir(parents=True)
        payload = root / "PAYLOAD.json"
        self._write_json(payload, {"value": 1})
        manifest: dict[str, object] = {
            "files": [
                {
                    "path": "PAYLOAD.json",
                    "bytes": payload.stat().st_size,
                    "sha256": MODULE.sha256_file(payload),
                }
            ]
        }
        if external_binding is not None:
            manifest["external_bindings"] = {"source": external_binding}
        manifest_path = root / "REPORT_MANIFEST.json"
        self._write_json(manifest_path, manifest)
        return root, MODULE.sha256_file(manifest_path)

    def _strict_report_package(self) -> tuple[Path, str]:
        candidate = self.workspace / "tmp/candidate"
        candidate.mkdir(parents=True)
        candidate_manifest = candidate / "CANDIDATE_MANIFEST.json"
        candidate_payload = candidate / "PAYLOAD.json"
        self._write_json(candidate_payload, {"candidate": True})
        self._write_json(
            candidate_manifest,
            {"run_id": "strict-test", "files": [{
                "path": "PAYLOAD.json",
                "bytes": candidate_payload.stat().st_size,
                "sha256": MODULE.sha256_file(candidate_payload),
            }]},
        )
        root = self.workspace / "reports/package"
        root.mkdir(parents=True)
        payload = root / "PAYLOAD.json"
        self._write_json(payload, {"value": 1})
        manifest_path = root / "REPORT_MANIFEST.json"
        self._write_json(
            manifest_path,
            {
                "run_id": "strict-test",
                "strict_disk_set_excludes": [
                    "REPORT_MANIFEST.json", "VERIFICATION.json"
                ],
                "files": [{
                    "path": "PAYLOAD.json",
                    "bytes": payload.stat().st_size,
                    "sha256": MODULE.sha256_file(payload),
                }],
            },
        )
        self._write_json(
            root / "VERIFICATION.json",
            {
                "run_id": "strict-test",
                "status": "PASS",
                "report_manifest": {
                    "path": manifest_path.relative_to(self.workspace).as_posix(),
                    "bytes": manifest_path.stat().st_size,
                    "sha256": MODULE.sha256_file(manifest_path),
                },
                "candidate_manifest": {
                    "path": candidate_manifest.relative_to(self.workspace).as_posix(),
                    "bytes": candidate_manifest.stat().st_size,
                    "sha256": MODULE.sha256_file(candidate_manifest),
                },
            },
        )
        return root, MODULE.sha256_file(manifest_path)

    def test_strict_package_accepts_exact_file_set(self) -> None:
        root, digest = self._package()
        result = MODULE.strict_manifest_package(
            package_id="TEST",
            root=root,
            manifest_name="REPORT_MANIFEST.json",
            expected_manifest_sha256=digest,
            workspace=self.workspace,
        )
        self.assertEqual("PASS", result["status"])
        self.assertEqual(0, result["file_set"]["extra_file_count"])

    def test_strict_package_rejects_undeclared_extra_file(self) -> None:
        root, digest = self._package()
        (root / "EXTRA.txt").write_text("extra", encoding="utf-8")
        result = MODULE.strict_manifest_package(
            package_id="TEST",
            root=root,
            manifest_name="REPORT_MANIFEST.json",
            expected_manifest_sha256=digest,
            workspace=self.workspace,
        )
        self.assertEqual("FAIL", result["status"])
        self.assertEqual(1, result["file_set"]["extra_file_count"])

    def test_strict_package_rejects_manifest_pin_mismatch(self) -> None:
        root, _ = self._package()
        result = MODULE.strict_manifest_package(
            package_id="TEST",
            root=root,
            manifest_name="REPORT_MANIFEST.json",
            expected_manifest_sha256="0" * 64,
            workspace=self.workspace,
        )
        self.assertEqual("FAIL", result["status"])
        self.assertIn(
            "manifest SHA-256 does not match explicit input pin",
            result["errors"],
        )

    def test_strict_report_excludes_only_verified_fixed_files(self) -> None:
        root, digest = self._strict_report_package()
        result = MODULE.strict_manifest_package(
            package_id="TEST", root=root, manifest_name="REPORT_MANIFEST.json",
            expected_manifest_sha256=digest, workspace=self.workspace,
        )
        self.assertEqual("PASS", result["status"])
        self.assertEqual(0, result["file_set"]["extra_file_count"])

    def test_strict_report_rejects_unknown_exclude_or_missing_verification(self) -> None:
        root, digest = self._strict_report_package()
        manifest_path = root / "REPORT_MANIFEST.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["strict_disk_set_excludes"] = ["REPORT_MANIFEST.json", "EXTRA.txt"]
        self._write_json(manifest_path, manifest)
        result = MODULE.strict_manifest_package(
            package_id="TEST", root=root, manifest_name="REPORT_MANIFEST.json",
            expected_manifest_sha256=None, workspace=self.workspace,
        )
        self.assertEqual("FAIL", result["status"])
        self.assertTrue(any("strict_disk_set_excludes" in error for error in result["errors"]))
        manifest["strict_disk_set_excludes"] = [
            "REPORT_MANIFEST.json", "VERIFICATION.json"
        ]
        self._write_json(manifest_path, manifest)
        (root / "VERIFICATION.json").unlink()
        result = MODULE.strict_manifest_package(
            package_id="TEST", root=root, manifest_name="REPORT_MANIFEST.json",
            expected_manifest_sha256=None, workspace=self.workspace,
        )
        self.assertEqual("FAIL", result["status"])
        self.assertIn("VERIFICATION.json missing", result["errors"])

    def test_strict_report_rejects_forged_verification_binding(self) -> None:
        root, digest = self._strict_report_package()
        verification_path = root / "VERIFICATION.json"
        verification = json.loads(verification_path.read_text(encoding="utf-8"))
        verification["report_manifest"]["sha256"] = "0" * 64
        self._write_json(verification_path, verification)
        result = MODULE.strict_manifest_package(
            package_id="TEST", root=root, manifest_name="REPORT_MANIFEST.json",
            expected_manifest_sha256=digest, workspace=self.workspace,
        )
        self.assertEqual("FAIL", result["status"])
        self.assertTrue(any("report_manifest binding failure" in error for error in result["errors"]))

    def test_strict_package_checks_root_external_binding(self) -> None:
        external = self.workspace / "control/source.json"
        self._write_json(external, {"source": True})
        binding = {
            "path": external.relative_to(self.workspace).as_posix(),
            "bytes": external.stat().st_size,
            "sha256": MODULE.sha256_file(external),
        }
        root, digest = self._package(external_binding=binding)
        external.write_text("{}\n", encoding="utf-8")
        result = MODULE.strict_manifest_package(
            package_id="TEST",
            root=root,
            manifest_name="REPORT_MANIFEST.json",
            expected_manifest_sha256=digest,
            workspace=self.workspace,
        )
        self.assertEqual("FAIL", result["status"])
        self.assertGreater(result["bindings"]["failed_binding_count"], 0)

    def test_independent_qa_is_fail_closed(self) -> None:
        qa = {
            component: {
                "status": "PASS",
                "p0": 0,
                "p1": 0,
                "p2": 0,
                "basis": "test",
            }
            for component in MODULE.EXPECTED_COMPONENT_IDS
        }
        qa["CORE02"]["status"] = "FAIL"
        ok, errors = MODULE.independent_qa_release(qa)
        self.assertFalse(ok)
        self.assertEqual(1, len(errors))

    def test_release_decision_never_promotes_formal_state(self) -> None:
        decision = MODULE.release_decision(
            packages_ok=True,
            executables_ok=True,
            upstream_ok=True,
            semantics_ok=True,
            independent_qa_ok=True,
            guards_ok=True,
        )
        self.assertTrue(decision["report_publish_eligible"])
        self.assertEqual("NONE", decision["formal_effect"])
        self.assertFalse(decision["formal_admission"])
        self.assertFalse(decision["research_authorization"])
        self.assertEqual("0/4", decision["formal_stage_a_after"])

    def test_release_decision_rebuild_required_on_any_failure(self) -> None:
        decision = MODULE.release_decision(
            packages_ok=True,
            executables_ok=True,
            upstream_ok=True,
            semantics_ok=True,
            independent_qa_ok=False,
            guards_ok=True,
        )
        self.assertFalse(decision["report_publish_eligible"])
        self.assertEqual(
            "UNPUBLISHABLE__REBUILD_REQUIRED", decision["status"]
        )

    def test_core02_q3_is_frozen_unknown(self) -> None:
        self.assertEqual(
            "UNKNOWN", MODULE.EXPECTED_GATE_STATUSES["CORE02-Q3"]
        )

    def test_proxy_boundary_requires_explicit_unknown(self) -> None:
        self.assertTrue(
            MODULE.find_proxy_unknown(
                {
                    "transport_boundary": {
                        "proxy_and_credential_origin_status": "UNKNOWN"
                    }
                }
            )
        )
        self.assertFalse(
            MODULE.find_proxy_unknown(
                {"transport_proxy_authentication_fully_observable": False}
            )
        )

    def test_stage_baseline_rejects_nonzero_ready_count(self) -> None:
        state = self.workspace / "STATE_MACHINE.json"
        self._write_json(
            state,
            {
                "phase_a": {
                    "ready_count": 1,
                    "target_count": 4,
                    "all_ready": False,
                    "status": "PARTIAL",
                }
            },
        )
        with self.assertRaises(ValueError):
            MODULE.stage_baseline(state)

    def test_serializable_input_spec_converts_nested_paths(self) -> None:
        normalized = {
            "packages": {
                "PACKAGE": {
                    "root": self.workspace / "reports/package",
                    "manifest": "REPORT_MANIFEST.json",
                    "sha256": "0" * 64,
                }
            },
            "stage_machine": {
                "path": self.workspace / "control/STATE_MACHINE.json",
                "sha256": "1" * 64,
            },
            "executables": {
                "CORE01": {
                    "generator_source": {
                        "path": self.workspace / "control/generator.py",
                        "sha256": "2" * 64,
                    }
                }
            },
            "independent_qa": {
                "CORE01": {
                    "status": "PASS",
                    "p0": 0,
                    "p1": 0,
                    "p2": 0,
                    "basis": "independent test",
                    "receipt": {
                        "path": self.workspace / "reports/qa.json",
                        "sha256": "3" * 64,
                    },
                }
            },
        }
        serializable = MODULE._serializable_input_spec(
            normalized, self.workspace
        )
        json.dumps(serializable)
        self.assertEqual(
            "reports/qa.json",
            serializable["independent_qa"]["CORE01"]["receipt"]["path"],
        )

    def test_output_allowlist_rejects_extra_file(self) -> None:
        root = self.workspace / "reports/output"
        root.mkdir(parents=True)
        for name in MODULE.EXPECTED_OUTPUT_FILES:
            (root / name).write_text("{}\n", encoding="utf-8")
        (root / "EXTRA.txt").write_text("extra", encoding="utf-8")
        extras, missing = MODULE.reject_extra_or_missing(root)
        self.assertEqual(["EXTRA.txt"], extras)
        self.assertEqual([], missing)


if __name__ == "__main__":
    unittest.main()
