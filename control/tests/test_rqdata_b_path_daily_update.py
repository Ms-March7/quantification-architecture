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
MODULE_PATH = TOOLS / "run_rqdata_b_path_daily_update.py"
CONTRACT_PATH = ROOT / "data/catalog/contracts/rqdata_b_path_daily_candidate_v1/contract.json"


def load_module():
    sys.path.insert(0, str(TOOLS))
    spec = importlib.util.spec_from_file_location("rqdata_b_path_daily", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeUser:
    def get_quota(self):
        return {"bytes_limit": 200 * 1024 * 1024, "bytes_used": 1 * 1024 * 1024}


class FakeRqdata:
    def __init__(self):
        self.user = FakeUser()
        self.ids = ["000001.XSHE", "300001.XSHE", "600000.XSHG"]

    def get_trading_dates(self, _start, _end):
        return [date(2026, 8, 4), date(2026, 8, 5)]

    def all_instruments(self, type="CS"):
        assert type == "CS"
        return pd.DataFrame([
            {"order_book_id": oid, "symbol": oid[:6], "type": "CS", "exchange": oid[-4:],
             "status": "Active", "listed_date": "2020-01-01", "de_listed_date": "0000-00-00"}
            for oid in self.ids
        ])

    def get_share_transformation(self):
        return None

    def get_price(self, ids, start_date, end_date, **_kwargs):
        rows = []
        for oid in ids:
            rows.append({
                "order_book_id": oid, "date": pd.Timestamp(start_date), "open": 10.0,
                "high": 10.5, "low": 9.8, "close": 10.2, "prev_close": 10.0,
                "volume": 1000.0, "total_turnover": 10200.0,
                "limit_up": 11.0, "limit_down": 9.0,
            })
        return pd.DataFrame(rows).set_index(["order_book_id", "date"])

    def is_st_stock(self, ids, start_date, end_date):
        return pd.DataFrame([[False] * len(ids)], index=[pd.Timestamp(start_date)], columns=ids)

    def is_suspended(self, ids, start_date, end_date):
        return pd.DataFrame([[False] * len(ids)], index=[pd.Timestamp(start_date)], columns=ids)

    def get_dividend_info(self, *_args, **_kwargs):
        return None

    def get_dividend(self, *_args, **_kwargs):
        return None

    def get_split(self, *_args, **_kwargs):
        return None

    def get_allotment(self, *_args, **_kwargs):
        return None

    def get_ex_factor(self, *_args, **_kwargs):
        return None

    def get_instrument_industry(self, ids, source, level, date):
        assert source == "sws" and level == 0
        return pd.DataFrame({
            "index_code": ["801780"] * len(ids),
            "index_name": ["银行"] * len(ids),
            "second_index_code": ["801192"] * len(ids),
            "second_index_name": ["银行Ⅱ"] * len(ids),
            "third_index_code": ["851911"] * len(ids),
            "third_index_name": ["银行Ⅲ"] * len(ids),
        }, index=pd.Index(ids, name="order_book_id"))

    def index_components(self, _index_id, date, return_create_tm):
        assert return_create_tm
        return self.ids, pd.Timestamp(f"{date} 17:00:00")

    def index_weights(self, _index_id, date):
        return pd.Series([0.3, 0.3, 0.4], index=pd.Index(self.ids, name="order_book_id"), name="weight")


class BPathDailyUpdaterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    def test_contract_is_candidate_only_and_financial_is_queue_only(self):
        safety = self.contract["safety"]
        self.assertEqual(safety["purpose_ceiling"], "INSPECTION_ONLY")
        self.assertTrue(safety["run_fail_closed"])
        for key in (
            "catalog_write", "roadmap_write", "source_registry_write", "quality_promotion",
            "formal_snapshot_write", "research_use", "credential_value_capture",
            "scheduled_task_creation",
        ):
            self.assertFalse(safety[key])
        self.assertEqual(self.contract["scope"]["financial_pit"]["execution"], "QUEUED_NOT_EXECUTED")

    def test_plan_is_explicitly_network_free_and_write_free(self):
        with mock.patch.object(self.module, "candidate_watermark", return_value=date(2026, 8, 3)), \
             mock.patch.object(self.module.pv, "formal_watermark", return_value=date(2026, 8, 3)):
            result = self.module.plan(date(2026, 8, 5))
        self.assertEqual(result["status"], "PLAN__NO_NETWORK_NO_WRITE")
        self.assertEqual(result["financial_pit"], "QUEUE_ONLY__NOT_EXECUTED")
        self.assertFalse(result["catalog_write"])
        self.assertFalse(result["scheduled_task_creation"])

    def test_master_normalization_filters_non_shsz_and_future_listings(self):
        frame = pd.DataFrame([
            {"order_book_id": "600000.XSHG", "listed_date": "1999-11-10", "de_listed_date": "0000-00-00"},
            {"order_book_id": "920001.BJSE", "listed_date": "2024-01-01", "de_listed_date": "0000-00-00"},
            {"order_book_id": "000002.XSHE", "listed_date": "2026-08-05", "de_listed_date": "0000-00-00"},
        ])
        result = self.module.normalize_master(frame, date(2026, 8, 4), date(2026, 8, 4))
        self.assertEqual(result["order_book_id"].tolist(), ["600000.XSHG"])
        self.assertIn("NOT_HISTORICAL_PIT_MASTER", result.iloc[0]["snapshot_semantics"])

    def test_daily_status_distinguishes_locked_limit_from_touch(self):
        price = pd.DataFrame([{
            "order_book_id": "600000.XSHG", "trade_date": date(2026, 8, 4),
            "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0,
            "prev_close": 10.0, "volume": 100.0, "total_turnover": 1100.0,
            "limit_up": 11.0, "limit_down": 9.0,
        }])
        st = pd.DataFrame([{"order_book_id": "600000.XSHG", "trade_date": date(2026, 8, 4), "is_st": False}])
        suspended = pd.DataFrame([{"order_book_id": "600000.XSHG", "trade_date": date(2026, 8, 4), "is_suspended": False}])
        master = pd.DataFrame([{"order_book_id": "600000.XSHG", "listed_date": date(1999, 11, 10), "de_listed_date": None}])
        result = self.module.build_daily_status(
            price, st, suspended, master, {date(2026, 8, 4): date(2026, 8, 5)}
        )
        self.assertTrue(bool(result.iloc[0]["limit_up_touched"]))
        self.assertTrue(bool(result.iloc[0]["whole_session_locked_limit_up"]))
        self.assertTrue(bool(result.iloc[0]["daily_bar_cannot_prove_intraday_queue_fill"]))

    def test_financial_queue_is_deterministic_exact_partition_and_unexecuted(self):
        ids = [f"{number:06d}.XSHE" for number in range(1, 8)]
        left = self.module.build_financial_queue(ids, date(2026, 8, 4), ["revenue"], 12, 3)
        right = self.module.build_financial_queue(ids, date(2026, 8, 4), ["revenue"], 12, 3)
        self.assertEqual(left, right)
        self.assertEqual(left["task_count"], 3)
        self.assertEqual(left["completed_tasks"], 0)
        self.assertTrue(all(task["state"] == "QUEUED_NOT_EXECUTED" for task in left["tasks"]))

    def test_end_to_end_fake_run_seals_hash_verified_candidate(self):
        contract = copy.deepcopy(self.contract)
        contract["scope"]["core_indices"] = ["000300.XSHG"]
        contract["limits"]["minimum_universe_rows"] = 2
        contract["limits"]["maximum_universe_rows"] = 10
        contract["limits"]["minimum_quota_remaining_before_run_bytes"] = 1
        contract["limits"]["action_security_batch_size"] = 2
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate_parent = root / "candidates"
            transaction_parent = root / "transactions"
            lock_parent = root / "shared-lock"
            with mock.patch.object(self.module, "load_contract", return_value=contract), \
                 mock.patch.object(self.module, "candidate_watermark", return_value=date(2026, 8, 3)), \
                 mock.patch.object(self.module, "ROOT", root), \
                 mock.patch.object(self.module, "CANDIDATE_PARENT", candidate_parent), \
                 mock.patch.object(self.module, "TRANSACTION_PARENT", transaction_parent), \
                 mock.patch.object(self.module.pv, "TRANSACTION_PARENT", lock_parent), \
                 mock.patch.object(self.module.pv, "LOCK_PATH", lock_parent / ".update.lock"):
                result = self.module.run(date(2026, 8, 4), client=FakeRqdata())
                candidate = candidate_parent / "delta-20260804-20260804-v1"
                manifest = self.module.verify_candidate(candidate)
            self.assertEqual(result["status"], "PASS__B_PATH_CANDIDATE_SEALED")
            self.assertEqual(manifest["purpose_ceiling"], "INSPECTION_ONLY")
            self.assertEqual(manifest["financial_pit"], "QUEUED_NOT_EXECUTED")
            self.assertFalse(manifest["research_ready"])
            self.assertTrue((candidate / "financial/queue.json").is_file())


if __name__ == "__main__":
    unittest.main()
