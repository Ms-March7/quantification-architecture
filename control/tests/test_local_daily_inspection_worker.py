from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "control" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
MODULE_PATH = TOOLS / "run_local_daily_inspection_worker.py"
SPEC = importlib.util.spec_from_file_location("local_daily_inspection_worker", MODULE_PATH)
assert SPEC and SPEC.loader
worker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = worker
SPEC.loader.exec_module(worker)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LocalDailyInspectionWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="local-daily-inspection-worker-test-"
        )
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
            "job_id": "synthetic-worker-test",
            "dataset_id": "synthetic-worker-daily",
            "source_id": "SYNTHETIC_FIXTURE",
            "timezone": "Asia/Shanghai",
            "calendar_dates": ["2026-07-30"],
            "max_attempts": 3,
            "lease_seconds": 300,
            "authorization_note": "isolated fixture only",
        }
        self.update_request = {
            "schema_version": 1,
            "mode": "local_daily_update_candidate",
            "dataset_scope": "synthetic_fixture",
            "dataset_id": "synthetic-worker-daily",
            "run_id": "worker-20260730-v1",
            "source_id": "SYNTHETIC_FIXTURE",
            "dataset_schema_version": "candidate-v1",
            "authorization_note": "isolated fixture only",
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
        self.orchestration_path = self.root / "orchestration.json"
        self.update_path = self.root / "update.json"
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.now = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)
        write_json(self.orchestration_path, self.orchestration_request)
        write_json(self.update_path, self.update_request)
        self.write_rows(
            [
                {
                    "security_id": "XSHG.600000",
                    "trade_date": "2026-07-30",
                    "close": 10.5,
                }
            ]
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_rows(self, rows: list[dict]) -> None:
        payload = "".join(
            json.dumps(row, ensure_ascii=False) + "\n" for row in rows
        )
        (self.inbox / "delta.jsonl").write_text(payload, encoding="utf-8")

    def run_worker(self, *, execute: bool = False) -> dict:
        return worker.run(
            workspace_root=self.root,
            orchestration_request_path=self.orchestration_path,
            update_request_path=self.update_path,
            inbox=self.inbox,
            through_date=date(2026, 7, 30),
            worker_id="fixture-worker",
            claim_event_id="fixture-claim",
            finish_event_id="fixture-finish",
            execute=execute,
            now=self.now,
        )

    def test_plan_is_write_free_and_identity_bound(self) -> None:
        catalog_before = sha256(self.catalog)
        result = self.run_worker()
        self.assertEqual(result["status"], "PLAN")
        self.assertEqual(result["incoming_date"], "2026-07-30")
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(sha256(self.catalog), catalog_before)
        self.assertFalse((self.root / "tmp").exists())
        self.assertFalse((self.root / "data" / "market").exists())

    def test_execute_claims_builds_and_completes_candidate(self) -> None:
        catalog_before = sha256(self.catalog)
        result = self.run_worker(execute=True)
        self.assertEqual(result["status"], "CANDIDATE_PUBLISHED")
        self.assertEqual(result["completion"]["status"], "SUCCEEDED")
        candidate = Path(result["candidate_path"])
        self.assertTrue((candidate / "manifest.json").is_file())
        self.assertEqual(result["result_sha256"], sha256(candidate / "manifest.json"))
        context = worker.orchestration.validate_workspace(
            self.root, self.orchestration_path
        )
        state = worker.orchestration.status(context)
        self.assertEqual(state["watermark_date"], "2026-07-30")
        self.assertEqual(state["dates"][0]["status"], "SUCCEEDED")
        self.assertFalse(state["formal_catalog_written"])
        self.assertEqual(sha256(self.catalog), catalog_before)
        self.assertFalse((self.root / "data" / "market").exists())

    def test_identity_mismatch_rejects_before_state_write(self) -> None:
        changed = copy.deepcopy(self.update_request)
        changed["source_id"] = "OTHER_FIXTURE"
        write_json(self.update_path, changed)
        with self.assertRaises(worker.WorkerError) as raised:
            self.run_worker(execute=True)
        self.assertEqual(raised.exception.code, "WORKER_IDENTITY_BINDING_MISMATCH")
        self.assertFalse((self.root / "tmp").exists())

    def test_multiple_input_dates_reject_before_state_write(self) -> None:
        self.write_rows(
            [
                {
                    "security_id": "XSHG.600000",
                    "trade_date": "2026-07-30",
                    "close": 10.5,
                },
                {
                    "security_id": "XSHG.600001",
                    "trade_date": "2026-07-31",
                    "close": 9.5,
                },
            ]
        )
        with self.assertRaises(worker.WorkerError) as raised:
            self.run_worker(execute=True)
        self.assertEqual(raised.exception.code, "WORKER_MULTIPLE_INPUT_DATES")
        self.assertFalse((self.root / "tmp").exists())

    def test_reparse_parent_chain_is_rejected_before_file_read(self) -> None:
        nested = self.root / "synthetic-reparse-parent"
        nested.mkdir()
        request = nested / "request.json"
        write_json(request, self.update_request)
        original_is_reparse = worker.orchestration.is_reparse

        def mark_parent_reparse(path: Path) -> bool:
            return path == nested or original_is_reparse(path)

        with mock.patch.object(
            worker.orchestration,
            "is_reparse",
            side_effect=mark_parent_reparse,
        ):
            with self.assertRaises(worker.WorkerError) as raised:
                worker.regular_workspace_file(
                    self.root, request, "synthetic request"
                )

        self.assertEqual(raised.exception.code, "WORKER_PATH_REJECTED")

    def test_later_input_does_not_consume_earlier_attempt(self) -> None:
        self.orchestration_request["calendar_dates"] = [
            "2026-07-29",
            "2026-07-30",
        ]
        write_json(self.orchestration_path, self.orchestration_request)
        result = self.run_worker(execute=True)
        self.assertEqual(result["status"], "INPUT_NOT_EARLIEST_DUE")
        self.assertEqual(result["next_unfinished_date"], "2026-07-29")
        context = worker.orchestration.validate_workspace(
            self.root, self.orchestration_path
        )
        state = worker.orchestration.status(context)
        self.assertEqual(state["dates"][0]["attempt_count"], 0)
        self.assertEqual(state["dates"][1]["attempt_count"], 0)

    def test_post_build_verification_failure_is_recorded_and_not_left_running(self) -> None:
        self.orchestration_request["max_attempts"] = 1
        write_json(self.orchestration_path, self.orchestration_request)
        original_verify = worker.daily_update.verify_candidate
        call_count = 0

        def fail_only_worker_reverification(path: Path) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return original_verify(path)
            raise worker.daily_update.DailyUpdateError(
                "synthetic post-build verification failure"
            )

        with mock.patch.object(
            worker.daily_update,
            "verify_candidate",
            side_effect=fail_only_worker_reverification,
        ):
            result = self.run_worker(execute=True)

        self.assertEqual(result["status"], "WORKER_FAILED")
        self.assertEqual(
            result["error_code"], "WORKER_RESULT_VERIFICATION_FAILED"
        )
        self.assertEqual(result["failure"]["status"], "FAILED_TERMINAL")
        context = worker.orchestration.validate_workspace(
            self.root, self.orchestration_path
        )
        state = worker.orchestration.status(context)
        self.assertEqual(state["dates"][0]["status"], "FAILED_TERMINAL")
        self.assertIsNone(state["dates"][0]["active_claim_token"])
        connection = worker.orchestration.open_readonly_database(
            context.database_path
        )
        try:
            attempt = connection.execute(
                "SELECT outcome, error_code FROM attempts"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(attempt["outcome"], "FAILED")
        self.assertEqual(
            attempt["error_code"], "WORKER_RESULT_VERIFICATION_FAILED"
        )

    def test_input_mutation_during_execute_cannot_complete_claimed_date(self) -> None:
        self.orchestration_request["max_attempts"] = 1
        write_json(self.orchestration_path, self.orchestration_request)
        original_run = worker.daily_update.run
        call_count = 0

        def mutate_on_execute(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                self.write_rows(
                    [
                        {
                            "security_id": "XSHG.600000",
                            "trade_date": "2026-07-31",
                            "close": 10.5,
                        }
                    ]
                )
            return original_run(**kwargs)

        with mock.patch.object(
            worker.daily_update,
            "run",
            side_effect=mutate_on_execute,
        ):
            result = self.run_worker(execute=True)

        self.assertEqual(call_count, 3)
        self.assertEqual(result["status"], "WORKER_FAILED")
        self.assertEqual(result["error_code"], "WORKER_RESULT_BINDING_DRIFT")
        self.assertEqual(result["failure"]["status"], "FAILED_TERMINAL")
        context = worker.orchestration.validate_workspace(
            self.root, self.orchestration_path
        )
        state = worker.orchestration.status(context)
        self.assertEqual(state["dates"][0]["status"], "FAILED_TERMINAL")
        self.assertIsNone(state["dates"][0]["active_claim_token"])
        self.assertIsNone(state["watermark_date"])
        connection = worker.orchestration.open_readonly_database(
            context.database_path
        )
        try:
            attempt = connection.execute(
                "SELECT outcome, error_code FROM attempts"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(attempt["outcome"], "FAILED")
        self.assertEqual(attempt["error_code"], "WORKER_RESULT_BINDING_DRIFT")

    def test_unexpected_execute_exception_fails_and_clears_claim(self) -> None:
        self.orchestration_request["max_attempts"] = 1
        write_json(self.orchestration_path, self.orchestration_request)
        original_run = worker.daily_update.run
        call_count = 0

        def raise_on_execute(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise OSError("synthetic disk failure")
            return original_run(**kwargs)

        with mock.patch.object(
            worker.daily_update,
            "run",
            side_effect=raise_on_execute,
        ):
            result = self.run_worker(execute=True)

        self.assertEqual(call_count, 3)
        self.assertEqual(result["status"], "WORKER_FAILED")
        self.assertEqual(result["error_code"], "WORKER_EXECUTION_EXCEPTION")
        self.assertEqual(result["failure"]["status"], "FAILED_TERMINAL")
        context = worker.orchestration.validate_workspace(
            self.root, self.orchestration_path
        )
        state = worker.orchestration.status(context)
        self.assertEqual(state["dates"][0]["status"], "FAILED_TERMINAL")
        self.assertIsNone(state["dates"][0]["active_claim_token"])
        connection = worker.orchestration.open_readonly_database(
            context.database_path
        )
        try:
            attempt = connection.execute(
                "SELECT outcome, error_code FROM attempts"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(attempt["outcome"], "FAILED")
        self.assertEqual(attempt["error_code"], "WORKER_EXECUTION_EXCEPTION")

    def test_worker_source_has_no_external_execution_surface(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
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
        self.assertNotIn("data/catalog/datasets.json", source)
        self.assertNotIn("data/market", source)


if __name__ == "__main__":
    unittest.main()
