from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "control" / "tools" / "run_local_daily_orchestration.py"
SPEC = importlib.util.spec_from_file_location("local_daily_orchestration", MODULE_PATH)
assert SPEC and SPEC.loader
orchestration = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = orchestration
SPEC.loader.exec_module(orchestration)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LocalDailyOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="local-daily-orchestration-test-"
        )
        self.root = Path(self.temporary.name)
        self.catalog = self.root / "data" / "catalog" / "datasets.json"
        write_json(
            self.catalog,
            {
                "schema_version": 1,
                "catalog_mode": "federated_logical_catalog",
                "datasets": [],
            },
        )
        self.request = {
            "schema_version": 1,
            "mode": "local_daily_orchestration_state",
            "purpose_ceiling": "INSPECTION_ONLY",
            "dataset_scope": "synthetic_fixture",
            "job_id": "synthetic-orchestration-test",
            "dataset_id": "synthetic-daily-update",
            "source_id": "SYNTHETIC_FIXTURE",
            "timezone": "Asia/Shanghai",
            "calendar_dates": [
                "2026-07-30",
                "2026-07-31",
                "2026-08-03",
            ],
            "max_attempts": 3,
            "lease_seconds": 60,
            "authorization_note": "isolated unit-test fixture",
        }
        self.request_path = self.root / "request.json"
        write_json(self.request_path, self.request)
        self.now = datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc)
        self.context = orchestration.validate_workspace(
            self.root, self.request_path
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def initialize(self) -> dict:
        return orchestration.initialize(self.context, now=self.now)

    def claim(self, event_id: str, *, now: datetime | None = None) -> dict:
        return orchestration.claim(
            self.context,
            event_id=event_id,
            worker_id="fixture-worker",
            through_date=date(2026, 8, 3),
            now=now or self.now,
        )

    def test_init_is_idempotent_and_boundary_is_explicit(self) -> None:
        catalog_before = sha256(self.catalog)
        first = self.initialize()
        self.assertEqual(first["status"], "INITIALIZED")
        second = self.initialize()
        self.assertEqual(second["status"], "NO_CHANGE")
        state = orchestration.status(self.context)
        self.assertEqual(state["event_count"], 1)
        self.assertEqual(state["job_status"], "READY")
        self.assertFalse(state["formal_catalog_written"])
        self.assertFalse(state["scheduler_created"])
        self.assertEqual(sha256(self.catalog), catalog_before)
        self.assertFalse((self.root / "data" / "market").exists())

    def test_plan_is_write_free_and_lists_missed_runs(self) -> None:
        self.initialize()
        database = self.context.database_path
        before = sha256(database)
        result = orchestration.plan(
            self.context, through_date=date(2026, 8, 3)
        )
        self.assertFalse(result["write_performed"])
        self.assertEqual(
            result["missed_run_replay_queue"],
            ["2026-07-30", "2026-07-31", "2026-08-03"],
        )
        self.assertEqual(sha256(database), before)

    def test_earliest_first_replay_and_contiguous_watermark(self) -> None:
        self.initialize()
        first = self.claim("claim-first")
        self.assertEqual(first["run_date"], "2026-07-30")
        completed = orchestration.complete(
            self.context,
            event_id="complete-first",
            run_date=date(2026, 7, 30),
            claim_token=first["claim_token"],
            result_sha256="a" * 64,
            outcome="SUCCEEDED",
            now=self.now + timedelta(seconds=10),
        )
        self.assertEqual(completed["watermark_date"], "2026-07-30")
        second = self.claim("claim-second", now=self.now + timedelta(seconds=11))
        self.assertEqual(second["run_date"], "2026-07-31")
        self.assertNotEqual(second["run_date"], "2026-08-03")

    def test_expired_lease_recovery_resumes_same_date(self) -> None:
        self.initialize()
        first = self.claim("claim-before-crash")
        database_before = sha256(self.context.database_path)
        with self.assertRaises(orchestration.OrchestrationError) as raised:
            orchestration.recover(
                self.context,
                event_id="recover-too-early",
                run_date=date(2026, 7, 30),
                claim_token=first["claim_token"],
                now=self.now + timedelta(seconds=30),
            )
        self.assertEqual(raised.exception.code, "LEASE_NOT_EXPIRED")
        self.assertEqual(sha256(self.context.database_path), database_before)
        recovered = orchestration.recover(
            self.context,
            event_id="recover-expired",
            run_date=date(2026, 7, 30),
            claim_token=first["claim_token"],
            now=self.now + timedelta(seconds=61),
        )
        self.assertEqual(recovered["status"], "RETRY_PENDING")
        second = self.claim(
            "claim-after-recovery", now=self.now + timedelta(seconds=62)
        )
        self.assertEqual(second["run_date"], "2026-07-30")
        self.assertEqual(second["attempt_no"], 2)
        with self.assertRaises(orchestration.OrchestrationError) as stale:
            orchestration.complete(
                self.context,
                event_id="stale-completion",
                run_date=date(2026, 7, 30),
                claim_token=first["claim_token"],
                result_sha256="b" * 64,
                outcome="SUCCEEDED",
                now=self.now + timedelta(seconds=63),
            )
        self.assertEqual(stale.exception.code, "CLAIM_TOKEN_MISMATCH")

    def test_event_idempotency_and_reuse_conflict(self) -> None:
        self.initialize()
        first = self.claim("stable-claim")
        event_count = orchestration.status(self.context)["event_count"]
        replay = self.claim(
            "stable-claim", now=self.now + timedelta(seconds=25)
        )
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["claim_token"], first["claim_token"])
        self.assertEqual(orchestration.status(self.context)["event_count"], event_count)
        database_before = sha256(self.context.database_path)
        with self.assertRaises(orchestration.OrchestrationError) as conflict:
            orchestration.claim(
                self.context,
                event_id="stable-claim",
                worker_id="different-worker",
                through_date=date(2026, 8, 3),
                now=self.now,
            )
        self.assertEqual(conflict.exception.code, "EVENT_ID_REUSE_CONFLICT")
        self.assertEqual(sha256(self.context.database_path), database_before)

    def test_failure_retries_then_blocks_without_skipping(self) -> None:
        self.request["max_attempts"] = 2
        write_json(self.request_path, self.request)
        self.context = orchestration.validate_workspace(
            self.root, self.request_path
        )
        self.initialize()
        first = self.claim("failure-claim-1")
        failed = orchestration.fail(
            self.context,
            event_id="failure-1",
            run_date=date(2026, 7, 30),
            claim_token=first["claim_token"],
            error_code="FIXTURE_FAILURE",
            now=self.now + timedelta(seconds=1),
        )
        self.assertEqual(failed["status"], "RETRY_PENDING")
        second = self.claim("failure-claim-2", now=self.now + timedelta(seconds=2))
        terminal = orchestration.fail(
            self.context,
            event_id="failure-2",
            run_date=date(2026, 7, 30),
            claim_token=second["claim_token"],
            error_code="FIXTURE_FAILURE",
            now=self.now + timedelta(seconds=3),
        )
        self.assertEqual(terminal["status"], "FAILED_TERMINAL")
        blocked = self.claim("blocked-claim", now=self.now + timedelta(seconds=4))
        self.assertEqual(blocked["status"], "BLOCKED_RETRY_EXHAUSTED")
        self.assertEqual(blocked["run_date"], "2026-07-30")

    def test_sqlite_single_writer_fails_closed(self) -> None:
        self.initialize()
        blocker = sqlite3.connect(
            str(self.context.database_path), timeout=0, isolation_level=None
        )
        try:
            blocker.execute("BEGIN IMMEDIATE")
            with self.assertRaises(orchestration.OrchestrationError) as busy:
                self.claim("concurrent-claim")
            self.assertEqual(busy.exception.code, "ORCHESTRATOR_WRITER_BUSY")
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(orchestration.status(self.context)["event_count"], 1)

    def test_request_tamper_is_rejected_without_state_change(self) -> None:
        self.initialize()
        before = sha256(self.context.database_path)
        changed = copy.deepcopy(self.request)
        changed["lease_seconds"] = 120
        write_json(self.request_path, changed)
        changed_context = orchestration.validate_workspace(
            self.root, self.request_path
        )
        with self.assertRaises(orchestration.OrchestrationError) as mismatch:
            orchestration.status(changed_context)
        self.assertEqual(mismatch.exception.code, "REQUEST_BINDING_MISMATCH")
        self.assertEqual(sha256(self.context.database_path), before)

    def test_registered_identity_binding_is_read_only(self) -> None:
        write_json(
            self.catalog,
            {
                "schema_version": 1,
                "catalog_mode": "federated_logical_catalog",
                "datasets": [{"dataset_id": "registered-fixture"}],
            },
        )
        self.request.update(
            {
                "dataset_scope": "registered_inspection",
                "dataset_id": "registered-fixture",
                "source_id": "LOCAL_REGISTERED_FIXTURE",
                "job_id": "registered-orchestration-test",
            }
        )
        write_json(self.request_path, self.request)
        self.context = orchestration.validate_workspace(
            self.root, self.request_path
        )
        before = sha256(self.catalog)
        self.initialize()
        self.assertEqual(sha256(self.catalog), before)
        self.assertFalse(orchestration.status(self.context)["formal_catalog_written"])

    def test_contract_and_source_have_no_execution_surface(self) -> None:
        contract_root = (
            ROOT
            / "data"
            / "catalog"
            / "contracts"
            / "local_daily_orchestration_v1"
        )
        schema = json.loads(
            (contract_root / "request.schema.json").read_text(encoding="utf-8")
        )
        example = orchestration.validate_request(
            json.loads(
                (contract_root / "example.request.json").read_text(encoding="utf-8")
            )
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(example["purpose_ceiling"], "INSPECTION_ONLY")
        source = MODULE_PATH.read_text(encoding="utf-8")
        for prohibited in (
            "import requests",
            "import urllib",
            "import socket",
            "import subprocess",
            "os.system(",
            "Popen(",
        ):
            self.assertNotIn(prohibited, source)
        self.assertEqual(
            orchestration.OUTPUT_REL.as_posix(),
            "tmp/local-daily-orchestration",
        )


if __name__ == "__main__":
    unittest.main()
