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
SCRIPT = WORKSPACE / "control" / "tools" / "validate_formal_atomic_publish_execution.py"
PREFLIGHT_SCRIPT = WORKSPACE / "control" / "tools" / "run_formal_publisher_preflight.py"
PREFLIGHT_REQUEST_ROOT = WORKSPACE / "tmp" / "formal-publisher-preflight" / "requests"
EXECUTION_REQUEST_ROOT = WORKSPACE / "tmp" / "formal-publisher-execution" / "requests"


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


class FormalAtomicPublishExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token = uuid.uuid4().hex
        self.root = WORKSPACE / "tmp" / "formal-publisher-execution-tests" / self.token
        self.root.mkdir(parents=True)
        PREFLIGHT_REQUEST_ROOT.mkdir(parents=True, exist_ok=True)
        EXECUTION_REQUEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.preflight_path = PREFLIGHT_REQUEST_ROOT / f"execution-{self.token}.json"
        self.execution_path = EXECUTION_REQUEST_ROOT / f"execution-{self.token}.json"
        self.dataset_id = f"test-formal-daily-{self.token[:12]}"
        self.snapshot_id = f"2026-08-01-{self.token[:12]}"
        self.target_relative = (
            f"data/market/snapshots/{self.dataset_id}/{self.snapshot_id}"
        )
        self.raw = self.root / "raw-response.json"
        self.curated = self.root / "part-000.parquet"
        self.raw.write_bytes(b'{"source":"fixture","status":200}\n')
        self.curated.write_bytes(b"PAR1 formal-candidate-fixture PAR1\n")
        self.additional_artifacts: list[Path] = []
        self.candidate = self.root / "candidate-manifest.json"
        self.stage = self.root / "stage-a.json"
        self.rights = self.root / "rights.json"
        self.authorization = self.root / "authorization.json"
        self._write_evidence(stage_ready=True)
        self._write_preflight()
        self._write_execution()
        self.guarded_before = self._guarded_state()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        self.preflight_path.unlink(missing_ok=True)
        self.execution_path.unlink(missing_ok=True)

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

    def _write_evidence(self, *, stage_ready: bool) -> None:
        artifacts = []
        for path in (self.raw, self.curated, *self.additional_artifacts):
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
            "all_ready": stage_ready,
            "ready_count": 4 if stage_ready else 3,
            "target_count": 4,
            "targets": ["CORE-01", "CORE-02", "CORE-03", "CORE-06"],
            "formal_effect": "NONE",
        }
        stage = {
            "schema_version": 1,
            "contract_id": "selected_core_stage_a_bundle_v1",
            "status": (
                "PASS__ALL_TARGETS_PRE_ADMISSION_READY"
                if stage_ready
                else "BLOCKED__PRE_ADMISSION_EVIDENCE_NOT_READY"
            ),
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
        self._write_json(self.stage, stage)
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
            "stage_a_bundle_sha256": stage["bundle_sha256"],
            "source_rights_receipt_sha256": digest(self.rights.read_bytes()),
            "single_use": True,
        }
        self._write_json(self.authorization, authorization)

    def _write_preflight(self) -> None:
        self.preflight = {
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

    def _write_execution(self) -> None:
        catalog = WORKSPACE / "data" / "catalog" / "datasets.json"
        catalog_payload = catalog.read_bytes()
        self.execution = {
            "schema_version": 1,
            "contract_id": "formal_atomic_publish_execution_v1",
            "execution_id": f"formal-execution-{self.token}",
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
                "known_limits": ["fixture only"],
                "notes": "Isolated formal publisher execution fixture.",
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
            [sys.executable, "-X", "utf8", str(SCRIPT), "--request", str(self.execution_path)],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_complete_chain_yields_sealed_no_write_execution_plan(self) -> None:
        process = self._run()
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["status"], "PASS__READY_FOR_ATOMIC_COMMIT__NO_WRITE")
        self.assertEqual(result["state"], "INPUT_INVENTORY_SEALED")
        self.assertTrue(result["execution_contract_ready"])
        self.assertFalse(result["write_performed"])
        self.assertEqual(result["formal_effect"], "NONE")
        self.assertEqual(result["candidate_artifact_count"], 2)
        self.assertRegex(result["execution_plan_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(self._guarded_state(), self.guarded_before)

    def test_stage_a_failure_is_inherited_from_preflight(self) -> None:
        self._write_evidence(stage_ready=False)
        self._write_preflight()
        self.execution["preflight_request"] = self._binding(
            self.preflight_path, "formal_publisher_preflight_v1"
        )
        self._write_json(self.execution_path, self.execution)
        process = self._run()
        self.assertEqual(process.returncode, 2)
        result = json.loads(process.stdout)
        self.assertEqual(result["status"], "FAIL_CLOSED")
        self.assertIn("Stage-A", result["blocking_gaps"][0])

    def test_payload_layout_must_exactly_cover_candidate_artifacts(self) -> None:
        extra = self.root / "quality-summary.json"
        extra.write_bytes(b'{"quality":"fixture"}\n')
        self.additional_artifacts.append(extra)
        self._write_evidence(stage_ready=True)
        self._write_preflight()
        self._write_execution()
        process = self._run()
        self.assertEqual(process.returncode, 2)
        self.assertIn("exactly cover", json.loads(process.stdout)["blocking_gaps"][0])

    def test_payload_hash_and_destination_collision_fail_closed(self) -> None:
        self.execution["payload_layout"][0]["sha256"] = "0" * 64
        self._write_json(self.execution_path, self.execution)
        self.assertEqual(self._run().returncode, 2)
        self._write_execution()
        self.execution["payload_layout"][1]["layer"] = "raw"
        self.execution["payload_layout"][1]["snapshot_relative_path"] = "response.json"
        self._write_json(self.execution_path, self.execution)
        process = self._run()
        self.assertEqual(process.returncode, 2)
        self.assertIn("destination", json.loads(process.stdout)["blocking_gaps"][0])

    def test_catalog_and_target_are_exactly_bound(self) -> None:
        self.execution["catalog_before"]["sha256"] = "0" * 64
        self._write_json(self.execution_path, self.execution)
        self.assertEqual(self._run().returncode, 2)
        self._write_execution()
        self.execution["target"]["snapshot_id"] = "different"
        self._write_json(self.execution_path, self.execution)
        process = self._run()
        self.assertEqual(process.returncode, 2)
        self.assertIn("target", json.loads(process.stdout)["blocking_gaps"][0])

    def test_every_atomic_and_postcondition_guard_is_mandatory(self) -> None:
        for field in self.execution["state_machine_requirements"]:
            with self.subTest(field=field):
                self._write_execution()
                if field == "failure_action":
                    self.execution["state_machine_requirements"][field] = "IGNORE"
                else:
                    self.execution["state_machine_requirements"][field] = False
                self._write_json(self.execution_path, self.execution)
                self.assertEqual(self._run().returncode, 2)

    def test_research_input_contract_is_required_non_empty_and_deterministic(self) -> None:
        for mode in ("missing", "empty", "non_finite"):
            with self.subTest(mode=mode):
                self._write_execution()
                if mode == "missing":
                    self.execution["publication_profile"].pop("research_input_contract")
                    self._write_json(self.execution_path, self.execution)
                elif mode == "empty":
                    self.execution["publication_profile"]["research_input_contract"] = {}
                    self._write_json(self.execution_path, self.execution)
                else:
                    self.execution["publication_profile"]["research_input_contract"] = {
                        "invalid": float("nan")
                    }
                    self.execution_path.write_text(
                        json.dumps(
                            self.execution,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=True,
                        ),
                        encoding="utf-8",
                    )
                process = self._run()
                self.assertEqual(process.returncode, 2)
                message = json.loads(process.stdout)["blocking_gaps"][0]
                self.assertTrue(
                    "research_input_contract" in message
                    or "non-standard JSON constant" in message,
                    message,
                )

    def test_tool_has_no_execution_or_formal_write_surface(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("--execute", source)
        self.assertNotIn("os.replace", source)
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
        self.assertEqual(self._guarded_state(), self.guarded_before)

    def test_contract_json_documents_parse(self) -> None:
        root = WORKSPACE / "data/catalog/contracts/formal_atomic_publish_execution_v1"
        for name in (
            "contract.json",
            "request.schema.json",
            "prepared_execution_receipt.schema.json",
            "isolated_execution_receipt.schema.json",
        ):
            self.assertIsInstance(json.loads((root / name).read_text(encoding="utf-8")), dict)

    def test_r0_shsz_research_input_contract_template_is_exact(self) -> None:
        contract = json.loads(
            (
                WORKSPACE / "data/catalog/contracts/r0_shsz_pv_v2/contract.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            contract["research_input_contract"],
            {
                "schema_version": 1,
                "research_tier": "R0_SHSZ_PV",
                "gate_id": "R0_SHSZ_PV",
                "contract_id": "r0_shsz_pv_v2",
                "dataset_id": "cn-a-share-r0-shsz-pv-panel",
                "snapshot_id": "20050104-20260803-v1",
                "market_scope": "XSHG_XSHE_ORDINARY_A",
                "timezone": "Asia/Shanghai",
                "coverage_start_date": "2005-01-04",
                "coverage_end_date": "2026-08-03",
                "decision_time": "TRADE_DATE_CLOSE",
                "available_at_semantics": "NEXT_OBSERVED_TRADING_SESSION_OPEN",
                "earliest_execution_time": "NEXT_OBSERVED_TRADING_SESSION_OPEN",
                "signal_lag_sessions_minimum": 1,
                "raw_price_convention": "UNADJUSTED",
                "return_convention": "ONE_PERIOD_REALIZED_TOTAL_RETURN",
                "adjusted_price_levels_allowed": False,
                "latest_forward_adjusted_history_allowed": False,
                "corporate_action_features_allowed": False,
                "execution_mask_role": "SIMULATION_ONLY__NEVER_SIGNAL_RANK_INPUT",
                "survivorship_policy": (
                    "DYNAMIC_LISTING_EPISODES__NO_CURRENT_ACTIVE_UNIVERSE_BACKFILL"
                ),
                "missing_value_policy": "NULL_NEVER_ZERO_FILL",
                "payload_schema": "r0-shsz-pv-panel-v2",
                "primary_key": ["order_book_id", "trade_date"],
                "payload_format": "PARQUET",
                "payload_discovery": "PARTITION_MANIFEST_CURATED_ENTRIES",
            },
        )


if __name__ == "__main__":
    unittest.main()
