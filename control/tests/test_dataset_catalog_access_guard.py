from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
CATALOG_DIR = WORKSPACE / "data" / "catalog"
DATASET_CLI = CATALOG_DIR / "dataset_cli.py"
FORMAL_CATALOG = CATALOG_DIR / "datasets.json"
PLANNED_CATALOG = CATALOG_DIR / "planned_datasets.json"

if str(CATALOG_DIR) not in sys.path:
    sys.path.insert(0, str(CATALOG_DIR))

from dataset_cli import CatalogError, check_access, load_catalog  # noqa: E402


SYNTHETIC_PREFIXES = ("synthetic-", "fixture-", "test-")


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def catalog_identities(path: Path) -> list[tuple[str, str]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return [
        (str(item.get("dataset_id", "")), str(item.get("snapshot_id", "")))
        for item in value.get("datasets", [])
    ]


def synthetic_identities(identities: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [
        identity
        for identity in identities
        if identity[0].lower().startswith(SYNTHETIC_PREFIXES)
        or identity[1].lower().startswith(SYNTHETIC_PREFIXES)
    ]


class DatasetCatalogAccessGuardTests(unittest.TestCase):
    """Keep planned and synthetic catalogs outside the formal qdata boundary."""

    def setUp(self) -> None:
        self.formal_bytes_before = FORMAL_CATALOG.read_bytes()
        self.formal_hash_before = file_digest(FORMAL_CATALOG)
        self.formal_identities_before = catalog_identities(FORMAL_CATALOG)
        self.temp = tempfile.TemporaryDirectory(prefix="qdata-access-guard-")
        self.temp_root = Path(self.temp.name).resolve()

    def tearDown(self) -> None:
        try:
            self.assertEqual(FORMAL_CATALOG.read_bytes(), self.formal_bytes_before)
            self.assertEqual(file_digest(FORMAL_CATALOG), self.formal_hash_before)
            identities_after = catalog_identities(FORMAL_CATALOG)
            self.assertEqual(identities_after, self.formal_identities_before)
            self.assertEqual(synthetic_identities(identities_after), [])
        finally:
            self.temp.cleanup()

    def run_cli(
        self,
        *arguments: str,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(DATASET_CLI), *arguments],
            cwd=self.temp_root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def write_catalog(self, name: str, value: dict) -> Path:
        path = self.temp_root / name
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def test_explicit_planned_catalog_is_rejected_by_loader_and_cli(self) -> None:
        with self.assertRaises(CatalogError):
            load_catalog(PLANNED_CATALOG)

        result = self.run_cli(
            "--catalog",
            str(PLANNED_CATALOG),
            "list",
            "--json",
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("DATA ACCESS DENIED", result.stderr)
        self.assertNotIn("cn-a-share-security-master-pit", result.stdout)

    def test_planned_catalog_environment_override_is_rejected(self) -> None:
        env = os.environ.copy()
        env["QUANT_DATA_CATALOG"] = str(PLANNED_CATALOG)

        result = self.run_cli("list", "--json", env=env)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("DATA ACCESS DENIED", result.stderr)
        self.assertNotIn("cn-a-share-security-master-pit", result.stdout)

    def test_empty_physical_path_is_rejected_instead_of_resolving_to_cwd(self) -> None:
        catalog = self.write_catalog(
            "empty-physical-path.json",
            {
                "schema_version": 1,
                "updated_at": "2026-07-21T00:00:00+08:00",
                "workspace_root": self.temp_root.as_posix(),
                "catalog_mode": "federated_logical_catalog",
                "physical_migration": False,
                "authority_precedence": ["snapshot_manifest_or_provenance"],
                "owners": ["workspace"],
                "stores": [],
                "datasets": [
                    {
                        "dataset_id": "synthetic-empty-path",
                        "snapshot_id": "fixture-empty-path",
                        "physical_path": "",
                    }
                ],
                "retired_datasets": [],
                "publication_contracts": [],
            },
        )

        with self.assertRaises(CatalogError):
            load_catalog(catalog)

        result = self.run_cli("--catalog", str(catalog), "list", "--json")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("physical_path", result.stderr)
        self.assertNotIn('"path_exists": true', result.stdout)
        self.assertNotIn(str(self.temp_root), result.stdout)

    def test_formal_catalog_read_is_byte_stable_and_fixture_free(self) -> None:
        loaded = load_catalog(FORMAL_CATALOG)
        self.assertEqual(len(loaded["datasets"]), len(self.formal_identities_before))
        self.assertEqual(FORMAL_CATALOG.read_bytes(), self.formal_bytes_before)
        self.assertEqual(file_digest(FORMAL_CATALOG), self.formal_hash_before)
        self.assertEqual(synthetic_identities(self.formal_identities_before), [])

    def test_research_access_is_clock_gated_by_available_at(self) -> None:
        catalog = {"owners": ["workspace"]}
        item = {
            "dataset_id": "fixture-clock-gate",
            "snapshot_id": "20260804-v1",
            "purpose_ceiling": "RESEARCH_ONLY",
            "mutability": "immutable",
            "owner_project": "workspace",
            "consumers": [],
            "available_at": "2026-08-04T09:30:00+08:00",
        }
        before = datetime(2026, 8, 4, 1, 29, 59, tzinfo=timezone.utc)
        at_gate = datetime(2026, 8, 4, 1, 30, 0, tzinfo=timezone.utc)

        with self.assertRaisesRegex(CatalogError, "not available until"):
            check_access(catalog, item, "research", None, now_utc=before)
        check_access(catalog, item, "research", None, now_utc=at_gate)

    def test_research_access_rejects_missing_or_naive_available_at(self) -> None:
        catalog = {"owners": ["workspace"]}
        base = {
            "dataset_id": "fixture-clock-gate",
            "snapshot_id": "20260804-v1",
            "purpose_ceiling": "RESEARCH_ONLY",
            "mutability": "immutable",
            "owner_project": "workspace",
            "consumers": [],
        }
        now = datetime(2026, 8, 4, 1, 30, 0, tzinfo=timezone.utc)
        for value, message in (
            (None, "requires a timezone-aware"),
            ("2026-08-04T09:30:00", "explicit UTC offset"),
        ):
            with self.subTest(available_at=value):
                item = dict(base)
                if value is not None:
                    item["available_at"] = value
                with self.assertRaisesRegex(CatalogError, message):
                    check_access(catalog, item, "research", None, now_utc=now)


if __name__ == "__main__":
    unittest.main()
