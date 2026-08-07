from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from capital_flow_attribution.comparison import compare_run_roots  # noqa: E402


def _write_run(root: Path, value: str, *, s1r: bool) -> None:
    table = root / "normalized" / "table.csv"
    table.parent.mkdir(parents=True, exist_ok=True)
    table.write_text(f"key,value\na,{value}\n", encoding="utf-8")
    raw = root / "raw" / "source.txt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("raw", encoding="utf-8")
    import hashlib

    raw_manifest = [
        {
            "relative_path": "raw/source.txt",
            "sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
        }
    ]
    manifests = root / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    (manifests / "raw_files.json").write_text(json.dumps(raw_manifest), encoding="utf-8")
    dataset_manifest = {
        "pandera_daily_state": "PASS" if s1r else None,
        "parquet_tables": {"table": 1} if s1r else {},
        "duckdb_counts": {"table": 1} if s1r else {},
    }
    (manifests / "dataset_manifest.json").write_text(json.dumps(dataset_manifest), encoding="utf-8")


class RunComparisonTests(unittest.TestCase):
    def test_identical_candidate_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline, candidate = root / "baseline", root / "candidate"
            _write_run(baseline, "1", s1r=False)
            _write_run(candidate, "1", s1r=True)
            result = compare_run_roots(baseline, candidate, relative_tables=("normalized/table.csv",))
            self.assertEqual(result["test_status"], "PASS")

    def test_changed_table_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline, candidate = root / "baseline", root / "candidate"
            _write_run(baseline, "1", s1r=False)
            _write_run(candidate, "2", s1r=True)
            result = compare_run_roots(baseline, candidate, relative_tables=("normalized/table.csv",))
            self.assertEqual(result["test_status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
