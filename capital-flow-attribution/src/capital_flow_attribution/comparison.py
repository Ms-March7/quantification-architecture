from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .storage import file_sha256


S1_CORE_TABLES = (
    "normalized/universe_research_proxy.csv",
    "normalized/market_context.csv",
    "normalized/margin_ledger.csv",
    "normalized/etf_scale_proxy.csv",
    "normalized/stock_connect_eligible_current.csv",
    "normalized/stock_connect_turnover.csv",
    "normalized/fx_reference.csv",
    "normalized/source_coverage.csv",
    "snapshots/unified_daily_state.csv",
)


def _raw_hashes(root: Path) -> dict[str, str]:
    path = root / "manifests" / "raw_files.json"
    items = json.loads(path.read_text(encoding="utf-8"))
    return {str(item["relative_path"]): str(item["sha256"]) for item in items}


def compare_run_roots(
    baseline_root: str | Path,
    candidate_root: str | Path,
    *,
    relative_tables: Iterable[str] = S1_CORE_TABLES,
) -> dict[str, object]:
    baseline = Path(baseline_root)
    candidate = Path(candidate_root)
    tables: list[dict[str, object]] = []
    for relative in relative_tables:
        old_path = baseline / relative
        new_path = candidate / relative
        old_exists, new_exists = old_path.is_file(), new_path.is_file()
        old_hash = file_sha256(old_path) if old_exists else None
        new_hash = file_sha256(new_path) if new_exists else None
        tables.append(
            {
                "relative_path": relative,
                "baseline_exists": old_exists,
                "candidate_exists": new_exists,
                "baseline_bytes": old_path.stat().st_size if old_exists else None,
                "candidate_bytes": new_path.stat().st_size if new_exists else None,
                "baseline_sha256": old_hash,
                "candidate_sha256": new_hash,
                "byte_identical": old_hash is not None and old_hash == new_hash,
            }
        )
    baseline_raw = _raw_hashes(baseline)
    candidate_raw = _raw_hashes(candidate)
    raw_keys_equal = set(baseline_raw) == set(candidate_raw)
    raw_hashes_equal = raw_keys_equal and all(baseline_raw[key] == candidate_raw[key] for key in baseline_raw)

    candidate_manifest_path = candidate / "manifests" / "dataset_manifest.json"
    candidate_manifest = json.loads(candidate_manifest_path.read_text(encoding="utf-8"))
    parquet_counts = candidate_manifest.get("parquet_tables") or {}
    duckdb_counts = candidate_manifest.get("duckdb_counts") or {}
    s1r_checks = {
        "pandera_daily_state": candidate_manifest.get("pandera_daily_state"),
        "parquet_duckdb_counts_equal": parquet_counts == duckdb_counts and bool(parquet_counts),
        "parquet_tables": parquet_counts,
        "duckdb_counts": duckdb_counts,
    }
    passed = (
        all(bool(item["byte_identical"]) for item in tables)
        and raw_hashes_equal
        and s1r_checks["pandera_daily_state"] == "PASS"
        and s1r_checks["parquet_duckdb_counts_equal"]
    )
    return {
        "schema_version": "s1r-run-comparison-v1",
        "baseline_root": baseline.as_posix(),
        "candidate_root": candidate.as_posix(),
        "test_status": "PASS" if passed else "FAIL",
        "tables": tables,
        "raw": {
            "baseline_files": len(baseline_raw),
            "candidate_files": len(candidate_raw),
            "relative_paths_equal": raw_keys_equal,
            "hashes_equal": raw_hashes_equal,
        },
        "candidate_s1r_checks": s1r_checks,
    }
