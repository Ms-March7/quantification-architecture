from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
PYTHON = sys.executable
SCRIPT = WORKSPACE / "control/tools/execute_formal_atomic_publish_isolated.py"
PREFLIGHT_REQUEST_ROOT = WORKSPACE / "tmp/formal-publisher-preflight/requests"
EXECUTION_REQUEST_ROOT = WORKSPACE / "tmp/formal-publisher-execution/requests"
ISOLATED_ROOT = WORKSPACE / "tmp/formal-publisher-isolated-workspaces"
SENTINEL = ".qdata-formal-publisher-isolated-workspace.json"


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


class IsolatedFormalAtomicPublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token = uuid.uuid4().hex
        self.fixture = WORKSPACE / "tmp/formal-publisher-isolated-tests" / self.token
        self.fixture.mkdir(parents=True)
        PREFLIGHT_REQUEST_ROOT.mkdir(parents=True, exist_ok=True)
        EXECUTION_REQUEST_ROOT.mkdir(parents=True, exist_ok=True)
        ISOLATED_ROOT.mkdir(parents=True, exist_ok=True)
        self.preflight_path = PREFLIGHT_REQUEST_ROOT / f"isolated-{self.token}.json"
        self.execution_path = EXECUTION_REQUEST_ROOT / f"isolated-{self.token}.json"
        self.isolated = ISOLATED_ROOT / self.token
        self.dataset_id = f"test-formal-daily-{self.token[:12]}"
        self.snapshot_id = f"2026-08-01-{self.token[:12]}"
        self.target_relative = f"data/market/snapshots/{self.dataset_id}/{self.snapshot_id}"
        self.raw = self.fixture / "raw-response.json"
        self.curated = self.fixture / "part-000.parquet"
        self.raw.write_bytes(b'{"source":"fixture","status":200}\n')
        self.curated.write_bytes(b"PAR1 isolated-formal-publisher PAR1\n")
        self.candidate = self.fixture / "candidate-manifest.json"
        self.stage_a = self.fixture / "stage-a.json"
        self.rights = self.fixture / "rights.json"
        self.authorization = self.fixture / "authorization.json"
        self._write_evidence()
        self._write_preflight()
        self._write_execution()
        self._write_isolated_workspace()
        self.real_before = self._real_guard()

    def tearDown(self) -> None:
        shutil.rmtree(self.fixture, ignore_errors=True)
        shutil.rmtree(self.isolated, ignore_errors=True)
        self.preflight_path.unlink(missing_ok=True)
        self.execution_path.unlink(missing_ok=True)

    def _relative(self, path: Path) -> str:
        return path.relative_to(WORKSPACE).as_posix()

    def _write_json(self, path: Path, value: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical(value))

    def _binding(self, path: Path, contract_id: str) -> dict[str, object]:
        payload = path.read_bytes()
        return {
            "relative_path": self._relative(path),
            "sha256": digest(payload),
            "bytes": len(payload),
            "contract_id": contract_id,
        }

    def _write_evidence(self) -> None:
        artifacts = []
        for path in (self.raw, self.curated):
            payload = path.read_bytes()
            artifacts.append(
                {
                    "relative_path": self._relative(path),
                    "sha256": digest(payload),
                    "bytes": len(payload),
                }
            )
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
            "artifacts": artifacts,
        }
        self._write_json(self.candidate, candidate)
        bundle = {
            "all_ready": True,
            "ready_count": 4,
            "target_count": 4,
            "targets": ["CORE-01", "CORE-02", "CORE-03", "CORE-06"],
            "formal_effect": "NONE",
        }
        stage_a = {
            "schema_version": 1,
            "contract_id": "selected_core_stage_a_bundle_v1",
            "status": "PASS__ALL_TARGETS_PRE_ADMISSION_READY",
            "bundle": bundle,
            "bundle_sha256": digest(
                json.dumps(
                    bundle,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ),
        }
        self._write_json(self.stage_a, stage_a)
        candidate_sha = digest(self.candidate.read_bytes())
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
        self._write_json(self.rights, rights)
        authorization = {
            "schema_version": 1,
            "contract_id": "formal_publish_authorization_receipt_v1",
            "authorization_id": f"auth-{self.token}",
            "action": "AUTHORIZE_EXACT_FORMAL_SNAPSHOT_PUBLICATION",
            "authorized_by": "USER",
            "authorized_at_utc": "2026-08-01T00:00:00Z",
            "statement": "Authorize only this exact immutable publication request.",
            "scope": bundle["targets"],
            "dataset_id": self.dataset_id,
            "snapshot_id": self.snapshot_id,
            "target_relative_path": self.target_relative,
            "candidate_manifest_sha256": candidate_sha,
            "stage_a_bundle_sha256": stage_a["bundle_sha256"],
            "source_rights_receipt_sha256": digest(self.rights.read_bytes()),
            "single_use": True,
        }
        self._write_json(self.authorization, authorization)

    def _write_preflight(self) -> None:
        self.preflight = {
            "schema_version": 1,
            "contract_id": "formal_publisher_preflight_v1",
            "preflight_id": f"preflight-{self.token}",
            "candidate_manifest": self._binding(self.candidate, "candidate_manifest_v1"),
            "stage_a_bundle": self._binding(self.stage_a, "selected_core_stage_a_bundle_v1"),
            "source_rights_receipt": self._binding(self.rights, "source_rights_receipt_v1"),
            "user_authorization_receipt": self._binding(
                self.authorization, "formal_publish_authorization_receipt_v1"
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
                "consumer": "formal-execution-test",
                "require_exact_identity": True,
                "forbid_latest": True,
                "require_immutable_snapshot": True,
                "require_catalog_atomic_visibility": True,
                "require_research_resolution_after_publish": True,
                "failure_action": "ROLLBACK_OR_FAIL_CLOSED",
            },
        }
        self._write_json(self.preflight_path, self.preflight)

    def _write_execution(self, execution_id: str | None = None) -> None:
        catalog = WORKSPACE / "data/catalog/datasets.json"
        catalog_payload = catalog.read_bytes()
        self.execution = {
            "schema_version": 1,
            "contract_id": "formal_atomic_publish_execution_v1",
            "execution_id": execution_id or f"formal-execution-{self.token}",
            "preflight_request": self._binding(
                self.preflight_path, "formal_publisher_preflight_v1"
            ),
            "catalog_before": {
                "relative_path": "data/catalog/datasets.json",
                "sha256": digest(catalog_payload),
                "bytes": len(catalog_payload),
            },
            "target": dict(self.preflight["target"]),
            "payload_layout": [
                {
                    "source_relative_path": self._relative(self.raw),
                    "layer": "raw",
                    "snapshot_relative_path": "response.json",
                    "sha256": digest(self.raw.read_bytes()),
                    "bytes": self.raw.stat().st_size,
                },
                {
                    "source_relative_path": self._relative(self.curated),
                    "layer": "curated",
                    "snapshot_relative_path": "year=2026/month=08/part-000.parquet",
                    "sha256": digest(self.curated.read_bytes()),
                    "bytes": self.curated.stat().st_size,
                },
            ],
            "publication_profile": {
                "store_id": "workspace-market-snapshots",
                "owner_project": "workspace",
                "lifecycle": "frozen",
                "mutability": "immutable",
                "purpose_ceiling": "RESEARCH_ONLY",
                "license_status": "VERIFIED",
                "quality_status": "OK",
                "consumers": ["formal-execution-test"],
                "known_limits": ["isolated fixture only"],
                "notes": "Formal publisher isolated execution fixture.",
                "source_id": "TEST_SOURCE",
                "dataset_contract_id": "test_daily_v1",
                "dataset_schema_version": "v1",
                "research_input_contract": {
                    "schema_version": 1,
                    "contract_id": "test_daily_v1",
                    "dataset_id": self.dataset_id,
                    "snapshot_id": self.snapshot_id,
                },
                "available_at": "2026-08-01T00:00:00+08:00",
                "published_at": "2026-08-01T01:00:00+08:00",
                "partition_segments": ["year=2026", "month=08"],
            },
            "state_machine_requirements": {
                "require_private_staging": True,
                "require_same_volume_finalize": True,
                "require_catalog_compare_and_swap": True,
                "require_crash_recovery": True,
                "require_qdata_postcondition_before_success": True,
                "require_catalog_rollback_on_qdata_failure": True,
                "consume_authorization_only_after_qdata_success": True,
                "failure_action": "ROLLBACK_OR_FAIL_CLOSED",
            },
        }
        self._write_json(self.execution_path, self.execution)

    def _write_isolated_workspace(self) -> None:
        authority = self.isolated / "data/catalog/ACCESS_POLICY.md"
        authority.parent.mkdir(parents=True, exist_ok=True)
        authority.write_text("isolated fixture\n", encoding="utf-8")
        root_text = self.isolated.as_posix()
        catalog = {
            "schema_version": 1,
            "updated_at": "2026-08-01T00:00:00+08:00",
            "workspace_root": root_text,
            "catalog_mode": "federated_logical_catalog",
            "physical_migration": True,
            "authority_precedence": ["snapshot_manifest_or_provenance"],
            "owners": ["workspace", "formal-execution-test"],
            "stores": [
                {
                    "store_id": "workspace-market-snapshots",
                    "owner_project": "workspace",
                    "physical_root": (self.isolated / "data/market/snapshots").as_posix(),
                    "kind": "immutable_snapshot_store",
                    "lifecycle": "active",
                    "mutability": "append_only",
                    "purpose_ceiling": "RESEARCH_ONLY",
                    "authority_paths": [authority.as_posix()],
                }
            ],
            "datasets": [],
            "retired_datasets": [],
            "publication_contracts": [],
        }
        self._write_json(self.isolated / "data/catalog/datasets.json", catalog)
        sentinel = {
            "schema_version": 1,
            "contract_id": "formal_publisher_isolated_workspace_v1",
            "workspace_id": self.token,
            "workspace_root": root_text,
            "source_workspace_root": WORKSPACE.as_posix(),
            "source_catalog_sha256": self.execution["catalog_before"]["sha256"],
            "allow_isolated_formal_execution": True,
        }
        self._write_json(self.isolated / SENTINEL, sentinel)
        (self.isolated / "data/market/snapshots").mkdir(parents=True, exist_ok=True)

    def _real_guard(self) -> dict[str, object]:
        return {
            "catalog": file_state(WORKSPACE / "data/catalog/datasets.json"),
            "registry": file_state(
                WORKSPACE
                / "data/catalog/contracts/source_admission_v1/current_source_registry.json"
            ),
            "snapshots": tree_state(WORKSPACE / "data/market/snapshots"),
            "published": tree_state(WORKSPACE / "data/published"),
        }

    def _run(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                PYTHON,
                "-X",
                "utf8",
                str(SCRIPT),
                "--request",
                str(self.execution_path),
                "--isolated-workspace",
                str(self.isolated),
                *extra,
            ],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_default_is_disabled_and_performs_no_write(self) -> None:
        before = tree_state(self.isolated)
        process = self._run()
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["status"], "PASS__ISOLATED_EXECUTION_DISABLED__NO_WRITE")
        self.assertFalse(result["write_performed"])
        self.assertEqual(tree_state(self.isolated), before)
        self.assertEqual(self._real_guard(), self.real_before)

    def test_success_executes_full_state_machine_only_in_isolated_copy(self) -> None:
        process = self._run("--execute-isolated")
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(result["state"], "PUBLISHED")
        catalog = json.loads((self.isolated / "data/catalog/datasets.json").read_text("utf-8"))
        self.assertEqual(len(catalog["datasets"]), 1)
        item = catalog["datasets"][0]
        self.assertEqual((item["dataset_id"], item["snapshot_id"]), (self.dataset_id, self.snapshot_id))
        target = self.isolated / self.target_relative
        for relative in (
            "request.json",
            "manifest.json",
            "provenance.json",
            "partition-manifest.json",
            "quality/summary.json",
        ):
            self.assertTrue((target / relative).is_file(), relative)
        manifest = json.loads((target / "manifest.json").read_text("utf-8"))
        self.assertEqual(
            manifest["research_input_contract"],
            self.execution["publication_profile"]["research_input_contract"],
        )
        ledger = json.loads(Path(result["authorization_ledger_path"]).read_text("utf-8"))
        self.assertEqual(ledger["state"], "CONSUMED")
        journal = json.loads(Path(result["journal_path"]).read_text("utf-8"))
        self.assertEqual(journal["state"], "PUBLISHED")
        self.assertEqual(self._real_guard(), self.real_before)

    def test_real_workspace_and_existing_target_fail_before_write(self) -> None:
        process = subprocess.run(
            [
                PYTHON,
                "-X",
                "utf8",
                str(SCRIPT),
                "--request",
                str(self.execution_path),
                "--isolated-workspace",
                str(WORKSPACE),
                "--execute-isolated",
            ],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(process.returncode, 2)
        self.assertIn("isolated", process.stderr.lower())
        target = self.isolated / self.target_relative
        target.mkdir(parents=True)
        marker = target / "user-owned.txt"
        marker.write_text("protected", encoding="utf-8")
        process = self._run("--execute-isolated")
        self.assertEqual(process.returncode, 2)
        self.assertEqual(marker.read_text("utf-8"), "protected")
        self.assertEqual(self._real_guard(), self.real_before)

    def test_injected_qdata_failure_restores_catalog_and_hides_payload(self) -> None:
        catalog = self.isolated / "data/catalog/datasets.json"
        before = catalog.read_bytes()
        process = self._run("--execute-isolated", "--inject-qdata-failure")
        self.assertEqual(process.returncode, 2)
        self.assertEqual(catalog.read_bytes(), before)
        self.assertFalse((self.isolated / self.target_relative).exists())
        rollback = self.isolated / "data/market/rollback" / self.execution["execution_id"]
        self.assertTrue(rollback.is_dir())
        ledger_path = self.isolated / "data/market/authorization-ledger" / f"auth-{self.token}.json"
        ledger = json.loads(ledger_path.read_text("utf-8"))
        self.assertEqual(ledger["state"], "ABORTED_NOT_CONSUMED")
        self.assertEqual(self._real_guard(), self.real_before)

    def test_single_use_authorization_rejects_second_execution(self) -> None:
        self.assertEqual(self._run("--execute-isolated").returncode, 0)
        self._write_execution(execution_id=f"formal-execution-replay-{self.token}")
        process = self._run("--execute-isolated")
        self.assertEqual(process.returncode, 2)
        self.assertIn("authorization", process.stderr.lower())
        self.assertEqual(self._real_guard(), self.real_before)

    def test_hard_crash_after_catalog_visibility_recovers_exact_run(self) -> None:
        crashed = self._run(
            "--execute-isolated",
            "--failpoint",
            "hard_crash_after_catalog_visible",
        )
        self.assertEqual(crashed.returncode, 92)
        lock = self.isolated / "data/catalog/.datasets.json.formal.lock"
        self.assertTrue(lock.is_file())
        lock_sha = digest(lock.read_bytes())
        recovered = self._run(
            "--execute-isolated",
            "--recover-stale-lock-sha256",
            lock_sha,
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(json.loads(recovered.stdout)["status"], "PUBLISHED")
        self.assertFalse(lock.exists())
        self.assertEqual(self._real_guard(), self.real_before)

    def test_hard_crash_after_qdata_recovers_before_authorization_consumption(self) -> None:
        crashed = self._run(
            "--execute-isolated",
            "--failpoint",
            "hard_crash_after_qdata_success",
        )
        self.assertEqual(crashed.returncode, 93)
        ledger_path = self.isolated / "data/market/authorization-ledger" / f"auth-{self.token}.json"
        self.assertEqual(json.loads(ledger_path.read_text("utf-8"))["state"], "RESERVED")
        lock = self.isolated / "data/catalog/.datasets.json.formal.lock"
        recovered = self._run(
            "--execute-isolated",
            "--recover-stale-lock-sha256",
            digest(lock.read_bytes()),
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(json.loads(ledger_path.read_text("utf-8"))["state"], "CONSUMED")
        self.assertEqual(self._real_guard(), self.real_before)


if __name__ == "__main__":
    unittest.main()
