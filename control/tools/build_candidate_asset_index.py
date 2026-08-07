#!/usr/bin/env python3
"""Build or verify the bounded central index for quarantine candidates.

The index inventories only evidence JSON files below ``data/market/quarantine``.
It never parses planned datasets, reads business payloads, changes the formal
dataset catalog, promotes quality, or publishes a research snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[2]
QUARANTINE_ROOT = ROOT / "data/market/quarantine"
INDEX_PATH = ROOT / "data/catalog/candidate_assets.json"
CONTRACT_PATH = ROOT / "data/catalog/contracts/candidate_asset_index_v1/contract.json"
EVIDENCE_MARKERS = ("manifest", "receipt", "verification", "result", "checkpoint")


class CandidateIndexError(RuntimeError):
    """The candidate index cannot be built safely."""


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def relative(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def evidence_type(path: Path) -> str:
    name = path.name.lower()
    for marker in EVIDENCE_MARKERS:
        if marker in name:
            return marker.upper()
    raise CandidateIndexError(f"not an evidence file: {path}")


def is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def iter_evidence(root: Path) -> Iterable[Path]:
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        safe_directories: list[str] = []
        for name in sorted(directories):
            candidate = current_path / name
            if is_reparse_point(candidate):
                raise CandidateIndexError(f"symlink/reparse directory is forbidden: {candidate}")
            safe_directories.append(name)
        directories[:] = safe_directories
        for name in sorted(files):
            path = current_path / name
            if is_reparse_point(path):
                raise CandidateIndexError(f"symlink evidence is forbidden: {path}")
            lower = name.lower()
            if lower.endswith(".json") and any(marker in lower for marker in EVIDENCE_MARKERS):
                yield path


def load_contract() -> dict[str, Any]:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("contract_id") != "candidate_asset_index_v1":
        raise CandidateIndexError("unexpected candidate asset index contract")
    if contract.get("scan_root") != "data/market/quarantine":
        raise CandidateIndexError("candidate asset index scan root is not bounded")
    safety = contract.get("safety", {})
    if safety.get("formal_catalog_write") is not False or safety.get("quality_promotion") is not False:
        raise CandidateIndexError("candidate asset index contract is not fail-closed")
    return contract


def build_index(quarantine_root: Path = QUARANTINE_ROOT) -> dict[str, Any]:
    load_contract()
    if not quarantine_root.is_dir():
        raise CandidateIndexError(f"quarantine root missing: {quarantine_root}")
    roots: list[dict[str, Any]] = []
    all_evidence: list[dict[str, Any]] = []
    for candidate_root in sorted((item for item in quarantine_root.iterdir() if item.is_dir()), key=lambda item: item.name.lower()):
        if is_reparse_point(candidate_root):
            raise CandidateIndexError(f"top-level candidate root is a symlink: {candidate_root}")
        evidence: list[dict[str, Any]] = []
        for path in iter_evidence(candidate_root):
            item = {
                "path": relative(path),
                "kind": evidence_type(path),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            evidence.append(item)
            all_evidence.append(item)
        if not evidence:
            raise CandidateIndexError(f"candidate root has no manifest/receipt/verification evidence: {candidate_root}")
        evidence.sort(key=lambda item: item["path"])
        fingerprint = hashlib.sha256(canonical_bytes(evidence)).hexdigest()
        roots.append({
            "candidate_root": relative(candidate_root),
            "evidence_count": len(evidence),
            "manifest_count": sum(item["kind"] == "MANIFEST" for item in evidence),
            "fingerprint_sha256": fingerprint,
            "evidence": evidence,
        })
    all_evidence.sort(key=lambda item: item["path"])
    return {
        "schema_version": 1,
        "index_id": "candidate-assets-central-index-v1",
        "scan_root": relative(quarantine_root),
        "candidate_root_count": len(roots),
        "evidence_file_count": len(all_evidence),
        "manifest_file_count": sum(item["kind"] == "MANIFEST" for item in all_evidence),
        "content_fingerprint_sha256": hashlib.sha256(canonical_bytes(roots)).hexdigest(),
        "purpose_ceiling": "INSPECTION_ONLY",
        "quality_status": "MIXED_CANDIDATES__NO_PROMOTION",
        "formal_effect": "NONE",
        "formal_catalog_write": False,
        "candidate_roots": roots,
    }


def validate_index(index: dict[str, Any]) -> None:
    required = {
        "schema_version", "index_id", "scan_root", "candidate_root_count",
        "evidence_file_count", "manifest_file_count", "content_fingerprint_sha256",
        "purpose_ceiling", "formal_effect", "formal_catalog_write", "candidate_roots",
    }
    missing = sorted(required - set(index))
    if missing:
        raise CandidateIndexError(f"index missing fields: {missing}")
    if index["purpose_ceiling"] != "INSPECTION_ONLY" or index["formal_effect"] != "NONE":
        raise CandidateIndexError("candidate index exceeds its purpose ceiling")
    if index["formal_catalog_write"] is not False:
        raise CandidateIndexError("candidate index claims a formal catalog write")
    roots = index["candidate_roots"]
    if not isinstance(roots, list) or len(roots) != index["candidate_root_count"]:
        raise CandidateIndexError("candidate root count mismatch")
    evidence_paths: list[str] = []
    for root in roots:
        evidence = root.get("evidence")
        if not isinstance(evidence, list) or len(evidence) != root.get("evidence_count"):
            raise CandidateIndexError(f"evidence count mismatch: {root.get('candidate_root')}")
        evidence_paths.extend(str(item.get("path")) for item in evidence)
    if len(evidence_paths) != index["evidence_file_count"] or len(evidence_paths) != len(set(evidence_paths)):
        raise CandidateIndexError("evidence paths are missing or duplicated")


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_index() -> dict[str, Any]:
    index = build_index()
    validate_index(index)
    payload = canonical_bytes(index)
    atomic_write(INDEX_PATH, payload)
    return {
        "status": "PASS__CANDIDATE_ASSET_INDEX_WRITTEN",
        "index_path": relative(INDEX_PATH),
        "candidate_root_count": index["candidate_root_count"],
        "evidence_file_count": index["evidence_file_count"],
        "manifest_file_count": index["manifest_file_count"],
        "index_sha256": hashlib.sha256(payload).hexdigest(),
        "formal_effect": "NONE",
    }


def check_index() -> dict[str, Any]:
    if not INDEX_PATH.is_file():
        raise CandidateIndexError(f"candidate index missing: {INDEX_PATH}")
    current = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    expected = build_index()
    validate_index(current)
    if canonical_bytes(current) != canonical_bytes(expected):
        raise CandidateIndexError("candidate asset index is stale")
    return {
        "status": "PASS__CANDIDATE_ASSET_INDEX_CURRENT",
        "index_path": relative(INDEX_PATH),
        "candidate_root_count": current["candidate_root_count"],
        "evidence_file_count": current["evidence_file_count"],
        "manifest_file_count": current["manifest_file_count"],
        "index_sha256": sha256_file(INDEX_PATH),
        "formal_effect": "NONE",
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = write_index() if args.write else check_index()
    except Exception as exc:
        print(json.dumps({"status": "FAIL_CLOSED", "error": {"type": type(exc).__name__, "message": str(exc)}}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
