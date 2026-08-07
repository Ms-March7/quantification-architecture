from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "control/tools/build_candidate_asset_index.py"


def load_module():
    spec = importlib.util.spec_from_file_location("build_candidate_asset_index", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CandidateAssetIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_build_indexes_only_bounded_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            quarantine = root / "data/market/quarantine"
            candidate = quarantine / "sample"
            candidate.mkdir(parents=True)
            (candidate / "manifest.json").write_text('{"id":"x"}\n', encoding="utf-8")
            (candidate / "payload.parquet").write_bytes(b"payload")
            with mock.patch.object(self.module, "ROOT", root):
                index = self.module.build_index(quarantine)
            self.assertEqual(index["candidate_root_count"], 1)
            self.assertEqual(index["manifest_file_count"], 1)
            self.assertEqual(index["evidence_file_count"], 1)
            self.assertNotIn("payload.parquet", json.dumps(index))

    def test_root_without_evidence_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            quarantine = root / "data/market/quarantine"
            candidate = quarantine / "orphan"
            candidate.mkdir(parents=True)
            (candidate / "payload.bin").write_bytes(b"x")
            with mock.patch.object(self.module, "ROOT", root):
                with self.assertRaises(self.module.CandidateIndexError):
                    self.module.build_index(quarantine)

    def test_validation_rejects_promotion_claim(self):
        value = {
            "schema_version": 1,
            "index_id": "candidate-assets-central-index-v1",
            "scan_root": "data/market/quarantine",
            "candidate_root_count": 0,
            "evidence_file_count": 0,
            "manifest_file_count": 0,
            "content_fingerprint_sha256": "0" * 64,
            "purpose_ceiling": "RESEARCH_ONLY",
            "quality_status": "OK",
            "formal_effect": "NONE",
            "formal_catalog_write": False,
            "candidate_roots": [],
        }
        with self.assertRaises(self.module.CandidateIndexError):
            self.module.validate_index(value)

    def test_atomic_write_replaces_existing_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "candidate_assets.json"
            target.write_text("old", encoding="utf-8")
            self.module.atomic_write(target, b"new\n")
            self.assertEqual(target.read_bytes(), b"new\n")


if __name__ == "__main__":
    unittest.main()
