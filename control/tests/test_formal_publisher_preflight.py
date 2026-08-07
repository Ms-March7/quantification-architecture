from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT = WORKSPACE / "control" / "tools" / "run_formal_publisher_preflight.py"
SPEC = importlib.util.spec_from_file_location("formal_publisher_preflight", SCRIPT)
assert SPEC and SPEC.loader
PREFLIGHT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PREFLIGHT
SPEC.loader.exec_module(PREFLIGHT)


def canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def file_state(path: Path) -> tuple[int, str] | None:
    if not path.exists():
        return None
    payload = path.read_bytes()
    return len(payload), digest(payload)


def tree_state(path: Path) -> tuple[tuple[str, int, str], ...]:
    if not path.exists():
        return ()
    return tuple(
        (item.relative_to(path).as_posix(), item.stat().st_size, digest(item.read_bytes()))
        for item in sorted(path.rglob("*"))
        if item.is_file()
    )


class FormalPublisherPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token = uuid.uuid4().hex
        self.root = WORKSPACE / "tmp" / "formal-publisher-preflight-tests" / self.token
        self.root.mkdir(parents=True)
        PREFLIGHT.REQUEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.request_path = PREFLIGHT.REQUEST_ROOT / f"test-{self.token}.json"
        self.dataset_id = f"test-daily-{self.token[:12]}"
        self.snapshot_id = f"2026-07-31-{self.token[:12]}"
        self.target_relative = (
            f"data/market/snapshots/{self.dataset_id}/{self.snapshot_id}"
        )
        self.artifact = self.root / "candidate.parquet"
        self.artifact.write_bytes(b"sealed candidate fixture\n")
        self.candidate_path = self.root / "candidate-manifest.json"
        self.stage_path = self.root / "stage-a.json"
        self.rights_path = self.root / "rights.json"
        self.auth_path = self.root / "authorization.json"
        self._write_all()
        self.guarded_before = self._guarded_state()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        self.request_path.unlink(missing_ok=True)

    def _relative(self, path: Path) -> str:
        return path.relative_to(WORKSPACE).as_posix()

    def _write_json(self, path: Path, value: dict[str, object]) -> None:
        path.write_bytes(canonical(value))

    def _binding(self, path: Path, contract_id: str) -> dict[str, object]:
        payload = path.read_bytes()
        return {
            "relative_path": self._relative(path),
            "sha256": digest(payload),
            "bytes": len(payload),
            "contract_id": contract_id,
        }

    def _write_all(self) -> None:
        artifact_payload = self.artifact.read_bytes()
        candidate = {
            "schema_version": 1,
            "contract_id": "candidate_manifest_v1",
            "candidate_id": f"candidate-{self.token}",
            "dataset_id": self.dataset_id,
            "snapshot_id": self.snapshot_id,
            "purpose_ceiling": "INSPECTION_ONLY",
            "formal_source_admission": False,
            "formal_catalog_written": False,
            "research_ready": False,
            "artifacts": [
                {
                    "relative_path": self._relative(self.artifact),
                    "sha256": digest(artifact_payload),
                    "bytes": len(artifact_payload),
                }
            ],
        }
        self._write_json(self.candidate_path, candidate)
        stage_bundle = {
            "all_ready": True,
            "ready_count": 4,
            "target_count": 4,
            "targets": ["CORE-01", "CORE-02", "CORE-03", "CORE-06"],
            "formal_effect": "NONE",
        }
        stage = {
            "schema_version": 1,
            "contract_id": "selected_core_stage_a_bundle_v1",
            "status": "PASS__ALL_TARGETS_PRE_ADMISSION_READY",
            "bundle": stage_bundle,
            "bundle_sha256": digest(
                json.dumps(
                    stage_bundle,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ),
        }
        self._write_json(self.stage_path, stage)
        candidate_sha = digest(self.candidate_path.read_bytes())
        rights = {
            "schema_version": 1,
            "contract_id": "source_rights_receipt_v1",
            "receipt_id": f"rights-{self.token}",
            "status": "PASS",
            "dataset_id": self.dataset_id,
            "source_id": "TEST_SOURCE",
            "candidate_manifest_sha256": candidate_sha,
            "rights": {
                "local_storage": True,
                "internal_research": True,
                "derived_research": True,
                "raw_retention": True,
                "no_external_redistribution": True,
            },
        }
        self._write_json(self.rights_path, rights)
        rights_sha = digest(self.rights_path.read_bytes())
        authorization = {
            "schema_version": 1,
            "contract_id": "formal_publish_authorization_receipt_v1",
            "authorization_id": f"auth-{self.token}",
            "action": PREFLIGHT.AUTHORIZATION_ACTION,
            "authorized_by": "USER",
            "authorized_at_utc": "2026-07-31T16:00:00Z",
            "statement": "Authorize only this exact immutable publication request.",
            "scope": stage_bundle["targets"],
            "dataset_id": self.dataset_id,
            "snapshot_id": self.snapshot_id,
            "target_relative_path": self.target_relative,
            "candidate_manifest_sha256": candidate_sha,
            "stage_a_bundle_sha256": stage["bundle_sha256"],
            "source_rights_receipt_sha256": rights_sha,
            "single_use": True,
        }
        self._write_json(self.auth_path, authorization)
        self.request = {
            "schema_version": 1,
            "contract_id": PREFLIGHT.CONTRACT_ID,
            "preflight_id": f"preflight-{self.token}",
            "candidate_manifest": self._binding(
                self.candidate_path, "candidate_manifest_v1"
            ),
            "stage_a_bundle": self._binding(
                self.stage_path, "selected_core_stage_a_bundle_v1"
            ),
            "source_rights_receipt": self._binding(
                self.rights_path, "source_rights_receipt_v1"
            ),
            "user_authorization_receipt": self._binding(
                self.auth_path, "formal_publish_authorization_receipt_v1"
            ),
            "target": {
                "dataset_id": self.dataset_id,
                "snapshot_id": self.snapshot_id,
                "formal_root": "data/market/snapshots",
                "target_relative_path": self.target_relative,
            },
            "allowed_formal_roots": ["data/market/snapshots"],
            "qdata_postconditions": {
                "dataset_id": self.dataset_id,
                "snapshot_id": self.snapshot_id,
                "intent": "research",
                "consumer": "formal-preflight-test",
                "require_exact_identity": True,
                "forbid_latest": True,
                "require_immutable_snapshot": True,
                "require_catalog_atomic_visibility": True,
                "require_research_resolution_after_publish": True,
                "failure_action": "ROLLBACK_OR_FAIL_CLOSED",
            },
        }
        self._write_json(self.request_path, self.request)

    def _rewrite_request(self) -> None:
        self._write_json(self.request_path, self.request)

    def _guarded_state(self) -> dict[str, object]:
        return {
            "catalog": file_state(WORKSPACE / "data/catalog/datasets.json"),
            "registry": file_state(
                WORKSPACE
                / "data/catalog/contracts/source_admission_v1/current_source_registry.json"
            ),
            "snapshots": tree_state(WORKSPACE / "data/market/snapshots"),
            "published": tree_state(WORKSPACE / "data/published"),
        }

    def _run(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPT), "--request", str(self.request_path)],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_complete_chain_returns_non_executable_stdout_plan(self) -> None:
        process = self._run()
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["status"], "PASS__NON_EXECUTABLE_PREFLIGHT_ONLY")
        self.assertFalse(result["executable"])
        self.assertFalse(result["formal_write"])
        self.assertFalse(result["write_performed"])
        self.assertEqual(result["formal_effect"], "NONE")
        self.assertEqual(result["blocking_gaps"], [])
        self.assertFalse((WORKSPACE / self.target_relative).exists())
        self.assertEqual(self._guarded_state(), self.guarded_before)

    def test_hash_or_byte_mismatch_fails_closed(self) -> None:
        self.request["candidate_manifest"]["bytes"] += 1
        self._rewrite_request()
        process = self._run()
        self.assertEqual(process.returncode, 2)
        result = json.loads(process.stdout)
        self.assertEqual(result["status"], "FAIL_CLOSED")
        self.assertFalse(result["executable"])
        self.assertFalse(result["formal_write"])
        self.assertEqual(self._guarded_state(), self.guarded_before)

    def test_stage_a_not_ready_fails_closed(self) -> None:
        stage = json.loads(self.stage_path.read_text(encoding="utf-8"))
        stage["bundle"]["all_ready"] = False
        stage["bundle"]["ready_count"] = 3
        stage["bundle_sha256"] = digest(
            json.dumps(
                stage["bundle"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        self._write_json(self.stage_path, stage)
        self.request["stage_a_bundle"] = self._binding(
            self.stage_path, "selected_core_stage_a_bundle_v1"
        )
        self._rewrite_request()
        process = self._run()
        self.assertEqual(process.returncode, 2)
        self.assertIn("Stage-A", json.loads(process.stdout)["blocking_gaps"][0])

    def test_authorization_must_bind_exact_rights_and_stage_a(self) -> None:
        authorization = json.loads(self.auth_path.read_text(encoding="utf-8"))
        authorization["source_rights_receipt_sha256"] = "0" * 64
        self._write_json(self.auth_path, authorization)
        self.request["user_authorization_receipt"] = self._binding(
            self.auth_path, "formal_publish_authorization_receipt_v1"
        )
        self._rewrite_request()
        process = self._run()
        self.assertEqual(process.returncode, 2)
        self.assertIn("authorization", json.loads(process.stdout)["blocking_gaps"][0])

    def test_mutable_identity_and_missing_qdata_condition_fail_closed(self) -> None:
        self.request["target"]["snapshot_id"] = "latest"
        self._rewrite_request()
        self.assertEqual(self._run().returncode, 2)
        self._write_all()
        self.request["qdata_postconditions"]["forbid_latest"] = False
        self._rewrite_request()
        process = self._run()
        self.assertEqual(process.returncode, 2)
        self.assertIn("forbid_latest", json.loads(process.stdout)["blocking_gaps"][0])

    def test_path_escape_and_unapproved_formal_root_fail_closed(self) -> None:
        self.request["candidate_manifest"]["relative_path"] = "../candidate.json"
        self._rewrite_request()
        self.assertEqual(self._run().returncode, 2)
        self._write_all()
        self.request["allowed_formal_roots"] = ["data/market/raw"]
        self._rewrite_request()
        self.assertEqual(self._run().returncode, 2)

    def test_tool_has_no_execute_or_publisher_call_surface(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("--execute", source)
        self.assertNotIn("atomic_snapshot_publish", source)
        self.assertNotIn("subprocess", source)
        help_result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0)
        self.assertNotIn("execute", help_result.stdout.lower())

    def test_contract_files_are_valid_json_and_guarded_state_is_unchanged(self) -> None:
        contract_root = (
            WORKSPACE / "data/catalog/contracts/formal_publisher_preflight_v1"
        )
        for name in ("contract.json", "request.schema.json"):
            value = json.loads((contract_root / name).read_text(encoding="utf-8"))
            self.assertIsInstance(value, dict)
        self.assertEqual(self._guarded_state(), self.guarded_before)


if __name__ == "__main__":
    unittest.main()
