from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from factor_factory.qdata_input import (
    QDataInputError,
    QDataResearchResolver,
    R0_PV_CORE_DATASET_IDS,
    R0PVInputBundlePreflight,
    ResearchSnapshotRef,
)


class _BundleRunner:
    def __init__(self, records: dict[str, dict[str, object]]) -> None:
        self.records = records
        self.reject_dataset: str | None = None
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        logical = tuple(str(part) for part in command)
        self.commands.append(logical)
        dataset_id = logical[2]
        if dataset_id == self.reject_dataset:
            return subprocess.CompletedProcess(
                logical,
                2,
                stdout="",
                stderr="research policy rejected fixture",
            )
        return subprocess.CompletedProcess(
            logical,
            0,
            stdout=json.dumps(self.records[dataset_id]),
            stderr="",
        )


class R0PVInputBundlePreflightTests(unittest.TestCase):
    SNAPSHOT_ID = "2026-08-01-v1"
    RESEARCH_START = "2005-01-04"
    RESEARCH_END = "2024-12-31"
    BUNDLE_AS_OF = "2026-08-01"
    BUNDLE_CONTRACT = "r0-pv-core01-06-v1"

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.workspace_root = Path(self._temporary_directory.name)
        qdata = self.workspace_root / "data" / "catalog" / "qdata.cmd"
        qdata.parent.mkdir(parents=True)
        qdata.write_text("@echo off\n", encoding="utf-8")
        self.records: dict[str, dict[str, object]] = {}
        self.snapshot_paths: dict[str, Path] = {}
        self.loaded_authorities: list[Path] = []
        for index, dataset_id in enumerate(R0_PV_CORE_DATASET_IDS, start=1):
            snapshot_path = (
                self.workspace_root
                / "data"
                / "market"
                / "snapshots"
                / dataset_id
                / self.SNAPSHOT_ID
            )
            self.snapshot_paths[dataset_id] = snapshot_path
            self._write_authorities(dataset_id, snapshot_path, index=index)
            payload = snapshot_path / "payload-must-not-be-opened.parquet"
            payload.write_bytes(b"NOT A REAL PARQUET FILE")
            authorities = [
                snapshot_path / "request.json",
                snapshot_path / "manifest.json",
                snapshot_path / "provenance.json",
                snapshot_path / "partition-manifest.json",
                snapshot_path / "quality" / "summary.json",
            ]
            self.records[dataset_id] = {
                "dataset_id": dataset_id,
                "snapshot_id": self.SNAPSHOT_ID,
                "physical_path": str(snapshot_path),
                "resolved_path": str(snapshot_path),
                "path_exists": True,
                "requested_intent": "research",
                "requested_consumer": None,
                "lifecycle": "frozen",
                "mutability": "immutable",
                "purpose_ceiling": "RESEARCH_ONLY",
                "quality_status": "OK",
                "license_status": "VERIFIED",
                "available_at": "2026-08-01T00:00:00+08:00",
                "content_hash": format(index, "064x"),
                "hash_algorithm": "sha256-snapshot-contract-v1",
                "authority_paths": [str(path) for path in authorities],
                "authority_status": [
                    {"path": str(path), "exists": True} for path in authorities
                ],
                "known_limits": [],
            }
        self.runner = _BundleRunner(self.records)
        self.resolver = QDataResearchResolver(
            workspace_root=self.workspace_root,
            runner=self.runner,
            now_provider=lambda: datetime(2026, 8, 4, 2, 0, tzinfo=timezone.utc),
        )
        self.preflight = R0PVInputBundlePreflight(
            resolver=self.resolver,
            authority_loader=self._load_authority,
        )

    def _semantic(self, dataset_id: str, index: int) -> dict[str, object]:
        roles = {
            dataset: f"CORE-{position:02d}"
            for position, dataset in enumerate(R0_PV_CORE_DATASET_IDS, start=1)
        }
        conventions = {
            "cn-a-share-daily-raw": "RAW_UNADJUSTED",
            "cn-a-share-daily-adjusted": "FORWARD_ADJUSTED",
        }
        return {
            "schema_version": 1,
            "research_tier": "R0_PV",
            "role": roles[dataset_id],
            "bundle_contract_id": self.BUNDLE_CONTRACT,
            "bundle_as_of_date": self.BUNDLE_AS_OF,
            "market_scope": "CN_A_SHARE",
            "timezone": "Asia/Shanghai",
            "security_identity_dataset_id": "cn-a-share-security-master-pit",
            "calendar_dataset_id": "cn-trading-calendar",
            "coverage_start_date": "1990-12-19",
            "coverage_end_date": "2026-07-31",
            "missing_value_policy": "NULL_NEVER_ZERO_FILL",
            "available_at_semantics": "PIT_REVISION_AWARE",
            "value_convention": conventions.get(dataset_id, "EVENT_OR_REFERENCE"),
            "fixture_sequence": index,
        }

    def _write_authorities(
        self,
        dataset_id: str,
        snapshot_path: Path,
        *,
        index: int,
    ) -> None:
        (snapshot_path / "quality").mkdir(parents=True, exist_ok=True)
        common = {"dataset_id": dataset_id, "snapshot_id": self.SNAPSHOT_ID}
        documents = {
            "request.json": {
                **common,
                "mode": "formal_research_snapshot",
            },
            "manifest.json": {
                **common,
                "status": "SEALED_SNAPSHOT",
                "synthetic_fixture": False,
                "hash_algorithm": "sha256-snapshot-contract-v1",
                "contract_id": "formal-r0-snapshot-v1",
                "dataset_schema_version": "1",
                "available_at": "2026-08-01T00:00:00+08:00",
                "research_input_contract": self._semantic(dataset_id, index),
            },
            "provenance.json": {
                **common,
                "mode": "formal_research_snapshot",
            },
            "partition-manifest.json": {
                **common,
                "files": [
                    {
                        "relative_path": "payload-must-not-be-opened.parquet",
                        "sha256": "f" * 64,
                    }
                ],
            },
            "quality/summary.json": {
                **common,
                "synthetic_fixture": False,
                "catalog_quality_status": "OK",
                "purpose_ceiling": "RESEARCH_ONLY",
            },
        }
        for relative, value in documents.items():
            path = snapshot_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(value, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )

    def _load_authority(self, path: Path) -> dict[str, object]:
        self.loaded_authorities.append(path)
        return json.loads(path.read_text(encoding="utf-8"))

    def _refs(self) -> list[ResearchSnapshotRef]:
        return [
            ResearchSnapshotRef(dataset_id, self.SNAPSHOT_ID)
            for dataset_id in R0_PV_CORE_DATASET_IDS
        ]

    def _run(self):
        return self.preflight.preflight(
            self._refs(),
            research_start_date=self.RESEARCH_START,
            research_end_date=self.RESEARCH_END,
        )

    def _rewrite_manifest(self, dataset_id: str, mutate) -> None:
        path = self.snapshot_paths[dataset_id] / "manifest.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        mutate(value)
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")

    def test_six_exact_qdata_resolves_freeze_descriptor_without_payload_access(self) -> None:
        descriptor = self._run()

        self.assertEqual(descriptor.research_tier, "R0_PV")
        self.assertEqual(len(descriptor.snapshots), 6)
        self.assertFalse(descriptor.payload_opened)
        self.assertEqual(descriptor.production_paneldata_status, "BLOCKED__PAYLOAD_NOT_OPENED")
        self.assertRegex(descriptor.descriptor_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(
            [command[2] for command in self.runner.commands],
            list(R0_PV_CORE_DATASET_IDS),
        )
        self.assertEqual(len(self.loaded_authorities), 30)
        self.assertTrue(
            all(path.name != "payload-must-not-be-opened.parquet" for path in self.loaded_authorities)
        )

    def test_missing_core_item_is_rejected_before_qdata(self) -> None:
        with self.assertRaisesRegex(QDataInputError, "exactly CORE-01..06"):
            self.preflight.preflight(
                self._refs()[:-1],
                research_start_date=self.RESEARCH_START,
                research_end_date=self.RESEARCH_END,
            )
        self.assertEqual(self.runner.commands, [])

    def test_duplicate_core_item_is_rejected_before_qdata(self) -> None:
        refs = self._refs()
        refs[-1] = refs[0]
        with self.assertRaisesRegex(QDataInputError, "duplicate R0 dataset identity"):
            self.preflight.preflight(
                refs,
                research_start_date=self.RESEARCH_START,
                research_end_date=self.RESEARCH_END,
            )
        self.assertEqual(self.runner.commands, [])

    def test_unexpected_dataset_is_rejected_before_qdata(self) -> None:
        refs = self._refs()
        refs[-1] = ResearchSnapshotRef("wrong-dataset", self.SNAPSHOT_ID)
        with self.assertRaisesRegex(QDataInputError, "unexpected"):
            self.preflight.preflight(
                refs,
                research_start_date=self.RESEARCH_START,
                research_end_date=self.RESEARCH_END,
            )
        self.assertEqual(self.runner.commands, [])

    def test_latest_is_rejected_before_qdata(self) -> None:
        refs = self._refs()
        refs[0] = ResearchSnapshotRef(refs[0].dataset_id, "latest")
        with self.assertRaisesRegex(QDataInputError, "forbids latest"):
            self.preflight.preflight(
                refs,
                research_start_date=self.RESEARCH_START,
                research_end_date=self.RESEARCH_END,
            )
        self.assertEqual(self.runner.commands, [])

    def test_qdata_policy_rejection_aborts_bundle(self) -> None:
        self.runner.reject_dataset = R0_PV_CORE_DATASET_IDS[2]
        with self.assertRaisesRegex(QDataInputError, "qdata research resolve was rejected"):
            self._run()
        self.assertEqual(len(self.runner.commands), 3)

    def test_missing_authority_document_is_rejected(self) -> None:
        dataset_id = R0_PV_CORE_DATASET_IDS[0]
        (self.snapshot_paths[dataset_id] / "quality" / "summary.json").unlink()
        with self.assertRaisesRegex(QDataInputError, "missing authority document"):
            self._run()

    def test_manifest_identity_mismatch_is_rejected(self) -> None:
        dataset_id = R0_PV_CORE_DATASET_IDS[0]
        self._rewrite_manifest(dataset_id, lambda value: value.update(dataset_id="other"))
        with self.assertRaisesRegex(QDataInputError, "different dataset_id"):
            self._run()

    def test_manifest_available_at_must_match_qdata_resolution(self) -> None:
        dataset_id = R0_PV_CORE_DATASET_IDS[0]
        self._rewrite_manifest(
            dataset_id,
            lambda value: value.update(
                available_at="2026-08-01T00:00:01+08:00"
            ),
        )
        with self.assertRaisesRegex(QDataInputError, "available_at disagrees with qdata"):
            self._run()

    def test_missing_formal_semantic_contract_keeps_paneldata_blocked(self) -> None:
        dataset_id = R0_PV_CORE_DATASET_IDS[0]
        self._rewrite_manifest(
            dataset_id,
            lambda value: value.pop("research_input_contract"),
        )
        with self.assertRaisesRegex(QDataInputError, "production PanelData remains blocked"):
            self._run()

    def test_cross_snapshot_as_of_mismatch_is_rejected(self) -> None:
        dataset_id = R0_PV_CORE_DATASET_IDS[-1]

        def mutate(value: dict[str, object]) -> None:
            value["research_input_contract"]["bundle_as_of_date"] = "2026-07-31"  # type: ignore[index]

        self._rewrite_manifest(dataset_id, mutate)
        with self.assertRaisesRegex(QDataInputError, "inconsistent bundle_as_of_date"):
            self._run()

    def test_cross_snapshot_semantic_mismatch_is_rejected(self) -> None:
        dataset_id = R0_PV_CORE_DATASET_IDS[-1]

        def mutate(value: dict[str, object]) -> None:
            value["research_input_contract"]["timezone"] = "UTC"  # type: ignore[index]

        self._rewrite_manifest(dataset_id, mutate)
        with self.assertRaisesRegex(QDataInputError, "timezone is inconsistent"):
            self._run()

    def test_insufficient_cross_snapshot_coverage_is_rejected(self) -> None:
        dataset_id = R0_PV_CORE_DATASET_IDS[3]

        def mutate(value: dict[str, object]) -> None:
            value["research_input_contract"]["coverage_start_date"] = "2010-01-01"  # type: ignore[index]

        self._rewrite_manifest(dataset_id, mutate)
        with self.assertRaisesRegex(QDataInputError, "does not cover"):
            self._run()


if __name__ == "__main__":
    unittest.main()
