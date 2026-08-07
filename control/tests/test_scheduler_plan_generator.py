from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "control" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
MODULE_PATH = TOOLS / "generate_windows_scheduler_plan.py"
SPEC = importlib.util.spec_from_file_location("scheduler_plan_generator", MODULE_PATH)
assert SPEC and SPEC.loader
generator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


class SchedulerPlanGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="scheduler-plan-test-")
        self.root = Path(self.temporary.name).resolve()
        fixed_files = {
            generator.PYTHON_REL: b"synthetic python executable",
            generator.WORKER_REL: b"synthetic inspection worker",
            generator.ORCHESTRATION_REL: b"synthetic orchestration engine",
            generator.DAILY_UPDATE_REL: b"synthetic daily update engine",
            generator.GENERATOR_REL: MODULE_PATH.read_bytes(),
            generator.REQUEST_SCHEMA_REL: (
                ROOT / generator.REQUEST_SCHEMA_REL
            ).read_bytes(),
        }
        for relative, payload in fixed_files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)

        self.orchestration = {
            "schema_version": 1,
            "mode": "local_daily_orchestration_state",
            "purpose_ceiling": "INSPECTION_ONLY",
            "dataset_scope": "synthetic_fixture",
            "job_id": "scheduler-plan-test",
            "dataset_id": "synthetic-scheduled-daily",
            "source_id": "SYNTHETIC_FIXTURE",
            "timezone": "Asia/Shanghai",
            "calendar_dates": ["2026-07-30", "2026-07-31"],
            "max_attempts": 3,
            "lease_seconds": 300,
            "authorization_note": "isolated fixture only",
        }
        self.orchestration_path = self.root / "requests" / "orchestration.json"
        write_json(self.orchestration_path, self.orchestration)
        jobs = []
        for trade_date in self.orchestration["calendar_dates"]:
            compact = trade_date.replace("-", "")
            update = {
                "schema_version": 1,
                "mode": "local_daily_update_candidate",
                "dataset_scope": "synthetic_fixture",
                "dataset_id": "synthetic-scheduled-daily",
                "run_id": f"scheduler-{compact}-v1",
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
                    "max_total_input_bytes": 1000000,
                    "max_rows": 1000,
                },
            }
            update_relative = f"requests/update-{trade_date}.json"
            write_json(self.root / update_relative, update)
            jobs.append(
                {
                    "trade_date": trade_date,
                    "update_request": update_relative,
                    "inbox": f"tmp/scheduler-inbox/{trade_date}",
                }
            )
        self.request = {
            "schema_version": 1,
            "mode": "scheduler_plan_generator",
            "purpose_ceiling": "INSPECTION_ONLY",
            "plan_id": "fixture-plan-v1",
            "timezone": "Asia/Shanghai",
            "windows_timezone_id": "China Standard Time",
            "account_placeholder": "__WINDOWS_ACCOUNT_PLACEHOLDER__",
            "trigger_local_time": "18:30:00",
            "orchestration_request": "requests/orchestration.json",
            "jobs": jobs,
            "safety": dict(generator.SAFETY_FIELDS),
            "authorization_note": "Isolated plan fixture only; no installation is authorized.",
        }
        self.request_path = self.root / "requests" / "scheduler.json"
        write_json(self.request_path, self.request)
        self.output = (
            self.root / "tmp" / "scheduler-plan-generator" / "fixture-plan-v1"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def bind(self):
        return generator.bind_request(self.root, self.request_path)

    def test_default_plan_is_write_free_and_frozen_date_bound(self) -> None:
        plan = generator.build_plan(self.bind(), include_xml=True)
        self.assertEqual(plan["status"], "PLAN")
        self.assertEqual(plan["purpose_ceiling"], "INSPECTION_ONLY")
        self.assertEqual(len(plan["tasks"]), 2)
        self.assertFalse((self.root / "tmp").exists())
        for task in plan["tasks"]:
            xml = task["xml_utf8"]
            self.assertIn("__WINDOWS_ACCOUNT_PLACEHOLDER__", xml)
            self.assertIn("<StartWhenAvailable>true</StartWhenAvailable>", xml)
            self.assertIn(
                "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", xml
            )
            self.assertIn("<RunOnlyIfNetworkAvailable>false", xml)
            self.assertTrue(task["trigger"]["start_boundary"].endswith("+08:00"))
            self.assertEqual(task["command"]["arguments"][-1], "--execute")
            self.assertEqual(
                task["account"]["user_id"], generator.ACCOUNT_PLACEHOLDER
            )

    def test_atomic_bundle_round_trip_and_non_overwrite(self) -> None:
        result = generator.write_bundle(self.bind(), self.output)
        self.assertEqual(result["status"], "PLAN_BUNDLE_WRITTEN")
        verified = generator.verify_existing_bundle(self.root, self.output)
        self.assertEqual(verified["status"], "PASS")
        self.assertEqual(verified["task_count"], 2)
        with self.assertRaises(generator.SchedulerPlanError) as raised:
            generator.write_bundle(self.bind(), self.output)
        self.assertEqual(raised.exception.code, "SCHEDULER_PLAN_OUTPUT_EXISTS")

    def test_failed_write_leaves_no_bundle_or_staging(self) -> None:
        with mock.patch.object(
            generator, "write_file_exclusive", side_effect=OSError("synthetic")
        ):
            with self.assertRaises(OSError):
                generator.write_bundle(self.bind(), self.output)
        self.assertFalse(self.output.exists())
        parent = self.output.parent
        if parent.exists():
            self.assertEqual(list(parent.glob(".fixture-plan-v1.staging-*")), [])

    def test_formal_or_promotion_request_fails_closed(self) -> None:
        for field in ("formal_data_write", "formal_catalog_write", "quality_promotion"):
            changed = copy.deepcopy(self.request)
            changed["safety"][field] = True
            write_json(self.request_path, changed)
            with self.assertRaises(generator.SchedulerPlanError) as raised:
                self.bind()
            self.assertEqual(
                raised.exception.code, "SCHEDULER_PLAN_FORMAL_SCOPE_REJECTED"
            )
        write_json(self.request_path, self.request)

    def test_unknown_formal_parameter_and_real_account_are_rejected(self) -> None:
        changed = copy.deepcopy(self.request)
        changed["formal"] = False
        write_json(self.request_path, changed)
        with self.assertRaises(generator.SchedulerPlanError) as raised:
            self.bind()
        self.assertEqual(raised.exception.code, "SCHEDULER_PLAN_SCHEMA_REJECTED")
        changed = copy.deepcopy(self.request)
        changed["account_placeholder"] = "DOMAIN\\person"
        write_json(self.request_path, changed)
        with self.assertRaises(generator.SchedulerPlanError) as raised:
            self.bind()
        self.assertEqual(raised.exception.code, "SCHEDULER_PLAN_CREDENTIAL_REJECTED")

    def test_non_calendar_date_and_identity_mismatch_are_rejected(self) -> None:
        changed = copy.deepcopy(self.request)
        changed["jobs"][1]["trade_date"] = "2026-08-01"
        write_json(self.request_path, changed)
        with self.assertRaises(generator.SchedulerPlanError) as raised:
            self.bind()
        self.assertEqual(
            raised.exception.code, "SCHEDULER_PLAN_NON_TRADING_DATE_REJECTED"
        )
        changed = copy.deepcopy(self.request)
        update_path = self.root / changed["jobs"][0]["update_request"]
        update = json.loads(update_path.read_text(encoding="utf-8"))
        update["source_id"] = "OTHER_FIXTURE"
        write_json(update_path, update)
        write_json(self.request_path, changed)
        with self.assertRaises(generator.SchedulerPlanError) as raised:
            self.bind()
        self.assertEqual(
            raised.exception.code, "SCHEDULER_PLAN_IDENTITY_BINDING_MISMATCH"
        )

    def test_verify_detects_xml_and_tool_tamper(self) -> None:
        generator.write_bundle(self.bind(), self.output)
        xml_path = self.output / "tasks" / "2026-07-30.xml"
        xml_path.write_bytes(xml_path.read_bytes() + b" ")
        with self.assertRaises(generator.SchedulerPlanError) as raised:
            generator.verify_existing_bundle(self.root, self.output)
        self.assertEqual(raised.exception.code, "SCHEDULER_PLAN_XML_REJECTED")

        second = self.root / "tmp" / "scheduler-plan-generator" / "second"
        generator.write_bundle(self.bind(), second)
        (self.root / generator.WORKER_REL).write_bytes(b"drifted worker")
        with self.assertRaises(generator.SchedulerPlanError) as raised:
            generator.verify_existing_bundle(self.root, second)
        self.assertEqual(raised.exception.code, "SCHEDULER_PLAN_MANIFEST_REJECTED")

    def test_output_outside_tmp_is_rejected(self) -> None:
        with self.assertRaises(generator.SchedulerPlanError) as raised:
            generator.write_bundle(self.bind(), self.root / "reports" / "plan")
        self.assertEqual(raised.exception.code, "SCHEDULER_PLAN_OUTPUT_REJECTED")

    def test_generator_has_no_execution_or_network_surface(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        for prohibited in (
            "import subprocess",
            "import requests",
            "import urllib",
            "import socket",
            "os.system(",
            "Popen(",
            "data/market",
            "data/catalog/datasets.json",
        ):
            self.assertNotIn(prohibited, source)


if __name__ == "__main__":
    unittest.main()
