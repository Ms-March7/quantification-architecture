from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "control/tools/run_candidate_daily_maintenance.py"
CONTRACT_PATH = ROOT / "data/catalog/contracts/candidate_daily_maintenance_v1/contract.json"


def load_module():
    spec = importlib.util.spec_from_file_location("candidate_daily_maintenance", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CandidateDailyMaintenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    def test_contract_is_candidate_only_and_ordered(self):
        self.assertEqual(
            self.contract["step_order"],
            [
                "rqdata_shsz_price_volume",
                "rqdata_b_path",
                "rqdata_financial_pit",
                "joinquant_share_capital_pit",
                "candidate_asset_index",
            ],
        )
        safety = self.contract["safety"]
        self.assertEqual(safety["purpose_ceiling"], "INSPECTION_ONLY")
        self.assertTrue(safety["candidate_index_write"])
        for key in (
            "formal_catalog_write", "formal_snapshot_write", "quality_promotion",
            "research_run", "validation_or_holdout_consumption",
            "credential_value_read", "credential_value_capture",
        ):
            self.assertFalse(safety[key])

    def test_plan_is_write_free_and_uses_index_check(self):
        result = self.module.plan(date(2026, 8, 5))
        self.assertEqual(result["status"], "PLAN__NO_NETWORK_NO_WRITE")
        self.assertEqual(result["commands"][0]["arguments"][0], "--plan")
        self.assertEqual(result["commands"][1]["arguments"][0], "--plan")
        self.assertEqual(
            result["commands"][2]["arguments"],
            ["--plan", "--discover-through", "2026-08-05"],
        )
        self.assertEqual(result["commands"][3]["arguments"], ["--plan"])
        self.assertEqual(result["commands"][4]["arguments"], ["--check"])
        self.assertFalse(result["formal_catalog_write"])
        self.assertFalse(result["credential_value_read"])

    def test_safe_text_redacts_credentials_and_endpoints(self):
        value = self.module.safe_text("license=abc tcp://secret.example:123 token:xyz")
        self.assertNotIn("abc", value)
        self.assertNotIn("secret.example", value)
        self.assertNotIn("xyz", value)

    def test_os_lock_is_automatically_released_after_hard_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / ".maintenance.lock"
            child = (
                "import importlib.util, os, pathlib, sys; "
                f"p=pathlib.Path({str(MODULE_PATH)!r}); "
                "s=importlib.util.spec_from_file_location('maintenance_lock_child',p); "
                "m=importlib.util.module_from_spec(s); sys.modules[s.name]=m; s.loader.exec_module(m); "
                f"m.TRANSACTION_PARENT=pathlib.Path({str(root)!r}); "
                f"m.LOCK_PATH=pathlib.Path({str(lock)!r}); "
                "m.acquire_lock(); os._exit(91)"
            )
            completed = subprocess.run([sys.executable, "-c", child], check=False)
            self.assertEqual(completed.returncode, 91)
            with mock.patch.object(self.module, "TRANSACTION_PARENT", root), \
                 mock.patch.object(self.module, "LOCK_PATH", lock):
                descriptor = self.module.acquire_lock()
                self.module.release_lock(descriptor)

    def test_shared_rqdata_lock_is_automatically_released_after_hard_exit(self):
        source_module = ROOT / "control/tools/run_rqdata_shsz_daily_update.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / ".update.lock"
            child = (
                "import importlib.util, os, pathlib, sys; "
                f"p=pathlib.Path({str(source_module)!r}); "
                "s=importlib.util.spec_from_file_location('rq_lock_child',p); "
                "m=importlib.util.module_from_spec(s); sys.modules[s.name]=m; s.loader.exec_module(m); "
                f"m.TRANSACTION_PARENT=pathlib.Path({str(root)!r}); "
                f"m.LOCK_PATH=pathlib.Path({str(lock)!r}); "
                "m.acquire_lock(); os._exit(92)"
            )
            completed = subprocess.run([sys.executable, "-c", child], check=False)
            self.assertEqual(completed.returncode, 92)
            spec = importlib.util.spec_from_file_location("rq_lock_parent", source_module)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            with mock.patch.object(module, "TRANSACTION_PARENT", root), \
                 mock.patch.object(module, "LOCK_PATH", lock):
                descriptor = module.acquire_lock()
                module.release_lock(descriptor)

    def test_run_continues_steps_writes_receipt_and_preserves_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "datasets.json"
            index = root / "candidate_assets.json"
            transaction_parent = root / "transactions"
            lock = transaction_parent / ".lock"
            catalog.write_text('{"datasets": []}\n', encoding="utf-8")
            index.write_text('{"assets": []}\n', encoding="utf-8")
            observed: list[str] = []

            def fake_execute(step, **_kwargs):
                observed.append(step.name)
                return {"name": step.name, "status": "PASS", "returncode": 0}

            with mock.patch.object(self.module, "FORMAL_CATALOG_PATH", catalog), \
                 mock.patch.object(self.module, "CANDIDATE_INDEX_PATH", index), \
                 mock.patch.object(self.module, "TRANSACTION_PARENT", transaction_parent), \
                 mock.patch.object(self.module, "LOCK_PATH", lock), \
                 mock.patch.object(self.module, "ROOT", root), \
                 mock.patch.object(self.module, "execute_step", side_effect=fake_execute):
                result = self.module.run(date(2026, 8, 5))
            self.assertEqual(observed, self.contract["step_order"])
            self.assertEqual(result["status"], "PASS__CANDIDATE_DAILY_MAINTENANCE")
            self.assertTrue(result["formal_catalog_unchanged"])
            self.assertEqual(len(list(transaction_parent.glob("candidate-maintenance-*.json"))), 1)

    def test_one_failure_still_runs_all_steps_and_fails_overall(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "datasets.json"
            index = root / "candidate_assets.json"
            transaction_parent = root / "transactions"
            lock = transaction_parent / ".lock"
            catalog.write_text('{"datasets": []}\n', encoding="utf-8")
            index.write_text('{"assets": []}\n', encoding="utf-8")
            observed: list[str] = []

            def fake_execute(step, **_kwargs):
                observed.append(step.name)
                code = 2 if step.name == "rqdata_financial_pit" else 0
                return {"name": step.name, "status": "FAIL" if code else "PASS", "returncode": code}

            with mock.patch.object(self.module, "FORMAL_CATALOG_PATH", catalog), \
                 mock.patch.object(self.module, "CANDIDATE_INDEX_PATH", index), \
                 mock.patch.object(self.module, "TRANSACTION_PARENT", transaction_parent), \
                 mock.patch.object(self.module, "LOCK_PATH", lock), \
                 mock.patch.object(self.module, "ROOT", root), \
                 mock.patch.object(self.module, "execute_step", side_effect=fake_execute):
                result = self.module.run(date(2026, 8, 5))
            self.assertEqual(observed, self.contract["step_order"])
            self.assertEqual(result["status"], "FAIL_CLOSED__CANDIDATE_DAILY_MAINTENANCE")


if __name__ == "__main__":
    unittest.main()
