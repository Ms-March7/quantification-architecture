from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


WORKSPACE = Path(__file__).resolve().parents[2]
TOOLS = WORKSPACE / "control/tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import execute_formal_atomic_publish_workspace as publisher  # noqa: E402


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


def tree_state(path: Path) -> tuple[tuple[str, int, str], ...]:
    return tuple(
        (
            item.relative_to(path).as_posix(),
            item.stat().st_size,
            digest(item.read_bytes()),
        )
        for item in sorted(path.rglob("*"))
        if item.is_file()
    )


class WorkspaceFormalAtomicPublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="formal-workspace-publisher-")
        self.root = Path(self.temporary.name).resolve()
        self.token = uuid.uuid4().hex
        self.dataset_id = f"test-workspace-daily-{self.token[:12]}"
        self.snapshot_id = f"2026-08-04-{self.token[:12]}"
        self.target_relative = (
            f"data/market/snapshots/{self.dataset_id}/{self.snapshot_id}"
        )
        self.candidate_root = self.root / "tmp/candidate"
        self.candidate_root.mkdir(parents=True)
        self.raw = self.candidate_root / "raw.json"
        self.curated = self.candidate_root / "part.parquet"
        self.raw.write_bytes(b'{"source":"fixture","status":200}\n')
        self.curated.write_bytes(b"PAR1 workspace formal fixture PAR1\n")
        self.candidate = self.candidate_root / "candidate-manifest.json"
        self.stage = self.candidate_root / "stage-a.json"
        self.rights = self.candidate_root / "rights.json"
        self.authorization = self.candidate_root / "authorization.json"
        self.preflight = (
            self.root
            / "tmp/formal-publisher-preflight/requests"
            / f"preflight-{self.token}.json"
        )
        self.execution = (
            self.root
            / "tmp/formal-publisher-execution/requests"
            / f"execution-{self.token}.json"
        )
        self._write_catalog()
        self._write_evidence()
        self._write_preflight()
        self._write_execution(license_status="DECLARED")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_json(self, path: Path, value: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical(value))

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _binding(self, path: Path, contract_id: str) -> dict[str, object]:
        payload = path.read_bytes()
        return {
            "relative_path": self._relative(path),
            "sha256": digest(payload),
            "bytes": len(payload),
            "contract_id": contract_id,
        }

    def _write_catalog(self) -> None:
        policy = self.root / "data/catalog/ACCESS_POLICY.md"
        policy.parent.mkdir(parents=True)
        policy.write_text("fixture policy\n", encoding="utf-8")
        snapshots = self.root / "data/market/snapshots"
        snapshots.mkdir(parents=True)
        self.catalog = self.root / "data/catalog/datasets.json"
        self._write_json(
            self.catalog,
            {
                "schema_version": 1,
                "updated_at": "2026-08-04T00:00:00+08:00",
                "workspace_root": self.root.as_posix(),
                "catalog_mode": "federated_logical_catalog",
                "physical_migration": True,
                "authority_precedence": ["snapshot_manifest_or_provenance"],
                "owners": ["workspace", "formal-workspace-test"],
                "stores": [
                    {
                        "store_id": "workspace-market-snapshots",
                        "owner_project": "workspace",
                        "physical_root": snapshots.as_posix(),
                        "kind": "immutable_snapshot_store",
                        "lifecycle": "active",
                        "mutability": "append_only",
                        "purpose_ceiling": "RESEARCH_ONLY",
                        "authority_paths": [policy.as_posix()],
                    }
                ],
                "datasets": [],
                "retired_datasets": [],
                "publication_contracts": [],
            },
        )
        (self.root / "data/catalog/qdata.cmd").write_text(
            "@echo off\nrem temporary qdata resolver fixture\n",
            encoding="utf-8",
        )
        self.baseline = digest(self.catalog.read_bytes())

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
        self._write_json(
            self.candidate,
            {
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
            },
        )
        bundle = {
            "all_ready": True,
            "ready_count": 4,
            "target_count": 4,
            "targets": ["CORE-01", "CORE-02", "CORE-03", "CORE-06"],
            "formal_effect": "NONE",
        }
        stage_value = {
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
        self._write_json(self.stage, stage_value)
        candidate_sha = digest(self.candidate.read_bytes())
        self._write_json(
            self.rights,
            {
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
            },
        )
        self._write_json(
            self.authorization,
            {
                "schema_version": 1,
                "contract_id": "formal_publish_authorization_receipt_v1",
                "authorization_id": f"auth-{self.token}",
                "action": "AUTHORIZE_EXACT_FORMAL_SNAPSHOT_PUBLICATION",
                "authorized_by": "USER",
                "authorized_at_utc": "2026-08-04T00:00:00Z",
                "statement": "Authorize only this exact immutable publication request.",
                "scope": bundle["targets"],
                "dataset_id": self.dataset_id,
                "snapshot_id": self.snapshot_id,
                "target_relative_path": self.target_relative,
                "candidate_manifest_sha256": candidate_sha,
                "stage_a_bundle_sha256": stage_value["bundle_sha256"],
                "source_rights_receipt_sha256": digest(self.rights.read_bytes()),
                "single_use": True,
            },
        )

    def _write_preflight(self) -> None:
        self.preflight_value = {
            "schema_version": 1,
            "contract_id": "formal_publisher_preflight_v1",
            "preflight_id": f"preflight-{self.token}",
            "candidate_manifest": self._binding(
                self.candidate, "candidate_manifest_v1"
            ),
            "stage_a_bundle": self._binding(
                self.stage, "selected_core_stage_a_bundle_v1"
            ),
            "source_rights_receipt": self._binding(
                self.rights, "source_rights_receipt_v1"
            ),
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
                "consumer": "formal-workspace-test",
                "require_exact_identity": True,
                "forbid_latest": True,
                "require_immutable_snapshot": True,
                "require_catalog_atomic_visibility": True,
                "require_research_resolution_after_publish": True,
                "failure_action": "ROLLBACK_OR_FAIL_CLOSED",
            },
        }
        self._write_json(self.preflight, self.preflight_value)

    def _write_execution(self, *, license_status: str) -> None:
        self.execution_value = {
            "schema_version": 1,
            "contract_id": "formal_atomic_publish_execution_v1",
            "execution_id": f"workspace-execution-{self.token}",
            "preflight_request": self._binding(
                self.preflight, "formal_publisher_preflight_v1"
            ),
            "catalog_before": {
                "relative_path": "data/catalog/datasets.json",
                "sha256": self.baseline,
                "bytes": self.catalog.stat().st_size,
            },
            "target": dict(self.preflight_value["target"]),
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
                    "snapshot_relative_path": "year=2026/month=08/part.parquet",
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
                "license_status": license_status,
                "quality_status": "OK",
                "consumers": ["formal-workspace-test"],
                "known_limits": ["temporary workspace fixture only"],
                "notes": "Formal workspace publisher temporary fixture.",
                "source_id": "TEST_SOURCE",
                "dataset_contract_id": "test_daily_v1",
                "dataset_schema_version": "v1",
                "research_input_contract": {
                    "schema_version": 1,
                    "contract_id": "test_daily_v1",
                    "dataset_id": self.dataset_id,
                    "snapshot_id": self.snapshot_id,
                },
                "available_at": "2026-08-04T00:00:00+08:00",
                "published_at": "2026-08-04T01:00:00+08:00",
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
        self._write_json(self.execution, self.execution_value)

    def _plain_binding(self, path: Path) -> dict[str, object]:
        payload = path.read_bytes()
        return {
            "relative_path": self._relative(path),
            "sha256": digest(payload),
            "bytes": len(payload),
        }

    def _upgrade_to_commit_clock_v2(self, *, available_at: str) -> None:
        rights_basis = self.candidate_root / "RIGHTS_BASIS.json"
        upstream_paths = {
            "csmar_rights_receipt": self.candidate_root / "csmar-rights.json",
            "csmar_rights_verification": self.candidate_root / "csmar-verification.json",
            "rqdata_quota_receipt": self.candidate_root / "rqdata-quota.json",
        }
        self._write_json(
            rights_basis,
            {
                "schema_version": 1,
                "status": "DECLARED__NOT_VERIFIED__NO_LEGAL_OPINION",
                "dataset_id": self.dataset_id,
                "snapshot_id": self.snapshot_id,
            },
        )
        for key, path in upstream_paths.items():
            self._write_json(path, {"schema_version": 1, "evidence_id": key})
        upstream = {
            key: self._plain_binding(path) for key, path in upstream_paths.items()
        }
        candidate_sha = digest(self.candidate.read_bytes())
        rights_value = {
            "schema_version": 2,
            "contract_id": "source_rights_receipt_v2",
            "receipt_id": f"rights-v2-{self.token}",
            "status": "PASS__DECLARED_SCOPE_BOUND__NOT_LEGALLY_VERIFIED",
            "dataset_id": self.dataset_id,
            "source_id": "TEST_SOURCE",
            "candidate_manifest_sha256": candidate_sha,
            "rights_basis": self._plain_binding(rights_basis),
            "upstream_evidence": upstream,
            "rights_status": "DECLARED__NOT_VERIFIED__NO_LEGAL_OPINION",
            "independent_contract_verification": False,
            "legal_opinion": False,
            "rights": {
                "local_storage": True,
                "internal_research": True,
                "derived_research": True,
                "raw_retention": True,
                "no_external_redistribution": True,
            },
        }
        self._write_json(self.rights, rights_value)
        stage_value = json.loads(self.stage.read_text("utf-8"))
        authorization_value = {
            "schema_version": 2,
            "contract_id": "formal_publish_authorization_receipt_v2",
            "authorization_id": f"auth-v2-{self.token}",
            "action": "AUTHORIZE_EXACT_FORMAL_SNAPSHOT_PUBLICATION",
            "authorized_by": "USER",
            "authorized_at_utc": "2026-08-03T18:00:00Z",
            "statement": "User authorized this exact formal snapshot publication.",
            "source_thread_id": f"thread-{self.token}",
            "scope": stage_value["bundle"]["targets"],
            "dataset_id": self.dataset_id,
            "snapshot_id": self.snapshot_id,
            "target_relative_path": self.target_relative,
            "candidate_manifest_sha256": candidate_sha,
            "stage_a_bundle_sha256": stage_value["bundle_sha256"],
            "source_rights_receipt_sha256": digest(self.rights.read_bytes()),
            "rights_basis_sha256": digest(rights_basis.read_bytes()),
            "upstream_evidence_sha256": {
                key: value["sha256"] for key, value in upstream.items()
            },
            "rights_status": "DECLARED__NOT_VERIFIED__NO_LEGAL_OPINION",
            "independent_contract_verification": False,
            "legal_opinion": False,
            "credential_value_captured": False,
            "single_use": True,
        }
        self._write_json(self.authorization, authorization_value)
        self.preflight_value["source_rights_receipt"] = self._binding(
            self.rights, "source_rights_receipt_v2"
        )
        self.preflight_value["user_authorization_receipt"] = self._binding(
            self.authorization, "formal_publish_authorization_receipt_v2"
        )
        self._write_json(self.preflight, self.preflight_value)
        self.execution_value["schema_version"] = 2
        self.execution_value["contract_id"] = "formal_atomic_publish_execution_v2"
        self.execution_value["preflight_request"] = self._binding(
            self.preflight, "formal_publisher_preflight_v1"
        )
        profile = self.execution_value["publication_profile"]
        profile.pop("published_at")
        profile["published_at_policy"] = "WORKSPACE_ATOMIC_COMMIT_CLOCK"
        profile["available_at"] = available_at
        self._write_json(self.execution, self.execution_value)

    def _run(
        self,
        *,
        enabled: bool,
        recover_hash: str | None = None,
        inject_qdata_failure: bool = False,
    ) -> dict[str, object]:
        return publisher._execute_at_workspace(  # noqa: SLF001
            request=self.execution,
            workspace_root=self.root,
            catalog_baseline_sha256=self.baseline,
            enabled=enabled,
            recover_hash=recover_hash,
            trusted_workspace_root=self.root,
            inject_qdata_failure=inject_qdata_failure,
            qdata_runner=self._qdata_runner,
        )

    def _qdata_runner(self, command: object) -> SimpleNamespace:
        parts = tuple(str(value) for value in command)
        self.assertIn("--intent", parts)
        self.assertIn("research", parts)
        catalog = publisher.dataset_cli.load_catalog(self.catalog)
        item = publisher.dataset_cli.find_dataset(
            catalog, self.dataset_id, self.snapshot_id
        )
        record = publisher.dataset_cli.enriched(item)
        record["requested_intent"] = "research"
        record["requested_consumer"] = None
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(record, ensure_ascii=False),
            stderr="",
        )

    def test_default_is_dry_run_and_performs_no_write(self) -> None:
        before = tree_state(self.root)
        result = self._run(enabled=False)
        self.assertEqual(result["status"], "PASS__WORKSPACE_EXECUTION_DISABLED__NO_WRITE")
        self.assertFalse(result["write_performed"])
        self.assertEqual(tree_state(self.root), before)

    def test_explicit_catalog_baseline_mismatch_fails_before_write(self) -> None:
        before = tree_state(self.root)
        with self.assertRaises(publisher.WorkspaceExecutionError):
            publisher._execute_at_workspace(  # noqa: SLF001
                request=self.execution,
                workspace_root=self.root,
                catalog_baseline_sha256="0" * 64,
                enabled=True,
                recover_hash=None,
                trusted_workspace_root=self.root,
            )
        self.assertEqual(tree_state(self.root), before)

    def test_success_supports_declared_rights_and_writes_five_authority_docs(self) -> None:
        result = self._run(enabled=True)
        self.assertEqual(result["status"], "PUBLISHED")
        catalog = json.loads(self.catalog.read_text("utf-8"))
        self.assertEqual(len(catalog["datasets"]), 1)
        self.assertEqual(catalog["datasets"][0]["license_status"], "DECLARED")
        self.assertEqual(catalog["datasets"][0]["lifecycle"], "frozen")
        target = self.root / self.target_relative
        self.assertEqual(
            {
                path.relative_to(target).as_posix()
                for path in target.rglob("*")
                if path.is_file()
            },
            {
                "request.json",
                "manifest.json",
                "provenance.json",
                "partition-manifest.json",
                "quality/summary.json",
            },
        )
        manifest = json.loads((target / "manifest.json").read_text("utf-8"))
        self.assertEqual(
            manifest["research_input_contract"],
            self.execution_value["publication_profile"]["research_input_contract"],
        )
        ledger = json.loads(Path(result["authorization_ledger_path"]).read_text("utf-8"))
        self.assertEqual(ledger["state"], "CONSUMED")

    def test_existing_target_collision_fails_without_catalog_change(self) -> None:
        target = self.root / self.target_relative
        target.mkdir(parents=True)
        marker = target / "user-owned.txt"
        marker.write_text("protected", encoding="utf-8")
        before = self.catalog.read_bytes()
        with self.assertRaises(publisher.WorkspaceExecutionError):
            self._run(enabled=True)
        self.assertEqual(self.catalog.read_bytes(), before)
        self.assertEqual(marker.read_text("utf-8"), "protected")

    def test_review_lifecycle_is_rejected_before_write(self) -> None:
        self.execution_value["publication_profile"]["lifecycle"] = "review"
        self._write_json(self.execution, self.execution_value)
        before = tree_state(self.root)
        with self.assertRaises(publisher.WorkspaceExecutionError):
            self._run(enabled=True)
        self.assertEqual(tree_state(self.root), before)

    def test_qdata_failure_restores_catalog_and_moves_payload_to_rollback(self) -> None:
        before = self.catalog.read_bytes()
        with self.assertRaises(publisher.QdataPostconditionError):
            self._run(enabled=True, inject_qdata_failure=True)
        self.assertEqual(self.catalog.read_bytes(), before)
        self.assertFalse((self.root / self.target_relative).exists())
        rollback = (
            self.root
            / "data/market/rollback/formal-workspace"
            / self.execution_value["execution_id"]
        )
        self.assertTrue((rollback / "snapshot").is_dir())
        ledger = json.loads(
            (
                self.root
                / "data/market/authorization-ledger"
                / f"auth-{self.token}.json"
            ).read_text("utf-8")
        )
        self.assertEqual(ledger["state"], "ABORTED_NOT_CONSUMED")

    def test_path_escape_is_rejected_before_write(self) -> None:
        self.execution_value["payload_layout"][0]["source_relative_path"] = "../escape"
        self._write_json(self.execution, self.execution_value)
        before = tree_state(self.root)
        with self.assertRaises(publisher.WorkspaceExecutionError):
            self._run(enabled=True)
        self.assertEqual(tree_state(self.root), before)

    def test_hard_crash_after_catalog_visibility_recovers_exact_run(self) -> None:
        code = (
            "import sys; from pathlib import Path; "
            f"sys.path.insert(0, {str(TOOLS)!r}); "
            "import execute_formal_atomic_publish_workspace as p; "
            f"p._execute_at_workspace(request=Path({str(self.execution)!r}), "
            f"workspace_root=Path({str(self.root)!r}), "
            f"catalog_baseline_sha256={self.baseline!r}, enabled=True, "
            "recover_hash=None, "
            f"trusted_workspace_root=Path({str(self.root)!r}), "
            "failpoint='hard_crash_after_catalog_visible')"
        )
        crashed = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(crashed.returncode, 92, crashed.stderr)
        lock = self.root / "data/catalog/.datasets.json.formal-workspace.lock"
        self.assertTrue(lock.is_file())
        result = self._run(enabled=True, recover_hash=digest(lock.read_bytes()))
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertFalse(lock.exists())

    def test_commit_clock_v2_binds_time_and_declared_rights_into_formal_state(self) -> None:
        self._upgrade_to_commit_clock_v2(available_at="2000-01-01T00:00:00Z")
        before = datetime.now(timezone.utc)
        result = self._run(enabled=True)
        after = datetime.now(timezone.utc)
        self.assertEqual(result["status"], "PUBLISHED")
        catalog = json.loads(self.catalog.read_text("utf-8"))
        item = catalog["datasets"][0]
        target = self.root / self.target_relative
        manifest = json.loads((target / "manifest.json").read_text("utf-8"))
        provenance = json.loads((target / "provenance.json").read_text("utf-8"))
        published_at = datetime.fromisoformat(
            item["published_at"].replace("Z", "+00:00")
        )
        self.assertLessEqual(before, published_at)
        self.assertLessEqual(published_at, after)
        self.assertEqual(manifest["created_at"], item["published_at"])
        self.assertEqual(provenance["published_at"], item["published_at"])
        self.assertEqual(item["available_at"], "2000-01-01T00:00:00Z")
        self.assertEqual(
            provenance["rights_status"],
            "DECLARED__NOT_VERIFIED__NO_LEGAL_OPINION",
        )
        self.assertFalse(provenance["independent_contract_verification"])
        self.assertFalse(provenance["legal_opinion"])
        self.assertEqual(provenance["rights_basis_sha256"], item["rights_basis_sha256"])
        self.assertEqual(
            provenance["upstream_evidence_sha256"],
            item["upstream_evidence_sha256"],
        )

    def test_commit_clock_v2_future_available_at_rejects_before_authorization_reservation(self) -> None:
        self._upgrade_to_commit_clock_v2(available_at="2099-01-01T00:00:00Z")
        before = self.catalog.read_bytes()
        with self.assertRaisesRegex(
            publisher.WorkspaceExecutionError,
            "blocked until available_at",
        ):
            self._run(enabled=True)
        self.assertEqual(self.catalog.read_bytes(), before)
        self.assertFalse(
            (
                self.root
                / "data/market/authorization-ledger"
                / f"auth-v2-{self.token}.json"
            ).exists()
        )
        self.assertFalse(
            (
                self.root
                / "data/market/transactions"
                / f"formal-workspace-{self.execution_value['execution_id']}.json"
            ).exists()
        )

    def test_cli_surface_pins_workspace_and_has_no_failpoint_option(self) -> None:
        options = {action.dest for action in publisher.parser()._actions}
        self.assertIn("execute_workspace", options)
        self.assertIn("catalog_baseline_sha256", options)
        self.assertNotIn("failpoint", options)
        with self.assertRaises(publisher.WorkspaceExecutionError):
            publisher.execute(
                request=self.execution,
                workspace_root=self.root,
                catalog_baseline_sha256=self.baseline,
                enabled=True,
                recover_hash=None,
            )

    def test_workspace_contract_document_parses(self) -> None:
        contract = (
            WORKSPACE
            / "data/catalog/contracts/formal_atomic_publish_workspace_v1/contract.json"
        )
        value = json.loads(contract.read_text("utf-8"))
        self.assertEqual(value["contract_id"], publisher.CONTRACT_ID)
        self.assertFalse(value["real_publication_performed_by_engine_verification"])


if __name__ == "__main__":
    unittest.main()
