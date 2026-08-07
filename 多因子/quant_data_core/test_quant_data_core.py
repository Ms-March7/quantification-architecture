from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from quant_data_core.catalog import (
    DatasetCatalog,
    ImplicitLatestForbidden,
    IntegrityError,
    PromotionDenied,
    ReleaseSpec,
)


class QuantDataCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.catalog = DatasetCatalog(self.base / "catalog.sqlite3")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_spec(
        self,
        snapshot_id: str = "2026-07-14",
        *,
        ceiling: str = "RESEARCH_ONLY",
        source_tier: str = "RESEARCH_ONLY",
        available_at: str = "2026-07-15T12:45:00Z",
        pit_verified: bool = False,
    ) -> tuple[ReleaseSpec, Path]:
        snapshot = self.base / f"snapshot-{snapshot_id}"
        root = snapshot / "qlib_bin"
        (root / "features" / "sh600000").mkdir(parents=True)
        (root / "calendars").mkdir()
        (root / "features" / "sh600000" / "close.day.bin").write_bytes(b"opaque-market-bytes")
        (root / "calendars" / "day.txt").write_text("2026-07-14\n", encoding="utf-8")
        provenance = snapshot / "PROVENANCE.json"
        provenance.write_text(
            json.dumps(
                {"snapshot_date": snapshot_id, "research_only": ceiling == "RESEARCH_ONLY"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return (
            ReleaseSpec(
                dataset_id="a-share-daily",
                snapshot_id=snapshot_id,
                source="fixture",
                source_tier=source_tier,
                frequency="1d",
                universe="SH,SZ",
                coverage={"start": "2026-07-14", "end": snapshot_id, "symbols": 1},
                schema_version="qlib-bin-v1",
                license="Apache-2.0",
                published_at=f"{snapshot_id}T00:00:00Z",
                available_at=available_at,
                ingested_at=available_at,
                revised_at=None,
                pit_verified=pit_verified,
                promotion_ceiling=ceiling,
                root_path=root,
            ),
            provenance,
        )

    def test_register_is_idempotent_but_release_is_immutable(self) -> None:
        spec, provenance = self.make_spec()
        first = self.catalog.register_release(spec, provenance_path=provenance)
        second = self.catalog.register_release(spec, provenance_path=provenance)
        self.assertEqual(first, second)
        self.assertTrue(Path(first.manifest_path).is_file())
        with self.assertRaises(IntegrityError):
            self.catalog.register_release(replace(spec, source="different"), provenance_path=provenance)
        connection = sqlite3.connect(self.catalog.database)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE releases SET source='tampered' WHERE dataset_id='a-share-daily'"
                )
        finally:
            connection.close()

    def test_formal_purposes_forbid_implicit_latest_and_enforce_ceiling(self) -> None:
        spec, provenance = self.make_spec()
        self.catalog.register_release(spec, provenance_path=provenance)
        with self.assertRaises(ImplicitLatestForbidden):
            self.catalog.resolve("a-share-daily", "research")
        resolved = self.catalog.resolve(
            "a-share-daily", "research", snapshot_id="2026-07-14"
        )
        self.assertEqual(resolved.snapshot_id, "2026-07-14")
        with self.assertRaises(PromotionDenied):
            self.catalog.resolve("a-share-daily", "shadow", snapshot_id="2026-07-14")
        with self.assertRaises(PromotionDenied):
            self.catalog.resolve("a-share-daily", "live", snapshot_id="2026-07-14")

    def test_as_of_selects_only_releases_already_available(self) -> None:
        first, first_provenance = self.make_spec()
        self.catalog.register_release(first, provenance_path=first_provenance)
        second, second_provenance = self.make_spec(
            "2026-07-15", available_at="2026-07-16T12:45:00Z"
        )
        self.catalog.register_release(second, provenance_path=second_provenance)
        early = self.catalog.resolve(
            "a-share-daily", "research", as_of="2026-07-15T18:00:00Z"
        )
        late = self.catalog.resolve(
            "a-share-daily", "research", as_of="2026-07-16T18:00:00Z"
        )
        self.assertEqual(early.snapshot_id, "2026-07-14")
        self.assertEqual(late.snapshot_id, "2026-07-15")

    def test_directory_and_provenance_tampering_are_detected(self) -> None:
        spec, provenance = self.make_spec()
        release = self.catalog.register_release(spec, provenance_path=provenance)
        self.catalog.verify_release(release)
        data_file = Path(spec.root_path) / "features" / "sh600000" / "close.day.bin"
        original = data_file.read_bytes()
        data_file.write_bytes(original + b"tamper")
        with self.assertRaises(IntegrityError):
            self.catalog.verify_release(release)
        data_file.write_bytes(original)
        provenance.write_text('{"snapshot_date":"2026-07-14","research_only":false}', encoding="utf-8")
        with self.assertRaises(IntegrityError):
            self.catalog.verify_release(release)

    def test_live_requires_pit_verified_even_at_live_ceiling(self) -> None:
        spec, provenance = self.make_spec(
            ceiling="LIVE_READY", source_tier="CANONICAL", pit_verified=False
        )
        self.catalog.register_release(spec, provenance_path=provenance)
        with self.assertRaises(PromotionDenied):
            self.catalog.resolve("a-share-daily", "live", snapshot_id="2026-07-14")


if __name__ == "__main__":
    unittest.main()
