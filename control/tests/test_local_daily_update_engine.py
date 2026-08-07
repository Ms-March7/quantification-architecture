from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "control" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from run_daily_update_local_smoke import (  # noqa: E402
    ENGINE_ID,
    InjectedFailure,
    load_base_rows,
    read_json,
    run,
    strict_request,
    verify_candidate,
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LocalDailyUpdateEngineTests(unittest.TestCase):
    """Inspection-only local incremental engine; formal catalog stays read-only."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="qdata-local-daily-update-")
        self.root = Path(self.temp.name).resolve()
        self.catalog = self.root / "data" / "catalog" / "datasets.json"
        write_json(
            self.catalog,
            {
                "schema_version": 1,
                "datasets": [
                    {
                        "dataset_id": "registered-inspection-fixture",
                        "purpose_ceiling": "INSPECTION_ONLY",
                    }
                ],
            },
        )
        self.inbox = self.root / "incoming" / "day-1"
        self.inbox.mkdir(parents=True)
        self.request_path = self.root / "requests" / "request.json"
        self.output_root = self.root / "tmp" / "local-daily-update"
        self.table = {
            "table_name": "daily_prices",
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
        }
        self._write_csv(
            self.inbox / "delta.csv",
            self.table["expected_columns"],
            [["XSHG.600000", "2026-07-30", "10.5"]],
        )
        self.request = self._request(run_id="r1")
        write_json(self.request_path, self.request)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _request(
        self,
        *,
        run_id: str,
        dataset_id: str = "synthetic-daily-update",
        dataset_scope: str = "synthetic_fixture",
        source_id: str = "SYNTHETIC_FIXTURE",
        base_candidate: str | None = None,
        table: dict | None = None,
    ) -> dict:
        return {
            "schema_version": 1,
            "mode": "local_daily_update_candidate",
            "dataset_scope": dataset_scope,
            "dataset_id": dataset_id,
            "run_id": run_id,
            "source_id": source_id,
            "dataset_schema_version": "1.0.0",
            "authorization_note": "isolated unit-test fixture",
            "include_patterns": ["**/*.csv"],
            "base_candidate": base_candidate,
            "table": self.table if table is None else table,
            "limits": {
                "max_files": 10,
                "max_total_input_bytes": 1_000_000,
                "max_rows": 10_000,
            },
        }

    @staticmethod
    def _write_csv(
        path: Path,
        columns: list[str],
        rows: list[list[object]],
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            writer.writerows(rows)

    def _run(
        self,
        *,
        execute: bool,
        inbox: Path | None = None,
        failpoint: str | None = None,
    ):
        return run(
            workspace_root=self.root,
            request_path=self.request_path,
            inbox=self.inbox if inbox is None else inbox,
            output_root=self.output_root,
            execute=execute,
            failpoint=failpoint,
        )

    def test_plan_is_write_free_and_registered_identity_is_read_only(self) -> None:
        before = sha256(self.catalog)
        plan = self._run(execute=False)
        self.assertEqual(plan.status, "PLAN")
        self.assertEqual(plan.row_count, 1)
        self.assertFalse(self.output_root.exists())
        self.assertEqual(sha256(self.catalog), before)

        self.request = self._request(
            run_id="registered-r1",
            dataset_id="registered-inspection-fixture",
            dataset_scope="registered_inspection",
            source_id="LOCAL_REGISTERED_FIXTURE",
        )
        write_json(self.request_path, self.request)
        result = self._run(execute=True)
        self.assertEqual(result.status, "CANDIDATE_PUBLISHED")
        self.assertEqual(sha256(self.catalog), before)
        manifest = verify_candidate(Path(result.candidate_path))
        self.assertFalse(manifest["catalog_registered"])
        self.assertFalse(manifest["source_admission"])
        self.assertEqual(manifest["purpose_ceiling"], "INSPECTION_ONLY")

    def test_publish_idempotent_merge_and_base_immutability(self) -> None:
        catalog_before = sha256(self.catalog)
        first = self._run(execute=True)
        self.assertEqual(first.status, "CANDIDATE_PUBLISHED")
        self.assertEqual(first.row_count, 1)
        self.assertEqual(first.partition_count, 1)
        first_root = Path(first.candidate_path)
        first_manifest_hash = sha256(first_root / "manifest.json")

        duplicate = self._run(execute=True)
        self.assertEqual(duplicate.status, "NO_CHANGE")
        self.assertEqual(duplicate.request_fingerprint, first.request_fingerprint)
        self.assertEqual(sha256(first_root / "manifest.json"), first_manifest_hash)

        day_2 = self.root / "incoming" / "day-2"
        self._write_csv(
            day_2 / "delta.csv",
            self.table["expected_columns"],
            [["XSHG.600000", "2026-08-03", "10.8"]],
        )
        self.request = self._request(
            run_id="r2",
            base_candidate=first_root.relative_to(self.root).as_posix(),
        )
        write_json(self.request_path, self.request)
        second = self._run(execute=True, inbox=day_2)
        self.assertEqual(second.status, "CANDIDATE_PUBLISHED")
        self.assertEqual(second.row_count, 2)
        self.assertEqual(second.new_row_count, 1)
        self.assertEqual(second.partition_count, 2)
        self.assertEqual(second.new_partition_count, 1)
        self.assertEqual(sha256(first_root / "manifest.json"), first_manifest_hash)
        self.assertEqual(sha256(self.catalog), catalog_before)

        rows = load_base_rows(
            base=Path(second.candidate_path),
            table=self.table,
            max_rows=10_000,
        )
        self.assertEqual(
            [row["trade_date"].isoformat() for row in rows],
            ["2026-07-30", "2026-08-03"],
        )

    def test_conflicting_key_is_quarantined_with_rollback_receipt(self) -> None:
        catalog_before = sha256(self.catalog)
        first = self._run(execute=True)
        first_root = Path(first.candidate_path)
        first_manifest_hash = sha256(first_root / "manifest.json")

        conflict_inbox = self.root / "incoming" / "conflict"
        self._write_csv(
            conflict_inbox / "delta.csv",
            self.table["expected_columns"],
            [["XSHG.600000", "2026-07-30", "999.0"]],
        )
        self.request = self._request(
            run_id="conflict-r2",
            base_candidate=first_root.relative_to(self.root).as_posix(),
        )
        write_json(self.request_path, self.request)
        conflict = self._run(execute=True, inbox=conflict_inbox)
        self.assertEqual(conflict.status, "QUARANTINED")
        self.assertIsNone(conflict.candidate_path)
        failure = read_json(Path(conflict.quarantine_path) / "failure.json")
        self.assertEqual(failure["reason"], "PRIMARY_KEY_CONTENT_CONFLICT")
        self.assertTrue(failure["rollback"]["staging_removed"])
        self.assertFalse(failure["rollback"]["candidate_published"])
        self.assertTrue(failure["formal_catalog_guard"]["unchanged"])
        self.assertEqual(sha256(first_root / "manifest.json"), first_manifest_hash)
        self.assertEqual(sha256(self.catalog), catalog_before)

    def test_schema_and_date_drift_fail_closed_with_receipt(self) -> None:
        catalog_before = sha256(self.catalog)
        drifted = self.root / "incoming" / "schema-drift"
        self._write_csv(
            drifted / "delta.csv",
            ["security_id", "trade_date", "close", "unexpected"],
            [["XSHG.600000", "2026-07-30", "10.5", "x"]],
        )
        schema_result = self._run(execute=True, inbox=drifted)
        self.assertEqual(schema_result.status, "QUARANTINED")
        schema_failure = read_json(
            Path(schema_result.quarantine_path) / "failure.json"
        )
        self.assertEqual(schema_failure["reason"], "BUSINESS_SCHEMA_DRIFT")

        invalid_date = self.root / "incoming" / "invalid-date"
        self._write_csv(
            invalid_date / "delta.csv",
            self.table["expected_columns"],
            [["XSHG.600000", "2026-02-30", "10.5"]],
        )
        self.request = self._request(run_id="invalid-date-r1")
        write_json(self.request_path, self.request)
        date_result = self._run(execute=True, inbox=invalid_date)
        self.assertEqual(date_result.status, "QUARANTINED")
        date_failure = read_json(
            Path(date_result.quarantine_path) / "failure.json"
        )
        self.assertEqual(date_failure["reason"], "LOCAL_UPDATE_REJECTED")
        self.assertIn("valid ISO date", date_failure["message"])
        self.assertEqual(sha256(self.catalog), catalog_before)

    def test_tampered_base_is_rejected_before_merge_with_receipt(self) -> None:
        first = self._run(execute=True)
        first_root = Path(first.candidate_path)
        raw_file = next((first_root / "raw").rglob("*.csv"))
        raw_file.write_text("tampered\n", encoding="utf-8")
        day_2 = self.root / "incoming" / "tampered-base-day-2"
        self._write_csv(
            day_2 / "delta.csv",
            self.table["expected_columns"],
            [["XSHG.600000", "2026-08-03", "10.8"]],
        )
        self.request = self._request(
            run_id="tampered-base-r2",
            base_candidate=first_root.relative_to(self.root).as_posix(),
        )
        write_json(self.request_path, self.request)
        result = self._run(execute=True, inbox=day_2)
        self.assertEqual(result.status, "QUARANTINED")
        failure = read_json(Path(result.quarantine_path) / "failure.json")
        self.assertEqual(failure["reason"], "LOCAL_UPDATE_REJECTED")
        self.assertIn("payload verification failed", failure["message"])
        self.assertFalse(
            (
                self.output_root
                / "synthetic-daily-update"
                / "tampered-base-r2"
            ).exists()
        )

    def test_unlisted_base_parquet_is_rejected_before_merge(self) -> None:
        first = self._run(execute=True)
        first_root = Path(first.candidate_path)
        listed_parquet = next((first_root / "curated").rglob("*.parquet"))
        extra_parquet = first_root / "curated" / "extra.parquet"
        shutil.copy2(listed_parquet, extra_parquet)

        day_2 = self.root / "incoming" / "extra-payload-day-2"
        self._write_csv(
            day_2 / "delta.csv",
            self.table["expected_columns"],
            [["XSHG.600000", "2026-08-03", "10.8"]],
        )
        self.request = self._request(
            run_id="extra-payload-r2",
            base_candidate=first_root.relative_to(self.root).as_posix(),
        )
        write_json(self.request_path, self.request)

        result = self._run(execute=True, inbox=day_2)
        self.assertEqual(result.status, "QUARANTINED")
        self.assertIsNone(result.candidate_path)
        failure = read_json(Path(result.quarantine_path) / "failure.json")
        self.assertEqual(failure["reason"], "LOCAL_UPDATE_REJECTED")
        self.assertIn("candidate file set differs", failure["message"])
        self.assertIn("curated/extra.parquet", failure["message"])
        self.assertFalse(
            (
                self.output_root
                / "synthetic-daily-update"
                / "extra-payload-r2"
            ).exists()
        )

    def test_revision_append_only_and_stale_revision_quarantine(self) -> None:
        revision_table = {
            "table_name": "pit_facts",
            "expected_columns": [
                "security_id",
                "report_date",
                "revision",
                "available_at",
                "value",
            ],
            "column_types": {
                "security_id": "VARCHAR",
                "report_date": "DATE",
                "revision": "BIGINT",
                "available_at": "TIMESTAMP_TZ",
                "value": "DOUBLE",
            },
            "date_column": "report_date",
            "primary_key_columns": [
                "security_id",
                "report_date",
                "revision",
            ],
            "required_columns": [
                "security_id",
                "report_date",
                "revision",
                "available_at",
                "value",
            ],
            "delimiter": ",",
            "revision": {
                "business_key_columns": ["security_id", "report_date"],
                "revision_column": "revision",
                "available_at_column": "available_at",
                "policy": "APPEND_ONLY_STRICT",
            },
        }
        self._write_csv(
            self.inbox / "delta.csv",
            revision_table["expected_columns"],
            [[
                "XSHG.600000",
                "2026-06-30",
                "1",
                "2026-07-30T08:00:00+08:00",
                "100.0",
            ]],
        )
        self.request = self._request(run_id="pit-r1", table=revision_table)
        write_json(self.request_path, self.request)
        first = self._run(execute=True)

        revision_2 = self.root / "incoming" / "revision-2"
        self._write_csv(
            revision_2 / "delta.csv",
            revision_table["expected_columns"],
            [[
                "XSHG.600000",
                "2026-06-30",
                "2",
                "2026-07-30T09:00:00+08:00",
                "101.0",
            ]],
        )
        self.request = self._request(
            run_id="pit-r2",
            base_candidate=Path(first.candidate_path).relative_to(self.root).as_posix(),
            table=revision_table,
        )
        write_json(self.request_path, self.request)
        second = self._run(execute=True, inbox=revision_2)
        self.assertEqual(second.status, "CANDIDATE_PUBLISHED")
        self.assertEqual(second.row_count, 2)

        stale = self.root / "incoming" / "revision-stale"
        self._write_csv(
            stale / "delta.csv",
            revision_table["expected_columns"],
            [[
                "XSHG.600000",
                "2026-06-30",
                "0",
                "2026-07-30T10:00:00+08:00",
                "102.0",
            ]],
        )
        self.request = self._request(
            run_id="pit-stale",
            base_candidate=Path(second.candidate_path).relative_to(self.root).as_posix(),
            table=revision_table,
        )
        write_json(self.request_path, self.request)
        rejected = self._run(execute=True, inbox=stale)
        self.assertEqual(rejected.status, "QUARANTINED")
        failure = read_json(Path(rejected.quarantine_path) / "failure.json")
        self.assertEqual(failure["reason"], "REVISION_NOT_APPEND_ONLY")

    def test_failpoint_removes_partial_and_preserves_catalog(self) -> None:
        catalog_before = sha256(self.catalog)
        with self.assertRaises(InjectedFailure):
            self._run(execute=True, failpoint="before_seal")
        final_root = self.output_root / "synthetic-daily-update" / "r1"
        self.assertFalse(final_root.exists())
        partials = list(
            (self.output_root / "synthetic-daily-update").glob(".*.partial-*")
        )
        self.assertEqual(partials, [])
        failures = list(
            (
                self.output_root
                / "quarantine"
                / "synthetic-daily-update"
            ).glob("*/failure.json")
        )
        self.assertEqual(len(failures), 1)
        failure = read_json(failures[0])
        self.assertEqual(failure["reason"], "INJECTED_BEFORE_SEAL")
        self.assertTrue(failure["formal_catalog_guard"]["unchanged"])
        self.assertEqual(sha256(self.catalog), catalog_before)

    def test_engine_identity_is_explicitly_candidate_only(self) -> None:
        self.assertIn("candidate", ENGINE_ID)
        result = self._run(execute=True)
        payload = result.as_dict()
        self.assertEqual(payload["purpose_ceiling"], "INSPECTION_ONLY")
        self.assertFalse(payload["source_admission"])
        self.assertFalse(payload["catalog_registered"])
        self.assertFalse(payload["quality_promotion"])
        self.assertFalse(payload["research_ready"])

    def test_shipped_example_matches_strict_runtime_contract(self) -> None:
        contract_root = (
            ROOT
            / "data"
            / "catalog"
            / "contracts"
            / "local_daily_update_v1"
        )
        example = strict_request(contract_root / "example.request.json")
        schema = read_json(contract_root / "request.schema.json")
        self.assertEqual(example["mode"], "local_daily_update_candidate")
        self.assertEqual(example["dataset_scope"], "synthetic_fixture")
        self.assertEqual(
            schema["$schema"],
            "https://json-schema.org/draft/2020-12/schema",
        )
        self.assertFalse(schema["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
