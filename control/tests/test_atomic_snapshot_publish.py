from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from atomic_snapshot_publish import (  # noqa: E402
    InjectedFailure,
    PublishError,
    TEST_SENTINEL,
    TEST_SENTINEL_CONTENT,
    plan_hash,
    prepare_candidate,
    process_is_alive,
    process_start_token,
    publish_snapshot,
    recover_snapshot,
    validate_json_schema_value,
    validate_supported_schema,
)
from validate_dataset_catalog import validate_catalog  # noqa: E402


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class AtomicSnapshotPublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="qdata-d12a-")
        self.root = Path(self.temp.name).resolve()
        (self.root / TEST_SENTINEL).write_text(TEST_SENTINEL_CONTENT, encoding="utf-8")
        (self.root / "data" / "market" / "snapshots").mkdir(parents=True)
        (self.root / "data" / "market" / "README.md").write_text("test store\n", encoding="utf-8")
        (self.root / "data" / "catalog").mkdir(parents=True)
        (self.root / "data" / "catalog" / "ACCESS_POLICY.md").write_text(
            "test access policy\n", encoding="utf-8"
        )
        self.catalog = self.root / "data" / "catalog" / "datasets.json"
        write_json(
            self.catalog,
            {
                "schema_version": 1,
                "updated_at": "2026-07-21T00:00:00+08:00",
                "workspace_root": self.root.as_posix(),
                "catalog_mode": "federated_logical_catalog",
                "physical_migration": False,
                "authority_precedence": ["snapshot_manifest_or_provenance"],
                "owners": ["workspace"],
                "stores": [
                    {
                        "store_id": "workspace-market-snapshots",
                        "owner_project": "workspace",
                        "physical_root": (self.root / "data" / "market" / "snapshots").as_posix(),
                        "kind": "immutable_snapshot_store",
                        "lifecycle": "active",
                        "mutability": "append_only",
                        "purpose_ceiling": "RESEARCH_ONLY",
                        "authority_paths": [
                            (self.root / "data" / "market" / "README.md").as_posix(),
                            (self.root / "data" / "catalog" / "ACCESS_POLICY.md").as_posix(),
                        ],
                    }
                ],
                "datasets": [],
                "retired_datasets": [],
                "publication_contracts": [],
            },
        )
        self.inputs = self.root / "inputs"
        self.raw = self.inputs / "raw"
        self.curated = self.inputs / "curated"
        self.raw.mkdir(parents=True)
        self.curated.mkdir(parents=True)
        (self.raw / "response.json").write_text(
            '{"source":"synthetic","rows":2}\n', encoding="utf-8"
        )
        (self.curated / "part-000.jsonl").write_text(
            '{"security_id":"TEST.000001","trade_date":"2026-07-20","close":10.0}\n'
            '{"security_id":"TEST.000002","trade_date":"2026-07-20","close":20.0}\n',
            encoding="utf-8",
        )
        self.request = self.root / "request.json"
        self.request_value = {
            "schema_version": 1,
            "mode": "synthetic_fixture",
            "run_id": "d12a-fixture-v1",
            "dataset_id": "synthetic-d12a-daily",
            "snapshot_id": "fixture-2026-07-21-v1",
            "source_id": "SYNTHETIC_FIXTURE",
            "owner_project": "workspace",
            "store_id": "workspace-market-snapshots",
            "contract_id": "g-data-core-d3a-v1",
            "dataset_schema_version": "1.0.0",
            "ingest_date": "2026-07-21",
            "created_at": "2026-07-21T00:00:00+08:00",
            "available_at": "2026-07-21T00:00:00+08:00",
            "partition_segments": ["year=2026", "month=07"],
            "license_status": "NOT_APPLICABLE",
            "quality_status": "REVIEW",
            "purpose_ceiling": "SMOKE_ONLY",
            "quality_gate": "PASS",
            "quality_checks": ["fixture_schema", "fixture_primary_key"],
            "consumers": [],
        }
        write_json(self.request, self.request_value)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def publish(self, *, failpoint: str | None = None):
        return publish_snapshot(
            workspace_root=self.root,
            catalog_path=self.catalog,
            request_path=self.request,
            raw_dir=self.raw,
            curated_dir=self.curated,
            failpoint=failpoint,
        )

    def test_success_and_idempotent_rerun(self) -> None:
        first = self.publish()
        self.assertEqual(first.status, "PUBLISHED")
        catalog = read_json(self.catalog)
        self.assertEqual(validate_catalog(catalog), [])
        self.assertEqual(len(catalog["datasets"]), 1)
        committed_hash = digest(self.catalog)

        second = self.publish()
        self.assertEqual(second.status, "NO_CHANGE")
        self.assertEqual(digest(self.catalog), committed_hash)
        self.assertEqual(len(read_json(self.catalog)["datasets"]), 1)

    def test_identity_conflict_is_quarantined_without_catalog_change(self) -> None:
        self.publish()
        catalog_hash = digest(self.catalog)
        (self.curated / "part-000.jsonl").write_text(
            '{"security_id":"TEST.000001","trade_date":"2026-07-20","close":999.0}\n',
            encoding="utf-8",
        )
        self.request_value["run_id"] = "d12a-fixture-identity-conflict"
        write_json(self.request, self.request_value)

        result = self.publish()
        self.assertEqual(result.status, "QUARANTINED")
        self.assertEqual(digest(self.catalog), catalog_hash)
        failure = read_json(Path(result.quarantine_path) / "failure.json")
        self.assertEqual(failure["reason"], "DATASET_SNAPSHOT_IDENTITY_CONFLICT")

    def test_failed_quality_gate_is_quarantined(self) -> None:
        self.request_value["quality_gate"] = "FAIL"
        self.request_value["run_id"] = "d12a-fixture-quality-fail"
        write_json(self.request, self.request_value)
        before = digest(self.catalog)

        result = self.publish()
        self.assertEqual(result.status, "QUARANTINED")
        self.assertEqual(digest(self.catalog), before)
        self.assertEqual(read_json(self.catalog)["datasets"], [])
        self.assertFalse(
            (self.root / "data" / "market" / "snapshots" / self.request_value["dataset_id"]).exists()
        )

    def test_expected_payload_hash_mismatch_is_quarantined(self) -> None:
        self.request_value["run_id"] = "d12a-fixture-hash-fail"
        self.request_value["expected_payload_hash"] = "0" * 64
        write_json(self.request, self.request_value)
        before = digest(self.catalog)

        result = self.publish()
        self.assertEqual(result.status, "QUARANTINED")
        self.assertEqual(digest(self.catalog), before)
        failure = read_json(Path(result.quarantine_path) / "failure.json")
        self.assertEqual(failure["reason"], "EXPECTED_PAYLOAD_HASH_MISMATCH")

    def test_failure_after_snapshot_finalize_keeps_old_catalog_and_recovers(self) -> None:
        before = digest(self.catalog)
        with self.assertRaises(InjectedFailure):
            self.publish(failpoint="after_snapshot_finalize")
        self.assertEqual(digest(self.catalog), before)
        self.assertEqual(read_json(self.catalog)["datasets"], [])
        snapshot = (
            self.root
            / "data"
            / "market"
            / "snapshots"
            / self.request_value["dataset_id"]
            / self.request_value["snapshot_id"]
        )
        self.assertTrue((snapshot / "manifest.json").is_file())

        recovered = self.publish()
        self.assertEqual(recovered.status, "PUBLISHED")
        self.assertEqual(len(read_json(self.catalog)["datasets"]), 1)

    def test_failure_before_catalog_replace_keeps_old_catalog_and_recovers(self) -> None:
        before = digest(self.catalog)
        with self.assertRaises(InjectedFailure):
            self.publish(failpoint="before_catalog_replace")
        self.assertEqual(digest(self.catalog), before)
        self.assertEqual(read_json(self.catalog)["datasets"], [])

        recovered = self.publish()
        self.assertEqual(recovered.status, "PUBLISHED")
        self.assertEqual(len(read_json(self.catalog)["datasets"]), 1)

    def test_failure_after_catalog_replace_is_visible_and_reruns_no_change(self) -> None:
        before = digest(self.catalog)
        with self.assertRaises(InjectedFailure):
            self.publish(failpoint="after_catalog_replace")
        self.assertNotEqual(digest(self.catalog), before)
        self.assertEqual(len(read_json(self.catalog)["datasets"]), 1)

        recovered = self.publish()
        self.assertEqual(recovered.status, "NO_CHANGE")
        self.assertEqual(len(read_json(self.catalog)["datasets"]), 1)

    def test_new_snapshot_does_not_hide_old_snapshot(self) -> None:
        first = self.publish()
        self.request_value["run_id"] = "d12a-fixture-v2"
        self.request_value["snapshot_id"] = "fixture-2026-07-21-v2"
        self.request_value["created_at"] = "2026-07-21T00:01:00+08:00"
        write_json(self.request, self.request_value)

        second = self.publish()
        self.assertEqual(first.status, "PUBLISHED")
        self.assertEqual(second.status, "PUBLISHED")
        identities = {
            (item["dataset_id"], item["snapshot_id"])
            for item in read_json(self.catalog)["datasets"]
        }
        self.assertEqual(
            identities,
            {
                ("synthetic-d12a-daily", "fixture-2026-07-21-v1"),
                ("synthetic-d12a-daily", "fixture-2026-07-21-v2"),
            },
        )

    def test_missing_isolation_sentinel_rejects_without_write(self) -> None:
        (self.root / TEST_SENTINEL).unlink()
        before = digest(self.catalog)
        with self.assertRaises(PublishError):
            self.publish()
        self.assertEqual(digest(self.catalog), before)

    def test_catalog_symlink_outside_workspace_is_rejected_without_external_write(self) -> None:
        catalog_payload = self.catalog.read_bytes()
        with tempfile.TemporaryDirectory(prefix="qdata-catalog-outside-") as outside_dir:
            external_catalog = Path(outside_dir) / "datasets.json"
            external_catalog.write_bytes(catalog_payload)
            self.catalog.unlink()
            try:
                self.catalog.symlink_to(external_catalog)
            except OSError as exc:
                self.catalog.write_bytes(catalog_payload)
                self.skipTest(f"file symlink is unavailable: {exc}")
            before = digest(external_catalog)

            with self.assertRaises(PublishError):
                self.publish()

            self.assertEqual(digest(external_catalog), before)

    def test_catalog_reparse_guard_rejects_before_write(self) -> None:
        before = digest(self.catalog)

        def fake_reparse(path: Path) -> bool:
            return Path(path) == self.catalog

        with mock.patch(
            "atomic_snapshot_publish.is_reparse_point",
            side_effect=fake_reparse,
        ):
            with self.assertRaises(PublishError):
                self.publish()

        self.assertEqual(digest(self.catalog), before)

    def test_existing_catalog_lock_rejects_concurrent_writer(self) -> None:
        lock = self.catalog.with_name(f".{self.catalog.name}.lock")
        lock.write_text("occupied\n", encoding="utf-8")
        before = digest(self.catalog)
        with self.assertRaises(PublishError):
            self.publish()
        self.assertEqual(digest(self.catalog), before)

    def test_request_schema_rejects_every_supported_violation_class(self) -> None:
        def remove_consumers(value: dict) -> None:
            value.pop("consumers")

        def add_extra_property(value: dict) -> None:
            value["unexpected"] = True

        cases = [
            ("missing_required", remove_consumers),
            ("additional_property", add_extra_property),
            ("boolean_is_not_integer_const", lambda value: value.__setitem__("schema_version", True)),
            ("wrong_const", lambda value: value.__setitem__("mode", "real_source")),
            ("component_pattern", lambda value: value.__setitem__("run_id", "../escape")),
            ("invalid_date", lambda value: value.__setitem__("ingest_date", "2026-02-30")),
            (
                "datetime_requires_timezone",
                lambda value: value.__setitem__("created_at", "2026-07-21T00:00:00"),
            ),
            (
                "date_requires_rfc3339_lexical_form",
                lambda value: value.__setitem__("ingest_date", "20260721"),
            ),
            (
                "datetime_rejects_week_date",
                lambda value: value.__setitem__(
                    "created_at", "2026-W30-2T00:00:00+08:00"
                ),
            ),
            (
                "datetime_rejects_space_separator",
                lambda value: value.__setitem__(
                    "available_at", "2026-07-21 00:00:00+08:00"
                ),
            ),
            ("empty_partition_array", lambda value: value.__setitem__("partition_segments", [])),
            (
                "duplicate_partition",
                lambda value: value.__setitem__(
                    "partition_segments", ["year=2026", "year=2026"]
                ),
            ),
            (
                "partition_item_type",
                lambda value: value.__setitem__("partition_segments", ["year=2026", 7]),
            ),
            (
                "partition_windows_trailing_dot",
                lambda value: value.__setitem__("partition_segments", ["year=2026."]),
            ),
            (
                "partition_windows_reserved_name",
                lambda value: value.__setitem__("partition_segments", ["CON"]),
            ),
            (
                "license_const",
                lambda value: value.__setitem__("license_status", "UNKNOWN"),
            ),
            (
                "quality_status_const",
                lambda value: value.__setitem__("quality_status", "OK"),
            ),
            (
                "purpose_const",
                lambda value: value.__setitem__("purpose_ceiling", "RESEARCH_ONLY"),
            ),
            (
                "quality_gate_enum",
                lambda value: value.__setitem__("quality_gate", "UNKNOWN"),
            ),
            (
                "duplicate_quality_checks",
                lambda value: value.__setitem__(
                    "quality_checks", ["fixture_schema", "fixture_schema"]
                ),
            ),
            (
                "quality_check_item_type",
                lambda value: value.__setitem__("quality_checks", ["fixture_schema", 1]),
            ),
            (
                "duplicate_consumers",
                lambda value: value.__setitem__("consumers", ["consumer-a", "consumer-a"]),
            ),
            (
                "consumer_item_type",
                lambda value: value.__setitem__("consumers", ["consumer-a", 1]),
            ),
            (
                "payload_hash_pattern",
                lambda value: value.__setitem__("expected_payload_hash", "A" * 64),
            ),
        ]
        original = copy.deepcopy(self.request_value)
        before = digest(self.catalog)
        for name, mutate in cases:
            with self.subTest(name=name):
                candidate = copy.deepcopy(original)
                mutate(candidate)
                write_json(self.request, candidate)
                with self.assertRaises(PublishError):
                    self.publish()
                self.assertEqual(digest(self.catalog), before)
                self.assertEqual(read_json(self.catalog)["datasets"], [])
                self.assertFalse((self.root / "data" / "market" / ".staging").exists())
                self.assertFalse((self.root / "data" / "market" / "raw").exists())
                self.assertFalse((self.root / "data" / "market" / "curated").exists())

    def test_optional_quality_checks_can_be_omitted(self) -> None:
        self.request_value.pop("quality_checks")
        write_json(self.request, self.request_value)
        result = self.publish()
        self.assertEqual(result.status, "PUBLISHED")

    def test_schema_subset_rejects_unknown_keywords_and_executes_ref_siblings(self) -> None:
        with self.assertRaises(PublishError):
            validate_supported_schema(
                {"type": "string", "maxLength": 3},
                "test.schema",
            )
        schema = {
            "$defs": {"text": {"type": "string"}},
            "$ref": "#/$defs/text",
            "pattern": "^allowed$",
        }
        validate_supported_schema(schema, "test.schema")
        with self.assertRaises(PublishError):
            validate_json_schema_value("blocked", schema, schema, "value")

    def run_hard_crash_recovery(
        self,
        failpoint: str,
        expected_exit: int,
        expected_recovery_status: str,
    ) -> None:
        publisher = TOOLS / "atomic_snapshot_publish.py"
        before = digest(self.catalog)
        crashed = subprocess.run(
            [
                sys.executable,
                str(publisher),
                "publish",
                "--workspace-root",
                str(self.root),
                "--catalog",
                str(self.catalog),
                "--request",
                str(self.request),
                "--raw-dir",
                str(self.raw),
                "--curated-dir",
                str(self.curated),
                "--failpoint",
                failpoint,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(crashed.returncode, expected_exit, crashed.stderr)
        if failpoint == "hard_crash_after_snapshot_finalize":
            self.assertEqual(digest(self.catalog), before)
        else:
            self.assertNotEqual(digest(self.catalog), before)
        lock = self.catalog.with_name(f".{self.catalog.name}.lock")
        self.assertTrue(lock.is_file())
        lock_value = read_json(lock)
        self.assertFalse(
            process_is_alive(
                lock_value["pid"],
                lock_value.get("process_start_token"),
            )
        )
        with self.assertRaises(PublishError):
            recover_snapshot(
                workspace_root=self.root,
                catalog_path=self.catalog,
                run_id=self.request_value["run_id"],
                confirm_lock_sha256="0" * 64,
            )
        self.assertTrue(lock.is_file())
        recovered = recover_snapshot(
            workspace_root=self.root,
            catalog_path=self.catalog,
            run_id=self.request_value["run_id"],
            confirm_lock_sha256=digest(lock),
        )
        self.assertEqual(recovered.status, expected_recovery_status)
        self.assertFalse(lock.exists())
        archived_locks = list(
            (self.root / "data" / "market" / "transactions" / "stale-locks").glob("*.json")
        )
        self.assertEqual(len(archived_locks), 1)
        self.assertEqual(len(read_json(self.catalog)["datasets"]), 1)
        self.assertNotEqual(digest(self.catalog), before)

    def test_hard_crash_after_snapshot_finalize_recovers_in_new_process(self) -> None:
        self.run_hard_crash_recovery(
            "hard_crash_after_snapshot_finalize",
            91,
            "PUBLISHED",
        )

    def test_hard_crash_after_catalog_replace_recovers_in_new_process(self) -> None:
        self.run_hard_crash_recovery(
            "hard_crash_after_catalog_replace",
            92,
            "NO_CHANGE",
        )

    def test_current_process_identity_is_not_treated_as_stale(self) -> None:
        token = process_start_token(os.getpid())
        if os.name == "nt":
            self.assertIsNotNone(token)
        self.assertTrue(process_is_alive(os.getpid(), token))

    def test_process_identity_reuse_and_unknown_state_fail_closed(self) -> None:
        with mock.patch(
            "atomic_snapshot_publish.inspect_process",
            return_value=("alive", "new-token"),
        ):
            self.assertFalse(process_is_alive(123, "old-token"))
        with mock.patch(
            "atomic_snapshot_publish.inspect_process",
            return_value=("unknown", None),
        ):
            self.assertTrue(process_is_alive(123, "old-token"))

    def test_stale_lock_cannot_recover_a_different_request_with_same_run_id(self) -> None:
        publisher = TOOLS / "atomic_snapshot_publish.py"
        original_request = copy.deepcopy(self.request_value)
        crashed = subprocess.run(
            [
                sys.executable,
                str(publisher),
                "publish",
                "--workspace-root",
                str(self.root),
                "--catalog",
                str(self.catalog),
                "--request",
                str(self.request),
                "--raw-dir",
                str(self.raw),
                "--curated-dir",
                str(self.curated),
                "--failpoint",
                "hard_crash_after_snapshot_finalize",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(crashed.returncode, 91, crashed.stderr)
        lock = self.catalog.with_name(f".{self.catalog.name}.lock")
        lock_hash = digest(lock)
        transaction = read_json(
            self.root
            / "data"
            / "market"
            / "transactions"
            / f"{original_request['run_id']}.json"
        )

        self.request_value["dataset_id"] = "synthetic-other-dataset"
        self.request_value["snapshot_id"] = "fixture-other-v1"
        write_json(self.request, self.request_value)
        with self.assertRaises(PublishError):
            self.publish()
        with self.assertRaises(PublishError):
            publish_snapshot(
                workspace_root=self.root,
                catalog_path=self.catalog,
                request_path=self.request,
                raw_dir=self.raw,
                curated_dir=self.curated,
                recover_stale_lock_sha256=lock_hash,
            )
        self.assertEqual(
            read_json(
                self.root
                / "data"
                / "market"
                / "transactions"
                / f"{original_request['run_id']}.json"
            ),
            transaction,
        )
        self.assertTrue(lock.is_file())

        self.request_value = original_request
        write_json(self.request, self.request_value)
        recovered = recover_snapshot(
            workspace_root=self.root,
            catalog_path=self.catalog,
            run_id=original_request["run_id"],
            confirm_lock_sha256=lock_hash,
        )
        self.assertEqual(recovered.dataset_id, original_request["dataset_id"])
        self.assertEqual(
            [item["dataset_id"] for item in read_json(self.catalog)["datasets"]],
            [original_request["dataset_id"]],
        )

    def test_recovery_rejects_rehashed_plan_paths_outside_workspace_before_move(self) -> None:
        prepared = prepare_candidate(
            workspace_root=self.root,
            request=copy.deepcopy(self.request_value),
            raw_dir=self.raw,
            curated_dir=self.curated,
        )
        stage = Path(prepared["stage_path"].replace("/", os.sep))
        plan_path = stage / "prepared-plan.json"
        transaction_path_value = (
            self.root
            / "data"
            / "market"
            / "transactions"
            / f"{self.request_value['run_id']}.json"
        )
        with tempfile.TemporaryDirectory(prefix="qdata-d12a-outside-") as outside_dir:
            outside = Path(outside_dir)
            escaped_raw = outside / "escaped-raw"
            escaped_curated = outside / "escaped-curated"
            tampered = read_json(plan_path)
            tampered["raw_final"] = escaped_raw.as_posix()
            tampered["curated_final"] = escaped_curated.as_posix()
            tampered["plan_hash"] = plan_hash(tampered)
            write_json(plan_path, tampered)
            transaction = read_json(transaction_path_value)
            transaction["prepared_plan_hash"] = tampered["plan_hash"]
            write_json(transaction_path_value, transaction)

            with self.assertRaises(PublishError):
                recover_snapshot(
                    workspace_root=self.root,
                    catalog_path=self.catalog,
                    run_id=self.request_value["run_id"],
                    confirm_lock_sha256=None,
                )

            self.assertFalse(escaped_raw.exists())
            self.assertFalse(escaped_curated.exists())
            self.assertTrue((stage / "raw").is_dir())
            self.assertTrue((stage / "curated").is_dir())
            self.assertEqual(read_json(self.catalog)["datasets"], [])

    def test_missing_stage_owner_blocks_recovery_before_any_commit_write(self) -> None:
        prepared = prepare_candidate(
            workspace_root=self.root,
            request=copy.deepcopy(self.request_value),
            raw_dir=self.raw,
            curated_dir=self.curated,
        )
        stage = Path(prepared["stage_path"].replace("/", os.sep))
        (stage / "owner.json").unlink()
        before = digest(self.catalog)

        with self.assertRaises(PublishError):
            recover_snapshot(
                workspace_root=self.root,
                catalog_path=self.catalog,
                run_id=self.request_value["run_id"],
                confirm_lock_sha256=None,
            )

        self.assertEqual(digest(self.catalog), before)
        self.assertEqual(read_json(self.catalog)["datasets"], [])
        self.assertTrue((stage / "raw").is_dir())
        self.assertTrue((stage / "curated").is_dir())
        self.assertFalse(
            Path(prepared["raw_final"].replace("/", os.sep)).exists()
        )
        self.assertFalse(
            Path(prepared["curated_final"].replace("/", os.sep)).exists()
        )

    def test_cleanup_failure_reports_visible_publish_as_cleanup_pending(self) -> None:
        with mock.patch(
            "atomic_snapshot_publish.safe_remove_owned_stage",
            side_effect=OSError("simulated cleanup failure"),
        ):
            result = self.publish()

        self.assertEqual(result.status, "PUBLISHED_CLEANUP_PENDING")
        self.assertEqual(len(read_json(self.catalog)["datasets"]), 1)
        transaction = read_json(
            self.root
            / "data"
            / "market"
            / "transactions"
            / f"{self.request_value['run_id']}.json"
        )
        self.assertEqual(transaction["state"], "PUBLISHED_CLEANUP_PENDING")
        self.assertIn("simulated cleanup failure", transaction["cleanup_error"])
        self.assertTrue(
            (
                self.root
                / "data"
                / "market"
                / ".staging"
                / self.request_value["dataset_id"]
                / self.request_value["run_id"]
            ).is_dir()
        )
        recovered = recover_snapshot(
            workspace_root=self.root,
            catalog_path=self.catalog,
            run_id=self.request_value["run_id"],
            confirm_lock_sha256=None,
        )
        self.assertEqual(recovered.status, "NO_CHANGE")
        self.assertFalse(
            (
                self.root
                / "data"
                / "market"
                / ".staging"
                / self.request_value["dataset_id"]
                / self.request_value["run_id"]
            ).exists()
        )

    def test_two_concurrent_recoveries_cannot_both_acquire_writer_lock(self) -> None:
        publisher = TOOLS / "atomic_snapshot_publish.py"
        crashed = subprocess.run(
            [
                sys.executable,
                str(publisher),
                "publish",
                "--workspace-root",
                str(self.root),
                "--catalog",
                str(self.catalog),
                "--request",
                str(self.request),
                "--raw-dir",
                str(self.raw),
                "--curated-dir",
                str(self.curated),
                "--failpoint",
                "hard_crash_after_snapshot_finalize",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(crashed.returncode, 91, crashed.stderr)
        lock = self.catalog.with_name(f".{self.catalog.name}.lock")
        lock_hash = digest(lock)
        command = [
            sys.executable,
            str(publisher),
            "recover",
            "--workspace-root",
            str(self.root),
            "--catalog",
            str(self.catalog),
            "--run-id",
            self.request_value["run_id"],
            "--confirm-lock-sha256",
            lock_hash,
        ]
        first = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        second = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        first_stdout, first_stderr = first.communicate(timeout=30)
        second_stdout, second_stderr = second.communicate(timeout=30)
        self.assertEqual(
            sorted([first.returncode, second.returncode]),
            [0, 2],
            (first_stdout, first_stderr, second_stdout, second_stderr),
        )
        self.assertFalse(lock.exists())
        self.assertEqual(len(read_json(self.catalog)["datasets"]), 1)
        archived = list(
            (self.root / "data" / "market" / "transactions" / "stale-locks").glob("*.json")
        )
        self.assertEqual(len(archived), 1)


if __name__ == "__main__":
    unittest.main()
