"""Audit temporary factor caches before and after an approved cleanup.

The temporary runs predate the sidecar-backed registered research runs, so
their NumPy payloads are inventoried and hashed directly.  This tool never
deletes files.  It also verifies that retained temporary run artifacts and
formal research evidence are unchanged across the cleanup.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from audit_factor_cache_cleanup import (
    atomic_json,
    atomic_jsonl,
    sha256_file,
    verify_research_evidence,
)


EXPECTED_TMP_RUNS = (
    "real-csiall-shsz-2018-2026",
    "real-full-shsz-2018-2026",
    "real-full-shsz-2018-2026-v2",
)
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def is_reparse_point(path: Path) -> bool:
    stat_result = path.stat(follow_symlinks=False)
    attributes = int(getattr(stat_result, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def expected_cache_directories(tmp_root: Path, *, must_exist: bool) -> list[Path]:
    tmp_root = tmp_root.resolve()
    if tmp_root.name != "factor-factory" or tmp_root.parent.name != ".tmp":
        raise RuntimeError(f"unexpected temporary factor-factory root: {tmp_root}")
    targets = [(tmp_root / run / "factor_cache").resolve() for run in EXPECTED_TMP_RUNS]
    for run_name, target in zip(EXPECTED_TMP_RUNS, targets, strict=True):
        expected_parent = (tmp_root / run_name).resolve()
        if target.parent != expected_parent or target.name != "factor_cache":
            raise RuntimeError(f"unsafe cache target: {target}")
        if tmp_root not in target.parents:
            raise RuntimeError(f"cache target escapes temporary root: {target}")
        if must_exist:
            if not target.is_dir() or is_reparse_point(target):
                raise RuntimeError(f"cache target missing or linked: {target}")
            if (target.parent / ".run.lock").exists():
                raise RuntimeError(f"temporary run is locked: {target.parent}")
        elif target.exists():
            raise RuntimeError(f"temporary cache target still exists: {target}")
    discovered = sorted(
        path.resolve() for path in tmp_root.glob("*/factor_cache") if path.is_dir()
    )
    if must_exist and discovered != sorted(targets):
        raise RuntimeError(
            "discovered temporary caches differ from the explicit allowlist: "
            f"{[str(path) for path in discovered]}"
        )
    if not must_exist and discovered:
        raise RuntimeError(f"unexpected temporary cache directories remain: {discovered}")
    return targets


def load_formula_map(run: Path) -> dict[str, str]:
    candidates = run / "candidates.jsonl"
    if not candidates.is_file():
        return {}
    result: dict[str, str] = {}
    with candidates.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            value = json.loads(line)
            candidate_id = value.get("candidate_id")
            formula = value.get("formula")
            if not isinstance(candidate_id, str) or not isinstance(formula, str):
                raise RuntimeError(
                    f"invalid candidate record at {candidates}:{line_number}"
                )
            result[candidate_id] = formula
    return result


def retained_artifacts(tmp_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for run_name in EXPECTED_TMP_RUNS:
        run = tmp_root / run_name
        if not run.exists():
            continue
        if not run.is_dir() or is_reparse_point(run):
            raise RuntimeError(f"temporary run directory missing or linked: {run}")
        for path in sorted(item for item in run.iterdir() if item.name != "factor_cache"):
            if path.name == ".run.lock":
                raise RuntimeError(f"temporary run became locked: {run}")
            if path.is_dir():
                nested = sorted(item for item in path.rglob("*") if item.is_file())
                for item in nested:
                    if is_reparse_point(item):
                        raise RuntimeError(f"retained artifact is linked: {item}")
                    records.append(
                        {
                            "path": str(item),
                            "relative_path": item.relative_to(tmp_root).as_posix(),
                            "bytes": item.stat().st_size,
                            "sha256": sha256_file(item),
                        }
                    )
            elif path.is_file():
                if is_reparse_point(path):
                    raise RuntimeError(f"retained artifact is linked: {path}")
                records.append(
                    {
                        "path": str(path),
                        "relative_path": path.relative_to(tmp_root).as_posix(),
                        "bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                )
            else:
                raise RuntimeError(f"unexpected temporary run entry: {path}")
    return records


def precheck(tmp_root: Path, evidence_root: Path, receipt_root: Path) -> int:
    targets = expected_cache_directories(tmp_root, must_exist=True)
    retained = retained_artifacts(tmp_root)
    evidence = verify_research_evidence(evidence_root)
    inventory: list[dict[str, Any]] = []
    hash_first_size: dict[str, int] = {}
    hash_counts: Counter[str] = Counter()
    total_bytes = 0
    processed = 0
    for cache in targets:
        files = sorted(path for path in cache.rglob("*") if path.is_file())
        unexpected = [path for path in files if path.suffix.lower() != ".npy"]
        if unexpected:
            raise RuntimeError(f"unexpected files in temporary cache {cache}: {unexpected[:5]}")
        formula_map = load_formula_map(cache.parent)
        for payload in files:
            if is_reparse_point(payload):
                raise RuntimeError(f"temporary payload is linked: {payload}")
            actual_bytes = payload.stat().st_size
            actual_hash = sha256_file(payload)
            candidate_id = payload.stem
            processed += 1
            total_bytes += actual_bytes
            hash_counts[actual_hash] += 1
            hash_first_size.setdefault(actual_hash, actual_bytes)
            inventory.append(
                {
                    "run": cache.parent.name,
                    "candidate_id": candidate_id,
                    "formula": formula_map.get(candidate_id),
                    "npy_path": str(payload),
                    "bytes": actual_bytes,
                    "modified_at_utc": datetime.fromtimestamp(
                        payload.stat().st_mtime, timezone.utc
                    ).isoformat(),
                    "npy_sha256": actual_hash,
                    "verified": True,
                }
            )
            if processed == 1 or processed % 25 == 0:
                print(
                    f"HASH_PROGRESS matrices={processed} gib={total_bytes / (1024 ** 3):.3f}",
                    flush=True,
                )
    unique_bytes = sum(hash_first_size.values())
    inventory_path = receipt_root / "tmp-factor-cache-inventory.jsonl"
    atomic_jsonl(inventory_path, inventory)
    summary = {
        "schema_version": 1,
        "status": "PRECHECK_PASS",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "tmp_root": str(tmp_root.resolve()),
        "evidence_root": str(evidence_root.resolve()),
        "targets": [str(path) for path in targets],
        "matrix_count": processed,
        "total_bytes": total_bytes,
        "total_gib": round(total_bytes / (1024 ** 3), 6),
        "unique_hashes": len(hash_counts),
        "unique_bytes": unique_bytes,
        "duplicate_copy_bytes": total_bytes - unique_bytes,
        "duplicate_hash_groups": sum(1 for count in hash_counts.values() if count > 1),
        "inventory": str(inventory_path),
        "inventory_sha256": sha256_file(inventory_path),
        "retained_artifacts": retained,
        "formal_research_evidence": evidence,
    }
    atomic_json(receipt_root / "precheck-summary.json", summary)
    print(
        "PRECHECK_PASS "
        f"targets={len(targets)} matrices={processed} total_gib={summary['total_gib']} "
        f"inventory_sha256={summary['inventory_sha256']}",
        flush=True,
    )
    return 0


def postcheck(tmp_root: Path, evidence_root: Path, receipt_root: Path) -> int:
    expected_cache_directories(tmp_root, must_exist=False)
    precheck_path = receipt_root / "precheck-summary.json"
    inventory_path = receipt_root / "tmp-factor-cache-inventory.jsonl"
    precheck_summary = json.loads(precheck_path.read_text(encoding="utf-8"))
    if precheck_summary.get("status") != "PRECHECK_PASS":
        raise RuntimeError("precheck summary is not PASS")
    inventory_hash = sha256_file(inventory_path)
    if inventory_hash != precheck_summary.get("inventory_sha256"):
        raise RuntimeError("temporary cleanup inventory changed after precheck")
    retained = retained_artifacts(tmp_root)
    if retained != precheck_summary.get("retained_artifacts"):
        raise RuntimeError("retained temporary artifacts changed during cleanup")
    evidence = verify_research_evidence(evidence_root)
    if evidence != precheck_summary.get("formal_research_evidence"):
        raise RuntimeError("formal research evidence changed during temporary cleanup")
    remaining_files = sorted(path for path in tmp_root.rglob("*") if path.is_file())
    remaining_npy = [path for path in remaining_files if path.suffix.lower() == ".npy"]
    if remaining_npy:
        raise RuntimeError(f"temporary NumPy payloads remain: {remaining_npy[:5]}")
    summary = {
        "schema_version": 1,
        "status": "POSTCHECK_PASS",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "tmp_root": str(tmp_root.resolve()),
        "removed_targets": precheck_summary["targets"],
        "removed_matrix_count": precheck_summary["matrix_count"],
        "removed_bytes": precheck_summary["total_bytes"],
        "removed_gib": precheck_summary["total_gib"],
        "inventory": str(inventory_path),
        "inventory_sha256": inventory_hash,
        "retained_artifact_count": len(retained),
        "remaining_file_count": len(remaining_files),
        "remaining_bytes": sum(path.stat().st_size for path in remaining_files),
        "formal_research_evidence": evidence,
    }
    atomic_json(receipt_root / "postcheck-summary.json", summary)
    print(
        "POSTCHECK_PASS "
        f"removed_targets={len(summary['removed_targets'])} "
        f"removed_matrices={summary['removed_matrix_count']} "
        f"removed_gib={summary['removed_gib']} "
        f"remaining_files={summary['remaining_file_count']}",
        flush=True,
    )
    return 0


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Audit temporary multi-factor caches")
    parser.add_argument("mode", choices=("precheck", "postcheck"))
    parser.add_argument(
        "--tmp-root",
        type=Path,
        default=project_root / ".tmp" / "factor-factory",
    )
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=project_root / "research_runs" / "factor_factory",
    )
    parser.add_argument("--receipt-root", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.mode == "precheck":
            return precheck(
                args.tmp_root.resolve(),
                args.evidence_root.resolve(),
                args.receipt_root.resolve(),
            )
        return postcheck(
            args.tmp_root.resolve(),
            args.evidence_root.resolve(),
            args.receipt_root.resolve(),
        )
    except Exception as exc:  # noqa: BLE001 - audit command must fail closed.
        print(f"AUDIT_FAIL: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
