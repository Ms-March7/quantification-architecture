"""Audit factor caches before and after an explicitly approved cleanup.

This tool never deletes files.  It verifies cache sidecars against the actual
NumPy payloads, verifies committed/aborted research evidence, and writes a
small external receipt that survives cache deletion.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable


EXPECTED_CACHE_RUNS = (
    "full-shsz-2018-2026-v2-research",
    "full-shsz-2018-2026-v3-research",
    "full-shsz-2018-2026-v4-gpu-research",
    "full-shsz-2018-2026-v5-gpu-batched-research",
    "full-shsz-2018-2026-v6-discovery-first-gpu-research",
)
REQUIRED_SIDECAR_FIELDS = {
    "schema_version",
    "candidate_id",
    "formula",
    "panel_fingerprint",
    "engine_fingerprint",
    "npy_sha256",
    "bytes",
}


def sha256_file(path: Path, block_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(block_size), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def atomic_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(path)


def verify_committed_run(run: Path) -> dict[str, Any]:
    manifest_path = run / "manifest.json"
    commit_path = run / "COMMITTED.json"
    commit = load_json(commit_path)
    if commit.get("registry_schema") != 2:
        raise RuntimeError(f"unsupported commit schema: {commit_path}")
    manifest_hash = sha256_file(manifest_path)
    if manifest_hash != commit.get("manifest_sha256"):
        raise RuntimeError(f"manifest hash mismatch: {manifest_path}")
    manifest = load_json(manifest_path)
    artifacts = manifest.get("artifacts")
    if manifest.get("registry_schema") != 2 or not isinstance(artifacts, dict):
        raise RuntimeError(f"invalid committed manifest: {manifest_path}")
    verified_artifacts: list[dict[str, Any]] = []
    for name, evidence in sorted(artifacts.items()):
        if not isinstance(name, str) or not isinstance(evidence, dict):
            raise RuntimeError(f"invalid artifact evidence in {manifest_path}")
        normalized = Path(name)
        if normalized.is_absolute() or ".." in normalized.parts:
            raise RuntimeError(f"unsafe artifact path in {manifest_path}: {name}")
        if normalized.parts and normalized.parts[0].lower() == "factor_cache":
            raise RuntimeError(f"factor_cache is unexpectedly committed: {manifest_path}")
        artifact = run / normalized
        if not artifact.is_file() or artifact.is_symlink():
            raise RuntimeError(f"committed artifact missing or linked: {artifact}")
        actual_bytes = artifact.stat().st_size
        actual_hash = sha256_file(artifact)
        if actual_bytes != int(evidence.get("bytes", -1)):
            raise RuntimeError(f"artifact byte count mismatch: {artifact}")
        if actual_hash != evidence.get("sha256"):
            raise RuntimeError(f"artifact hash mismatch: {artifact}")
        verified_artifacts.append(
            {"path": str(artifact), "bytes": actual_bytes, "sha256": actual_hash}
        )
    return {
        "run": run.name,
        "state": manifest.get("attempt_terminal_state") or "COMMITTED_LEGACY",
        "commit": str(commit_path),
        "manifest": str(manifest_path),
        "manifest_sha256": manifest_hash,
        "artifact_count": len(verified_artifacts),
        "artifacts": verified_artifacts,
    }


def read_attempt_row(database: Path, protocol_id: str) -> tuple[Any, ...]:
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        row = connection.execute(
            """
            SELECT protocol_hash, run_directory, state, state_evidence_json
            FROM attempts WHERE protocol_id = ?
            """,
            (protocol_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise RuntimeError(f"governance attempt is missing: {protocol_id}")
    return row


def verify_aborted_run(run: Path) -> dict[str, Any]:
    aborted_path = run / "ABORTED.json"
    aborted = load_json(aborted_path)
    if aborted.get("state") != "ABORTED_TECHNICAL":
        raise RuntimeError(f"unexpected aborted state: {aborted_path}")
    if aborted.get("holdout_was_opened") is not False or aborted.get("result_committed") is not False:
        raise RuntimeError(f"aborted run has unsafe terminal flags: {aborted_path}")
    for forbidden in ("COMMITTED.json", "manifest.json", "selected.json", "freeze.json"):
        if (run / forbidden).exists():
            raise RuntimeError(f"aborted run contains forbidden result: {run / forbidden}")
    database = Path(str(aborted["attempt_database"]))
    if not database.is_file():
        raise RuntimeError(f"attempt database is missing: {database}")
    protocol_hash, run_directory, state, raw_evidence = read_attempt_row(
        database, str(aborted["protocol_id"])
    )
    if protocol_hash != aborted.get("protocol_hash") or state != "ABORTED_TECHNICAL":
        raise RuntimeError(f"aborted governance state mismatch: {aborted_path}")
    if Path(str(run_directory)).resolve() != run.resolve():
        raise RuntimeError(f"aborted governance run path mismatch: {aborted_path}")
    state_evidence = json.loads(raw_evidence)
    terminal = state_evidence.get("ABORTED_TECHNICAL", {})
    aborted_hash = sha256_file(aborted_path)
    if terminal.get("abort_artifact_sha256") != aborted_hash:
        raise RuntimeError(f"aborted artifact hash mismatch: {aborted_path}")
    if int(terminal.get("cached_factor_matrices", -1)) != int(
        aborted.get("cached_factor_matrices", -2)
    ):
        raise RuntimeError(f"aborted cache count evidence mismatch: {aborted_path}")
    return {
        "run": run.name,
        "state": "ABORTED_TECHNICAL",
        "aborted": str(aborted_path),
        "aborted_sha256": aborted_hash,
        "attempt_database": str(database),
        "protocol_id": aborted["protocol_id"],
        "protocol_hash": protocol_hash,
    }


def verify_research_evidence(factory_root: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for run in sorted(path for path in factory_root.iterdir() if path.is_dir()):
        if (run / "COMMITTED.json").is_file():
            results.append(verify_committed_run(run))
        elif (run / "ABORTED.json").is_file():
            results.append(verify_aborted_run(run))
    if not results:
        raise RuntimeError("no committed or aborted research evidence found")
    return results


def expected_cache_directories(factory_root: Path, *, must_exist: bool) -> list[Path]:
    factory_root = factory_root.resolve()
    targets = [(factory_root / run / "factor_cache").resolve() for run in EXPECTED_CACHE_RUNS]
    for run_name, target in zip(EXPECTED_CACHE_RUNS, targets, strict=True):
        expected_parent = (factory_root / run_name).resolve()
        if target.parent != expected_parent or target.name != "factor_cache":
            raise RuntimeError(f"unsafe cache target: {target}")
        if factory_root not in target.parents:
            raise RuntimeError(f"cache target escapes factor factory root: {target}")
        if must_exist:
            if not target.is_dir() or target.is_symlink():
                raise RuntimeError(f"cache target missing or linked: {target}")
            if (target.parent / ".run.lock").exists():
                raise RuntimeError(f"run is locked: {target.parent}")
        elif target.exists():
            raise RuntimeError(f"cache target still exists: {target}")
    discovered = sorted(
        path.resolve()
        for path in factory_root.glob("*/factor_cache")
        if path.is_dir()
    )
    if must_exist and discovered != sorted(targets):
        raise RuntimeError(
            "discovered cache targets differ from the explicit allowlist: "
            f"{[str(path) for path in discovered]}"
        )
    if not must_exist and discovered:
        raise RuntimeError(f"unexpected cache directories remain: {discovered}")
    return targets


def precheck(factory_root: Path, receipt_root: Path) -> int:
    targets = expected_cache_directories(factory_root, must_exist=True)
    evidence = verify_research_evidence(factory_root)
    inventory: list[dict[str, Any]] = []
    hash_first_size: dict[str, int] = {}
    hash_counts: Counter[str] = Counter()
    total_bytes = 0
    processed = 0
    for cache in targets:
        files = sorted(path for path in cache.rglob("*") if path.is_file())
        sidecars = [path for path in files if path.suffix.lower() == ".json"]
        payloads = [path for path in files if path.suffix.lower() == ".npy"]
        unexpected = [path for path in files if path.suffix.lower() not in {".json", ".npy"}]
        if unexpected:
            raise RuntimeError(f"unexpected files in cache {cache}: {unexpected[:5]}")
        if len(sidecars) != len(payloads):
            raise RuntimeError(f"sidecar/payload count mismatch in {cache}")
        for sidecar in sidecars:
            metadata = load_json(sidecar)
            missing = REQUIRED_SIDECAR_FIELDS - metadata.keys()
            if missing:
                raise RuntimeError(f"sidecar missing {sorted(missing)}: {sidecar}")
            payload = sidecar.with_suffix(".npy")
            if not payload.is_file() or payload.is_symlink():
                raise RuntimeError(f"payload missing or linked: {payload}")
            actual_bytes = payload.stat().st_size
            if actual_bytes != int(metadata["bytes"]):
                raise RuntimeError(f"payload byte count mismatch: {payload}")
            actual_hash = sha256_file(payload)
            if actual_hash != metadata["npy_sha256"]:
                raise RuntimeError(f"payload hash mismatch: {payload}")
            processed += 1
            total_bytes += actual_bytes
            hash_counts[actual_hash] += 1
            hash_first_size.setdefault(actual_hash, actual_bytes)
            inventory.append(
                {
                    "run": cache.parent.name,
                    "candidate_id": metadata["candidate_id"],
                    "formula": metadata["formula"],
                    "panel_fingerprint": metadata["panel_fingerprint"],
                    "engine_fingerprint": metadata["engine_fingerprint"],
                    "npy_path": str(payload),
                    "sidecar_path": str(sidecar),
                    "bytes": actual_bytes,
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
    inventory_path = receipt_root / "factor-cache-inventory.jsonl"
    atomic_jsonl(inventory_path, inventory)
    summary = {
        "schema_version": 1,
        "status": "PRECHECK_PASS",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "factory_root": str(factory_root.resolve()),
        "targets": [str(path) for path in targets],
        "matrix_count": processed,
        "sidecar_count": processed,
        "total_bytes": total_bytes,
        "total_gib": round(total_bytes / (1024 ** 3), 6),
        "unique_hashes": len(hash_counts),
        "unique_bytes": unique_bytes,
        "duplicate_copy_bytes": total_bytes - unique_bytes,
        "duplicate_hash_groups": sum(1 for count in hash_counts.values() if count > 1),
        "inventory": str(inventory_path),
        "inventory_sha256": sha256_file(inventory_path),
        "research_evidence": evidence,
    }
    atomic_json(receipt_root / "precheck-summary.json", summary)
    print(
        "PRECHECK_PASS "
        f"targets={len(targets)} matrices={processed} total_gib={summary['total_gib']} "
        f"inventory_sha256={summary['inventory_sha256']}",
        flush=True,
    )
    return 0


def postcheck(factory_root: Path, receipt_root: Path) -> int:
    expected_cache_directories(factory_root, must_exist=False)
    precheck_path = receipt_root / "precheck-summary.json"
    inventory_path = receipt_root / "factor-cache-inventory.jsonl"
    precheck_summary = load_json(precheck_path)
    if precheck_summary.get("status") != "PRECHECK_PASS":
        raise RuntimeError("precheck summary is not PASS")
    inventory_hash = sha256_file(inventory_path)
    if inventory_hash != precheck_summary.get("inventory_sha256"):
        raise RuntimeError("cleanup inventory hash changed after precheck")
    evidence = verify_research_evidence(factory_root)
    summary = {
        "schema_version": 1,
        "status": "POSTCHECK_PASS",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "factory_root": str(factory_root.resolve()),
        "removed_targets": precheck_summary["targets"],
        "removed_matrix_count": precheck_summary["matrix_count"],
        "removed_bytes": precheck_summary["total_bytes"],
        "removed_gib": precheck_summary["total_gib"],
        "inventory": str(inventory_path),
        "inventory_sha256": inventory_hash,
        "research_evidence": evidence,
    }
    atomic_json(receipt_root / "postcheck-summary.json", summary)
    print(
        "POSTCHECK_PASS "
        f"removed_targets={len(summary['removed_targets'])} "
        f"removed_matrices={summary['removed_matrix_count']} "
        f"removed_gib={summary['removed_gib']}",
        flush=True,
    )
    return 0


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Audit multi-factor cache cleanup")
    parser.add_argument("mode", choices=("precheck", "postcheck"))
    parser.add_argument(
        "--factory-root",
        type=Path,
        default=project_root / "research_runs" / "factor_factory",
    )
    parser.add_argument("--receipt-root", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.mode == "precheck":
            return precheck(args.factory_root.resolve(), args.receipt_root.resolve())
        return postcheck(args.factory_root.resolve(), args.receipt_root.resolve())
    except Exception as exc:  # noqa: BLE001 - audit command must fail closed.
        print(f"AUDIT_FAIL: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
