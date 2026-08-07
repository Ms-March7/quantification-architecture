#!/usr/bin/env python3
"""Audit the local CSMAR 33-package closure without admitting research data.

The audit is deliberately separate from collection, parsing, candidate
construction, source admission, catalog registration, and quality promotion.
It reads only the already sealed intake, the explicitly enumerated local
candidates, and their immutable evidence.  The default mode performs the full
audit without creating a directory or writing a file.  ``--execute`` writes a
deterministic evidence bundle only below::

    reports/data-governance/csmar-33pkg-local-closure-20260729/

The evidence bundle covers:

* all 33 ZIP packages and all 25 observed logical tables;
* the exact source-field lists and candidate keys;
* 32 candidate runs plus the one overlap-calibration package;
* CORE-01 through CORE-20 coverage and remaining rights/schema/PIT/revision/
  quality gaps;
* ordinary versus bank/insurance/securities financial-statement overlap;
* FIN_AuditStats/FIN_Audit/FIN_AuditDetail reconciliation; and
* the hash chain from each sealed audit-text ZIP through the reversible repair
  manifest and sidecar into the authoritative Parquet candidate.

Nothing in this audit changes the mandatory ``INSPECTION_ONLY/NOT_ADMITTED``
status.  A PASS means that the local evidence is internally reproducible under
the checks in this file; it is not source admission or research readiness.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import uuid
import zipfile
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

try:
    import duckdb  # type: ignore
except ImportError:  # pragma: no cover - exercised by the explicit runtime gate
    duckdb = None


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOOL_ID = "audit_csmar_33pkg_local_closure.py/csmar-33pkg-closure-v1"
SCHEMA_VERSION = 1
AS_OF_DATE = "2026-07-29"
PURPOSE_CEILING = "INSPECTION_ONLY"
ADMISSION_DECISION = "NOT_ADMITTED"
HASH_ALGORITHM = "sha256-local-download-candidate-v1"

REPORT_RELATIVE = Path(
    "reports/data-governance/csmar-33pkg-local-closure-20260729"
)
INTAKE_RELATIVE = Path(
    "data/market/quarantine/csmar-manual-export/intake-20260729-33pkg-v1"
)
INTAKE_MANIFEST_RELATIVE = INTAKE_RELATIVE / "INTAKE_MANIFEST.json"
SOURCE_DELETION_RECEIPT_RELATIVE = (
    INTAKE_RELATIVE / "SOURCE_DELETION_RECEIPT.json"
)
FORMAL_CATALOG_RELATIVE = Path("data/catalog/datasets.json")
PLANNED_CATALOG_RELATIVE = Path("data/catalog/planned_datasets.json")
EXPECTED_FORMAL_CATALOG_SHA256 = (
    "debdbea74d3597dab66efc850517d3ab8ed6062a58229a61303556351883be00"
)
FORMAL_GATE_TARGETS = {
    "CORE": ("G_DATA_CORE", 20),
    "GCTX": ("G_DATA_GLOBAL_CONTEXT", 8),
    "HK": ("G_DATA_HK_EQUITY", 4),
    "GLEQ": ("G_DATA_GLOBAL_EQUITY", 3),
}
OVERLAP_RECEIPT_RELATIVE = Path(
    "tmp/csmar_pilot/d2a04b-inbox/"
    "d2a04b-csmar-trd-dalyr-20210729-20260728-"
    "overlap-quarantine-20260729-v1/inspection-receipt.json"
)

REPORT_FILENAMES = (
    "README.md",
    "CLOSURE_AUDIT.json",
    "SCHEMA_INVENTORY.json",
    "HASH_BINDINGS.json",
    "VERIFICATION.json",
    "REPORT_MANIFEST.json",
)

# These are the only non-audit-text candidates in the approved 33-package
# batch.  Explicit paths prevent a mutable "latest" lookup or an accidental
# scan of unrelated tmp candidates.
BASE_CANDIDATE_RELATIVES = (
    "csmar-trd-co-pilot/d2a04b-csmar-trd-co-full-current-20260728-v1",
    "csmar-trd-cptl-pilot/d2a04b-csmar-trd-cptl-full-1990-2026-20260729-v1",
    "csmar-fs-comins/d2a04b-csmar-fs-comins-full-history-20260729-v1",
    "csmar-trd-nolimit/d2a04b-csmar-trd-nolimit-full-1990-2026-20260729-v1",
    "csmar-trd-dalyr-pilot/d2a04b-csmar-trd-dalyr-1990-1994-20260729-v1",
    "csmar-trd-dalyr-pilot/d2a04b-csmar-trd-dalyr-1995-1999-20260729-v1",
    "csmar-trd-dalyr-pilot/d2a04b-csmar-trd-dalyr-2000-2004-20260729-v1",
    "csmar-trd-dalyr-pilot/d2a04b-csmar-trd-dalyr-2005-2009-20260729-v1",
    "csmar-trd-dalyr-pilot/d2a04b-csmar-trd-dalyr-2010-2014-20260729-v1",
    "csmar-trd-dalyr-pilot/d2a04b-csmar-trd-dalyr-2015-2019-20260728-v1",
    "csmar-trd-dalyr-pilot/d2a02-2020-2024-20260728-v1",
    "csmar-trd-dalyr-pilot/d2a04b-csmar-trd-dalyr-2022-2026-20260728-v1",
    "csmar-trd-cale-pilot/d2a04b-csmar-trd-cale-full-1990-2026-20260728-v1",
    "csmar-fs-comscfd/d2a04b-csmar-fs-comscfd-full-history-20260729-v1",
    "csmar-trd-capchg/d2a04b-csmar-trd-capchg-full-history-20260729-v1",
    "csmar-trd-adjustfactor/d2a04b-csmar-trd-adjustfactor-full-1991-2026-20260729-v1",
    "csmar-fs-combas/d2a04b-csmar-fs-combas-full-history-20260729-v1",
    "csmar-fin-auditstats/csmar-33pkg-csmar-fin-auditstats-20260729-v1",
    "csmar-fs-bcombas/csmar-33pkg-csmar-fs-bcombas-20260729-v1",
    "csmar-fs-bcomins/csmar-33pkg-csmar-fs-bcomins-20260729-v1",
    "csmar-fs-bcomscfd/csmar-33pkg-csmar-fs-bcomscfd-20260729-v1",
    "csmar-fs-bcomscfi/csmar-33pkg-csmar-fs-bcomscfi-20260729-v1",
    "csmar-fs-icombas/csmar-33pkg-csmar-fs-icombas-20260729-v1",
    "csmar-fs-icomins/csmar-33pkg-csmar-fs-icomins-20260729-v1",
    "csmar-fs-icomscfd/csmar-33pkg-csmar-fs-icomscfd-20260729-v1",
    "csmar-fs-icomscfi/csmar-33pkg-csmar-fs-icomscfi-20260729-v1",
    "csmar-fs-scombas/csmar-33pkg-csmar-fs-scombas-20260729-v1",
    "csmar-fs-scomins/csmar-33pkg-csmar-fs-scomins-20260729-v1",
    "csmar-fs-scomscfd/csmar-33pkg-csmar-fs-scomscfd-20260729-v1",
    "csmar-fs-scomscfi/csmar-33pkg-csmar-fs-scomscfi-20260729-v1",
)

# The v1 Parquet candidates are intentionally retained, but their content
# hashes do not bind the reversible repair sidecars.  Authoritative v2 paths
# are updated in this constant only after the provenance-ZIP candidates exist.
AUDIT_TEXT_BINDINGS: dict[str, dict[str, str]] = {
    "FIN_Audit": {
        "repair_candidate": (
            "tmp/csmar-audit-text-candidate/csmar-fin-audit/"
            "csmar-33pkg-fin-audit-text-closure-20260729-v2"
        ),
        "authoritative_candidate": (
            "tmp/local-dataset-build/csmar-fin-audit/"
            "csmar-33pkg-fin-audit-text-closure-20260729-v2-parquet-v1"
        ),
        "superseded_candidate": (
            "tmp/local-dataset-build/csmar-fin-audit/"
            "csmar-33pkg-fin-audit-text-closure-20260729-v1-parquet-v1"
        ),
    },
    "FIN_AuditDetail": {
        "repair_candidate": (
            "tmp/csmar-audit-text-candidate/csmar-fin-auditdetail/"
            "csmar-33pkg-fin-auditdetail-text-closure-20260729-v2"
        ),
        "authoritative_candidate": (
            "tmp/local-dataset-build/csmar-fin-auditdetail/"
            "csmar-33pkg-fin-auditdetail-text-closure-20260729-v2-parquet-v1"
        ),
        "superseded_candidate": (
            "tmp/local-dataset-build/csmar-fin-auditdetail/"
            "csmar-33pkg-fin-auditdetail-text-closure-20260729-v1-parquet-v1"
        ),
    },
}

FINANCIAL_DATASET_GROUPS = {
    "combas": {
        "ordinary": "csmar-fs-combas",
        "sector": (
            "csmar-fs-bcombas",
            "csmar-fs-icombas",
            "csmar-fs-scombas",
        ),
    },
    "comins": {
        "ordinary": "csmar-fs-comins",
        "sector": (
            "csmar-fs-bcomins",
            "csmar-fs-icomins",
            "csmar-fs-scomins",
        ),
    },
    "comscfd": {
        "ordinary": "csmar-fs-comscfd",
        "sector": (
            "csmar-fs-bcomscfd",
            "csmar-fs-icomscfd",
            "csmar-fs-scomscfd",
        ),
    },
}
SECTOR_FINANCIAL_DATASETS = tuple(
    f"csmar-fs-{prefix}{suffix}"
    for prefix in ("b", "i", "s")
    for suffix in ("combas", "comins", "comscfd", "comscfi")
)
ALL_FINANCIAL_DATASETS = (
    "csmar-fs-combas",
    "csmar-fs-comins",
    "csmar-fs-comscfd",
    *SECTOR_FINANCIAL_DATASETS,
)


class ClosureAuditError(RuntimeError):
    """Fail-closed error for missing, changed, or inconsistent evidence."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ClosureAuditError(f"required JSON evidence is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClosureAuditError(f"cannot read JSON evidence {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ClosureAuditError(f"JSON evidence must be an object: {path}")
    return value


def is_below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def relative_path(path: Path, workspace_root: Path) -> str:
    return path.resolve().relative_to(workspace_root.resolve()).as_posix()


def derive_formal_counts(workspace_root: Path) -> dict[str, Any]:
    """Derive current planned-gate intersections from the two catalog authorities."""
    workspace = workspace_root.resolve()
    formal_path = workspace / FORMAL_CATALOG_RELATIVE
    planned_path = workspace / PLANNED_CATALOG_RELATIVE
    formal = load_json(formal_path)
    planned = load_json(planned_path)
    formal_rows = formal.get("datasets")
    planned_rows = planned.get("datasets")
    if not isinstance(formal_rows, list) or not all(
        isinstance(row, dict) for row in formal_rows
    ):
        raise ClosureAuditError("formal catalog datasets must be an object array")
    if not isinstance(planned_rows, list) or not all(
        isinstance(row, dict) for row in planned_rows
    ):
        raise ClosureAuditError("planned catalog datasets must be an object array")

    formal_ids = {
        str(row.get("dataset_id", ""))
        for row in formal_rows
        if str(row.get("dataset_id", ""))
    }
    counts: dict[str, Any] = {}
    for label, (gate, expected_target) in FORMAL_GATE_TARGETS.items():
        gate_rows = [row for row in planned_rows if row.get("gate") == gate]
        if len(gate_rows) != expected_target:
            raise ClosureAuditError(
                f"planned gate target drifted: {gate} "
                f"expected={expected_target} actual={len(gate_rows)}"
            )
        matched_rows = [
            row for row in gate_rows if str(row.get("dataset_id", "")) in formal_ids
        ]
        matched_plan_ids = sorted(
            str(row.get("plan_id", "")) for row in matched_rows
        )
        matched_dataset_ids = sorted(
            str(row.get("dataset_id", "")) for row in matched_rows
        )
        count = len(matched_rows)
        counts[label] = {
            "count": count,
            "target": expected_target,
            "display": f"{count}/{expected_target}",
            "gate": gate,
            "plan_ids": matched_plan_ids,
            "dataset_ids": matched_dataset_ids,
        }
    return {
        "formal_catalog_path": relative_path(formal_path, workspace),
        "formal_catalog_sha256": sha256_file(formal_path),
        "planned_catalog_path": relative_path(planned_path, workspace),
        "planned_catalog_sha256": sha256_file(planned_path),
        "active_formal_dataset_count": len(formal_rows),
        "counts": counts,
    }


def sql_literal(value: str) -> str:
    return value.replace("'", "''")


def require_duckdb() -> Any:
    if duckdb is None:
        raise ClosureAuditError(
            "DuckDB is required; use control/runtime/data-engineering/.venv"
        )
    return duckdb


def report_root_for(workspace_root: Path) -> Path:
    return (workspace_root.resolve() / REPORT_RELATIVE).resolve()


def validate_report_root(
    workspace_root: Path,
    report_root: Path,
) -> Path:
    workspace = workspace_root.resolve()
    expected = report_root_for(workspace)
    resolved = report_root.resolve()
    reports_root = (workspace / "reports").resolve()
    if resolved != expected or not is_below(resolved, reports_root):
        raise ClosureAuditError(
            "report output must be the fixed csmar-33pkg local-closure directory"
        )
    current = workspace
    for component in REPORT_RELATIVE.parts:
        current = current / component
        if current.exists() and current.is_symlink():
            raise ClosureAuditError(f"report path contains a symlink: {current}")
    return resolved


def logical_table_name(member_name: str) -> str:
    stem = PurePosixPath(member_name).stem
    if re.fullmatch(r"TRD_Dalyr\d*", stem):
        return "TRD_Dalyr"
    return stem


def inspect_zip_schema(path: Path) -> dict[str, Any]:
    before = path.stat()
    csv_members: list[dict[str, Any]] = []
    total_unpacked = 0
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        total_unpacked = sum(item.file_size for item in members)
        for item in members:
            if not item.filename.casefold().endswith(".csv"):
                continue
            with archive.open(item) as raw:
                text = io.TextIOWrapper(
                    raw,
                    encoding="utf-8-sig",
                    errors="strict",
                    newline="",
                )
                reader = csv.reader(text, strict=True)
                try:
                    header = next(reader)
                except (csv.Error, StopIteration, UnicodeError) as exc:
                    raise ClosureAuditError(
                        f"cannot read CSV header from {path}!{item.filename}: {exc}"
                    ) from exc
            csv_members.append(
                {
                    "member_name": item.filename,
                    "logical_table": logical_table_name(item.filename),
                    "fields": header,
                    "field_count": len(header),
                    "compressed_bytes": item.compress_size,
                    "uncompressed_bytes": item.file_size,
                    "crc32": f"{item.CRC:08x}",
                }
            )
    after = path.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise ClosureAuditError(f"sealed ZIP changed while it was read: {path}")
    if not csv_members:
        raise ClosureAuditError(f"sealed ZIP has no CSV member: {path}")
    tables = sorted({item["logical_table"] for item in csv_members})
    if len(tables) != 1:
        raise ClosureAuditError(
            f"one package unexpectedly contains multiple logical tables: {path}"
        )
    headers = {tuple(item["fields"]) for item in csv_members}
    if len(headers) != 1:
        raise ClosureAuditError(f"split CSV headers differ inside {path}")
    return {
        "archive_member_count": len(members),
        "csv_member_count": len(csv_members),
        "total_unpacked_bytes": total_unpacked,
        "logical_table": tables[0],
        "fields": list(next(iter(headers))),
        "field_count": len(next(iter(headers))),
        "csv_members": csv_members,
    }


def audit_intake(workspace_root: Path) -> dict[str, Any]:
    intake_root = workspace_root / INTAKE_RELATIVE
    raw_root = intake_root / "raw"
    manifest_path = workspace_root / INTAKE_MANIFEST_RELATIVE
    deletion_path = workspace_root / SOURCE_DELETION_RECEIPT_RELATIVE
    manifest = load_json(manifest_path)
    deletion = load_json(deletion_path)
    declared = manifest.get("packages")
    if not isinstance(declared, list):
        raise ClosureAuditError("intake manifest packages must be a list")
    actual_paths = sorted(raw_root.glob("*.zip"), key=lambda path: path.name)
    if manifest.get("package_count") != 33 or len(declared) != 33:
        raise ClosureAuditError("intake manifest does not declare exactly 33 packages")
    if len(actual_paths) != 33:
        raise ClosureAuditError(f"sealed intake contains {len(actual_paths)} ZIPs, not 33")
    if any(path.is_symlink() for path in actual_paths):
        raise ClosureAuditError("sealed intake must not contain symlink ZIPs")

    declared_by_sha: dict[str, dict[str, Any]] = {}
    for entry in declared:
        if not isinstance(entry, dict):
            raise ClosureAuditError("intake package entry must be an object")
        digest = str(entry.get("sha256", "")).casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ClosureAuditError("intake package has an invalid SHA-256")
        if digest in declared_by_sha:
            raise ClosureAuditError("intake package SHA-256 values are not unique")
        declared_by_sha[digest] = entry

    packages: list[dict[str, Any]] = []
    actual_by_sha: dict[str, dict[str, Any]] = {}
    for path in actual_paths:
        digest = sha256_file(path)
        if digest not in declared_by_sha:
            raise ClosureAuditError(f"sealed ZIP is absent from intake manifest: {path}")
        entry = declared_by_sha[digest]
        if path.stat().st_size != int(entry.get("bytes", -1)):
            raise ClosureAuditError(f"sealed ZIP size differs from manifest: {path}")
        schema = inspect_zip_schema(path)
        record = {
            "filename": path.name,
            "path": relative_path(path, workspace_root),
            "sha256": digest,
            "bytes": path.stat().st_size,
            "source_drive": entry.get("source_drive"),
            "manifest_crc_read_status": entry.get("zip_crc_read_status"),
            **schema,
        }
        packages.append(record)
        actual_by_sha[digest] = record

    if set(actual_by_sha) != set(declared_by_sha):
        raise ClosureAuditError("sealed ZIP and intake-manifest SHA sets differ")
    total_bytes = sum(item["bytes"] for item in packages)
    if total_bytes != int(manifest.get("total_bytes", -1)):
        raise ClosureAuditError("sealed package bytes do not equal intake manifest total")

    table_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for package in packages:
        table_groups[package["logical_table"]].append(package)
    if len(table_groups) != 25:
        raise ClosureAuditError(
            f"sealed intake exposes {len(table_groups)} logical tables, not 25"
        )
    for table, entries in table_groups.items():
        headers = {tuple(entry["fields"]) for entry in entries}
        if len(headers) != 1:
            raise ClosureAuditError(f"source fields differ across {table} packages")

    return {
        "run_id": manifest.get("run_id"),
        "path": relative_path(intake_root, workspace_root),
        "manifest_path": relative_path(manifest_path, workspace_root),
        "manifest_sha256": sha256_file(manifest_path),
        "source_deletion_receipt_path": relative_path(
            deletion_path, workspace_root
        ),
        "source_deletion_receipt_sha256": sha256_file(deletion_path),
        "package_count": len(packages),
        "logical_table_count": len(table_groups),
        "total_bytes": total_bytes,
        "package_shas_unique": len(actual_by_sha) == len(packages),
        "packages": packages,
        "packages_by_sha": actual_by_sha,
        "deletion_receipt": {
            "status": deletion.get("status"),
            "deleted_source_count": deletion.get("deleted_source_count"),
            "deleted_source_bytes": deletion.get("deleted_source_bytes"),
        },
    }


def candidate_contract_hash(candidate_root: Path) -> str:
    request = load_json(candidate_root / "request.json")
    manifest = load_json(candidate_root / "manifest.json")
    provenance = load_json(candidate_root / "provenance.json")
    partitions = load_json(candidate_root / "partition-manifest.json")
    quality = load_json(candidate_root / "quality" / "summary.json")
    manifest_without_hash = dict(manifest)
    manifest_without_hash.pop("content_hash", None)
    return sha256_bytes(
        canonical_bytes(
            {
                "hash_algorithm": HASH_ALGORITHM,
                "request": request,
                "manifest": manifest_without_hash,
                "provenance": provenance,
                "partition_manifest": partitions,
                "quality": quality,
            }
        )
    )


def verify_payload_inventory(
    candidate_root: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    entries = manifest.get("payload_inventory")
    if not isinstance(entries, list) or not entries:
        raise ClosureAuditError(
            f"candidate payload inventory is absent: {candidate_root}"
        )
    verified_bytes = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ClosureAuditError("payload inventory entry must be an object")
        relative = Path(str(entry.get("relative_path", "")))
        path = (candidate_root / relative).resolve()
        if not is_below(path, candidate_root.resolve()) or not path.is_file():
            raise ClosureAuditError(f"candidate payload path is unsafe/missing: {path}")
        if path.is_symlink():
            raise ClosureAuditError(f"candidate payload is a symlink: {path}")
        expected_bytes = int(entry.get("bytes", -1))
        if path.stat().st_size != expected_bytes:
            raise ClosureAuditError(f"candidate payload size mismatch: {path}")
        expected_hash = str(entry.get("sha256", "")).casefold()
        actual_hash = sha256_file(path)
        if actual_hash != expected_hash:
            raise ClosureAuditError(f"candidate payload hash mismatch: {path}")
        verified_bytes += expected_bytes
    return {
        "payload_count": len(entries),
        "payload_bytes": verified_bytes,
        "all_payload_hashes_match": True,
    }


def parquet_glob(candidate_root: Path) -> str:
    return (candidate_root / "curated" / "**" / "*.parquet").as_posix()


def query_candidate_row_count(candidate_root: Path) -> int:
    engine = require_duckdb()
    connection = engine.connect(":memory:")
    try:
        value = connection.execute(
            "SELECT COUNT(*) FROM read_parquet(?)",
            [parquet_glob(candidate_root)],
        ).fetchone()
        if value is None:
            raise ClosureAuditError(f"cannot count candidate rows: {candidate_root}")
        return int(value[0])
    finally:
        connection.close()


def source_zip_sha_for_candidate(
    table_name: str,
    provenance: dict[str, Any],
    workspace_root: Path,
) -> str:
    if table_name in AUDIT_TEXT_BINDINGS:
        repair_root = workspace_root / AUDIT_TEXT_BINDINGS[table_name][
            "repair_candidate"
        ]
        repair_manifest = load_json(repair_root / "manifest.json")
        return str(repair_manifest["source"]["zip_sha256"]).casefold()
    packages = provenance.get("input_packages")
    if not isinstance(packages, list) or len(packages) != 1:
        raise ClosureAuditError("candidate must have exactly one input package")
    digest = str(packages[0].get("sha256", "")).casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ClosureAuditError("candidate provenance input SHA-256 is invalid")
    return digest


def authoritative_candidate_relatives() -> tuple[str, ...]:
    audit_paths = tuple(
        str(Path(value["authoritative_candidate"]).relative_to("tmp/local-dataset-build"))
        for value in AUDIT_TEXT_BINDINGS.values()
    )
    return (*BASE_CANDIDATE_RELATIVES, *audit_paths)


def audit_candidates(
    workspace_root: Path,
    intake: dict[str, Any],
) -> dict[str, Any]:
    candidates_root = workspace_root / "tmp" / "local-dataset-build"
    package_by_sha = intake["packages_by_sha"]
    records: list[dict[str, Any]] = []
    by_dataset_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    package_candidates: dict[str, list[str]] = defaultdict(list)

    relatives = authoritative_candidate_relatives()
    if len(relatives) != 32 or len(set(relatives)) != 32:
        raise ClosureAuditError("authoritative candidate path list must contain 32 runs")

    for candidate_relative in relatives:
        candidate_root = (candidates_root / candidate_relative).resolve()
        if (
            not is_below(candidate_root, candidates_root.resolve())
            or not candidate_root.is_dir()
            or candidate_root.is_symlink()
        ):
            raise ClosureAuditError(
                f"authoritative candidate path is unsafe/missing: {candidate_root}"
            )
        request = load_json(candidate_root / "request.json")
        manifest = load_json(candidate_root / "manifest.json")
        provenance = load_json(candidate_root / "provenance.json")
        quality = load_json(candidate_root / "quality" / "summary.json")
        partitions = load_json(candidate_root / "partition-manifest.json")

        if manifest.get("purpose_ceiling") != PURPOSE_CEILING:
            raise ClosureAuditError(
                f"candidate purpose ceiling changed: {candidate_root}"
            )
        if manifest.get("catalog_registered") is not False:
            raise ClosureAuditError(f"candidate claims catalog registration: {candidate_root}")
        if manifest.get("source_admission") is not False:
            raise ClosureAuditError(f"candidate claims source admission: {candidate_root}")
        if manifest.get("research_ready") is not False:
            raise ClosureAuditError(f"candidate claims research readiness: {candidate_root}")
        if quality.get("admission_decision") != ADMISSION_DECISION:
            raise ClosureAuditError(
                f"candidate admission boundary changed: {candidate_root}"
            )
        if quality.get("mechanical_status") != "PASS":
            raise ClosureAuditError(f"candidate mechanical status is not PASS: {candidate_root}")

        content_hash = str(manifest.get("content_hash", "")).casefold()
        computed_content_hash = candidate_contract_hash(candidate_root)
        if content_hash != computed_content_hash:
            raise ClosureAuditError(
                f"candidate contract content hash mismatch: {candidate_root}"
            )
        payload = verify_payload_inventory(candidate_root, manifest)
        row_count = query_candidate_row_count(candidate_root)
        declared_rows = int(quality["metrics"]["row_count"])
        if row_count != declared_rows:
            raise ClosureAuditError(
                f"candidate Parquet row count mismatch: {candidate_root}"
            )
        actual_partitions = len(list((candidate_root / "curated").rglob("*.parquet")))
        declared_partitions = int(quality["metrics"]["partition_count"])
        if actual_partitions != declared_partitions:
            raise ClosureAuditError(
                f"candidate Parquet partition count mismatch: {candidate_root}"
            )

        dataset_id = str(manifest.get("dataset_id"))
        table_config = request.get("table")
        if not isinstance(table_config, dict):
            raise ClosureAuditError(f"candidate request table is absent: {candidate_root}")
        expected_fields = table_config.get("expected_columns")
        if not isinstance(expected_fields, list) or not expected_fields:
            raise ClosureAuditError(
                f"candidate request has no expected columns: {candidate_root}"
            )

        # The source table is resolved through the sealed ZIP SHA, never from a
        # mutable directory name.
        provisional_table = (
            "FIN_Audit"
            if dataset_id == "csmar-fin-audit"
            else "FIN_AuditDetail"
            if dataset_id == "csmar-fin-auditdetail"
            else ""
        )
        source_sha = source_zip_sha_for_candidate(
            provisional_table, provenance, workspace_root
        )
        if source_sha not in package_by_sha:
            raise ClosureAuditError(
                f"candidate source ZIP is outside the sealed 33-package intake: {candidate_root}"
            )
        package = package_by_sha[source_sha]
        table_name = str(package["logical_table"])
        if expected_fields != package["fields"]:
            raise ClosureAuditError(
                f"candidate/source header mismatch for {candidate_root}"
            )

        record = {
            "candidate_id": manifest.get("candidate_id"),
            "dataset_id": dataset_id,
            "run_id": manifest.get("run_id"),
            "path": relative_path(candidate_root, workspace_root),
            "logical_table": table_name,
            "source_zip_sha256": source_sha,
            "source_zip_filename": package["filename"],
            "field_count": len(expected_fields),
            "fields": expected_fields,
            "key_columns": table_config.get("key_columns"),
            "date_column": table_config.get("date_column"),
            "row_count": row_count,
            "date_min": quality["metrics"].get("date_min"),
            "date_max": quality["metrics"].get("date_max"),
            "partition_count": actual_partitions,
            "content_hash": content_hash,
            "manifest_sha256": sha256_file(candidate_root / "manifest.json"),
            "content_hash_recomputed": computed_content_hash,
            "content_hash_matches": True,
            "payload_verification": payload,
            "purpose_ceiling": manifest.get("purpose_ceiling"),
            "admission_decision": quality.get("admission_decision"),
            "quality_status": quality.get("quality_status"),
            "catalog_registered": manifest.get("catalog_registered"),
            "research_ready": manifest.get("research_ready"),
            "supersedes_local_candidate": None,
        }
        if table_name in AUDIT_TEXT_BINDINGS:
            superseded = workspace_root / AUDIT_TEXT_BINDINGS[table_name][
                "superseded_candidate"
            ]
            if superseded.resolve() != candidate_root:
                record["supersedes_local_candidate"] = relative_path(
                    superseded, workspace_root
                )
        records.append(record)
        by_dataset_id[dataset_id].append(record)
        package_candidates[source_sha].append(str(manifest.get("candidate_id")))

    if len(records) != 32:
        raise ClosureAuditError("candidate audit did not resolve exactly 32 runs")
    if len(package_candidates) != 32:
        raise ClosureAuditError(
            "32 candidate runs must bind to 32 distinct sealed ZIP packages"
        )
    if any(len(value) != 1 for value in package_candidates.values()):
        raise ClosureAuditError("one sealed ZIP maps to multiple candidate runs")

    return {
        "candidate_count": len(records),
        "records": records,
        "by_dataset_id": dict(by_dataset_id),
        "package_candidates": dict(package_candidates),
    }


def audit_overlap_package(
    workspace_root: Path,
    intake: dict[str, Any],
    candidates: dict[str, Any],
) -> dict[str, Any]:
    receipt_path = workspace_root / OVERLAP_RECEIPT_RELATIVE
    receipt = load_json(receipt_path)
    digest = str(receipt.get("sha256", "")).casefold()
    package_by_sha = intake["packages_by_sha"]
    if digest not in package_by_sha:
        raise ClosureAuditError("overlap package is absent from the sealed intake")
    if digest in candidates["package_candidates"]:
        raise ClosureAuditError("overlap calibration package must not build a candidate")
    leftover = set(package_by_sha) - set(candidates["package_candidates"])
    if leftover != {digest}:
        raise ClosureAuditError("candidate/overlap classification does not cover 33 ZIPs")
    csv_audit = receipt.get("csv_audit")
    overlap = receipt.get("overlap_validation")
    assertions = receipt.get("admission_assertions")
    if not isinstance(csv_audit, dict) or not isinstance(overlap, dict):
        raise ClosureAuditError("overlap receipt is incomplete")
    if not isinstance(assertions, dict):
        raise ClosureAuditError("overlap admission boundary is absent")
    if any(
        assertions.get(key) is not False
        for key in (
            "candidate_built_from_this_archive",
            "catalog_registered",
            "source_admitted",
            "quality_promoted",
            "research_ready",
        )
    ):
        raise ClosureAuditError("overlap receipt claims an unauthorized promotion")
    comparisons = overlap.get("comparisons")
    if not isinstance(comparisons, list) or len(comparisons) != 2:
        raise ClosureAuditError("overlap receipt must contain two candidate comparisons")
    if any(int(item.get("typed_row_fingerprint_mismatch", -1)) != 0 for item in comparisons):
        raise ClosureAuditError("overlap calibration contains a typed-field mismatch")
    crosscheck = overlap.get("candidate_pair_crosscheck")
    if (
        not isinstance(crosscheck, dict)
        or int(crosscheck.get("typed_row_fingerprint_mismatch", -1)) != 0
    ):
        raise ClosureAuditError("candidate-pair overlap crosscheck failed")
    return {
        "path": relative_path(receipt_path, workspace_root),
        "receipt_sha256": sha256_file(receipt_path),
        "source_zip_sha256": digest,
        "source_zip_filename": package_by_sha[digest]["filename"],
        "logical_table": package_by_sha[digest]["logical_table"],
        "candidate_built": False,
        "row_count": int(csv_audit["row_count"]),
        "security_count": int(csv_audit["security_count"]),
        "trading_date_count": int(csv_audit["trading_date_count"]),
        "date_min": csv_audit["min_date"],
        "date_max": csv_audit["max_date"],
        "fields": csv_audit["fields"],
        "field_count": int(csv_audit["field_count"]),
        "typed_fingerprint_mismatch_count": 0,
        "purpose_ceiling": PURPOSE_CEILING,
        "admission_decision": ADMISSION_DECISION,
    }


def build_schema_inventory(
    intake: dict[str, Any],
    candidates: dict[str, Any],
    overlap: dict[str, Any],
) -> dict[str, Any]:
    table_groups: dict[str, dict[str, Any]] = {}
    candidates_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates["records"]:
        candidates_by_table[candidate["logical_table"]].append(candidate)
    for package in intake["packages"]:
        table = package["logical_table"]
        if table not in table_groups:
            table_groups[table] = {
                "logical_table": table,
                "field_count": package["field_count"],
                "fields": package["fields"],
                "packages": [],
                "candidate_runs": [],
                "key_candidates": [],
                "date_columns": [],
            }
        table_groups[table]["packages"].append(
            {
                "filename": package["filename"],
                "sha256": package["sha256"],
                "bytes": package["bytes"],
                "csv_members": [
                    member["member_name"] for member in package["csv_members"]
                ],
            }
        )
    for table, records in candidates_by_table.items():
        group = table_groups[table]
        for record in records:
            group["candidate_runs"].append(
                {
                    "candidate_id": record["candidate_id"],
                    "path": record["path"],
                    "row_count": record["row_count"],
                    "date_min": record["date_min"],
                    "date_max": record["date_max"],
                    "content_hash": record["content_hash"],
                }
            )
            keys = record.get("key_columns")
            if keys and keys not in group["key_candidates"]:
                group["key_candidates"].append(keys)
            date_column = record.get("date_column")
            if date_column and date_column not in group["date_columns"]:
                group["date_columns"].append(date_column)
    table_groups["TRD_Dalyr"]["overlap_calibration"] = {
        "source_zip_sha256": overlap["source_zip_sha256"],
        "row_count": overlap["row_count"],
        "date_min": overlap["date_min"],
        "date_max": overlap["date_max"],
        "candidate_built": False,
    }
    tables = [table_groups[name] for name in sorted(table_groups)]
    observed_fields = [
        field for table in tables for field in table["fields"]
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of_date": AS_OF_DATE,
        "package_count": intake["package_count"],
        "logical_table_count": len(table_groups),
        "table_field_occurrence_count": len(observed_fields),
        "unique_observed_field_name_count_case_sensitive": len(
            set(observed_fields)
        ),
        "unique_observed_field_name_count_casefold": len(
            {field.casefold() for field in observed_fields}
        ),
        "tables": tables,
    }


def dataset_record(
    candidates: dict[str, Any],
    dataset_id: str,
) -> dict[str, Any]:
    records = candidates["by_dataset_id"].get(dataset_id)
    if not isinstance(records, list) or len(records) != 1:
        raise ClosureAuditError(
            f"expected one authoritative candidate for {dataset_id}"
        )
    return records[0]


def financial_overlap_audit(
    workspace_root: Path,
    candidates: dict[str, Any],
) -> dict[str, Any]:
    engine = require_duckdb()
    connection = engine.connect(":memory:")
    try:
        statements: list[dict[str, Any]] = []
        for statement, group in FINANCIAL_DATASET_GROUPS.items():
            ordinary = dataset_record(candidates, group["ordinary"])
            sectors = [
                dataset_record(candidates, dataset_id)
                for dataset_id in group["sector"]
            ]
            sector_union = " UNION ALL ".join(
                (
                    "SELECT Stkcd, EndDate AS period, Typrep, "
                    f"'{sql_literal(record['dataset_id'])}' AS source_dataset "
                    "FROM read_parquet("
                    f"'{sql_literal(parquet_glob((workspace_root / record['path']).resolve()))}'"
                    ")"
                )
                for record in sectors
            )
            ordinary_glob = sql_literal(
                parquet_glob((workspace_root / ordinary["path"]).resolve())
            )
            query = f"""
                WITH ordinary_keys AS (
                    SELECT DISTINCT Stkcd, Accper AS period, Typrep
                    FROM read_parquet('{ordinary_glob}')
                ),
                sector_rows AS ({sector_union}),
                sector_keys AS (
                    SELECT DISTINCT Stkcd, period, Typrep FROM sector_rows
                )
                SELECT
                    (SELECT COUNT(*) FROM ordinary_keys),
                    (SELECT COUNT(*) FROM sector_keys),
                    (
                        SELECT COUNT(*) FROM ordinary_keys
                        JOIN sector_keys USING (Stkcd, period, Typrep)
                    ),
                    (
                        SELECT COUNT(*) FROM sector_keys
                        LEFT JOIN ordinary_keys USING (Stkcd, period, Typrep)
                        WHERE ordinary_keys.Stkcd IS NULL
                    ),
                    (
                        SELECT COUNT(DISTINCT Stkcd) FROM ordinary_keys
                        JOIN sector_keys USING (Stkcd)
                    ),
                    (
                        SELECT COUNT(*) FROM (
                            SELECT Stkcd, period, Typrep, COUNT(*) AS copies
                            FROM sector_rows
                            GROUP BY 1, 2, 3
                            HAVING COUNT(*) > 1
                        )
                    )
            """
            result = connection.execute(query).fetchone()
            if result is None:
                raise ClosureAuditError("financial overlap query returned no row")
            statements.append(
                {
                    "statement": statement,
                    "ordinary_dataset": ordinary["dataset_id"],
                    "sector_datasets": [record["dataset_id"] for record in sectors],
                    "ordinary_key_count": int(result[0]),
                    "sector_key_count": int(result[1]),
                    "overlap_key_count": int(result[2]),
                    "sector_only_key_count": int(result[3]),
                    "overlap_security_count": int(result[4]),
                    "cross_sector_duplicate_key_count": int(result[5]),
                    "interpretation": (
                        "Sector tables add industry-specific fields for the same "
                        "issuer-period-report-scope keys; they are not additive rows."
                    ),
                }
            )

        balance_records = [
            dataset_record(candidates, dataset_id)
            for dataset_id in (
                "csmar-fs-bcombas",
                "csmar-fs-icombas",
                "csmar-fs-scombas",
            )
        ]
        b_glob, i_glob, s_glob = [
            sql_literal(
                parquet_glob((workspace_root / record["path"]).resolve())
            )
            for record in balance_records
        ]
        cross_query = f"""
            WITH b AS (
                SELECT DISTINCT Stkcd, EndDate, Typrep
                FROM read_parquet('{b_glob}')
            ), i AS (
                SELECT DISTINCT Stkcd, EndDate, Typrep
                FROM read_parquet('{i_glob}')
            ), s AS (
                SELECT DISTINCT Stkcd, EndDate, Typrep
                FROM read_parquet('{s_glob}')
            )
            SELECT
                (SELECT COUNT(DISTINCT b.Stkcd) FROM b JOIN i USING (Stkcd)),
                (SELECT COUNT(DISTINCT b.Stkcd) FROM b JOIN s USING (Stkcd)),
                (SELECT COUNT(DISTINCT i.Stkcd) FROM i JOIN s USING (Stkcd)),
                (
                    SELECT COUNT(*) FROM b JOIN i
                    USING (Stkcd, EndDate, Typrep)
                ),
                (
                    SELECT COUNT(*) FROM b JOIN s
                    USING (Stkcd, EndDate, Typrep)
                ),
                (
                    SELECT COUNT(*) FROM i JOIN s
                    USING (Stkcd, EndDate, Typrep)
                )
        """
        cross = connection.execute(cross_query).fetchone()
        if cross is None:
            raise ClosureAuditError("cross-sector overlap query returned no row")

        pit_diagnostics: list[dict[str, Any]] = []
        for dataset_id in ALL_FINANCIAL_DATASETS:
            record = dataset_record(candidates, dataset_id)
            glob = sql_literal(
                parquet_glob((workspace_root / record["path"]).resolve())
            )
            opening_expression = (
                "COUNT(*) FILTER (WHERE month(EndDate)=1 AND day(EndDate)=1)"
                if dataset_id in SECTOR_FINANCIAL_DATASETS
                else "0"
            )
            query = f"""
                SELECT
                    COUNT(*),
                    COUNT(*) FILTER (
                        WHERE CAST(IfCorrect AS VARCHAR) = '1'
                    ),
                    COUNT(*) FILTER (
                        WHERE TRY_CAST(DeclareDate AS DATE) IS NOT NULL
                    ),
                    COUNT(*) FILTER (
                        WHERE CAST(IfCorrect AS VARCHAR) = '0'
                          AND TRY_CAST(DeclareDate AS DATE) IS NOT NULL
                    ),
                    COUNT(*) FILTER (
                        WHERE CAST(IfCorrect AS VARCHAR) = '1'
                          AND TRY_CAST(DeclareDate AS DATE) IS NULL
                    ),
                    STRING_AGG(
                        DISTINCT CAST(Typrep AS VARCHAR),
                        ',' ORDER BY CAST(Typrep AS VARCHAR)
                    ),
                    {opening_expression}
                FROM read_parquet('{glob}')
            """
            values = connection.execute(query).fetchone()
            if values is None:
                raise ClosureAuditError("financial PIT query returned no row")
            pit_diagnostics.append(
                {
                    "dataset_id": dataset_id,
                    "row_count": int(values[0]),
                    "ifcorrect_1_count": int(values[1]),
                    "declare_date_nonnull_count": int(values[2]),
                    "base_rows_with_declare_date": int(values[3]),
                    "corrected_rows_without_declare_date": int(values[4]),
                    "typrep_values": str(values[5]).split(","),
                    "january_1_comparison_rows": int(values[6]),
                    "initial_available_at_present": False,
                }
            )

        return {
            "statement_overlap": statements,
            "sector_cross_overlap": {
                "bank_insurance_security_overlap_securities": {
                    "bank_insurance": int(cross[0]),
                    "bank_securities": int(cross[1]),
                    "insurance_securities": int(cross[2]),
                },
                "bank_insurance_security_overlap_keys": {
                    "bank_insurance": int(cross[3]),
                    "bank_securities": int(cross[4]),
                    "insurance_securities": int(cross[5]),
                },
            },
            "pit_diagnostics": pit_diagnostics,
            "pit_conclusion": (
                "DeclareDate is present exactly on correction rows and absent on "
                "initial rows; Accper/EndDate is a report period, not available_at."
            ),
        }
    finally:
        connection.close()


def audit_three_table_reconciliation(
    workspace_root: Path,
    candidates: dict[str, Any],
) -> dict[str, Any]:
    engine = require_duckdb()
    stats = dataset_record(candidates, "csmar-fin-auditstats")
    audit = dataset_record(candidates, "csmar-fin-audit")
    detail = dataset_record(candidates, "csmar-fin-auditdetail")
    stats_glob = sql_literal(
        parquet_glob((workspace_root / stats["path"]).resolve())
    )
    audit_glob = sql_literal(
        parquet_glob((workspace_root / audit["path"]).resolve())
    )
    detail_glob = sql_literal(
        parquet_glob((workspace_root / detail["path"]).resolve())
    )
    connection = engine.connect(":memory:")
    try:
        query = f"""
            WITH stats AS (
                SELECT Stkcd, Accper, TRY_CAST(AuditNo AS BIGINT) AS AuditNo
                FROM read_parquet('{stats_glob}')
            ), audit AS (
                SELECT Stkcd, Accper
                FROM read_parquet('{audit_glob}')
            ), detail AS (
                SELECT Stkcd, Accper, COUNT(*) AS detail_count
                FROM read_parquet('{detail_glob}')
                GROUP BY 1, 2
            )
            SELECT
                (SELECT COUNT(*) FROM stats),
                (SELECT COUNT(*) FROM audit),
                (SELECT COUNT(*) FROM read_parquet('{detail_glob}')),
                (
                    SELECT COUNT(*) FROM stats
                    LEFT JOIN detail USING (Stkcd, Accper)
                    WHERE detail.Stkcd IS NULL
                ),
                (
                    SELECT COUNT(*) FROM detail
                    LEFT JOIN stats USING (Stkcd, Accper)
                    WHERE stats.Stkcd IS NULL
                ),
                (
                    SELECT COUNT(*) FROM stats
                    JOIN detail USING (Stkcd, Accper)
                    WHERE stats.AuditNo <> detail.detail_count
                ),
                (
                    SELECT COALESCE(
                        SUM(ABS(stats.AuditNo - detail.detail_count)), 0
                    ) FROM stats
                    JOIN detail USING (Stkcd, Accper)
                    WHERE stats.AuditNo <> detail.detail_count
                ),
                (
                    SELECT COUNT(*) FROM stats
                    LEFT JOIN audit USING (Stkcd, Accper)
                    WHERE audit.Stkcd IS NULL
                ),
                (
                    SELECT COUNT(*) FROM audit
                    LEFT JOIN stats USING (Stkcd, Accper)
                    WHERE stats.Stkcd IS NULL
                ),
                (
                    SELECT COUNT(*) - COUNT(
                        DISTINCT Stkcd || '|' || CAST(Accper AS VARCHAR)
                    ) FROM audit
                )
        """
        values = connection.execute(query).fetchone()
        if values is None:
            raise ClosureAuditError("audit three-table query returned no row")
        missing_query = f"""
            SELECT stats.Stkcd, stats.Accper
            FROM read_parquet('{stats_glob}') AS stats
            LEFT JOIN read_parquet('{audit_glob}') AS audit
            USING (Stkcd, Accper)
            WHERE audit.Stkcd IS NULL
            ORDER BY stats.Stkcd, stats.Accper
        """
        missing = [
            {"Stkcd": str(row[0]), "Accper": row[1].isoformat()}
            for row in connection.execute(missing_query).fetchall()
        ]
        return {
            "stats_rows": int(values[0]),
            "audit_rows": int(values[1]),
            "detail_rows": int(values[2]),
            "stats_without_detail_keys": int(values[3]),
            "detail_without_stats_keys": int(values[4]),
            "auditno_detail_count_mismatch_keys": int(values[5]),
            "auditno_detail_count_absolute_difference": int(values[6]),
            "stats_without_audit_keys": int(values[7]),
            "audit_keys_outside_stats_periods": int(values[8]),
            "audit_duplicate_stkcd_accper_keys": int(values[9]),
            "stats_without_audit_examples": missing,
            "join_keys": {
                "stats_to_detail": ["Stkcd", "Accper"],
                "detail_business_key": ["Stkcd", "Accper", "ItemNo"],
                "audit_candidate_key": ["Stkcd", "Accper"],
            },
        }
    finally:
        connection.close()


def verify_audit_provenance_bundle(
    *,
    table_name: str,
    repair_root: Path,
    repair_manifest: dict[str, Any],
    authoritative_root: Path,
    authoritative_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Verify that the downstream raw ZIP binds every v2 repair artifact."""

    output = repair_manifest.get("outputs", {}).get("provenance_zip")
    if not isinstance(output, dict):
        raise ClosureAuditError(
            f"v2 repair candidate has no provenance ZIP: {table_name}"
        )
    source_bundle = (repair_root / str(output.get("relative_path", ""))).resolve()
    if not is_below(source_bundle, repair_root) or not source_bundle.is_file():
        raise ClosureAuditError(f"unsafe/missing provenance ZIP: {table_name}")
    expected_bundle_hash = str(output.get("sha256", "")).casefold()
    if sha256_file(source_bundle) != expected_bundle_hash:
        raise ClosureAuditError(f"repair provenance ZIP hash mismatch: {table_name}")
    if source_bundle.stat().st_size != int(output.get("bytes", -1)):
        raise ClosureAuditError(f"repair provenance ZIP size mismatch: {table_name}")

    raw_payloads = [
        item
        for item in authoritative_manifest.get("payload_inventory", [])
        if item.get("layer") == "raw"
    ]
    matching = [
        item
        for item in raw_payloads
        if str(item.get("sha256", "")).casefold() == expected_bundle_hash
    ]
    if len(raw_payloads) != 1 or len(matching) != 1:
        raise ClosureAuditError(
            f"authoritative candidate must retain exactly the v2 provenance ZIP: {table_name}"
        )
    raw_entry = matching[0]
    downstream_bundle = (
        authoritative_root / str(raw_entry.get("relative_path", ""))
    ).resolve()
    if (
        not is_below(downstream_bundle, authoritative_root)
        or not downstream_bundle.is_file()
        or downstream_bundle.is_symlink()
    ):
        raise ClosureAuditError(
            f"authoritative raw provenance ZIP is unsafe/missing: {table_name}"
        )
    if (
        downstream_bundle.stat().st_size != int(raw_entry.get("bytes", -1))
        or sha256_file(downstream_bundle) != expected_bundle_hash
    ):
        raise ClosureAuditError(
            f"authoritative raw provenance ZIP differs from v2 repair evidence: {table_name}"
        )

    declared_members = output.get("members")
    if not isinstance(declared_members, list) or len(declared_members) != 5:
        raise ClosureAuditError(
            f"v2 provenance ZIP must declare exactly five members: {table_name}"
        )
    declared_by_name = {
        str(item.get("member")): item
        for item in declared_members
        if isinstance(item, dict)
    }
    if len(declared_by_name) != 5:
        raise ClosureAuditError(
            f"v2 provenance ZIP member names are invalid/duplicated: {table_name}"
        )
    with zipfile.ZipFile(downstream_bundle) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or set(names) != set(declared_by_name):
            raise ClosureAuditError(
                f"v2 provenance ZIP members differ from manifest: {table_name}"
            )
        member_hashes: dict[str, str] = {}
        for info in infos:
            if (
                info.is_dir()
                or info.filename.startswith("/")
                or ".." in PurePosixPath(info.filename).parts
            ):
                raise ClosureAuditError(
                    f"unsafe provenance ZIP member: {table_name}!{info.filename}"
                )
            payload = archive.read(info)
            digest = sha256_bytes(payload)
            declared = declared_by_name[info.filename]
            if (
                len(payload) != int(declared.get("bytes", -1))
                or digest != str(declared.get("sha256", "")).casefold()
            ):
                raise ClosureAuditError(
                    f"provenance ZIP member hash/size mismatch: {table_name}!{info.filename}"
                )
            member_hashes[info.filename] = digest

    local_bindings: dict[str, dict[str, Any]] = {}
    for output_key in (
        "parser_manifest",
        "quality_summary",
        "repair_sidecar",
        "repaired_csv",
        "upstream_provenance_receipt",
    ):
        artifact = repair_manifest.get("outputs", {}).get(output_key)
        if not isinstance(artifact, dict):
            raise ClosureAuditError(
                f"v2 repair output is missing: {table_name}/{output_key}"
            )
        local_path = (
            repair_root / str(artifact.get("relative_path", ""))
        ).resolve()
        if not is_below(local_path, repair_root) or not local_path.is_file():
            raise ClosureAuditError(
                f"v2 repair output path is unsafe/missing: {table_name}/{output_key}"
            )
        digest = sha256_file(local_path)
        member_name = str(artifact.get("package_member", ""))
        if (
            digest != str(artifact.get("sha256", "")).casefold()
            or member_hashes.get(member_name) != digest
        ):
            raise ClosureAuditError(
                f"local repair output is not byte-bound in provenance ZIP: {table_name}/{output_key}"
            )
        local_bindings[output_key] = {
            "absolute_path": local_path.as_posix(),
            "package_member": member_name,
            "bytes": local_path.stat().st_size,
            "sha256": digest,
        }

    return {
        "format": output.get("format"),
        "repair_bundle_path": source_bundle.as_posix(),
        "authoritative_raw_path": downstream_bundle.as_posix(),
        "sha256": expected_bundle_hash,
        "bytes": source_bundle.stat().st_size,
        "member_count": len(member_hashes),
        "member_hashes": dict(sorted(member_hashes.items())),
        "local_artifact_bindings": local_bindings,
        "bound_by_candidate_content_hash": True,
    }


def audit_text_hash_bindings(
    workspace_root: Path,
    candidates: dict[str, Any],
    intake: dict[str, Any],
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for table_name, paths in AUDIT_TEXT_BINDINGS.items():
        repair_root = (workspace_root / paths["repair_candidate"]).resolve()
        authoritative_root = (
            workspace_root / paths["authoritative_candidate"]
        ).resolve()
        superseded_root = (
            workspace_root / paths["superseded_candidate"]
        ).resolve()
        repair_manifest_path = repair_root / "manifest.json"
        repair_manifest = load_json(repair_manifest_path)
        repair_quality = load_json(repair_root / "quality" / "summary.json")
        authoritative_manifest_path = authoritative_root / "manifest.json"
        authoritative_manifest = load_json(authoritative_manifest_path)
        authoritative_quality = load_json(
            authoritative_root / "quality" / "summary.json"
        )

        sidecar = repair_root / repair_manifest["outputs"]["repair_sidecar"][
            "relative_path"
        ]
        repaired_csv = repair_root / repair_manifest["outputs"]["repaired_csv"][
            "relative_path"
        ]
        declared_sidecar_hash = str(
            repair_manifest["outputs"]["repair_sidecar"]["sha256"]
        ).casefold()
        actual_sidecar_hash = sha256_file(sidecar)
        declared_csv_hash = str(
            repair_manifest["outputs"]["repaired_csv"]["sha256"]
        ).casefold()
        actual_csv_hash = sha256_file(repaired_csv)
        if declared_sidecar_hash != actual_sidecar_hash:
            raise ClosureAuditError(f"repair sidecar hash mismatch for {table_name}")
        if declared_csv_hash != actual_csv_hash:
            raise ClosureAuditError(f"repaired CSV hash mismatch for {table_name}")

        provenance_bundle = verify_audit_provenance_bundle(
            table_name=table_name,
            repair_root=repair_root,
            repair_manifest=repair_manifest,
            authoritative_root=authoritative_root,
            authoritative_manifest=authoritative_manifest,
        )

        candidate = next(
            record
            for record in candidates["records"]
            if record["logical_table"] == table_name
        )
        source_zip_sha = str(repair_manifest["source"]["zip_sha256"]).casefold()
        if source_zip_sha not in intake["packages_by_sha"]:
            raise ClosureAuditError(
                f"repair manifest source ZIP is outside intake: {table_name}"
            )
        if int(repair_manifest["metrics"]["accounted_record_count"]) != int(
            authoritative_quality["metrics"]["row_count"]
        ):
            raise ClosureAuditError(
                f"repair/Parquet row count differs for {table_name}"
            )
        entries.append(
            {
                "logical_table": table_name,
                "source_zip": {
                    "path": intake["packages_by_sha"][source_zip_sha]["path"],
                    "sha256": source_zip_sha,
                    "member_sha256": repair_manifest["source"][
                        "csv_member_sha256"
                    ],
                },
                "repair_candidate": {
                    "path": relative_path(repair_root, workspace_root),
                    "manifest_path": relative_path(
                        repair_manifest_path, workspace_root
                    ),
                    "manifest_sha256": sha256_file(repair_manifest_path),
                    "parser_manifest_sha256": provenance_bundle[
                        "local_artifact_bindings"
                    ]["parser_manifest"]["sha256"],
                    "sidecar_path": relative_path(sidecar, workspace_root),
                    "sidecar_sha256": actual_sidecar_hash,
                    "sidecar_rows": repair_manifest["outputs"][
                        "repair_sidecar"
                    ]["row_count"],
                    "repaired_csv_path": relative_path(
                        repaired_csv, workspace_root
                    ),
                    "repaired_csv_sha256": actual_csv_hash,
                    "accounted_record_count": repair_manifest["metrics"][
                        "accounted_record_count"
                    ],
                    "strict_valid_record_count": repair_manifest["metrics"][
                        "strict_valid_record_count"
                    ],
                    "repaired_record_count": repair_manifest["metrics"][
                        "repaired_record_count"
                    ],
                    "semantic_review_record_count": repair_manifest["metrics"][
                        "semantic_review_record_count"
                    ],
                    "mechanical_status": repair_quality["mechanical_status"],
                },
                "authoritative_parquet_candidate": {
                    "path": relative_path(authoritative_root, workspace_root),
                    "manifest_path": relative_path(
                        authoritative_manifest_path, workspace_root
                    ),
                    "manifest_sha256": sha256_file(
                        authoritative_manifest_path
                    ),
                    "content_hash": authoritative_manifest["content_hash"],
                    "row_count": authoritative_quality["metrics"]["row_count"],
                    "provenance_bundle": provenance_bundle,
                    "repair_evidence_bound_through_raw_provenance_zip": True,
                    "candidate_id": candidate["candidate_id"],
                },
                "superseded_v1_candidate": {
                    "path": relative_path(superseded_root, workspace_root),
                    "retained": superseded_root.is_dir(),
                    "deleted": False,
                    "authority_status": (
                        "SUPERSEDED_BY_V2_PROVENANCE_BOUND_CANDIDATE"
                    ),
                },
                "binding_status": "PASS",
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "binding_count": len(entries),
        "entries": entries,
    }


def _legacy_core_mapping_mojibake() -> list[dict[str, Any]]:
    # Each item deliberately keeps the five admission dimensions separate.
    rows = [
        (
            "CORE-01",
            "证券身份主数据",
            "PARTIAL_CANDIDATE",
            ["TRD_Co"],
            "只有当前截面；缺永久身份、代码/名称/上市 episode 历史。",
        ),
        (
            "CORE-02",
            "交易日历与会话",
            "PARTIAL_CANDIDATE",
            ["TRD_Cale"],
            "有供应商逐日状态，无三交易所官方逐日会话和临时规则闭合。",
        ),
        (
            "CORE-03",
            "未复权日行情",
            "STRONG_CANDIDATE",
            ["TRD_Dalyr"],
            "36 年价量候选完整度高，但身份、单位、停牌/缺失语义和权利未闭合。",
        ),
        (
            "CORE-04",
            "公司行动",
            "PARTIAL_CANDIDATE",
            ["TRD_Cptl", "TRD_Capchg", "TRD_AdjustFactor"],
            "缺稳定 action_id、方案阶段、官方公告全文和撤回/更正链。",
        ),
        (
            "CORE-05",
            "前复权投影",
            "CALIBRATION_INPUT_ONLY",
            ["TRD_AdjustFactor", "TRD_Dalyr"],
            "供应商因子只能校准；缺因果 knowledge-cutoff 投影和公式版本。",
        ),
        (
            "CORE-06",
            "可交易性",
            "PARTIAL_CANDIDATE",
            ["TRD_Dalyr", "TRD_NoLimit", "TRD_Co"],
            "缺历史状态/涨跌停规则字典与盘前可用状态。",
        ),
        (
            "CORE-07",
            "股本",
            "PARTIAL_CANDIDATE",
            ["TRD_Capchg", "TRD_Dalyr"],
            "有变动事实但无公告时刻/修订链，未来生效记录不可提前使用。",
        ),
        (
            "CORE-08",
            "日估值",
            "DERIVATION_INPUT_ONLY",
            ["TRD_Dalyr", "TRD_Capchg", "FS_Combas", "FS_Comins"],
            "可派生部分市值/估值，缺口径版本和所有上游 PIT。",
        ),
        (
            "CORE-09",
            "指数主数据",
            "NO_SUPPORT",
            [],
            "33 包没有指数主表。",
        ),
        (
            "CORE-10",
            "指数成份权重 PIT",
            "NO_SUPPORT",
            [],
            "33 包没有历史成份和权重。",
        ),
        (
            "CORE-11",
            "行业分类 PIT",
            "WEAK_CURRENT_ONLY",
            ["TRD_Co", "FIN_AuditStats"],
            "只有当前/报告期行业码，不能把当前分类回填历史。",
        ),
        (
            "CORE-12",
            "指数日行情",
            "NO_SUPPORT",
            [],
            "33 包没有指数价格或收益变体。",
        ),
        (
            "CORE-13",
            "财务事实 PIT",
            "STRONG_CONTENT_PIT_BLOCKED",
            [
                "FS_Combas",
                "FS_Comins",
                "FS_Comscfd",
                "FS_BCombas",
                "FS_BComins",
                "FS_BComscfd",
                "FS_BComscfi",
                "FS_ICombas",
                "FS_IComins",
                "FS_IComscfd",
                "FS_IComscfi",
                "FS_SCombas",
                "FS_SComins",
                "FS_SComscfd",
                "FS_SComscfi",
            ],
            "内容广，但初始 available_at、旧值和完整修订链缺失。",
        ),
        (
            "CORE-14",
            "财务指标 PIT",
            "DERIVATION_INPUT_ONLY",
            ["CORE-13 inputs"],
            "缺分行业公式版本、同范围/同币种约束和 PIT 输入。",
        ),
        (
            "CORE-15",
            "业绩预告与快报 PIT",
            "NO_SUPPORT",
            [],
            "33 包没有业绩预告/快报表。",
        ),
        (
            "CORE-16",
            "审计意见与关键审计事项",
            "CONTENT_CANDIDATE_PIT_BLOCKED",
            ["FIN_Audit", "FIN_AuditStats", "FIN_AuditDetail"],
            "文本已零丢失候选化，但首次公告、报告 ID、意见 taxonomy 和修订链缺失。",
        ),
        (
            "CORE-17",
            "分红配股 PIT",
            "PARTIAL_CANDIDATE",
            ["TRD_Cptl"],
            "有分配事实，缺稳定方案 ID、完整阶段/税务/撤回链和公告时刻。",
        ),
        (
            "CORE-18",
            "主营分部 PIT",
            "NO_SUPPORT",
            [],
            "33 包没有分部业务表。",
        ),
        (
            "CORE-19",
            "基准与无风险利率",
            "NO_SUPPORT",
            [],
            "33 包没有无风险曲线或基准利率。",
        ),
        (
            "CORE-20",
            "风险描述子",
            "DERIVATION_INPUT_ONLY",
            ["TRD_Dalyr", "CORE-13 inputs", "TRD_Co"],
            "只有部分原料；缺正式收益、指数、PIT 行业、无风险和模型版本。",
        ),
    ]
    missing_support = {"CORE-09", "CORE-10", "CORE-12", "CORE-15", "CORE-18", "CORE-19"}
    result: list[dict[str, Any]] = []
    for core_id, name, support, tables, summary in rows:
        no_support = core_id in missing_support
        result.append(
            {
                "core_id": core_id,
                "name": name,
                "candidate_support": support,
                "logical_tables": tables,
                "formal_closure": False,
                "summary": summary,
                "gaps": {
                    "rights": (
                        "No package-specific independent proof of local retention, "
                        "internal sharing, or redistribution terms."
                        if not no_support
                        else "No source package and no admitted source-rights evidence."
                    ),
                    "schema": (
                        "Observed columns exist, but identifiers, enums, units, "
                        "taxonomies, and field semantics are not formally admitted."
                        if not no_support
                        else "Required logical schema has no source table in this batch."
                    ),
                    "pit": (
                        "Decision-time available_at and anti-lookahead behavior are "
                        "not closed."
                        if not no_support
                        else "No PIT-capable observations exist in this batch."
                    ),
                    "revision": (
                        "First publication, corrections, supersedes, withdrawals, and "
                        "version chain are incomplete."
                        if not no_support
                        else "No revision-chain evidence exists in this batch."
                    ),
                    "quality": (
                        "Mechanical candidate checks do not establish official "
                        "coverage, cross-source agreement, or research quality."
                        if not no_support
                        else "No candidate exists on which to run formal quality gates."
                    ),
                },
            }
        )
    return result


def core_mapping() -> list[dict[str, Any]]:
    """Return the explicit CORE-01..20 candidate-coverage matrix."""

    rows = [
        ("CORE-01", "security identity master", "PARTIAL_CANDIDATE", ["TRD_Co"],
         "Current cross-section only; permanent identity and listing-episode history remain absent."),
        ("CORE-02", "exchange calendar and sessions", "PARTIAL_CANDIDATE", ["TRD_Cale"],
         "Vendor day status exists, but official sessions and temporary rules are not closed."),
        ("CORE-03", "unadjusted daily market data", "STRONG_CANDIDATE", ["TRD_Dalyr"],
         "Thirty-six years of price-volume candidates are strong; identity, units, missingness and rights remain open."),
        ("CORE-04", "corporate actions", "PARTIAL_CANDIDATE",
         ["TRD_Cptl", "TRD_Capchg", "TRD_AdjustFactor"],
         "Stable action IDs, lifecycle stages, official notices and correction chains are missing."),
        ("CORE-05", "PIT adjusted-price projection", "CALIBRATION_INPUT_ONLY",
         ["TRD_AdjustFactor", "TRD_Dalyr"],
         "Vendor factors are calibration inputs; knowledge-cutoff projection and formula versions are absent."),
        ("CORE-06", "tradability", "PARTIAL_CANDIDATE",
         ["TRD_Dalyr", "TRD_NoLimit", "TRD_Co"],
         "Historical state, price-limit dictionaries and decision-time tradability remain incomplete."),
        ("CORE-07", "share capital", "PARTIAL_CANDIDATE", ["TRD_Capchg", "TRD_Dalyr"],
         "Capital changes exist without first-publication timestamps and complete revision chains."),
        ("CORE-08", "daily valuation", "DERIVATION_INPUT_ONLY",
         ["TRD_Dalyr", "TRD_Capchg", "FS_Combas", "FS_Comins"],
         "Some measures can be derived, but methodology versions and PIT inputs are not closed."),
        ("CORE-09", "index master", "NO_SUPPORT", [],
         "No index-master package is present in this intake."),
        ("CORE-10", "historical index membership and weights", "NO_SUPPORT", [],
         "No historical constituent or weight package is present."),
        ("CORE-11", "PIT industry classification", "WEAK_CURRENT_ONLY",
         ["TRD_Co", "FIN_AuditStats"],
         "Current or report-period industry codes cannot backfill historical PIT classifications."),
        ("CORE-12", "index daily market data", "NO_SUPPORT", [],
         "No index price or return-variant package is present."),
        ("CORE-13", "PIT financial facts", "STRONG_CONTENT_PIT_BLOCKED",
         [
             "FS_Combas", "FS_Comins", "FS_Comscfd",
             "FS_BCombas", "FS_BComins", "FS_BComscfd", "FS_BComscfi",
             "FS_ICombas", "FS_IComins", "FS_IComscfd", "FS_IComscfi",
             "FS_SCombas", "FS_SComins", "FS_SComscfd", "FS_SComscfi",
         ],
         "Content breadth is strong; initial available_at, prior values and full revision chains are absent."),
        ("CORE-14", "PIT financial indicators", "DERIVATION_INPUT_ONLY",
         ["CORE-13 inputs"],
         "Industry formulas, scope/currency constraints and PIT inputs are missing."),
        ("CORE-15", "PIT earnings forecasts and preliminary results", "NO_SUPPORT", [],
         "No forecast or preliminary-results package is present."),
        ("CORE-16", "audit opinions and key audit matters", "CONTENT_CANDIDATE_PIT_BLOCKED",
         ["FIN_Audit", "FIN_AuditStats", "FIN_AuditDetail"],
         "Text is losslessly candidate-built; publication time, report IDs, taxonomy and revisions are missing."),
        ("CORE-17", "PIT distributions and rights issues", "PARTIAL_CANDIDATE",
         ["TRD_Cptl"],
         "Facts exist without stable scheme IDs, full stages, tax/withdrawal chains or announcement times."),
        ("CORE-18", "PIT business segments", "NO_SUPPORT", [],
         "No business-segment package is present."),
        ("CORE-19", "benchmark and risk-free rates", "NO_SUPPORT", [],
         "No risk-free curve or benchmark-rate package is present."),
        ("CORE-20", "risk descriptors", "DERIVATION_INPUT_ONLY",
         ["TRD_Dalyr", "CORE-13 inputs", "TRD_Co"],
         "Only partial inputs exist; formal returns, index, PIT industry, risk-free rate and model versions are missing."),
    ]
    missing_support = {
        "CORE-09", "CORE-10", "CORE-12", "CORE-15", "CORE-18", "CORE-19"
    }
    result: list[dict[str, Any]] = []
    for core_id, name, support, tables, summary in rows:
        no_support = core_id in missing_support
        result.append(
            {
                "core_id": core_id,
                "name": name,
                "candidate_support": support,
                "logical_tables": tables,
                "formal_closure": False,
                "summary": summary,
                "gaps": {
                    "rights": (
                        "No package-specific proof of retention, internal sharing, or redistribution terms."
                        if not no_support
                        else "No source package and no admitted source-rights evidence."
                    ),
                    "schema": (
                        "Observed columns exist, but identifiers, enums, units, taxonomies and semantics are not admitted."
                        if not no_support
                        else "The required logical schema has no source table in this batch."
                    ),
                    "pit": (
                        "Decision-time available_at and anti-lookahead behavior are not closed."
                        if not no_support
                        else "No PIT-capable observations exist in this batch."
                    ),
                    "revision": (
                        "First publication, corrections, supersedes, withdrawals and version chain are incomplete."
                        if not no_support
                        else "No revision-chain evidence exists in this batch."
                    ),
                    "quality": (
                        "Mechanical checks do not establish official coverage, cross-source agreement or research quality."
                        if not no_support
                        else "No candidate exists on which to run formal quality gates."
                    ),
                },
            }
        )
    return result


def make_check(name: str, passed: bool, detail: Any) -> dict[str, Any]:
    return {"check": name, "status": "PASS" if passed else "FAIL", "detail": detail}


def build_audit(workspace_root: Path) -> dict[str, Any]:
    workspace = workspace_root.resolve()
    catalog_path = workspace / FORMAL_CATALOG_RELATIVE
    if not catalog_path.is_file():
        raise ClosureAuditError(f"formal catalog is missing: {catalog_path}")
    catalog_before = sha256_file(catalog_path)
    intake = audit_intake(workspace)
    candidates = audit_candidates(workspace, intake)
    overlap = audit_overlap_package(workspace, intake, candidates)
    schema = build_schema_inventory(intake, candidates, overlap)
    financial = financial_overlap_audit(workspace, candidates)
    audit_reconciliation = audit_three_table_reconciliation(
        workspace, candidates
    )
    hash_bindings = audit_text_hash_bindings(workspace, candidates, intake)
    catalog_after = sha256_file(catalog_path)
    if catalog_after != catalog_before:
        raise ClosureAuditError("formal catalog changed during the read-only audit")

    formal_count_evidence = derive_formal_counts(workspace)
    core = core_mapping()
    formal_core_plan_ids = set(
        formal_count_evidence["counts"]["CORE"]["plan_ids"]
    )
    for item in core:
        item["formal_closure"] = item["core_id"] in formal_core_plan_ids
    authoritative_v2 = all(
        value["authoritative_candidate"] != value["superseded_candidate"]
        for value in AUDIT_TEXT_BINDINGS.values()
    )
    repaired_counts = {
        entry["logical_table"]: {
            "accounted": entry["repair_candidate"]["accounted_record_count"],
            "repaired": entry["repair_candidate"]["repaired_record_count"],
            "semantic_review": entry["repair_candidate"][
                "semantic_review_record_count"
            ],
        }
        for entry in hash_bindings["entries"]
    }
    checks = [
        make_check("sealed_package_count", intake["package_count"] == 33, 33),
        make_check(
            "logical_table_count",
            schema["logical_table_count"] == 25,
            schema["logical_table_count"],
        ),
        make_check(
            "schema_field_name_counts",
            schema["table_field_occurrence_count"] == 1197
            and schema[
                "unique_observed_field_name_count_case_sensitive"
            ]
            == 559
            and schema["unique_observed_field_name_count_casefold"] == 474,
            {
                "table_field_occurrences": schema[
                    "table_field_occurrence_count"
                ],
                "unique_case_sensitive": schema[
                    "unique_observed_field_name_count_case_sensitive"
                ],
                "unique_casefold": schema[
                    "unique_observed_field_name_count_casefold"
                ],
            },
        ),
        make_check(
            "candidate_count",
            candidates["candidate_count"] == 32,
            candidates["candidate_count"],
        ),
        make_check(
            "candidate_plus_overlap_package_coverage",
            len(candidates["package_candidates"]) + 1 == 33,
            "32 candidate ZIPs + 1 overlap calibration ZIP",
        ),
        make_check(
            "formal_catalog_unchanged",
            catalog_before == catalog_after,
            catalog_before,
        ),
        make_check(
            "formal_catalog_matches_locked_pre_execution_baseline",
            catalog_before == EXPECTED_FORMAL_CATALOG_SHA256,
            {
                "expected": EXPECTED_FORMAL_CATALOG_SHA256,
                "actual": catalog_before,
            },
        ),
        make_check(
            "all_candidate_payload_and_contract_hashes",
            all(
                record["content_hash_matches"]
                and record["payload_verification"]["all_payload_hashes_match"]
                for record in candidates["records"]
            ),
            candidates["candidate_count"],
        ),
        make_check(
            "audit_full_preservation",
            repaired_counts.get("FIN_Audit")
            == {"accounted": 80301, "repaired": 238, "semantic_review": 1}
            and repaired_counts.get("FIN_AuditDetail")
            == {"accounted": 82264, "repaired": 103, "semantic_review": 0},
            repaired_counts,
        ),
        make_check(
            "auditstats_detail_reconciliation",
            audit_reconciliation["stats_without_detail_keys"] == 0
            and audit_reconciliation["detail_without_stats_keys"] == 0
            and audit_reconciliation["auditno_detail_count_mismatch_keys"] == 0,
            audit_reconciliation,
        ),
        make_check(
            "authoritative_v2_provenance_binding",
            authoritative_v2
            and all(
                entry["binding_status"] == "PASS"
                for entry in hash_bindings["entries"]
            ),
            "v1 retained; v2 must bind repair evidence and Parquet content",
        ),
        make_check(
            "formal_core_closure_remains_zero",
            formal_count_evidence["counts"]["CORE"]["count"] == 0
            and all(item["formal_closure"] is False for item in core),
            formal_count_evidence["counts"]["CORE"]["display"],
        ),
        make_check(
            "formal_required_gate_intersections_remain_zero",
            all(
                entry["count"] == 0
                for entry in formal_count_evidence["counts"].values()
            ),
            {
                label: entry["display"]
                for label, entry in formal_count_evidence["counts"].items()
            },
        ),
    ]
    failures = [check for check in checks if check["status"] != "PASS"]
    status = (
        "PASS__LOCAL_EVIDENCE_REPRODUCIBLE__FORMAL_ADMISSION_0"
        if not failures
        else "FAIL__LOCAL_CLOSURE_EVIDENCE_INCOMPLETE"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_id": TOOL_ID,
        "as_of_date": AS_OF_DATE,
        "status": status,
        "write_performed": False,
        "scope": {
            "network_access": False,
            "sealed_zip_modification": False,
            "candidate_modification": False,
            "catalog_write": False,
            "roadmap_write": False,
            "quality_promotion": False,
            "source_admission": False,
            "research_use": False,
            "purpose_ceiling": PURPOSE_CEILING,
            "admission_decision": ADMISSION_DECISION,
        },
        "formal_catalog": {
            "path": relative_path(catalog_path, workspace),
            "sha256_before": catalog_before,
            "sha256_after": catalog_after,
            "unchanged": catalog_before == catalog_after,
        },
        "formal_counts": {
            label: entry["display"]
            for label, entry in formal_count_evidence["counts"].items()
        },
        "formal_count_evidence": formal_count_evidence,
        "intake": {
            key: value
            for key, value in intake.items()
            if key not in {"packages_by_sha"}
        },
        "candidate_batch": {
            "candidate_count": candidates["candidate_count"],
            "records": candidates["records"],
        },
        "overlap_calibration": overlap,
        "schema_inventory": schema,
        "financial_statement_overlap": financial,
        "audit_three_table_reconciliation": audit_reconciliation,
        "audit_text_hash_bindings": hash_bindings,
        "core_mapping": core,
        "closure_summary": {
            "packages": "33/33",
            "logical_tables": "25/25",
            "candidate_packages": "32/32",
            "overlap_calibration_packages": "1/1",
            "core_items_touched": "14/20",
            "core_items_formally_closed": "0/20",
            "unsupported_core_items": [
                "CORE-09",
                "CORE-10",
                "CORE-12",
                "CORE-15",
                "CORE-18",
                "CORE-19",
            ],
            "remaining_gate": (
                "rights + formal schema/units/enums + PIT available_at + "
                "revision chains + official/cross-source quality"
            ),
        },
        "verification": {
            "checks": checks,
            "failure_count": len(failures),
        },
    }


def _legacy_render_readme_mojibake(audit: dict[str, Any]) -> str:
    closure = audit["closure_summary"]
    financial_rows = []
    for row in audit["financial_statement_overlap"]["statement_overlap"]:
        financial_rows.append(
            "| {statement} | {ordinary_key_count:,} | {sector_key_count:,} | "
            "{overlap_key_count:,} | {sector_only_key_count:,} |".format(**row)
        )
    core_rows = []
    for row in audit["core_mapping"]:
        tables = ", ".join(row["logical_tables"]) or "—"
        core_rows.append(
            f"| {row['core_id']} | {row['candidate_support']} | {tables} | "
            f"{row['summary']} |"
        )
    binding_rows = []
    for row in audit["audit_text_hash_bindings"]["entries"]:
        repair = row["repair_candidate"]
        parquet = row["authoritative_parquet_candidate"]
        binding_rows.append(
            f"| {row['logical_table']} | {repair['accounted_record_count']:,} | "
            f"{repair['repaired_record_count']:,} | "
            f"{repair['semantic_review_record_count']:,} | "
            f"`{repair['manifest_sha256']}` | `{repair['sidecar_sha256']}` | "
            f"`{parquet['content_hash']}` |"
        )
    return f"""# CSMAR 33 包本地第一阶段闭合审计

日期：{AS_OF_DATE}

状态：`{audit["status"]}`

## 结论

- 封存包：{closure["packages"]}，逻辑表：{closure["logical_tables"]}。
- 正式候选映射：{closure["candidate_packages"]}；另有
  {closure["overlap_calibration_packages"]} 重叠校准包，不进入事实并集。
- 33 包触达 CORE 14/20，但正式闭合仍为
  **{closure["core_items_formally_closed"]}**；GCTX/HK/GLEQ 也没有新增正式快照。
- 本报告只证明本地证据链可复核，不证明来源权利、字段正式语义、PIT、
  修订链、跨源质量或研究可用性。
- 全部输入和候选继续固定为
  `INSPECTION_ONLY/NOT_ADMITTED`；正式 catalog SHA-256 前后均为
  `{audit["formal_catalog"]["sha256_before"]}`。

## 审计文本零丢失与哈希绑定

| 表 | 全保全记录 | 修复记录 | 语义复核 | 中间 manifest SHA-256 | sidecar SHA-256 | 权威 Parquet content_hash |
|---|---:|---:|---:|---|---|---|
{os.linesep.join(binding_rows)}

v1 Parquet 候选保留且未删除，但其权威地位由绑定 provenance ZIP、修复
manifest、逐记录 sidecar 与修复 CSV 的 v2 候选取代。

三表勾稽结果：

- `FIN_AuditStats`：{audit["audit_three_table_reconciliation"]["stats_rows"]:,} 行；
- `FIN_Audit`：{audit["audit_three_table_reconciliation"]["audit_rows"]:,} 行；
- `FIN_AuditDetail`：{audit["audit_three_table_reconciliation"]["detail_rows"]:,} 行；
- Stats/Detail 两向孤儿均为
  {audit["audit_three_table_reconciliation"]["stats_without_detail_keys"]}；
- `AuditNo` 与明细数不一致键为
  {audit["audit_three_table_reconciliation"]["auditno_detail_count_mismatch_keys"]}；
- Stats 中有
  {audit["audit_three_table_reconciliation"]["stats_without_audit_keys"]}
  个键暂未连接 `FIN_Audit`，作为语义/覆盖缺口保留，不填补、不删除。

## 普通与金融专表重叠

| 报表 | 普通表键 | 金融专表键 | 重叠键 | 专表独有键 |
|---|---:|---:|---:|---:|
{os.linesep.join(financial_rows)}

金融专表是同一发行人—期末—报表范围键的行业专用字段模板，不是新增证券
或新增报告期。不得与普通表直接纵向拼接并重复计数；两套原观察均应保留，
差异必须显式校准。

所有普通/金融报表中 `DeclareDate` 只出现在 `IfCorrect=1` 的修订行，
初始行没有首次 `available_at`。`Accper/EndDate` 是报告期，不是可见时间；
财务表不能直接进入可靠 PIT 因子。

## CORE-01～20

| CORE | 本批候选支持 | 相关逻辑表 | 仍未闭合 |
|---|---|---|---|
{os.linesep.join(core_rows)}

每项的 rights/schema/PIT/revision/quality 五类缺口见
`CLOSURE_AUDIT.json` 的 `core_mapping`。

## 证据文件

- `CLOSURE_AUDIT.json`：完整机器可读审计。
- `SCHEMA_INVENTORY.json`：33 包、25 逻辑表和全部源字段。
- `HASH_BINDINGS.json`：审计文本从 sealed ZIP 到 v2 Parquet 的哈希链。
- `VERIFICATION.json`：闭合不变量和失败计数。
- `REPORT_MANIFEST.json`：上述报告文件的 SHA-256 清单。

## 强制边界

本工具没有联网、没有改 sealed ZIP、没有改 `tmp` 候选、没有写
`data/catalog/datasets.json`、没有改 Roadmap、没有来源准入、没有质量晋级，
也没有运行任何因子或回测。可靠研究仍须通过正式来源、字段语义、PIT、
修订、质量、不可变快照和 `qdata --intent research` 阶段门。
"""


def render_readme(audit: dict[str, Any]) -> str:
    """Render a compact human-readable boundary report."""

    closure = audit["closure_summary"]
    financial_rows = [
        "| {statement} | {ordinary_key_count:,} | {sector_key_count:,} | "
        "{overlap_key_count:,} | {sector_only_key_count:,} |".format(**row)
        for row in audit["financial_statement_overlap"]["statement_overlap"]
    ]
    core_rows = [
        f"| {row['core_id']} | {row['candidate_support']} | "
        f"{', '.join(row['logical_tables']) or '-'} | {row['summary']} |"
        for row in audit["core_mapping"]
    ]
    binding_rows = []
    for row in audit["audit_text_hash_bindings"]["entries"]:
        repair = row["repair_candidate"]
        parquet = row["authoritative_parquet_candidate"]
        binding_rows.append(
            f"| {row['logical_table']} | {repair['accounted_record_count']:,} | "
            f"{repair['repaired_record_count']:,} | "
            f"{repair['semantic_review_record_count']:,} | "
            f"`{repair['manifest_sha256']}` | `{repair['sidecar_sha256']}` | "
            f"`{parquet['content_hash']}` |"
        )
    reconcile = audit["audit_three_table_reconciliation"]
    return f"""# CSMAR 33-package local closure audit

As of: {AS_OF_DATE}

Status: `{audit["status"]}`

## Decision

- Sealed packages: {closure["packages"]}; logical tables: {closure["logical_tables"]}.
- Schema inventory: {audit["schema_inventory"]["table_field_occurrence_count"]:,} table-field occurrences, {audit["schema_inventory"]["unique_observed_field_name_count_case_sensitive"]:,} case-sensitive field names, and {audit["schema_inventory"]["unique_observed_field_name_count_casefold"]:,} names after case folding.
- Candidate packages: {closure["candidate_packages"]}; overlap-only calibration packages: {closure["overlap_calibration_packages"]}.
- The batch touches 14/20 CORE items, but formally closes **{closure["core_items_formally_closed"]}**.
- This report proves only reproducible local evidence. It does not prove source rights, admitted semantics, PIT safety, revision closure, cross-source quality, or research readiness.
- Every input remains `INSPECTION_ONLY/NOT_ADMITTED`; the formal catalog hash stayed `{audit["formal_catalog"]["sha256_before"]}`.

## Lossless audit-text repair and hash binding

| table | preserved rows | repaired rows | semantic review | repair manifest SHA-256 | sidecar SHA-256 | authoritative Parquet content_hash |
|---|---:|---:|---:|---|---|---|
{os.linesep.join(binding_rows)}

The v1 Parquet candidates are retained and not deleted. Their authority is superseded by v2 candidates whose provenance payload binds the repair manifest, row-level sidecar, and repaired CSV.

Three-table reconciliation:

- `FIN_AuditStats`: {reconcile["stats_rows"]:,} rows
- `FIN_Audit`: {reconcile["audit_rows"]:,} rows
- `FIN_AuditDetail`: {reconcile["detail_rows"]:,} rows
- Stats without Detail: {reconcile["stats_without_detail_keys"]}
- Detail without Stats: {reconcile["detail_without_stats_keys"]}
- `AuditNo` versus detail-count mismatches: {reconcile["auditno_detail_count_mismatch_keys"]}
- Stats without Audit: {reconcile["stats_without_audit_keys"]}; retained as a semantic coverage gap, never filled or deleted

## Ordinary versus financial-sector statement overlap

| statement | ordinary keys | sector keys | overlap keys | sector-only keys |
|---|---:|---:|---:|---:|
{os.linesep.join(financial_rows)}

Sector statements add industry-specific fields for the same issuer-period-scope keys; they are not additive rows. Both observations remain preserved and differences require explicit calibration. `DeclareDate` occurs only on `IfCorrect=1` rows, so initial `available_at` remains absent; `Accper`/`EndDate` is a report period, not a visibility time.

## CORE-01 through CORE-20

| CORE | local candidate support | logical tables | remaining gap summary |
|---|---|---|---|
{os.linesep.join(core_rows)}

Machine-readable rights/schema/PIT/revision/quality gaps are in `CLOSURE_AUDIT.json` under `core_mapping`.

## Evidence files

- `CLOSURE_AUDIT.json`: complete machine-readable audit.
- `SCHEMA_INVENTORY.json`: all 33 packages, 25 logical tables, fields and candidate keys.
- `HASH_BINDINGS.json`: sealed ZIP to repair evidence to authoritative v2 Parquet chain.
- `VERIFICATION.json`: fail-closed invariants and failure count.
- `REPORT_MANIFEST.json`: SHA-256 inventory of the report bundle.

## Enforced boundary

The tool does not use the network; modify sealed ZIPs or candidates; write the catalog or Roadmap; admit sources; promote quality; or run factors/backtests. Research use still requires admitted sources and semantics, PIT/revision/quality gates, an immutable snapshot, and `qdata --intent research`.
"""


def report_documents(audit: dict[str, Any]) -> dict[str, bytes]:
    verification = {
        "schema_version": SCHEMA_VERSION,
        "tool_id": TOOL_ID,
        "as_of_date": AS_OF_DATE,
        "status": audit["status"],
        "formal_catalog": audit["formal_catalog"],
        "formal_counts": audit["formal_counts"],
        **audit["verification"],
    }
    documents = {
        "README.md": render_readme(audit).encode("utf-8"),
        "CLOSURE_AUDIT.json": (
            json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8"),
        "SCHEMA_INVENTORY.json": (
            json.dumps(
                audit["schema_inventory"],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
        "HASH_BINDINGS.json": (
            json.dumps(
                audit["audit_text_hash_bindings"],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
        "VERIFICATION.json": (
            json.dumps(verification, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8"),
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "tool_id": TOOL_ID,
        "as_of_date": AS_OF_DATE,
        "files": [
            {
                "relative_path": name,
                "bytes": len(value),
                "sha256": sha256_bytes(value),
            }
            for name, value in sorted(documents.items())
        ],
    }
    documents["REPORT_MANIFEST.json"] = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return documents


def write_reports(
    workspace_root: Path,
    audit: dict[str, Any],
    *,
    report_root: Path | None = None,
) -> dict[str, Any]:
    target = validate_report_root(
        workspace_root,
        report_root if report_root is not None else report_root_for(workspace_root),
    )
    if target.exists():
        unexpected = [
            item.name
            for item in target.iterdir()
            if item.name not in REPORT_FILENAMES
        ]
        if unexpected:
            raise ClosureAuditError(
                "report directory contains unexpected files: "
                + ", ".join(sorted(unexpected))
            )
        if any(item.is_symlink() for item in target.iterdir()):
            raise ClosureAuditError("report directory contains a symlink")
    target.mkdir(parents=True, exist_ok=True)
    documents = report_documents(audit)
    for name, content in documents.items():
        if name not in REPORT_FILENAMES:
            raise ClosureAuditError(f"unapproved report filename: {name}")
        destination = target / name
        temporary = target / f".{name}.partial-{uuid.uuid4().hex}"
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
    return {
        "report_root": target.as_posix(),
        "written_files": [
            {
                "path": (target / name).as_posix(),
                "bytes": (target / name).stat().st_size,
                "sha256": sha256_file(target / name),
            }
            for name in REPORT_FILENAMES
        ],
    }


def run(
    *,
    workspace_root: Path,
    execute: bool,
    audit_builder: Callable[[Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    workspace = workspace_root.resolve()
    target = validate_report_root(workspace, report_root_for(workspace))
    builder = audit_builder or build_audit
    audit = builder(workspace)
    failures = int(audit.get("verification", {}).get("failure_count", 0))
    if not execute:
        return {
            "schema_version": SCHEMA_VERSION,
            "tool_id": TOOL_ID,
            "operation": "AUDIT_PLAN",
            "status": audit.get("status"),
            "write_performed": False,
            "target_report_root": target.as_posix(),
            "package_count": audit.get("intake", {}).get("package_count"),
            "logical_table_count": audit.get("schema_inventory", {}).get(
                "logical_table_count"
            ),
            "candidate_count": audit.get("candidate_batch", {}).get(
                "candidate_count"
            ),
            "verification_failure_count": failures,
        }
    if failures:
        raise ClosureAuditError(
            f"closure audit has {failures} failed checks; report not written"
        )
    written = write_reports(workspace, audit, report_root=target)
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_id": TOOL_ID,
        "operation": "EXECUTE_REPORT",
        "status": audit.get("status"),
        "write_performed": True,
        "verification_failure_count": failures,
        **written,
    }


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=WORKSPACE_ROOT,
        help="Quantification workspace root (defaults to the tool's workspace)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="write the deterministic report bundle to the fixed reports path",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = run(
            workspace_root=args.workspace_root,
            execute=args.execute,
        )
    except ClosureAuditError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
