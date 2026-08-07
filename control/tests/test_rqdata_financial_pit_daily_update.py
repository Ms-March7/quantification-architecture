from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "control/tools"
MODULE_PATH = TOOLS / "run_rqdata_financial_pit_daily_update.py"
CONTRACT_PATH = ROOT / "data/catalog/contracts/rqdata_financial_pit_daily_candidate_v1/contract.json"


def load_module():
    sys.path.insert(0, str(TOOLS))
    spec = importlib.util.spec_from_file_location("rqdata_financial_pit_daily", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_queue(
    module,
    *,
    as_of_date: str = "2026-08-05",
    queue_id: str = "rqdata-financial-pit-20260805-v1",
):
    request = {
        "endpoint": "get_pit_financials_ex",
        "order_book_ids": ["000001.XSHE", "600000.XSHG"],
        "fields": ["revenue", "net_profit"],
        "as_of_date": as_of_date,
        "lookback_quarters": 12,
    }
    request_sha = module.canonical_sha(request)
    return {
        "schema_version": 1,
        "queue_id": queue_id,
        "status": "QUEUED_NOT_EXECUTED__NO_FINANCIAL_API_CALLS",
        "task_count": 1,
        "security_count": 2,
        "tasks": [{
            "task_id": f"financial-pit-20260805-0001-{request_sha[:12]}",
            "request": request,
            "request_sha256": request_sha,
            "state": "QUEUED_NOT_EXECUTED",
            "attempts": 0,
            "checkpoint": None,
            "payload_path": None,
        }],
    }


class FakeUser:
    def get_quota(self):
        return {"bytes_limit": 200 * 1024 * 1024, "bytes_used": 1024 * 1024}


class FakeRqdata:
    def __init__(self):
        self.user = FakeUser()
        self.financial_calls = 0
        self.calendar_calls = 0

    def get_pit_financials_ex(self, ids, fields, start_quarter, end_quarter, date, statements):
        self.financial_calls += 1
        assert ids == ["000001.XSHE", "600000.XSHG"]
        assert fields == ["revenue", "net_profit"]
        assert (start_quarter, end_quarter) == ("2023q3", "2026q2")
        assert date == "2026-08-05" and statements == "all"
        return pd.DataFrame([
            {
                "order_book_id": "000001.XSHE", "quarter": "2026q2",
                "info_date": "2026-07-31", "if_adjusted": 0,
                "rice_create_tm": "2026-08-01 02:00:00",
                "revenue": 100.0, "net_profit": 10.0,
            },
            {
                "order_book_id": "000001.XSHE", "quarter": "2026q2",
                "info_date": "2026-08-03", "if_adjusted": 1,
                "rice_create_tm": "2026-08-03 20:00:00",
                "revenue": 105.0, "net_profit": 11.0,
            },
            {
                "order_book_id": "600000.XSHG", "quarter": "2026q2",
                "info_date": "2026-08-02", "if_adjusted": 0,
                "rice_create_tm": "2026-08-03 01:00:00",
                "revenue": 200.0, "net_profit": 20.0,
            },
        ]).set_index(["order_book_id", "quarter"])

    def get_trading_dates(self, start, end):
        self.calendar_calls += 1
        assert start == "2026-08-01" and end == "2026-08-17"
        return [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)]


class FakeSmokeRqdata(FakeRqdata):
    def get_pit_financials_ex(self, ids, fields, start_quarter, end_quarter, date, statements):
        self.financial_calls += 1
        assert ids == ["000001.XSHE", "600000.XSHG"]
        assert fields == ["revenue", "net_profit"]
        assert (start_quarter, end_quarter) == ("2025q3", "2026q2")
        assert date == "2026-08-05" and statements == "all"
        return pd.DataFrame([{
            "order_book_id": "000001.XSHE", "quarter": "2026q2",
            "info_date": "2026-08-03", "if_adjusted": 0,
            "rice_create_tm": "2026-08-03 20:00:00",
            "revenue": 105.0, "net_profit": 11.0,
        }]).set_index(["order_book_id", "quarter"])

    def get_trading_dates(self, start, end):
        self.calendar_calls += 1
        assert start == "2026-08-04" and end == "2026-08-17"
        return [date(2026, 8, 4), date(2026, 8, 5)]


class FinancialPitDailyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    def test_contract_is_bounded_candidate_only(self):
        safety = self.contract["safety"]
        self.assertEqual(safety["purpose_ceiling"], "INSPECTION_ONLY")
        self.assertFalse(safety["full_market_historical_backfill"])
        self.assertEqual(self.contract["limits"]["maximum_tasks_per_run"], 1)
        for key in (
            "catalog_write", "roadmap_write", "source_registry_write",
            "quality_promotion", "formal_snapshot_write", "research_use",
            "credential_value_capture", "scheduled_task_creation",
        ):
            self.assertFalse(safety[key])

    def test_quarter_range_uses_last_completed_quarter(self):
        self.assertEqual(self.module.quarter_range(date(2026, 8, 5), 12), ("2023q3", "2026q2"))
        self.assertEqual(self.module.quarter_range(date(2026, 1, 2), 1), ("2025q4", "2025q4"))

    def test_normalize_preserves_rice_create_tm_only_as_vendor_metadata(self):
        queue = build_queue(self.module)
        request = queue["tasks"][0]["request"]
        frame = FakeRqdata().get_pit_financials_ex(
            request["order_book_ids"], request["fields"], "2023q3", "2026q2",
            request["as_of_date"], "all",
        )
        result = self.module.normalize_frame(frame, request)
        self.assertIn("rice_create_tm_vendor_metadata", result.columns)
        self.assertNotIn("rice_create_tm", result.columns)
        self.assertNotIn("available_at", result.columns)

    def test_next_session_is_strictly_after_info_date(self):
        result = self.module.next_session_map(
            [date(2026, 7, 31), date(2026, 8, 2), date(2026, 8, 3)],
            [date(2026, 7, 31), date(2026, 8, 3), date(2026, 8, 4)],
        )
        self.assertEqual(result[date(2026, 7, 31)], date(2026, 8, 3))
        self.assertEqual(result[date(2026, 8, 2)], date(2026, 8, 3))
        self.assertEqual(result[date(2026, 8, 3)], date(2026, 8, 4))

    def test_revision_chain_is_deterministic_and_excludes_vendor_time(self):
        queue = build_queue(self.module)
        request = queue["tasks"][0]["request"]
        client = FakeRqdata()
        raw = self.module.normalize_frame(
            client.get_pit_financials_ex(
                request["order_book_ids"], request["fields"], "2023q3", "2026q2",
                request["as_of_date"], "all",
            ),
            request,
        )
        sessions = self.module.next_session_map(
            [date.fromisoformat(item) for item in raw["info_date"]],
            [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)],
        )
        left = self.module.build_events(raw, request["fields"], sessions, "2026-08-05T10:00:00+00:00")
        right = self.module.build_events(raw, request["fields"], sessions, "2026-08-05T10:00:00+00:00")
        self.assertEqual(left, right)
        revenue = [
            row for row in left
            if row["business_key"] == {"order_book_id": "000001.XSHE", "metric": "revenue"}
        ]
        self.assertEqual([row["revision_no"] for row in revenue], [1, 2])
        self.assertIsNone(revenue[0]["supersedes_revision_id"])
        self.assertEqual(revenue[1]["supersedes_revision_id"], revenue[0]["revision_id"])
        self.assertEqual(revenue[0]["available_at"], "2026-08-03T09:30:00+08:00")
        self.assertNotIn("rice_create_tm", json.dumps(left))

    def test_same_chain_same_info_date_conflict_fails_closed(self):
        frame = pd.DataFrame([
            {"order_book_id": "000001.XSHE", "quarter": "2026q2", "info_date": "2026-08-03", "if_adjusted": 0, "revenue": 1.0},
            {"order_book_id": "000001.XSHE", "quarter": "2026q2", "info_date": "2026-08-03", "if_adjusted": 0, "revenue": 2.0},
        ])
        with self.assertRaises(self.module.UpdateError):
            self.module.build_events(
                frame, ["revenue"], {date(2026, 8, 3): date(2026, 8, 4)},
                "2026-08-05T10:00:00+00:00",
            )

    def test_fake_end_to_end_seals_candidate_and_resume_is_network_free(self):
        queue = build_queue(self.module)
        queue_sha = self.module.canonical_sha(queue)
        contract = copy.deepcopy(self.contract)
        client = FakeRqdata()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue_path = root / "source/financial/queue.json"
            queue_path.parent.mkdir(parents=True)
            queue_path.write_text(json.dumps(queue), encoding="utf-8")
            candidate_parent = root / "candidates"
            state_parent = root / "state"
            with mock.patch.object(self.module, "ROOT", root), \
                 mock.patch.object(self.module, "CANDIDATE_PARENT", candidate_parent), \
                 mock.patch.object(self.module, "STATE_PARENT", state_parent), \
                 mock.patch.object(self.module, "load_contract", return_value=contract), \
                 mock.patch.object(self.module, "verify_source_queue", return_value=(queue, queue_sha, root / "source")), \
                 mock.patch.object(self.module.pv, "acquire_lock", return_value={}), \
                 mock.patch.object(self.module.pv, "release_lock"):
                first = self.module.run(queue_path, client=client)
                second = self.module.run(queue_path, client=client)
                with mock.patch.object(self.module, "discover_queue_path", return_value=queue_path):
                    discovered = self.module.run_discovered(date(2026, 8, 5), client=client)
                task_id = queue["tasks"][0]["task_id"]
                candidate = candidate_parent / queue["queue_id"] / f"{task_id}-v1"
                manifest = self.module.verify_task_candidate(candidate, queue["tasks"][0], queue_sha)
            self.assertEqual(first["status"], "PASS__BOUNDED_FINANCIAL_PIT_CANDIDATE_TASK")
            self.assertEqual(second["status"], "NO_CHANGE__QUEUE_COMPLETE__NO_NETWORK")
            self.assertEqual(discovered["status"], "NO_CHANGE__QUEUE_COMPLETE__NO_NETWORK")
            self.assertEqual(client.financial_calls, 1)
            self.assertEqual(client.calendar_calls, 1)
            self.assertEqual(manifest["raw_rows"], 3)
            self.assertEqual(manifest["revision_events"], 6)
            self.assertFalse(manifest["research_ready"])
            self.assertTrue((candidate / "revision_events.jsonl").is_file())

    def test_discovery_selects_oldest_pending_eligible_queue(self):
        queues = [
            build_queue(
                self.module,
                as_of_date=value,
                queue_id=f"rqdata-financial-pit-{value.replace('-', '')}-v1",
            )
            for value in ("2026-08-03", "2026-08-04", "2026-08-05")
        ]
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            paths = []
            for queue, day in zip(queues, ("20260803", "20260804", "20260805")):
                path = parent / f"delta-{day}-{day}-v1/financial/queue.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(queue), encoding="utf-8")
                paths.append(path)

            def fake_verify(path):
                queue = json.loads(path.read_text(encoding="utf-8"))
                return queue, self.module.canonical_sha(queue), path.parents[1]

            def fake_state(queue, queue_sha):
                completed = {}
                if queue["queue_id"].endswith("20260803-v1"):
                    task = queue["tasks"][0]
                    completed[task["task_id"]] = {}
                return {
                    "queue_id": queue["queue_id"],
                    "queue_sha256": queue_sha,
                    "completed": completed,
                }

            with mock.patch.object(self.module, "B_PATH_CANDIDATE_PARENT", parent), \
                 mock.patch.object(self.module, "verify_source_queue", side_effect=fake_verify), \
                 mock.patch.object(self.module, "load_state", side_effect=fake_state), \
                 mock.patch.object(self.module, "reconcile_state"):
                selected = self.module.discover_queue_path(date(2026, 8, 5))
            self.assertEqual(selected, paths[1])

    def test_live_smoke_is_separate_and_never_advances_queue_state(self):
        queue = build_queue(self.module)
        queue_sha = self.module.canonical_sha(queue)
        client = FakeSmokeRqdata()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue_path = root / "source/financial/queue.json"
            queue_path.parent.mkdir(parents=True)
            queue_path.write_text(json.dumps(queue), encoding="utf-8")
            smoke_parent = root / "smoke"
            state_parent = root / "state"
            with mock.patch.object(self.module, "ROOT", root), \
                 mock.patch.object(self.module, "SMOKE_PARENT", smoke_parent), \
                 mock.patch.object(self.module, "STATE_PARENT", state_parent), \
                 mock.patch.object(self.module, "verify_source_queue", return_value=(queue, queue_sha, root / "source")), \
                 mock.patch.object(self.module.pv, "acquire_lock", return_value={}), \
                 mock.patch.object(self.module.pv, "release_lock"):
                first = self.module.smoke(queue_path, client=client)
                second = self.module.smoke(queue_path, client=client)
            self.assertEqual(first["status"], "PASS__LIVE_SMOKE_CANDIDATE_ONLY")
            self.assertEqual(second["status"], "NO_CHANGE__SMOKE_CANDIDATE_EXISTS__NO_NETWORK")
            self.assertFalse(first["queue_state_advanced"])
            self.assertEqual(client.financial_calls, 1)
            self.assertEqual(client.calendar_calls, 1)
            self.assertFalse(state_parent.exists())


if __name__ == "__main__":
    unittest.main()
