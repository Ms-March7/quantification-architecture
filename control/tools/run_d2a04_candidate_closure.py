#!/usr/bin/env python3
"""Build the offline, inspection-only D2A-04 Phase-A candidate closure.

This runner is deliberately fail closed:

* the frozen execution plan and every input identity are cryptographically pinned;
* local-download candidate manifests, content contracts, and payloads are verified;
* the official-PIT input is verified by a deterministic whole-tree hash;
* the formal catalog hash is checked before validation, before publication, and after;
* the 20 GiB and 25 percent free-space guards are enforced before and after execution;
* no network, source admission, quality promotion, catalog write, factor research,
  adjusted-price projection, cleanup, or scheduler behavior is implemented;
* the 5.85 million daily rows stay in their immutable upstream partitions.  This
  package writes only references, mappings, diagnostics, and small evidence tables.

Without ``build --execute``, all commands are read-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
CATALOG_LIB = WORKSPACE_ROOT / "data" / "catalog"
if str(CATALOG_LIB) not in sys.path:
    sys.path.insert(0, str(CATALOG_LIB))

from snapshot_contract import (  # noqa: E402
    SnapshotContractError,
    canonical_bytes,
    is_reparse_point,
    load_json_object,
    sha256_bytes,
    sha256_file,
)


TOOL_ID = "run_d2a04_candidate_closure.py/d2a04-phase-a-v1"
PLAN_ID = "d2a04-core01-to-core06-candidate-closure-v1"
WORK_PACKAGE = (
    "L1-D2A-04_CORE01_TO_CORE06_CANDIDATE_CLOSURE_AND_SELECTED_SOURCE_PILOT"
)
PURPOSE_CEILING = "INSPECTION_ONLY"
CANDIDATE_DATASET_ID = "d2a04-core01-to-core06-candidate"
HASH_ALGORITHM = "sha256-d2a04-candidate-closure-v1"
TREE_HASH_ALGORITHM = "sha256-relative-path-size-file-sha256-v1"
LOCAL_DOWNLOAD_HASH_ALGORITHM = "sha256-local-download-candidate-v1"
DEFAULT_PLAN = (
    WORKSPACE_ROOT
    / "data"
    / "catalog"
    / "contracts"
    / "source_admission_v1"
    / "execution_plans"
    / "d2a04_core01_to_core06_candidate_closure_v1.json"
)
CATALOG_PATH = WORKSPACE_ROOT / "data" / "catalog" / "datasets.json"
OUTPUT_BASE = WORKSPACE_ROOT / "tmp" / "d2a04-core01-to-core06-candidate"
REPORT_DIR = (
    WORKSPACE_ROOT
    / "reports"
    / "data-governance"
    / "d2a-04-core01-to-core06-candidate-20260728"
)
DATASET_CLI = CATALOG_LIB / "dataset_cli.py"
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

CSMAR_MANUAL_URL = (
    "https://file.csmar.com/group1/M00/AA/99/"
    "CuIKV2XAnKiABeLbAB4nDGC5Ano840.pdf"
)
MANUAL_DEFINED_DAILY_FIELDS = {
    "Stkcd",
    "Trddt",
    "Trdsta",
    "Opnprc",
    "Hiprc",
    "Loprc",
    "Clsprc",
    "Dnshrtrd",
    "Dnvaltrd",
    "Dsmvosd",
    "Dsmvtll",
    "Dretwd",
    "Dretnd",
    "Adjprcwd",
    "Adjprcnd",
    "Markettype",
    "Capchgdt",
    "Ahshrtrd_D",
    "Ahvaltrd_D",
    "PreClosePrice",
    "ChangeRatio",
}
UNRESOLVED_EXPORT_EXTENSION_FIELDS = {
    "LimitDown",
    "LimitUp",
    "LimitStatus",
}
EXPECTED_SOURCE_FIELDS = MANUAL_DEFINED_DAILY_FIELDS | UNRESOLVED_EXPORT_EXTENSION_FIELDS
CORE06_REFERENCE_FIELDS = [
    "Stkcd",
    "Trddt",
    "Markettype",
    "Trdsta",
    "PreClosePrice",
    "LimitDown",
    "LimitUp",
    "LimitStatus",
]
MARKETTYPE_EVIDENCE = {
    1: {
        "exchange_mic_claim": "XSHG",
        "market_segment_claim": "SSE_A",
        "a_share_scope": True,
    },
    2: {
        "exchange_mic_claim": "XSHG",
        "market_segment_claim": "SSE_B",
        "a_share_scope": False,
    },
    4: {
        "exchange_mic_claim": "XSHE",
        "market_segment_claim": "SZSE_A",
        "a_share_scope": True,
    },
    8: {
        "exchange_mic_claim": "XSHE",
        "market_segment_claim": "SZSE_B",
        "a_share_scope": False,
    },
    16: {
        "exchange_mic_claim": "XSHE",
        "market_segment_claim": "CHINEXT_A",
        "a_share_scope": True,
    },
    32: {
        "exchange_mic_claim": "XSHG",
        "market_segment_claim": "STAR_A",
        "a_share_scope": True,
    },
    64: {
        "exchange_mic_claim": "XBSE",
        "market_segment_claim": "BSE_A",
        "a_share_scope": True,
    },
}


class ClosureError(RuntimeError):
    """A controlled fail-closed D2A-04 rejection."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def strict_json(path: Path) -> dict[str, Any]:
    try:
        return load_json_object(path)
    except SnapshotContractError as exc:
        raise ClosureError(str(exc)) from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            json_safe(value),
            handle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value.replace("\r\n", "\n"))
        handle.flush()
        os.fsync(handle.fileno())


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def validate_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value.lower()):
        raise ClosureError(f"{label} must be a SHA-256")
    return value.lower()


def validate_run_id(value: str) -> str:
    if not SAFE_RUN_ID.fullmatch(value):
        raise ClosureError("run_id must be a safe 1-96 character path component")
    if value in {".", ".."} or value.rstrip(" .") != value:
        raise ClosureError("run_id has an unsafe Windows path form")
    return value


def require_plain_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ClosureError(f"{label} must be absolute: {path}")
    if not path.exists() or not path.is_dir():
        raise ClosureError(f"{label} is not a directory: {path}")
    if is_reparse_point(path):
        raise ClosureError(f"{label} cannot be a reparse point: {path}")
    return path.resolve()


def require_workspace_path(path: Path, label: str, *, directory: bool) -> Path:
    candidate = path.resolve(strict=False)
    root = WORKSPACE_ROOT.resolve()
    try:
        if Path(os.path.commonpath([root, candidate])) != root:
            raise ClosureError(f"{label} escapes the workspace: {path}")
    except ValueError as exc:
        raise ClosureError(f"{label} is on a different volume: {path}") from exc
    relative = candidate.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.exists() and is_reparse_point(current):
            raise ClosureError(f"{label} crosses a reparse point: {current}")
    if directory:
        return require_plain_directory(candidate, label)
    if not candidate.is_file() or is_reparse_point(candidate):
        raise ClosureError(f"{label} is not a plain file: {candidate}")
    return candidate


def safe_payload_path(root: Path, relative_value: Any, label: str) -> Path:
    if not isinstance(relative_value, str) or not relative_value:
        raise ClosureError(f"{label} relative_path must be a non-empty string")
    pure = PurePosixPath(relative_value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ClosureError(f"{label} has unsafe relative_path: {relative_value!r}")
    candidate = root.joinpath(*pure.parts)
    resolved_root = root.resolve()
    resolved = candidate.resolve(strict=False)
    try:
        if Path(os.path.commonpath([resolved_root, resolved])) != resolved_root:
            raise ClosureError(f"{label} escapes candidate root")
    except ValueError as exc:
        raise ClosureError(f"{label} is on a different volume") from exc
    if not candidate.is_file() or is_reparse_point(candidate):
        raise ClosureError(f"{label} is missing or not a plain file: {candidate}")
    return candidate


def deterministic_tree_hash(root: Path) -> tuple[str, list[dict[str, Any]]]:
    root = require_plain_directory(root, "tree root")
    inventory: list[dict[str, Any]] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept: list[str] = []
        for name in sorted(directories):
            child = current_path / name
            if is_reparse_point(child):
                raise ClosureError(f"tree contains a reparse directory: {child}")
            kept.append(name)
        directories[:] = kept
        for name in sorted(files):
            child = current_path / name
            if is_reparse_point(child) or not child.is_file():
                raise ClosureError(f"tree contains a non-plain file: {child}")
            inventory.append(
                {
                    "relative_path": child.relative_to(root).as_posix(),
                    "bytes": child.stat().st_size,
                    "sha256": sha256_file(child),
                }
            )
    inventory.sort(key=lambda item: item["relative_path"])
    value = {"algorithm": TREE_HASH_ALGORITHM, "files": inventory}
    return sha256_bytes(canonical_bytes(value)), inventory


def local_candidate_contract_hash(root: Path) -> str:
    request = strict_json(root / "request.json")
    manifest = strict_json(root / "manifest.json")
    provenance = strict_json(root / "provenance.json")
    partitions = strict_json(root / "partition-manifest.json")
    quality = strict_json(root / "quality" / "summary.json")
    manifest_without_hash = dict(manifest)
    manifest_without_hash.pop("content_hash", None)
    value = {
        "hash_algorithm": LOCAL_DOWNLOAD_HASH_ALGORITHM,
        "request": request,
        "manifest": manifest_without_hash,
        "provenance": provenance,
        "partition_manifest": partitions,
        "quality": quality,
    }
    return sha256_bytes(canonical_bytes(value))


def validate_local_candidate(spec: dict[str, Any]) -> dict[str, Any]:
    role = str(spec.get("role", "input"))
    root = require_workspace_path(
        Path(str(spec.get("root", ""))), f"{role} root", directory=True
    )
    manifest_path = root / "manifest.json"
    expected_manifest = validate_sha256(
        spec.get("manifest_sha256"), f"{role} manifest_sha256"
    )
    actual_manifest = sha256_file(manifest_path)
    if actual_manifest != expected_manifest:
        raise ClosureError(
            f"{role} manifest SHA-256 mismatch: {actual_manifest} != "
            f"{expected_manifest}"
        )
    manifest = strict_json(manifest_path)
    expected_content = validate_sha256(spec.get("content_hash"), f"{role} content_hash")
    if manifest.get("content_hash") != expected_content:
        raise ClosureError(f"{role} manifest content_hash does not match the plan")
    actual_content = local_candidate_contract_hash(root)
    if actual_content != expected_content:
        raise ClosureError(
            f"{role} content contract mismatch: {actual_content} != "
            f"{expected_content}"
        )
    if manifest.get("catalog_registered") is not False:
        raise ClosureError(f"{role} must remain absent from the formal catalog")
    if manifest.get("purpose_ceiling") != PURPOSE_CEILING:
        raise ClosureError(f"{role} purpose ceiling is not INSPECTION_ONLY")
    if manifest.get("research_ready") is not False:
        raise ClosureError(f"{role} unexpectedly claims research readiness")
    payloads = manifest.get("payload_inventory")
    if not isinstance(payloads, list) or not payloads:
        raise ClosureError(f"{role} payload inventory is empty")
    seen: set[str] = set()
    verified_payloads: list[dict[str, Any]] = []
    for index, entry in enumerate(payloads):
        if not isinstance(entry, dict):
            raise ClosureError(f"{role} payload[{index}] is not an object")
        relative = entry.get("relative_path")
        if not isinstance(relative, str) or relative in seen:
            raise ClosureError(f"{role} payload path is invalid or duplicated")
        seen.add(relative)
        payload_path = safe_payload_path(root, relative, f"{role} payload[{index}]")
        expected_bytes = entry.get("bytes")
        if not isinstance(expected_bytes, int) or expected_bytes < 0:
            raise ClosureError(f"{role} payload[{index}] bytes is invalid")
        actual_bytes = payload_path.stat().st_size
        if actual_bytes != expected_bytes:
            raise ClosureError(f"{role} payload size mismatch: {relative}")
        expected_hash = validate_sha256(
            entry.get("sha256"), f"{role} payload[{index}] sha256"
        )
        actual_hash = sha256_file(payload_path)
        if actual_hash != expected_hash:
            raise ClosureError(f"{role} payload hash mismatch: {relative}")
        verified_payloads.append(
            {
                "relative_path": relative,
                "bytes": actual_bytes,
                "sha256": actual_hash,
                "layer": entry.get("layer"),
            }
        )
    quality = strict_json(root / "quality" / "summary.json")
    metrics = quality.get("metrics")
    if not isinstance(metrics, dict):
        raise ClosureError(f"{role} quality metrics are missing")
    expected_rows = spec.get("expected_rows")
    if isinstance(expected_rows, int) and metrics.get("row_count") != expected_rows:
        raise ClosureError(
            f"{role} row count mismatch: {metrics.get('row_count')} != {expected_rows}"
        )
    partitions = strict_json(root / "partition-manifest.json")
    if partitions.get("total_rows") != metrics.get("row_count"):
        raise ClosureError(f"{role} partition and quality row counts disagree")
    return {
        "role": role,
        "root": root,
        "manifest_path": manifest_path,
        "manifest_sha256": actual_manifest,
        "content_hash": actual_content,
        "manifest": manifest,
        "quality": quality,
        "partition_manifest": partitions,
        "payloads": verified_payloads,
    }


def validate_official_pit_candidate(spec: dict[str, Any]) -> dict[str, Any]:
    role = str(spec.get("role", "D2A03_OFFICIAL_PIT_CANDIDATE"))
    root = require_workspace_path(
        Path(str(spec.get("root", ""))), f"{role} root", directory=True
    )
    expected = validate_sha256(spec.get("tree_sha256"), f"{role} tree_sha256")
    actual, inventory = deterministic_tree_hash(root)
    if actual != expected:
        raise ClosureError(f"{role} tree SHA-256 mismatch: {actual} != {expected}")
    summary = strict_json(root / "parse-summary.json")
    if summary.get("status") != (
        "COMPLETED__PARTIAL__BSE_BUSINESS_AND_PIT_BLOCKED__"
        "INSPECTION_ONLY__NOT_ADMITTED"
    ):
        raise ClosureError(f"{role} current status is not the frozen partial candidate")
    if summary.get("available_at_values_created") != 0:
        raise ClosureError(f"{role} unexpectedly created available_at values")
    return {
        "role": role,
        "root": root,
        "tree_sha256": actual,
        "tree_inventory": inventory,
        "parse_summary": summary,
        "receipt": strict_json(root / "receipt.json"),
    }


def validate_plan(plan_path: Path) -> dict[str, Any]:
    plan_path = require_workspace_path(plan_path, "D2A-04 plan", directory=False)
    if plan_path.resolve() != DEFAULT_PLAN.resolve():
        raise ClosureError(f"only the frozen D2A-04 plan is accepted: {DEFAULT_PLAN}")
    plan = strict_json(plan_path)
    if plan.get("schema_version") != 1 or plan.get("plan_id") != PLAN_ID:
        raise ClosureError("unexpected D2A-04 plan identity")
    if plan.get("work_package") != WORK_PACKAGE:
        raise ClosureError("unexpected D2A-04 work-package identity")
    authorization = plan.get("authorization")
    if not isinstance(authorization, dict):
        raise ClosureError("plan authorization is missing")
    required_authorization = {
        "execution_authorization_active": True,
        "purpose_ceiling": PURPOSE_CEILING,
        "formal_catalog_write": False,
        "formal_quality_promotion_or_waiver": False,
        "factor_efficacy_research": False,
        "full_history_or_full_database_backfill": False,
        "paid_source_or_new_subscription": False,
        "credential_cookie_token_storage": False,
        "unattended_scheduler": False,
        "automatic_cleanup": False,
    }
    for key, expected in required_authorization.items():
        if authorization.get(key) != expected:
            raise ClosureError(f"plan authorization mismatch for {key}")
    phase = plan.get("phase_a")
    if not isinstance(phase, dict) or phase.get("network_requests_authorized") is not False:
        raise ClosureError("Phase A must be explicitly offline")
    tasks = phase.get("tasks")
    task_ids = {
        item.get("task_id")
        for item in tasks
        if isinstance(tasks, list) and isinstance(item, dict)
    }
    expected_tasks = {f"D2A04-A{index}" for index in range(1, 7)}
    if task_ids != expected_tasks:
        raise ClosureError("Phase A task set is incomplete or changed")
    inputs = phase.get("inputs")
    if not isinstance(inputs, list):
        raise ClosureError("Phase A inputs are missing")
    by_role = {
        str(item.get("role")): item for item in inputs if isinstance(item, dict)
    }
    expected_roles = {
        "CSMAR_DAILY_2020_2024_CANDIDATE",
        "CSMAR_CURRENT_COMPANY_CANDIDATE",
        "D2A03_OFFICIAL_PIT_CANDIDATE",
    }
    if set(by_role) != expected_roles:
        raise ClosureError("Phase A input roles changed")
    guard = phase.get("formal_catalog_guard")
    if not isinstance(guard, dict):
        raise ClosureError("formal catalog guard is missing")
    guard_path = Path(str(guard.get("path", ""))).resolve()
    if guard_path != CATALOG_PATH.resolve() or guard.get("must_remain_unchanged") is not True:
        raise ClosureError("formal catalog guard does not bind the workspace catalog")
    storage = phase.get("storage_guard")
    if not isinstance(storage, dict):
        raise ClosureError("storage guard is missing")
    if (
        storage.get("drive") != "G:"
        or storage.get("max_new_footprint_gib") != 20
        or float(storage.get("minimum_free_fraction", -1)) != 0.25
        or storage.get("automatic_cleanup") is not False
    ):
        raise ClosureError("storage guard is not the frozen 20 GiB / 25% contract")
    return {
        "path": plan_path,
        "sha256": sha256_file(plan_path),
        "value": plan,
        "phase": phase,
        "inputs_by_role": by_role,
        "catalog_guard": guard,
        "storage_guard": storage,
    }


def storage_guard(
    value: dict[str, Any], usage: os.statvfs_result | Any | None = None
) -> dict[str, Any]:
    max_bytes = int(value["max_new_footprint_gib"]) * 1024**3
    minimum_fraction = float(value["minimum_free_fraction"])
    if max_bytes > 20 * 1024**3 or minimum_fraction < 0.25:
        raise ClosureError("storage guard is weaker than the frozen contract")
    if usage is None:
        usage = shutil.disk_usage(WORKSPACE_ROOT.anchor or WORKSPACE_ROOT)
    total = int(usage.total)
    free = int(usage.free)
    if total <= 0 or free < 0:
        raise ClosureError("invalid G: disk-usage result")
    free_fraction = free / total
    worst_case_fraction = (free - max_bytes) / total
    if free_fraction < minimum_fraction:
        raise ClosureError(
            f"G: free fraction {free_fraction:.6f} is below {minimum_fraction:.6f}"
        )
    if worst_case_fraction < minimum_fraction:
        raise ClosureError(
            "G: would fall below the minimum free fraction at the 20 GiB limit"
        )
    return {
        "drive": "G:",
        "total_bytes": total,
        "free_bytes": free,
        "free_gib": free / 1024**3,
        "free_fraction": free_fraction,
        "max_new_footprint_bytes": max_bytes,
        "minimum_free_fraction": minimum_fraction,
        "worst_case_free_fraction": worst_case_fraction,
        "status": "PASS",
    }


def validate_inputs(plan_path: Path) -> dict[str, Any]:
    plan = validate_plan(plan_path)
    expected_catalog = validate_sha256(
        plan["catalog_guard"].get("expected_sha256"), "catalog expected_sha256"
    )
    catalog_before = sha256_file(CATALOG_PATH)
    if catalog_before != expected_catalog:
        raise ClosureError(
            f"formal catalog hash mismatch: {catalog_before} != {expected_catalog}"
        )
    daily = validate_local_candidate(
        plan["inputs_by_role"]["CSMAR_DAILY_2020_2024_CANDIDATE"]
    )
    company = validate_local_candidate(
        plan["inputs_by_role"]["CSMAR_CURRENT_COMPANY_CANDIDATE"]
    )
    official = validate_official_pit_candidate(
        plan["inputs_by_role"]["D2A03_OFFICIAL_PIT_CANDIDATE"]
    )
    storage = storage_guard(plan["storage_guard"])
    return {
        "plan": plan,
        "catalog_sha256_before": catalog_before,
        "daily": daily,
        "company": company,
        "official": official,
        "storage_preflight": storage,
    }


def import_duckdb() -> Any:
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise ClosureError("DuckDB is required for D2A-04 Phase A") from exc
    return duckdb


def candidate_partition_paths(candidate: dict[str, Any]) -> list[Path]:
    root = candidate["root"]
    partitions = candidate["partition_manifest"].get("partitions")
    if not isinstance(partitions, list) or not partitions:
        raise ClosureError(f"{candidate['role']} has no declared partitions")
    result: list[Path] = []
    for index, entry in enumerate(partitions):
        if not isinstance(entry, dict):
            raise ClosureError(f"{candidate['role']} partition[{index}] is invalid")
        path = safe_payload_path(
            root, entry.get("relative_path"), f"{candidate['role']} partition[{index}]"
        )
        expected = validate_sha256(
            entry.get("sha256"), f"{candidate['role']} partition[{index}] sha256"
        )
        if sha256_file(path) != expected or path.stat().st_size != entry.get("bytes"):
            raise ClosureError(f"{candidate['role']} partition pin mismatch: {path}")
        result.append(path)
    return result


def fetch_dicts(connection: Any, query: str) -> list[dict[str, Any]]:
    cursor = connection.execute(query)
    columns = [str(item[0]) for item in cursor.description]
    return [
        {columns[index]: json_safe(value) for index, value in enumerate(row)}
        for row in cursor.fetchall()
    ]


def sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def copy_query_to_parquet(connection: Any, query: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    connection.execute(
        f"COPY ({query}) TO '{sql_path(target)}' "
        "(FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    if not target.is_file() or target.stat().st_size <= 0:
        raise ClosureError(f"DuckDB did not create expected Parquet: {target}")


def exchange_case(column: str = "Markettype") -> str:
    return (
        f"CASE {column} "
        "WHEN 1 THEN 'XSHG' WHEN 2 THEN 'XSHG' "
        "WHEN 4 THEN 'XSHE' WHEN 8 THEN 'XSHE' "
        "WHEN 16 THEN 'XSHE' WHEN 32 THEN 'XSHG' "
        "WHEN 64 THEN 'XBSE' ELSE NULL END"
    )


def segment_case(column: str = "Markettype") -> str:
    return (
        f"CASE {column} "
        "WHEN 1 THEN 'SSE_A' WHEN 2 THEN 'SSE_B' "
        "WHEN 4 THEN 'SZSE_A' WHEN 8 THEN 'SZSE_B' "
        "WHEN 16 THEN 'CHINEXT_A' WHEN 32 THEN 'STAR_A' "
        "WHEN 64 THEN 'BSE_A' ELSE 'UNKNOWN' END"
    )


def a_share_case(column: str = "Markettype") -> str:
    return f"CASE WHEN {column} IN (1, 4, 16, 32, 64) THEN TRUE ELSE FALSE END"


def field_contract(connection: Any) -> dict[str, Any]:
    described = fetch_dicts(connection, "DESCRIBE daily_source")
    source_fields = [
        row["column_name"]
        for row in described
        if row["column_name"] != "__qdata_source_file"
    ]
    if set(source_fields) != EXPECTED_SOURCE_FIELDS or len(source_fields) != 24:
        raise ClosureError(
            "daily source schema is not the pinned 24-field CSMAR export"
        )
    rows: list[dict[str, Any]] = []
    by_name = {row["column_name"]: row for row in described}
    for name in source_fields:
        if name in UNRESOLVED_EXPORT_EXTENSION_FIELDS:
            status = "UNRESOLVED_EXPORT_EXTENSION_NOT_IN_VERIFIED_MANUAL_TABLE_7"
        else:
            status = "DEFINED_IN_VERIFIED_CSMAR_MANUAL_TABLE_7"
        note = None
        if name == "Markettype":
            note = "Official manual mapping 1/2/4/8/16/32/64 is recorded."
        elif name == "Trdsta":
            note = (
                "Official manual contains a 1..16 code table; raw numeric values "
                "are preserved, but the label mapping is not promoted here."
            )
        elif name in UNRESOLVED_EXPORT_EXTENSION_FIELDS:
            note = (
                "Field is present in the current 24-field export but absent from "
                "the verified public manual table, so semantics remain blocking."
            )
        elif name in {"Adjprcwd", "Adjprcnd"}:
            note = "Preserved as source observations; forbidden as CORE-05 output."
        elif name == "Capchgdt":
            note = "Preserved only as a clue; not corporate-action terms or lifecycle."
        rows.append(
            {
                "source_field": name,
                "duckdb_type": by_name[name]["column_type"],
                "manual_verification_status": status,
                "semantic_note": note,
            }
        )
    return {
        "schema_version": 1,
        "source_table": "TRD_Dalyr",
        "observed_source_field_count": len(source_fields),
        "lineage_field": "__qdata_source_file",
        "verified_manual_url": CSMAR_MANUAL_URL,
        "verified_manual_field_count": len(MANUAL_DEFINED_DAILY_FIELDS),
        "export_extension_field_count": len(UNRESOLVED_EXPORT_EXTENSION_FIELDS),
        "version_alignment_status": (
            "BLOCKED__CURRENT_EXPORT_HAS_3_FIELDS_NOT_PRESENT_IN_VERIFIED_MANUAL"
        ),
        "fields": rows,
        "interpretation": (
            "A schema/type observation is not source admission, unit closure, "
            "PIT closure, or research readiness."
        ),
    }


def candidate_inventory(root: Path, *, exclude: Iterable[str] = ()) -> list[dict[str, Any]]:
    excluded = set(exclude)
    inventory: list[dict[str, Any]] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(directories)
        for name in sorted(files):
            child = current_path / name
            relative = child.relative_to(root).as_posix()
            if relative in excluded:
                continue
            if is_reparse_point(child) or not child.is_file():
                raise ClosureError(f"candidate contains a non-plain file: {child}")
            inventory.append(
                {
                    "relative_path": relative,
                    "bytes": child.stat().st_size,
                    "sha256": sha256_file(child),
                }
            )
    inventory.sort(key=lambda item: item["relative_path"])
    return inventory


def directory_bytes(root: Path) -> int:
    return sum(item["bytes"] for item in candidate_inventory(root))


def build_candidate(
    validated: dict[str, Any],
    *,
    run_id: str,
    stage: Path,
    final_path: Path,
) -> dict[str, Any]:
    duckdb = import_duckdb()
    daily_files = candidate_partition_paths(validated["daily"])
    company_files = candidate_partition_paths(validated["company"])
    if len(daily_files) != 60 or len(company_files) != 1:
        raise ClosureError("unexpected daily/company partition counts")
    stage.mkdir(parents=True, exist_ok=False)
    connection = duckdb.connect()
    try:
        connection.from_parquet([str(path) for path in daily_files]).create_view(
            "daily_source"
        )
        connection.from_parquet([str(path) for path in company_files]).create_view(
            "company_source"
        )
        actual_daily = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT Stkcd), COUNT(DISTINCT Trddt), "
            "MIN(Trddt), MAX(Trddt) FROM daily_source"
        ).fetchone()
        expected_daily = validated["plan"]["inputs_by_role"][
            "CSMAR_DAILY_2020_2024_CANDIDATE"
        ]
        if actual_daily[0] != expected_daily["expected_rows"]:
            raise ClosureError("actual daily row count does not match the frozen plan")
        if actual_daily[1] != expected_daily["expected_distinct_source_codes"]:
            raise ClosureError(
                "actual daily distinct-code count does not match the frozen plan"
            )
        actual_company = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT Stkcd) FROM company_source"
        ).fetchone()
        if actual_company[0] != validated["plan"]["inputs_by_role"][
            "CSMAR_CURRENT_COMPANY_CANDIDATE"
        ]["expected_rows"]:
            raise ClosureError("actual company row count does not match the frozen plan")
        if actual_company[0] != actual_company[1]:
            raise ClosureError("company source code is not unique in the current export")

        episode_query = f"""
            WITH daily_code AS (
                SELECT
                    Stkcd AS source_local_code,
                    Markettype AS source_markettype,
                    MIN(Trddt) AS observed_from,
                    MAX(Trddt) AS observed_to,
                    COUNT(*) AS source_row_count,
                    COUNT(DISTINCT Trddt) AS observed_date_count
                FROM daily_source
                GROUP BY Stkcd, Markettype
            )
            SELECT
                'CSMAR_MANUAL_EXPORT' AS source_id,
                'TRD_Dalyr' AS source_table,
                'CSMAR_CODE_EPISODE:' || CAST(source_markettype AS VARCHAR)
                    || ':' || source_local_code AS provisional_code_episode_key,
                TRUE AS is_provisional_not_permanent_security_id,
                source_local_code,
                source_markettype,
                {exchange_case("source_markettype")} AS provisional_exchange_mic_claim,
                {segment_case("source_markettype")} AS provisional_market_segment_claim,
                {a_share_case("source_markettype")} AS a_share_scope_claim,
                observed_from,
                observed_to,
                source_row_count,
                observed_date_count,
                co.Stknme AS current_name_observation,
                co.Listdt AS current_list_date_observation,
                co.Stkcd IS NOT NULL AS current_company_observation_matched,
                CAST(NULL AS VARCHAR) AS permanent_security_id,
                'CANDIDATE_CODE_EPISODE__NOT_A_PERMANENT_ID'
                    AS identity_status
            FROM daily_code d
            LEFT JOIN company_source co ON co.Stkcd = d.source_local_code
            ORDER BY source_markettype, source_local_code
        """
        copy_query_to_parquet(
            connection,
            episode_query,
            stage / "core01" / "provisional-code-episode-observations.parquet",
        )

        company_query = f"""
            WITH daily_code AS (
                SELECT
                    Stkcd AS source_local_code,
                    COUNT(*) AS source_row_count,
                    MIN(Trddt) AS observed_from,
                    MAX(Trddt) AS observed_to,
                    COUNT(DISTINCT Markettype) AS observed_markettype_count,
                    STRING_AGG(
                        DISTINCT CAST(Markettype AS VARCHAR), ',' ORDER BY
                        CAST(Markettype AS VARCHAR)
                    ) AS observed_markettypes,
                    MIN(Markettype) AS only_markettype
                FROM daily_source
                GROUP BY Stkcd
            )
            SELECT
                'CSMAR_MANUAL_EXPORT' AS source_id,
                'TRD_Co' AS source_table,
                co.Stkcd AS source_local_code,
                co.Stknme AS current_name_observation,
                co.Listdt AS current_list_date_observation,
                d.source_local_code IS NOT NULL AS matched_to_2020_2024_daily,
                d.source_row_count,
                d.observed_from,
                d.observed_to,
                d.observed_markettype_count,
                d.observed_markettypes,
                CASE WHEN d.observed_markettype_count = 1
                    THEN {exchange_case("d.only_markettype")} ELSE NULL END
                    AS provisional_exchange_mic_claim,
                CASE WHEN d.observed_markettype_count = 1
                    THEN {segment_case("d.only_markettype")} ELSE NULL END
                    AS provisional_market_segment_claim,
                CAST(NULL AS VARCHAR) AS permanent_security_id,
                'CURRENT_MASTER_OBSERVATION__NO_CODE_HISTORY__NO_PERMANENT_ID'
                    AS identity_status
            FROM company_source co
            LEFT JOIN daily_code d ON d.source_local_code = co.Stkcd
            ORDER BY co.Stkcd
        """
        copy_query_to_parquet(
            connection,
            company_query,
            stage / "core01" / "current-company-master-observations.parquet",
        )

        calendar_query = f"""
            SELECT
                'CSMAR_MANUAL_EXPORT' AS source_id,
                Markettype AS source_markettype,
                {exchange_case()} AS provisional_exchange_mic_claim,
                {segment_case()} AS provisional_market_segment_claim,
                {a_share_case()} AS a_share_scope_claim,
                Trddt AS observed_trade_date,
                COUNT(*) AS source_row_count,
                COUNT(DISTINCT Stkcd) AS observed_source_code_count,
                'OBSERVED_ROWS_EXIST__NOT_AN_OFFICIAL_CALENDAR_OR_SESSION'
                    AS diagnostic_status
            FROM daily_source
            GROUP BY Markettype, Trddt
            ORDER BY Markettype, Trddt
        """
        copy_query_to_parquet(
            connection,
            calendar_query,
            stage / "core02" / "observed-trade-date-diagnostics.parquet",
        )

        mapping_query = f"""
            SELECT
                'CSMAR_MANUAL_EXPORT' AS source_id,
                d.Stkcd AS source_local_code,
                d.Markettype AS source_markettype,
                {exchange_case("d.Markettype")} AS provisional_exchange_mic_claim,
                {segment_case("d.Markettype")} AS provisional_market_segment_claim,
                {a_share_case("d.Markettype")} AS a_share_scope_claim,
                COUNT(*) AS source_row_count,
                COUNT(DISTINCT d.Trddt) AS observed_date_count,
                MIN(d.Trddt) AS observed_from,
                MAX(d.Trddt) AS observed_to,
                co.Stkcd IS NOT NULL AS current_company_observation_matched,
                CAST(NULL AS VARCHAR) AS permanent_security_id,
                'PROVISIONAL_SOURCE_CODE_MAPPING__NOT_CORE01_IDENTITY'
                    AS mapping_status
            FROM daily_source d
            LEFT JOIN company_source co ON co.Stkcd = d.Stkcd
            GROUP BY
                d.Stkcd, d.Markettype, co.Stkcd
            ORDER BY d.Markettype, d.Stkcd
        """
        copy_query_to_parquet(
            connection,
            mapping_query,
            stage / "core03" / "code-mapping-coverage.parquet",
        )

        capchg_transition_query = """
            WITH ordered AS (
                SELECT
                    Stkcd,
                    Markettype,
                    Trddt,
                    Capchgdt,
                    LAG(Capchgdt) OVER (
                        PARTITION BY Stkcd ORDER BY Trddt
                    ) AS previous_capchgdt
                FROM daily_source
            )
            SELECT
                'CSMAR_MANUAL_EXPORT' AS source_id,
                Stkcd AS source_local_code,
                Markettype AS source_markettype,
                Trddt AS observation_trade_date,
                previous_capchgdt,
                Capchgdt AS observed_capchgdt,
                CASE
                    WHEN Capchgdt < previous_capchgdt THEN 'BACKWARD_CHANGE'
                    ELSE 'FORWARD_CHANGE'
                END AS clue_direction,
                'CAPCHGDT_TRANSITION_CLUE_ONLY__NOT_ACTION_TERMS_OR_REVISION'
                    AS interpretation
            FROM ordered
            WHERE previous_capchgdt IS NOT NULL
              AND Capchgdt IS DISTINCT FROM previous_capchgdt
            ORDER BY Stkcd, Trddt
        """
        copy_query_to_parquet(
            connection,
            capchg_transition_query,
            stage / "core04" / "capchgdt-transition-clues.parquet",
        )

        trdsta_distribution_query = f"""
            SELECT
                'CSMAR_MANUAL_EXPORT' AS source_id,
                Markettype AS source_markettype,
                {exchange_case()} AS provisional_exchange_mic_claim,
                Trdsta AS raw_trdsta_code,
                COUNT(*) AS source_row_count,
                COUNT(DISTINCT Stkcd) AS observed_source_code_count,
                MIN(Trddt) AS observed_from,
                MAX(Trddt) AS observed_to,
                'RAW_CODE_DISTRIBUTION__NO_ELIGIBILITY_OR_SUSPENSION_INFERENCE'
                    AS semantic_status
            FROM daily_source
            GROUP BY Markettype, Trdsta
            ORDER BY Markettype, Trdsta
        """
        copy_query_to_parquet(
            connection,
            trdsta_distribution_query,
            stage / "core06" / "trdsta-value-distribution.parquet",
        )

        limit_distribution_query = f"""
            SELECT
                'CSMAR_MANUAL_EXPORT' AS source_id,
                Markettype AS source_markettype,
                {exchange_case()} AS provisional_exchange_mic_claim,
                LimitStatus AS raw_limitstatus_code,
                COUNT(*) AS source_row_count,
                COUNT(DISTINCT Stkcd) AS observed_source_code_count,
                COUNT(*) FILTER (WHERE LimitDown IS NULL) AS limitdown_null_rows,
                COUNT(*) FILTER (WHERE LimitUp IS NULL) AS limitup_null_rows,
                MIN(LimitDown) AS observed_limitdown_min,
                MAX(LimitDown) AS observed_limitdown_max,
                MIN(LimitUp) AS observed_limitup_min,
                MAX(LimitUp) AS observed_limitup_max,
                'UNRESOLVED_EXPORT_EXTENSION__RAW_VALUES_ONLY'
                    AS semantic_status
            FROM daily_source
            GROUP BY Markettype, LimitStatus
            ORDER BY Markettype, LimitStatus NULLS LAST
        """
        copy_query_to_parquet(
            connection,
            limit_distribution_query,
            stage / "core06" / "limit-field-value-distribution.parquet",
        )

        source_contract = field_contract(connection)
        write_json(stage / "core03" / "source-field-contract.json", source_contract)

        daily_partition_manifest = validated["daily"]["partition_manifest"]
        partition_references: list[dict[str, Any]] = []
        for entry, path in zip(
            daily_partition_manifest["partitions"], daily_files, strict=True
        ):
            partition_references.append(
                {
                    "relative_path_in_upstream": entry["relative_path"],
                    "absolute_path": path.resolve().as_posix(),
                    "bytes": entry["bytes"],
                    "sha256": entry["sha256"],
                    "row_count": entry["row_count"],
                    "year": entry.get("year"),
                    "month": entry.get("month"),
                }
            )
        partition_reference_value = {
            "schema_version": 1,
            "reference_type": "IMMUTABLE_UPSTREAM_PARTITIONS__NO_ROW_COPY",
            "upstream_candidate_id": validated["daily"]["manifest"]["candidate_id"],
            "upstream_root": validated["daily"]["root"].as_posix(),
            "upstream_content_hash": validated["daily"]["content_hash"],
            "source_table": "TRD_Dalyr",
            "source_fields_preserved": source_contract["observed_source_field_count"],
            "lineage_field_preserved": "__qdata_source_file",
            "partition_count": len(partition_references),
            "total_rows": daily_partition_manifest["total_rows"],
            "referenced_curated_bytes": sum(
                item["bytes"] for item in partition_references
            ),
            "partitions": partition_references,
            "consumer_boundary": (
                "This is an inspection-only provenance reference, not a qdata "
                "resolver result and not authorization for research."
            ),
        }
        write_json(
            stage / "core03" / "partition-references.json",
            partition_reference_value,
        )
        write_json(
            stage / "core06" / "status-observation-reference.json",
            {
                "schema_version": 1,
                "reference_type": (
                    "RAW_STATUS_FIELD_PROJECTION_OVER_PINNED_UPSTREAM_PARTITIONS"
                ),
                "upstream_partition_reference": "../core03/partition-references.json",
                "projection_fields": CORE06_REFERENCE_FIELDS,
                "source_row_count": actual_daily[0],
                "row_copy_created": False,
                "manual_evidence": {
                    "url": CSMAR_MANUAL_URL,
                    "Trdsta": (
                        "A 1..16 code table exists in the verified manual; labels "
                        "are not promoted by this candidate."
                    ),
                    "LimitDown_LimitUp_LimitStatus": (
                        "Absent from the verified manual table and therefore "
                        "semantically unresolved."
                    ),
                },
                "interpretation": (
                    "Raw source values are preserved. No ST, suspension, price-limit, "
                    "listing-eligibility, or historical-rule state is inferred."
                ),
            },
        )

        markettype_coverage = fetch_dicts(
            connection,
            f"""
                SELECT
                    Markettype AS source_markettype,
                    {exchange_case()} AS provisional_exchange_mic_claim,
                    {segment_case()} AS provisional_market_segment_claim,
                    {a_share_case()} AS a_share_scope_claim,
                    COUNT(*) AS source_row_count,
                    COUNT(DISTINCT Stkcd) AS source_code_count,
                    COUNT(DISTINCT Trddt) AS observed_date_count,
                    MIN(Trddt) AS observed_from,
                    MAX(Trddt) AS observed_to
                FROM daily_source
                GROUP BY Markettype
                ORDER BY Markettype
            """,
        )
        company_coverage = fetch_dicts(
            connection,
            """
                SELECT
                    COUNT(*) AS current_company_rows,
                    COUNT(DISTINCT co.Stkcd) AS current_company_codes,
                    COUNT(*) FILTER (WHERE d.Stkcd IS NOT NULL)
                        AS matched_to_daily_codes,
                    COUNT(*) FILTER (WHERE d.Stkcd IS NULL)
                        AS not_observed_in_daily_2020_2024
                FROM company_source co
                LEFT JOIN (
                    SELECT DISTINCT Stkcd FROM daily_source
                ) d ON d.Stkcd = co.Stkcd
            """,
        )[0]
        identity_coverage = fetch_dicts(
            connection,
            """
                SELECT
                    COUNT(*) AS daily_source_codes,
                    COUNT(*) FILTER (WHERE co.Stkcd IS NOT NULL)
                        AS matched_to_current_company,
                    COUNT(*) FILTER (WHERE co.Stkcd IS NULL)
                        AS unmatched_to_current_company
                FROM (SELECT DISTINCT Stkcd FROM daily_source) d
                LEFT JOIN company_source co ON co.Stkcd = d.Stkcd
            """,
        )[0]
        calendar_coverage = fetch_dicts(
            connection,
            """
                SELECT
                    COUNT(DISTINCT Trddt) AS distinct_observed_trade_dates,
                    MIN(Trddt) AS observed_from,
                    MAX(Trddt) AS observed_to,
                    COUNT(DISTINCT Markettype) AS observed_markettype_count
                FROM daily_source
            """,
        )[0]
        capchg_metrics = fetch_dicts(
            connection,
            """
                WITH ordered AS (
                    SELECT
                        Stkcd,
                        Trddt,
                        Capchgdt,
                        LAG(Capchgdt) OVER (
                            PARTITION BY Stkcd ORDER BY Trddt
                        ) AS previous_capchgdt
                    FROM daily_source
                )
                SELECT
                    COUNT(*) AS source_row_count,
                    COUNT(Capchgdt) AS nonnull_capchgdt_rows,
                    COUNT(DISTINCT Capchgdt) AS distinct_capchgdt_values,
                    MIN(Capchgdt) AS observed_capchgdt_min,
                    MAX(Capchgdt) AS observed_capchgdt_max,
                    COUNT(*) FILTER (
                        WHERE previous_capchgdt IS NOT NULL
                          AND Capchgdt IS DISTINCT FROM previous_capchgdt
                    ) AS transition_clue_count,
                    COUNT(DISTINCT Stkcd) FILTER (
                        WHERE previous_capchgdt IS NOT NULL
                          AND Capchgdt IS DISTINCT FROM previous_capchgdt
                    ) AS source_codes_with_transition_clue,
                    COUNT(*) FILTER (
                        WHERE previous_capchgdt IS NOT NULL
                          AND Capchgdt < previous_capchgdt
                    ) AS backward_transition_clues
                FROM ordered
            """,
        )[0]
        trdsta_distribution = fetch_dicts(
            connection,
            """
                SELECT
                    Trdsta AS raw_trdsta_code,
                    COUNT(*) AS source_row_count,
                    COUNT(DISTINCT Stkcd) AS source_code_count
                FROM daily_source
                GROUP BY Trdsta
                ORDER BY source_row_count DESC, Trdsta
            """,
        )
        limitstatus_distribution = fetch_dicts(
            connection,
            """
                SELECT
                    LimitStatus AS raw_limitstatus_code,
                    COUNT(*) AS source_row_count,
                    COUNT(DISTINCT Stkcd) AS source_code_count
                FROM daily_source
                GROUP BY LimitStatus
                ORDER BY source_row_count DESC, LimitStatus NULLS LAST
            """,
        )
        in_scope = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT Stkcd) FROM daily_source "
            "WHERE Markettype IN (1, 4, 16, 32, 64)"
        ).fetchone()
        out_scope = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT Stkcd) FROM daily_source "
            "WHERE Markettype IN (2, 8)"
        ).fetchone()
        calendar_diagnostic_rows = connection.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT Markettype, Trddt FROM daily_source GROUP BY Markettype, Trddt"
            ")"
        ).fetchone()[0]
        provisional_episode_rows = connection.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT Stkcd, Markettype FROM daily_source GROUP BY Stkcd, Markettype"
            ")"
        ).fetchone()[0]

        coverage = {
            "schema_version": 1,
            "candidate_id": f"{CANDIDATE_DATASET_ID}@{run_id}",
            "purpose_ceiling": PURPOSE_CEILING,
            "CORE-01": {
                "current_company_observations": company_coverage,
                "daily_to_company_mapping": identity_coverage,
                "provisional_code_episode_rows": provisional_episode_rows,
                "permanent_security_id_rows": 0,
                "status": "PROVISIONAL_SOURCE_CODE_OBSERVATIONS_ONLY",
            },
            "CORE-02": {
                **calendar_coverage,
                "diagnostic_rows": calendar_diagnostic_rows,
                "official_calendar_rows": 0,
                "official_session_rule_rows": 0,
                "status": "OBSERVED_TRADE_DATES_ONLY__NOT_OFFICIAL_CALENDAR",
            },
            "CORE-03": {
                "source_row_count": actual_daily[0],
                "source_code_count": actual_daily[1],
                "distinct_observed_trade_dates": actual_daily[2],
                "observed_from": actual_daily[3],
                "observed_to": actual_daily[4],
                "source_field_count": 24,
                "partition_count": len(daily_files),
                "partition_rows_copied": 0,
                "a_share_scope_rows": in_scope[0],
                "a_share_scope_codes": in_scope[1],
                "observed_b_share_rows": out_scope[0],
                "observed_b_share_codes": out_scope[1],
                "markettype_coverage": markettype_coverage,
                "permanent_identity_mapping_rows": 0,
                "multi_source_calibration_rows": 0,
                "status": "PINNED_SOURCE_PARTITION_REFERENCES_AND_MAPPING_DIAGNOSTICS",
            },
            "CORE-04": {
                **capchg_metrics,
                "official_pit_normalized_metadata_candidates": validated["official"][
                    "parse_summary"
                ]["normalized_pit_candidate_count"],
                "official_pit_available_at_values_created": 0,
                "action_term_rows": 0,
                "action_revision_chain_rows": 0,
                "status": "CLUE_INVENTORY_ONLY__NO_ACTION_DATASET",
            },
            "CORE-05": {
                "adjustment_projection_rows": 0,
                "forward_adjustment_factor_rows": 0,
                "adjusted_daily_rows": 0,
                "status": "BLOCKED_NO_OUTPUT",
            },
            "CORE-06": {
                "raw_status_rows_referenced": actual_daily[0],
                "row_copy_created": False,
                "trdsta_distribution": trdsta_distribution,
                "limitstatus_distribution": limitstatus_distribution,
                "official_historical_rule_rows": 0,
                "status": "RAW_SOURCE_CODE_OBSERVATIONS_ONLY",
            },
            "manual_evidence": {
                "url": CSMAR_MANUAL_URL,
                "defined_export_fields": sorted(MANUAL_DEFINED_DAILY_FIELDS),
                "unresolved_current_export_extensions": sorted(
                    UNRESOLVED_EXPORT_EXTENSION_FIELDS
                ),
                "version_alignment_status": (
                    "BLOCKED__VERIFIED_MANUAL_HAS_21_FIELDS__"
                    "CURRENT_EXPORT_HAS_24"
                ),
            },
            "mandatory_interpretation": (
                "Coverage and mechanical observations do not establish source "
                "admission, PIT, official identity, official calendar, corporate "
                "actions, adjustment validity, tradability semantics, or research use."
            ),
        }
        write_json(stage / "coverage.json", coverage)

        gap_matrix = {
            "schema_version": 1,
            "candidate_id": f"{CANDIDATE_DATASET_ID}@{run_id}",
            "overall_status": (
                "PHASE_A_ENGINEERING_COMPLETE__D2A_04_COMPLETION_GATE_NOT_MET__"
                "INSPECTION_ONLY__NOT_ADMITTED"
            ),
            "completion_gate_passed": False,
            "D2B_blocked": True,
            "datasets": {
                "CORE-01": {
                    "status": "PARTIAL_PROVISIONAL_CODE_OBSERVATIONS",
                    "preserved": [
                        "6013 current company source-code/name/list-date observations",
                        "5659 daily-observed source codes and markettype claims",
                        "provisional code episodes explicitly marked non-permanent",
                    ],
                    "missing": [
                        "permanent security_id",
                        "official code/name/board/status history and valid intervals",
                        "complete delisted/relisted evidence",
                        "published_at, available_at, and revision chain",
                    ],
                },
                "CORE-02": {
                    "status": "OBSERVED_TRADE_DATE_DIAGNOSTIC_ONLY",
                    "preserved": [
                        "dates on which candidate source rows were observed by markettype"
                    ],
                    "missing": [
                        "official open and closed day rows",
                        "temporary closure evidence",
                        "official session rules and historical revisions",
                        "PIT available_at for each calendar revision",
                    ],
                },
                "CORE-03": {
                    "status": "PINNED_SOURCE_REFERENCES_WITH_CRITICAL_GAPS",
                    "preserved": [
                        "5852287 rows in 60 immutable upstream partitions",
                        "all 24 source fields and source-file lineage by reference",
                        "markettype mapping from verified official manual",
                        "source-code mapping and coverage diagnostics",
                    ],
                    "missing": [
                        "permanent CORE-01 identity",
                        "qualified CORE-02 official calendar",
                        "formal units, NULL and sentinel closure",
                        "multi-source calibration",
                        "source admission and retention-right closure",
                        (
                            "LimitDown/LimitUp/LimitStatus semantics: fields are "
                            "absent from the verified 21-field manual table"
                        ),
                    ],
                },
                "CORE-04": {
                    "status": "CLUES_ONLY__NO_CONTRACT_CONFORMANT_ACTION_ROWS",
                    "preserved": [
                        "Capchgdt values and transition clues",
                        "14 official/CNINFO metadata candidates by pinned tree reference",
                    ],
                    "missing": [
                        "action_id, action_type, and lifecycle stage",
                        "cash/bonus/rights/split terms and ratios",
                        "record, ex, payment, and implementation dates",
                        "published_at and conservative available_at",
                        "correction and supersedes revision chain",
                    ],
                    "proof": (
                        "Capchgdt is a repeated source clue and official metadata has "
                        "zero accepted available_at values; neither contains the "
                        "required event terms or revision lifecycle."
                    ),
                },
                "CORE-05": {
                    "status": "BLOCKED_NO_OUTPUT",
                    "preserved": [],
                    "missing": [
                        "qualified pinned CORE-03 snapshot",
                        "qualified pinned CORE-04 snapshot",
                        "knowledge cutoff, base date, and formula version",
                    ],
                    "prohibition": (
                        "Adjprcwd and Adjprcnd remain source observations and are not "
                        "used to invent a CORE-05 projection."
                    ),
                },
                "CORE-06": {
                    "status": "RAW_CODES_ONLY__SEMANTICS_AND_RULE_HISTORY_OPEN",
                    "preserved": [
                        "Trdsta/LimitStatus/LimitDown/LimitUp/PreClosePrice by "
                        "immutable partition reference",
                        "raw value distributions without semantic inference",
                        "manual evidence that a Trdsta 1..16 code table exists",
                    ],
                    "missing": [
                        "frozen Trdsta label mapping and source version",
                        "LimitDown/LimitUp/LimitStatus source dictionary",
                        "historical ST, suspension, delisting and eligibility events",
                        "board/IPO/relisting price-limit rule versions",
                    ],
                },
            },
            "rights": {
                "status": (
                    "USER_ASSERTED_ACADEMIC_LOCAL_RESEARCH_ACCESS__"
                    "INDEPENDENT_RETENTION_TERMS_OPEN"
                ),
                "blocks_formal_admission": True,
            },
            "next_action": "PAUSE_AND_REPORT__DO_NOT_START_D2B",
        }
        write_json(stage / "gap-matrix.json", gap_matrix)
        write_json(
            stage / "core04" / "gap-evidence.json",
            {
                "schema_version": 1,
                "dataset": "CORE-04",
                "capchgdt_metrics": capchg_metrics,
                "official_pit": {
                    "root": validated["official"]["root"].as_posix(),
                    "tree_sha256": validated["official"]["tree_sha256"],
                    "normalized_metadata_candidates": validated["official"][
                        "parse_summary"
                    ]["normalized_pit_candidate_count"],
                    "available_at_values_created": 0,
                    "bse_policy": validated["official"]["parse_summary"]["bse_policy"],
                    "cninfo_policy": validated["official"]["parse_summary"][
                        "cninfo_policy"
                    ],
                },
                "contract_conformant_action_rows": 0,
                "conclusion": (
                    "Capchgdt transitions and disclosure metadata are useful discovery "
                    "clues but cannot supply event terms, ratios, lifecycle stages, "
                    "publication availability, or revision chains."
                ),
            },
        )
        catalog_preseal = sha256_file(CATALOG_PATH)
        if catalog_preseal != validated["catalog_sha256_before"]:
            raise ClosureError("formal catalog changed before candidate sealing")
        provenance = {
            "schema_version": 1,
            "candidate_id": f"{CANDIDATE_DATASET_ID}@{run_id}",
            "created_at": utc_now(),
            "tool_id": TOOL_ID,
            "runner_path": Path(__file__).resolve().as_posix(),
            "runner_sha256": sha256_file(Path(__file__).resolve()),
            "plan": {
                "path": validated["plan"]["path"].as_posix(),
                "sha256": validated["plan"]["sha256"],
                "plan_id": PLAN_ID,
            },
            "inputs": [
                {
                    "role": validated["daily"]["role"],
                    "root": validated["daily"]["root"].as_posix(),
                    "manifest_sha256": validated["daily"]["manifest_sha256"],
                    "content_hash": validated["daily"]["content_hash"],
                },
                {
                    "role": validated["company"]["role"],
                    "root": validated["company"]["root"].as_posix(),
                    "manifest_sha256": validated["company"]["manifest_sha256"],
                    "content_hash": validated["company"]["content_hash"],
                },
                {
                    "role": validated["official"]["role"],
                    "root": validated["official"]["root"].as_posix(),
                    "tree_sha256": validated["official"]["tree_sha256"],
                },
            ],
            "official_manual_evidence": {
                "url": CSMAR_MANUAL_URL,
                "verified_scope": (
                    "TRD_Dalyr pages 23-24: 21 fields, Markettype mapping, "
                    "and a Trdsta 1..16 code table"
                ),
                "version_mismatch": (
                    "Current export additionally contains LimitDown, LimitUp, "
                    "and LimitStatus, which are absent from the verified table."
                ),
            },
            "catalog_guard": {
                "path": CATALOG_PATH.as_posix(),
                "sha256_before": validated["catalog_sha256_before"],
                "sha256_preseal": catalog_preseal,
                "write_attempted": False,
            },
            "authorization": {
                "purpose_ceiling": PURPOSE_CEILING,
                "network_requests": False,
                "formal_catalog_write": False,
                "source_admission": False,
                "quality_promotion_or_waiver": False,
                "factor_efficacy_research": False,
                "adjusted_projection": False,
                "automatic_cleanup": False,
            },
            "rights_status": (
                "USER_ASSERTED_ACADEMIC_LOCAL_RESEARCH_ACCESS__"
                "INDEPENDENT_RETENTION_TERMS_OPEN"
            ),
            "output_root": final_path.as_posix(),
        }
        write_json(stage / "provenance.json", provenance)

        referenced_bytes = partition_reference_value["referenced_curated_bytes"]
        footprint = {
            "schema_version": 1,
            "candidate_id": f"{CANDIDATE_DATASET_ID}@{run_id}",
            "storage_guard": validated["storage_preflight"],
            "upstream_daily_curated_bytes_referenced": referenced_bytes,
            "upstream_daily_rows_referenced": actual_daily[0],
            "upstream_daily_rows_copied": 0,
            "estimated_duplicate_bytes_avoided": referenced_bytes,
            "candidate_bytes_including_manifest": 0,
            "max_new_footprint_bytes": validated["storage_preflight"][
                "max_new_footprint_bytes"
            ],
            "status": "PENDING_SEAL",
        }
        manifest_path = stage / "manifest.json"
        final_candidate_bytes = 0
        manifest: dict[str, Any] = {}
        for _ in range(6):
            write_json(stage / "footprint.json", footprint)
            inventory = candidate_inventory(stage, exclude={"manifest.json"})
            content_contract = {
                "hash_algorithm": HASH_ALGORITHM,
                "candidate_id": f"{CANDIDATE_DATASET_ID}@{run_id}",
                "plan_sha256": validated["plan"]["sha256"],
                "input_pins": provenance["inputs"],
                "payload_inventory": inventory,
            }
            manifest = {
                "schema_version": 1,
                "dataset_id": CANDIDATE_DATASET_ID,
                "snapshot_id": run_id,
                "candidate_id": f"{CANDIDATE_DATASET_ID}@{run_id}",
                "work_package": WORK_PACKAGE,
                "plan_id": PLAN_ID,
                "lifecycle": "candidate",
                "mutability": "immutable_after_seal",
                "purpose_ceiling": PURPOSE_CEILING,
                "catalog_registered": False,
                "source_admission": False,
                "quality_promoted": False,
                "research_ready": False,
                "formal_factor_research_allowed": False,
                "network_requests_performed": False,
                "root_path": final_path.as_posix(),
                "hash_algorithm": HASH_ALGORITHM,
                "content_hash": sha256_bytes(canonical_bytes(content_contract)),
                "payload_inventory": inventory,
                "implemented_scope": [
                    "CORE-01 provisional code episodes and current observations",
                    "CORE-02 observed-trade-date diagnostics",
                    "CORE-03 pinned partition references and mapping coverage",
                    "CORE-04 clue inventory and gap proof",
                    "CORE-06 raw status references and value distributions",
                ],
                "blocked_no_output_scope": ["CORE-05"],
                "overall_status": gap_matrix["overall_status"],
            }
            write_json(manifest_path, manifest)
            final_candidate_bytes = directory_bytes(stage)
            if footprint["candidate_bytes_including_manifest"] == final_candidate_bytes:
                break
            footprint["candidate_bytes_including_manifest"] = final_candidate_bytes
            footprint["status"] = "PASS"
        else:
            raise ClosureError("candidate footprint/manifest seal did not converge")
        if final_candidate_bytes > validated["storage_preflight"][
            "max_new_footprint_bytes"
        ]:
            raise ClosureError("candidate exceeds the 20 GiB footprint limit")
        if strict_json(stage / "footprint.json")[
            "candidate_bytes_including_manifest"
        ] != final_candidate_bytes:
            raise ClosureError("sealed footprint does not equal actual candidate bytes")
        if sha256_file(CATALOG_PATH) != validated["catalog_sha256_before"]:
            raise ClosureError("formal catalog changed during candidate construction")
        return {
            "manifest": manifest,
            "coverage": coverage,
            "gap_matrix": gap_matrix,
            "footprint": strict_json(stage / "footprint.json"),
            "candidate_bytes": final_candidate_bytes,
            "daily_actual": actual_daily,
            "company_actual": actual_company,
            "partition_count": len(daily_files),
        }
    finally:
        connection.close()


def qdata_research_rejection(dataset_id: str, snapshot_id: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(DATASET_CLI),
            "resolve",
            dataset_id,
            snapshot_id,
            "--intent",
            "research",
            "--json",
        ],
        cwd=WORKSPACE_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode == 0:
        raise ClosureError("qdata unexpectedly resolved the D2A-04 candidate for research")
    return {
        "command": (
            f"dataset_cli.py resolve {dataset_id} {snapshot_id} "
            "--intent research --json"
        ),
        "returncode": result.returncode,
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
        "status": "PASS__CANDIDATE_REJECTED_FOR_RESEARCH",
    }


def run_build(plan_path: Path, *, run_id: str, execute: bool) -> dict[str, Any]:
    run_id = validate_run_id(run_id)
    validated = validate_inputs(plan_path)
    final_path = OUTPUT_BASE / run_id
    if final_path.exists():
        raise ClosureError(f"candidate output already exists: {final_path}")
    if REPORT_DIR.exists():
        raise ClosureError(f"report directory already exists: {REPORT_DIR}")
    plan_result = {
        "status": "PLAN_READY__NO_WRITES",
        "write_performed": False,
        "plan_id": PLAN_ID,
        "plan_sha256": validated["plan"]["sha256"],
        "candidate_path": final_path.as_posix(),
        "report_path": REPORT_DIR.as_posix(),
        "catalog_sha256": validated["catalog_sha256_before"],
        "daily_content_hash": validated["daily"]["content_hash"],
        "company_content_hash": validated["company"]["content_hash"],
        "official_pit_tree_sha256": validated["official"]["tree_sha256"],
        "storage_preflight": validated["storage_preflight"],
        "purpose_ceiling": PURPOSE_CEILING,
    }
    if not execute:
        return plan_result

    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    stage = OUTPUT_BASE / f".{run_id}.staging-{uuid.uuid4().hex}"
    report_stage = REPORT_DIR.parent / f".{REPORT_DIR.name}.staging-{uuid.uuid4().hex}"
    if stage.exists() or report_stage.exists():
        raise ClosureError("unexpected staging path collision")
    build = build_candidate(
        validated, run_id=run_id, stage=stage, final_path=final_path
    )
    catalog_pre_publish = sha256_file(CATALOG_PATH)
    if catalog_pre_publish != validated["catalog_sha256_before"]:
        raise ClosureError("formal catalog changed before candidate publication")
    os.replace(stage, final_path)
    catalog_after_candidate = sha256_file(CATALOG_PATH)
    if catalog_after_candidate != validated["catalog_sha256_before"]:
        raise ClosureError("formal catalog changed after candidate publication")

    candidate_tree_hash, candidate_tree_inventory = deterministic_tree_hash(final_path)
    qdata_guard = qdata_research_rejection(CANDIDATE_DATASET_ID, run_id)
    post_storage = storage_guard(validated["plan"]["storage_guard"])
    if post_storage["free_fraction"] < 0.25:
        raise ClosureError("post-build G: free fraction fell below 25 percent")
    if build["candidate_bytes"] > 20 * 1024**3:
        raise ClosureError("post-build candidate footprint exceeded 20 GiB")

    report_stage.mkdir(parents=True, exist_ok=False)
    work_package = {
        "schema_version": 1,
        "work_package": WORK_PACKAGE,
        "plan_id": PLAN_ID,
        "phase": "PHASE_A_LOCAL_EVIDENCE_CLOSURE",
        "run_id": run_id,
        "candidate_id": f"{CANDIDATE_DATASET_ID}@{run_id}",
        "candidate_path": final_path.as_posix(),
        "candidate_content_hash": build["manifest"]["content_hash"],
        "candidate_tree_sha256": candidate_tree_hash,
        "status": (
            "COMPLETED__PHASE_A_LOCAL_ENGINEERING__"
            "D2A_04_COMPLETION_GATE_NOT_MET__PAUSED"
        ),
        "formal_admissions": 0,
        "catalog_registered": False,
        "research_ready": False,
        "CORE-05_output_rows": 0,
        "outputs": {
            "CORE-01": [
                "core01/provisional-code-episode-observations.parquet",
                "core01/current-company-master-observations.parquet",
            ],
            "CORE-02": ["core02/observed-trade-date-diagnostics.parquet"],
            "CORE-03": [
                "core03/partition-references.json",
                "core03/code-mapping-coverage.parquet",
                "core03/source-field-contract.json",
            ],
            "CORE-04": [
                "core04/capchgdt-transition-clues.parquet",
                "core04/gap-evidence.json",
            ],
            "CORE-05": [],
            "CORE-06": [
                "core06/status-observation-reference.json",
                "core06/trdsta-value-distribution.parquet",
                "core06/limit-field-value-distribution.parquet",
            ],
        },
        "completion_gate_passed": False,
        "D2B_blocked": True,
        "next_action": "PAUSE_AND_REPORT__SEPARATE_USER_START_REQUIRED",
    }
    catalog_after = sha256_file(CATALOG_PATH)
    verification = {
        "schema_version": 1,
        "work_package": WORK_PACKAGE,
        "run_id": run_id,
        "status": "PASS_WITH_DECLARED_D2A04_GAPS",
        "checks": {
            "frozen_plan_validated": True,
            "daily_manifest_content_and_payload_hashes_validated": True,
            "company_manifest_content_and_payload_hashes_validated": True,
            "official_pit_tree_hash_validated": True,
            "daily_actual_rows": build["daily_actual"][0],
            "daily_actual_distinct_source_codes": build["daily_actual"][1],
            "daily_partition_count": build["partition_count"],
            "company_actual_rows": build["company_actual"][0],
            "catalog_sha256_before": validated["catalog_sha256_before"],
            "catalog_sha256_pre_publish": catalog_pre_publish,
            "catalog_sha256_after": catalog_after,
            "catalog_unchanged": catalog_after
            == validated["catalog_sha256_before"],
            "network_requests_performed": False,
            "candidate_registered": False,
            "research_ready": False,
            "qdata_research_guard": qdata_guard,
            "CORE05_output_rows": 0,
            "storage_preflight": validated["storage_preflight"],
            "storage_postbuild": post_storage,
            "candidate_bytes": build["candidate_bytes"],
            "candidate_tree_file_count": len(candidate_tree_inventory),
            "candidate_tree_sha256": candidate_tree_hash,
            "candidate_under_20_gib": build["candidate_bytes"] <= 20 * 1024**3,
            "postbuild_free_fraction_at_least_25_percent": post_storage[
                "free_fraction"
            ]
            >= 0.25,
        },
        "declared_blockers": build["gap_matrix"],
        "mandatory_interpretation": (
            "PASS verifies offline candidate engineering and guards only. It does "
            "not pass the D2A-04 completion gate, admit data, or authorize research."
        ),
    }
    write_json(report_stage / "WORK_PACKAGE.json", work_package)
    write_json(report_stage / "VERIFICATION.json", verification)
    write_text(
        report_stage / "README.md",
        "# D2A-04 Phase A local candidate closure\n\n"
        f"- Candidate: `{final_path.as_posix()}`\n"
        f"- Candidate bytes: `{build['candidate_bytes']}`\n"
        f"- Candidate tree SHA-256: `{candidate_tree_hash}`\n"
        "- Formal catalog admissions: `0`\n"
        "- Purpose ceiling: `INSPECTION_ONLY`\n"
        "- CORE-05 adjusted output rows: `0`\n"
        "- Result: Phase-A engineering completed, but the D2A-04 completion "
        "gate remains unmet and D2B stays blocked.\n",
    )
    os.replace(report_stage, REPORT_DIR)
    final_catalog = sha256_file(CATALOG_PATH)
    if final_catalog != validated["catalog_sha256_before"]:
        raise ClosureError("formal catalog changed after report publication")
    return {
        **plan_result,
        "status": work_package["status"],
        "write_performed": True,
        "candidate_path": final_path.as_posix(),
        "report_path": REPORT_DIR.as_posix(),
        "candidate_content_hash": build["manifest"]["content_hash"],
        "candidate_tree_sha256": candidate_tree_hash,
        "candidate_bytes": build["candidate_bytes"],
        "catalog_sha256_after": final_catalog,
        "qdata_research_guard": qdata_guard["status"],
        "completion_gate_passed": False,
        "D2B_blocked": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline D2A-04 Phase-A candidate closure runner"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser(
        "validate", help="validate frozen plan and inputs without writing"
    )
    validate.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    build = subparsers.add_parser(
        "build", help="plan or execute the isolated candidate build"
    )
    build.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    build.add_argument("--run-id", required=True)
    build.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            validated = validate_inputs(args.plan)
            result = {
                "status": "VALIDATED__READ_ONLY__NO_OUTPUT_WRITTEN",
                "plan_id": PLAN_ID,
                "plan_sha256": validated["plan"]["sha256"],
                "catalog_sha256": validated["catalog_sha256_before"],
                "daily_content_hash": validated["daily"]["content_hash"],
                "company_content_hash": validated["company"]["content_hash"],
                "official_pit_tree_sha256": validated["official"]["tree_sha256"],
                "storage_preflight": validated["storage_preflight"],
            }
        else:
            result = run_build(
                args.plan, run_id=args.run_id, execute=bool(args.execute)
            )
    except ClosureError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            json_safe(result),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
