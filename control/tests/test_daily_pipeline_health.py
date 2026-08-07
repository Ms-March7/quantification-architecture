from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "control" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


health = load_module("daily_pipeline_health_test_module", "run_daily_pipeline_health.py")
worker = load_module(
    "daily_pipeline_health_worker_fixture", "run_local_daily_inspection_worker.py"
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DailyPipelineHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="daily-health-test-")
        self.root = Path(self.temporary.name).resolve()
        self.catalog = self.root / "data" / "catalog" / "datasets.json"
        write_json(
            self.catalog,
            {
                "schema_version": 1,
                "catalog_mode": "federated_logical_catalog",
                "datasets": [],
            },
        )
        self.orchestration_request = {
            "schema_version": 1,
            "mode": "local_daily_orchestration_state",
            "purpose_ceiling": "INSPECTION_ONLY",
            "dataset_scope": "synthetic_fixture",
            "job_id": "synthetic-health-job",
            "dataset_id": "synthetic-health-daily",
            "source_id": "SYNTHETIC_FIXTURE",
            "timezone": "Asia/Shanghai",
            "calendar_dates": ["2026-07-30", "2026-07-31"],
            "max_attempts": 1,
            "lease_seconds": 60,
            "authorization_note": "isolated health fixture only",
        }
        self.update_request = {
            "schema_version": 1,
            "mode": "local_daily_update_candidate",
            "dataset_scope": "synthetic_fixture",
            "dataset_id": "synthetic-health-daily",
            "run_id": "health-candidate-20260730",
            "source_id": "SYNTHETIC_FIXTURE",
            "dataset_schema_version": "candidate-v1",
            "authorization_note": "isolated health fixture only",
            "include_patterns": ["*.jsonl"],
            "base_candidate": None,
            "table": {
                "table_name": "daily_price",
                "expected_columns": ["security_id", "trade_date", "close"],
                "column_types": {
                    "security_id": "VARCHAR",
                    "trade_date": "DATE",
                    "close": "DOUBLE",
                },
                "date_column": "trade_date",
                "primary_key_columns": ["security_id", "trade_date"],
                "required_columns": ["security_id", "trade_date", "close"],
                "delimiter": ",",
                "revision": None,
            },
            "limits": {
                "max_files": 10,
                "max_total_input_bytes": 1_000_000,
                "max_rows": 1000,
            },
        }
        self.orchestration_path = self.root / "requests" / "orchestration.json"
        self.update_path = self.root / "requests" / "update.json"
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        write_json(self.orchestration_path, self.orchestration_request)
        write_json(self.update_path, self.update_request)
        (self.inbox / "delta.jsonl").write_text(
            json.dumps(
                {
                    "security_id": "XSHG.600000",
                    "trade_date": "2026-07-30",
                    "close": 10.5,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.catalog_hash = sha256(self.catalog)
        built = worker.run(
            workspace_root=self.root,
            orchestration_request_path=self.orchestration_path,
            update_request_path=self.update_path,
            inbox=self.inbox,
            through_date=date(2026, 7, 30),
            worker_id="health-fixture-worker",
            claim_event_id="health-fixture-claim",
            finish_event_id="health-fixture-finish",
            execute=True,
            now=datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc),
        )
        self.candidate_root = Path(built["candidate_path"])
        self.manifest_path = self.candidate_root / "manifest.json"
        self.database = (
            self.root
            / "tmp"
            / "local-daily-orchestration"
            / self.orchestration_request["job_id"]
            / "orchestrator.sqlite3"
        )
        quality = json.loads(
            (self.candidate_root / "quality" / "summary.json").read_text(
                encoding="utf-8"
            )
        )
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.health_request = {
            "schema_version": 1,
            "mode": "daily_pipeline_health",
            "purpose_ceiling": "INSPECTION_ONLY",
            "run_id": "health-check-v1",
            "as_of": "2026-07-31T02:00:00+00:00",
            "through_date": "2026-07-30",
            "max_watermark_lag_due_dates": 0,
            "orchestration_request": self.bound(self.orchestration_path),
            "orchestration_database": {
                "relative_path": self.relative(self.database)
            },
            "candidate_manifests": [
                {
                    "run_date": "2026-07-30",
                    **self.bound(self.manifest_path),
                    "expected_row_count": manifest["row_count"],
                    "expected_date_min": quality["metrics"]["date_min"],
                    "expected_date_max": quality["metrics"]["date_max"],
                }
            ],
            "quarantine_receipts": [],
        }
        self.health_path = self.root / "requests" / "health.json"
        write_json(self.health_path, self.health_request)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def bound(self, path: Path) -> dict[str, object]:
        return {
            "relative_path": self.relative(path),
            "sha256": sha256(path),
            "bytes": path.stat().st_size,
        }

    def run_health(self) -> dict:
        write_json(self.health_path, self.health_request)
        return health.run(
            workspace_root=self.root, health_request_path=self.health_path
        )

    def test_healthy_completed_pipeline_passes_read_only(self) -> None:
        database_hash = sha256(self.database)
        report = self.run_health()
        self.assertEqual(report["status"], health.PASS_STATUS)
        self.assertEqual(report["job"]["recomputed_watermark"], "2026-07-30")
        self.assertIsNone(report["watermark"]["earliest_unfinished"])
        self.assertEqual(report["candidates"]["verified_count"], 1)
        self.assertEqual(sha256(self.database), database_hash)
        self.assertEqual(sha256(self.catalog), self.catalog_hash)

    def test_lag_and_earliest_unfinished_respect_threshold(self) -> None:
        self.health_request["through_date"] = "2026-07-31"
        self.health_request["max_watermark_lag_due_dates"] = 1
        report = self.run_health()
        self.assertEqual(report["status"], health.PASS_STATUS)
        self.assertEqual(report["watermark"]["earliest_unfinished"], "2026-07-31")
        self.assertEqual(report["watermark"]["lag_due_dates"], 1)
        self.health_request["max_watermark_lag_due_dates"] = 0
        report = self.run_health()
        self.assertEqual(report["status"], health.FAIL_STATUS)
        self.assertIn("WATERMARK_LAG_EXCEEDS_THRESHOLD", report["health_failures"])

    def test_terminal_failure_is_reported(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE schedule_dates SET status='FAILED_TERMINAL', "
                "attempt_count=1, last_error_code='SYNTHETIC_FAILURE' "
                "WHERE run_date='2026-07-31'"
            )
            connection.execute(
                "UPDATE jobs SET status='BLOCKED' WHERE job_id=?",
                (self.orchestration_request["job_id"],),
            )
            connection.commit()
        finally:
            connection.close()
        self.health_request["through_date"] = "2026-07-31"
        self.health_request["max_watermark_lag_due_dates"] = 1
        report = self.run_health()
        self.assertEqual(report["status"], health.FAIL_STATUS)
        self.assertEqual(report["terminal_failures"][0]["last_error_code"], "SYNTHETIC_FAILURE")

    def test_expired_lease_is_reported_without_claim_token(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE schedule_dates SET status='RUNNING', attempt_count=1, "
                "active_claim_token='SECRET_TOKEN_MUST_NOT_LEAK', "
                "claimed_at='2026-07-31T00:00:00+00:00', "
                "lease_expires_at='2026-07-31T00:01:00+00:00' "
                "WHERE run_date='2026-07-31'"
            )
            connection.execute(
                "UPDATE jobs SET status='RUNNING' WHERE job_id=?",
                (self.orchestration_request["job_id"],),
            )
            connection.commit()
        finally:
            connection.close()
        self.health_request["through_date"] = "2026-07-31"
        self.health_request["max_watermark_lag_due_dates"] = 1
        report = self.run_health()
        rendered = json.dumps(report)
        self.assertEqual(report["status"], health.FAIL_STATUS)
        self.assertEqual(report["expired_leases"][0]["run_date"], "2026-07-31")
        self.assertNotIn("SECRET_TOKEN_MUST_NOT_LEAK", rendered)

    def test_candidate_row_expectation_and_sqlite_hash_drift_fail(self) -> None:
        self.health_request["candidate_manifests"][0]["expected_row_count"] = 99
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE schedule_dates SET result_sha256=? WHERE run_date='2026-07-30'",
                ("b" * 64,),
            )
            connection.commit()
        finally:
            connection.close()
        report = self.run_health()
        codes = {item["code"] for item in report["candidates"]["drift"]}
        self.assertEqual(report["status"], health.FAIL_STATUS)
        self.assertIn("SQLITE_RESULT_HASH_DRIFT", codes)
        self.assertIn("CANDIDATE_ROW_COUNT_EXPECTATION_DRIFT", codes)

    def test_candidate_payload_or_quality_tamper_fails_seal_verification(self) -> None:
        quality_path = self.candidate_root / "quality" / "summary.json"
        quality = json.loads(quality_path.read_text(encoding="utf-8"))
        quality["metrics"]["merged_rows"] = 999
        write_json(quality_path, quality)
        report = self.run_health()
        codes = {item["code"] for item in report["candidates"]["drift"]}
        self.assertEqual(report["status"], health.FAIL_STATUS)
        self.assertIn("CANDIDATE_SEAL_VERIFICATION_FAILED", codes)

    def test_valid_quarantine_receipt_is_summarized(self) -> None:
        receipt_path = (
            self.root
            / "tmp/local-daily-update/quarantine/synthetic-health-daily/q1/failure.json"
        )
        receipt = {
            "schema_version": 1,
            "engine_id": health.DAILY_UPDATE.ENGINE_ID,
            "status": "QUARANTINED",
            "dataset_id": "synthetic-health-daily",
            "run_id": "q1",
            "reason": "SYNTHETIC_REJECTED",
            "message": "fixture",
            "request_fingerprint": None,
            "failed_at": "2026-07-30T01:00:00Z",
            "rollback": {
                "candidate_published": False,
                "formal_catalog_written": False,
                "staging_removed": True,
            },
            "formal_catalog_guard": {
                "sha256_before": self.catalog_hash,
                "sha256_after": self.catalog_hash,
                "unchanged": True,
            },
            "purpose_ceiling": "INSPECTION_ONLY",
            "source_admission": False,
            "catalog_registered": False,
            "quality_promotion": False,
            "research_ready": False,
        }
        write_json(receipt_path, receipt)
        self.health_request["quarantine_receipts"] = [self.bound(receipt_path)]
        report = self.run_health()
        self.assertEqual(report["status"], health.PASS_STATUS)
        self.assertEqual(report["quarantine"]["reason_counts"], {"SYNTHETIC_REJECTED": 1})

    def test_optional_output_is_atomic_write_once(self) -> None:
        write_json(self.health_path, self.health_request)
        report = health.run(
            workspace_root=self.root, health_request_path=self.health_path
        )
        output = self.root / "tmp/daily-pipeline-health/health-check-v1.json"
        written = health.write_once(self.root, report["run_id"], output, report)
        self.assertEqual(written, output)
        before = sha256(output)
        with self.assertRaises(health.HealthError):
            health.write_once(self.root, report["run_id"], output, report)
        self.assertEqual(sha256(output), before)

    def test_source_and_contract_have_no_external_or_formal_surface(self) -> None:
        source = (TOOLS / "run_daily_pipeline_health.py").read_text(encoding="utf-8")
        for prohibited in (
            "import requests",
            "import urllib",
            "import socket",
            "import subprocess",
            "os.system(",
            "Popen(",
            "schtasks",
        ):
            self.assertNotIn(prohibited, source)
        contract = json.loads(
            (
                ROOT
                / "data/catalog/contracts/daily_pipeline_health_v1/contract.json"
            ).read_text(encoding="utf-8")
        )
        self.assertFalse(contract["formal_effect"]["catalog_registration"])
        self.assertFalse(contract["formal_effect"]["research_ready"])


if __name__ == "__main__":
    unittest.main()
