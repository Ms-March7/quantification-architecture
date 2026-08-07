from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from factor_factory.qdata_input import QDataInputError, QDataResearchResolver


class _RecordingRunner:
    def __init__(
        self,
        *,
        payload: dict[str, object] | None = None,
        returncode: int = 0,
        stderr: str = "",
        stdout: str | None = None,
    ) -> None:
        self.payload = payload
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        logical_command = tuple(str(part) for part in command)
        self.commands.append(logical_command)
        stdout = self.stdout
        if stdout is None:
            stdout = json.dumps(self.payload)
        return subprocess.CompletedProcess(
            logical_command,
            self.returncode,
            stdout=stdout,
            stderr=self.stderr,
        )


class QDataResearchResolverTests(unittest.TestCase):
    DATASET_ID = "cn-a-share-daily-bars"
    SNAPSHOT_ID = "2026-07-28-v1"

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.workspace_root = Path(self._temporary_directory.name)
        qdata_command = self.workspace_root / "data" / "catalog" / "qdata.cmd"
        qdata_command.parent.mkdir(parents=True)
        qdata_command.write_text("@echo off\n", encoding="utf-8")
        self.snapshot_path = (
            self.workspace_root
            / "data"
            / "market"
            / "snapshots"
            / self.DATASET_ID
            / self.SNAPSHOT_ID
        )
        self.authority_path = self.snapshot_path / "MANIFEST.json"

    def _record(self, **overrides: object) -> dict[str, object]:
        record: dict[str, object] = {
            "dataset_id": self.DATASET_ID,
            "snapshot_id": self.SNAPSHOT_ID,
            "physical_path": str(self.snapshot_path),
            "resolved_path": str(self.snapshot_path),
            "path_exists": True,
            "requested_intent": "research",
            "requested_consumer": None,
            "lifecycle": "active",
            "mutability": "immutable",
            "purpose_ceiling": "RESEARCH_ONLY",
            "quality_status": "OK",
            "license_status": "VERIFIED",
            "available_at": "2026-07-29T00:00:00+08:00",
            "content_hash": "a" * 64,
            "hash_algorithm": "sha256-contract-tree-v1",
            "authority_paths": [str(self.authority_path)],
            "authority_status": [{"path": str(self.authority_path), "exists": True}],
            "known_limits": ["fixture only"],
        }
        record.update(overrides)
        return record

    def _resolver(self, runner: _RecordingRunner) -> QDataResearchResolver:
        return QDataResearchResolver(
            workspace_root=self.workspace_root,
            runner=runner,
            now_provider=lambda: datetime(2026, 8, 4, 2, 0, tzinfo=timezone.utc),
        )

    def test_exact_qdata_research_command_returns_descriptor_without_opening_payload(
        self,
    ) -> None:
        self.assertFalse(self.snapshot_path.exists())
        runner = _RecordingRunner(payload=self._record())

        resolved = self._resolver(runner).resolve(self.DATASET_ID, self.SNAPSHOT_ID)

        self.assertEqual(resolved.identity, f"{self.DATASET_ID}@{self.SNAPSHOT_ID}")
        self.assertEqual(resolved.resolved_path, self.snapshot_path)
        self.assertEqual(resolved.quality_status, "OK")
        self.assertEqual(
            resolved.available_at,
            datetime.fromisoformat("2026-07-29T00:00:00+08:00"),
        )
        self.assertEqual(
            runner.commands,
            [
                (
                    str(self.workspace_root / "data" / "catalog" / "qdata.cmd"),
                    "resolve",
                    self.DATASET_ID,
                    self.SNAPSHOT_ID,
                    "--intent",
                    "research",
                    "--json",
                )
            ],
        )
        self.assertFalse(self.snapshot_path.exists())

    def test_empty_snapshot_is_rejected_before_qdata(self) -> None:
        runner = _RecordingRunner(payload=self._record())
        with self.assertRaisesRegex(QDataInputError, "snapshot_id must be non-empty"):
            self._resolver(runner).resolve(self.DATASET_ID, "")
        self.assertEqual(runner.commands, [])

    def test_latest_snapshot_variants_are_rejected_before_qdata(self) -> None:
        for snapshot_id in ("latest", "LATEST", "2026-07-latest"):
            with self.subTest(snapshot_id=snapshot_id):
                runner = _RecordingRunner(payload=self._record())
                with self.assertRaisesRegex(QDataInputError, "forbids latest"):
                    self._resolver(runner).resolve(self.DATASET_ID, snapshot_id)
                self.assertEqual(runner.commands, [])

    def test_direct_candidate_path_is_not_accepted_as_catalog_identity(self) -> None:
        runner = _RecordingRunner(payload=self._record())
        direct_path = str(
            self.workspace_root / "tmp" / "local-dataset-build" / "candidate"
        )
        with self.assertRaisesRegex(QDataInputError, "paths and shell syntax are forbidden"):
            self._resolver(runner).resolve(direct_path, self.SNAPSHOT_ID)
        self.assertEqual(runner.commands, [])

    def test_unregistered_or_policy_rejected_identity_fails_closed(self) -> None:
        runner = _RecordingRunner(
            returncode=2,
            stderr="未登记数据身份",
        )
        with self.assertRaisesRegex(QDataInputError, "qdata research resolve was rejected"):
            self._resolver(runner).resolve(self.DATASET_ID, self.SNAPSHOT_ID)

    def test_non_research_resolution_is_rejected(self) -> None:
        runner = _RecordingRunner(
            payload=self._record(requested_intent="inspection"),
        )
        with self.assertRaisesRegex(QDataInputError, "not a research-intent resolution"):
            self._resolver(runner).resolve(self.DATASET_ID, self.SNAPSHOT_ID)

    def test_mutable_or_low_ceiling_resolution_is_rejected(self) -> None:
        cases = (
            ("mutable", {"mutability": "append_only"}, "immutable snapshot"),
            (
                "smoke ceiling",
                {"purpose_ceiling": "SMOKE_ONLY"},
                "does not permit research",
            ),
        )
        for label, overrides, message in cases:
            with self.subTest(label=label):
                runner = _RecordingRunner(payload=self._record(**overrides))
                with self.assertRaisesRegex(QDataInputError, message):
                    self._resolver(runner).resolve(self.DATASET_ID, self.SNAPSHOT_ID)

    def test_quality_and_license_warnings_cannot_become_factor_inputs(self) -> None:
        cases = (
            ("quality", {"quality_status": "REVIEW"}, "quality_status"),
            ("license", {"license_status": "UNVERIFIED"}, "license_status"),
        )
        for label, overrides, message in cases:
            with self.subTest(label=label):
                runner = _RecordingRunner(payload=self._record(**overrides))
                with self.assertRaisesRegex(QDataInputError, message):
                    self._resolver(runner).resolve(self.DATASET_ID, self.SNAPSHOT_ID)

    def test_future_or_naive_available_at_is_rejected(self) -> None:
        cases = (
            (
                "future",
                {"available_at": "2026-08-04T11:00:00+08:00"},
                "not available until",
            ),
            (
                "naive",
                {"available_at": "2026-08-04T09:30:00"},
                "explicit UTC offset",
            ),
            (
                "missing",
                {"available_at": None},
                "missing available_at",
            ),
        )
        for label, overrides, message in cases:
            with self.subTest(label=label):
                runner = _RecordingRunner(payload=self._record(**overrides))
                with self.assertRaisesRegex(QDataInputError, message):
                    self._resolver(runner).resolve(self.DATASET_ID, self.SNAPSHOT_ID)

    def test_missing_path_or_authority_is_rejected(self) -> None:
        cases = (
            ("payload", {"path_exists": False}, "snapshot path is missing"),
            (
                "authority",
                {
                    "authority_status": [
                        {"path": str(self.authority_path), "exists": False}
                    ]
                },
                "authority paths are missing",
            ),
        )
        for label, overrides, message in cases:
            with self.subTest(label=label):
                runner = _RecordingRunner(payload=self._record(**overrides))
                with self.assertRaisesRegex(QDataInputError, message):
                    self._resolver(runner).resolve(self.DATASET_ID, self.SNAPSHOT_ID)

    def test_mismatched_identity_is_rejected(self) -> None:
        runner = _RecordingRunner(payload=self._record(snapshot_id="other-snapshot"))
        with self.assertRaisesRegex(QDataInputError, "different dataset identity"):
            self._resolver(runner).resolve(self.DATASET_ID, self.SNAPSHOT_ID)

    def test_qdata_cannot_redirect_research_to_workspace_candidate_area(self) -> None:
        candidate_path = (
            self.workspace_root
            / "tmp"
            / "local-dataset-build"
            / self.DATASET_ID
            / self.SNAPSHOT_ID
        )
        runner = _RecordingRunner(
            payload=self._record(
                physical_path=str(candidate_path),
                resolved_path=str(candidate_path),
            )
        )
        with self.assertRaisesRegex(QDataInputError, "staging/candidate path"):
            self._resolver(runner).resolve(self.DATASET_ID, self.SNAPSHOT_ID)

    def test_invalid_json_is_rejected(self) -> None:
        runner = _RecordingRunner(stdout="not-json")
        with self.assertRaisesRegex(QDataInputError, "valid JSON"):
            self._resolver(runner).resolve(self.DATASET_ID, self.SNAPSHOT_ID)


if __name__ == "__main__":
    unittest.main()
