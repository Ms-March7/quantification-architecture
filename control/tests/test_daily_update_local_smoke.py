from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "control" / "tools"
CATALOG_LIB = ROOT / "data" / "catalog"
for import_root in (TOOLS, CATALOG_LIB):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from adapter_gate import canonical_row_sha256, validate_adapter_sample  # noqa: E402
from atomic_snapshot_publish import (  # noqa: E402
    InjectedFailure,
    PublishError,
    TEST_SENTINEL,
    TEST_SENTINEL_CONTENT,
    publish_snapshot,
)
from validate_dataset_catalog import validate_catalog  # noqa: E402


CONTRACT_PATH = (
    CATALOG_LIB
    / "contracts"
    / "adapter_gate_v1"
    / "tencent_daily_inspection.json"
)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DailyUpdateLocalSmokeTests(unittest.TestCase):
    """Synthetic-only daily update semantics; never touches the formal catalog."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="qdata-daily-update-smoke-")
        self.root = Path(self.temp.name).resolve()
        (self.root / TEST_SENTINEL).write_text(
            TEST_SENTINEL_CONTENT,
            encoding="utf-8",
        )
        (self.root / "data" / "market" / "snapshots").mkdir(parents=True)
        (self.root / "data" / "market" / "README.md").write_text(
            "isolated daily update smoke store\n",
            encoding="utf-8",
        )
        (self.root / "data" / "catalog").mkdir(parents=True)
        (self.root / "data" / "catalog" / "ACCESS_POLICY.md").write_text(
            "isolated daily update smoke access policy\n",
            encoding="utf-8",
        )
        self.catalog = self.root / "data" / "catalog" / "datasets.json"
        write_json(
            self.catalog,
            {
                "schema_version": 1,
                "updated_at": "2026-07-30T00:00:00+08:00",
                "workspace_root": self.root.as_posix(),
                "catalog_mode": "federated_logical_catalog",
                "physical_migration": False,
                "authority_precedence": ["snapshot_manifest_or_provenance"],
                "owners": ["workspace"],
                "stores": [
                    {
                        "store_id": "workspace-market-snapshots",
                        "owner_project": "workspace",
                        "physical_root": (
                            self.root / "data" / "market" / "snapshots"
                        ).as_posix(),
                        "kind": "immutable_snapshot_store",
                        "lifecycle": "active",
                        "mutability": "append_only",
                        "purpose_ceiling": "RESEARCH_ONLY",
                        "authority_paths": [
                            (
                                self.root / "data" / "market" / "README.md"
                            ).as_posix(),
                            (
                                self.root
                                / "data"
                                / "catalog"
                                / "ACCESS_POLICY.md"
                            ).as_posix(),
                        ],
                    }
                ],
                "datasets": [],
                "retired_datasets": [],
                "publication_contracts": [],
            },
        )
        self.raw = self.root / "inputs" / "raw"
        self.curated = self.root / "inputs" / "curated"
        self.raw.mkdir(parents=True)
        self.curated.mkdir(parents=True)
        self.request_path = self.root / "request.json"
        self.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.sample = self._valid_adapter_sample()
        self._write_payload(trade_date="2026-07-29", close=10.5)
        self.request = self._request(
            run_id="daily-smoke-v1",
            snapshot_id="2026-07-29-v1",
            ingest_date="2026-07-30",
            created_at="2026-07-30T00:10:00+08:00",
            available_at="2026-07-30T00:05:00+08:00",
        )
        write_json(self.request_path, self.request)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _valid_adapter_sample(self) -> dict:
        sample = {
            "schema_version": 1,
            "contract_id": self.contract["contract_id"],
            "declared_units": copy.deepcopy(self.contract["expected_units"]),
            "raw_rows": [
                [
                    "20260729",
                    "10",
                    "11",
                    "9",
                    "10.5",
                    "100",
                    "1",
                    "1050",
                    "x",
                    "y",
                    "z",
                ]
            ],
            "normalized_rows": [
                {
                    "security_id": "XSHG.600000",
                    "trade_date": "2026-07-29",
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.5,
                    "volume": 100,
                    "turnover": 1.0,
                    "amount": 1050.0,
                }
            ],
        }
        sample["row_lineage"] = [
            {
                "raw_index": 0,
                "normalized_index": 0,
                "raw_row_sha256": canonical_row_sha256(sample["raw_rows"][0]),
            }
        ]
        return sample

    def _request(
        self,
        *,
        run_id: str,
        snapshot_id: str,
        ingest_date: str,
        created_at: str,
        available_at: str,
    ) -> dict:
        return {
            "schema_version": 1,
            "mode": "synthetic_fixture",
            "run_id": run_id,
            "dataset_id": "synthetic-daily-update-smoke",
            "snapshot_id": snapshot_id,
            "source_id": "SYNTHETIC_FIXTURE",
            "owner_project": "workspace",
            "store_id": "workspace-market-snapshots",
            "contract_id": "g-data-core-d3a-v1",
            "dataset_schema_version": "1.0.0",
            "ingest_date": ingest_date,
            "created_at": created_at,
            "available_at": available_at,
            "partition_segments": ["year=2026", "month=07"],
            "license_status": "NOT_APPLICABLE",
            "quality_status": "REVIEW",
            "purpose_ceiling": "SMOKE_ONLY",
            "quality_gate": "PASS",
            "quality_checks": [
                "adapter_gate",
                "primary_key",
                "schema_exact",
            ],
            "consumers": [],
        }

    def _write_payload(self, *, trade_date: str, close: float) -> None:
        (self.raw / "response.json").write_text(
            json.dumps(
                {
                    "source": "synthetic",
                    "trade_date": trade_date,
                    "rows": 1,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        (self.curated / "part-000.jsonl").write_text(
            json.dumps(
                {
                    "security_id": "XSHG.600000",
                    "trade_date": trade_date,
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": close,
                    "volume": 100,
                    "amount": 1050.0,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    def _publish(self, *, failpoint: str | None = None):
        return publish_snapshot(
            workspace_root=self.root,
            catalog_path=self.catalog,
            request_path=self.request_path,
            raw_dir=self.raw,
            curated_dir=self.curated,
            failpoint=failpoint,
        )

    def _snapshot_path(self, snapshot_id: str) -> Path:
        return (
            self.root
            / "data"
            / "market"
            / "snapshots"
            / "synthetic-daily-update-smoke"
            / snapshot_id
        )

    def test_valid_day_duplicate_revision_and_late_arrival(self) -> None:
        gate = validate_adapter_sample(self.contract, self.sample)
        self.assertEqual(gate["status"], "PASS")
        self.assertFalse(gate["source_admission"])

        first = self._publish()
        self.assertEqual(first.status, "PUBLISHED")
        first_catalog_hash = sha256(self.catalog)
        first_raw = next(
            (
                self.root
                / "data"
                / "market"
                / "raw"
                / "SYNTHETIC_FIXTURE"
                / "synthetic-daily-update-smoke"
            ).rglob("response.json")
        )
        first_raw_hash = sha256(first_raw)

        duplicate = self._publish()
        self.assertEqual(duplicate.status, "NO_CHANGE")
        self.assertEqual(sha256(self.catalog), first_catalog_hash)

        self._write_payload(trade_date="2026-07-29", close=10.6)
        self.request = self._request(
            run_id="daily-smoke-r2",
            snapshot_id="2026-07-29-r2",
            ingest_date="2026-07-30",
            created_at="2026-07-30T01:10:00+08:00",
            available_at="2026-07-30T01:05:00+08:00",
        )
        write_json(self.request_path, self.request)
        revision = self._publish()
        self.assertEqual(revision.status, "PUBLISHED")
        self.assertEqual(sha256(first_raw), first_raw_hash)

        self._write_payload(trade_date="2026-07-28", close=10.4)
        late_available_at = "2026-07-30T02:05:00+08:00"
        self.request = self._request(
            run_id="daily-smoke-late1",
            snapshot_id="2026-07-28-late1",
            ingest_date="2026-07-30",
            created_at="2026-07-30T02:10:00+08:00",
            available_at=late_available_at,
        )
        write_json(self.request_path, self.request)
        late = self._publish()
        self.assertEqual(late.status, "PUBLISHED")

        catalog = read_json(self.catalog)
        self.assertEqual(validate_catalog(catalog), [])
        self.assertEqual(
            {item["snapshot_id"] for item in catalog["datasets"]},
            {"2026-07-29-v1", "2026-07-29-r2", "2026-07-28-late1"},
        )
        late_snapshot = self._snapshot_path("2026-07-28-late1")
        self.assertEqual(
            read_json(late_snapshot / "manifest.json")["available_at"],
            late_available_at,
        )
        self.assertEqual(
            read_json(late_snapshot / "provenance.json")["ingest_date"],
            "2026-07-30",
        )

    def test_business_and_operational_schema_drift_fail_closed(self) -> None:
        before = sha256(self.catalog)
        drifted = copy.deepcopy(self.sample)
        drifted["raw_rows"][0].append("unexpected-column")
        gate = validate_adapter_sample(self.contract, drifted)
        self.assertEqual(gate["status"], "FAIL")
        self.assertFalse(gate["checks"]["raw_width"])
        self.assertEqual(sha256(self.catalog), before)

        self.request["unexpected_request_field"] = "schema-drift"
        write_json(self.request_path, self.request)
        with self.assertRaises(PublishError):
            self._publish()
        self.assertEqual(sha256(self.catalog), before)
        self.assertEqual(read_json(self.catalog)["datasets"], [])

    def test_same_identity_changed_payload_is_quarantined(self) -> None:
        first = self._publish()
        self.assertEqual(first.status, "PUBLISHED")
        committed_hash = sha256(self.catalog)
        self._write_payload(trade_date="2026-07-29", close=999.0)
        self.request["run_id"] = "daily-smoke-identity-conflict"
        write_json(self.request_path, self.request)

        conflict = self._publish()
        self.assertEqual(conflict.status, "QUARANTINED")
        self.assertEqual(sha256(self.catalog), committed_hash)
        failure = read_json(Path(conflict.quarantine_path) / "failure.json")
        self.assertEqual(
            failure["reason"],
            "DATASET_SNAPSHOT_IDENTITY_CONFLICT",
        )

    def test_atomic_failure_is_invisible_until_recovery(self) -> None:
        before = sha256(self.catalog)
        with self.assertRaises(InjectedFailure):
            self._publish(failpoint="after_snapshot_finalize")
        self.assertEqual(sha256(self.catalog), before)
        self.assertEqual(read_json(self.catalog)["datasets"], [])
        self.assertTrue(
            (self._snapshot_path("2026-07-29-v1") / "manifest.json").is_file()
        )

        recovered = self._publish()
        self.assertEqual(recovered.status, "PUBLISHED")
        self.assertEqual(len(read_json(self.catalog)["datasets"]), 1)
        transaction = read_json(
            self.root
            / "data"
            / "market"
            / "transactions"
            / "daily-smoke-v1.json"
        )
        self.assertEqual(transaction["state"], "CATALOG_COMMITTED")


if __name__ == "__main__":
    unittest.main()
