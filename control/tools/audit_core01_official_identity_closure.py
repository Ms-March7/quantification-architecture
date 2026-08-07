#!/usr/bin/env python3
"""Build a fail-closed CORE-01 official identity evidence package.

This audit is deliberately admission-neutral.  It reads only the exact
hash-bound SSE/SZSE/BSE inspection packages already present in the workspace,
plus the frozen CORE-01 contract and the prior clause-level measurements.  It
does not fetch data, modify the catalog, publish a snapshot, or promote quality.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import stat
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import openpyxl


RUN_ID = "stage-a-core01-official-identity-closure-20260730-v1"
SHANGHAI = timezone(timedelta(hours=8))
DEFAULT_OUTPUT = (
    "reports/data-governance/"
    "stage-a-core01-official-identity-closure-20260730-v1"
)
REPORT_FILES = {
    "CLAUSE_DECISIONS.json",
    "IDENTITY_AMBIGUITY_QUARANTINE.json",
    "OFFICIAL_COVERAGE_MATRIX.json",
    "README.md",
    "REPORT_MANIFEST.json",
    "RIGHTS_AND_ENDPOINT_DECISION.json",
    "SUBCHECK_EVIDENCE_MATRIX.json",
    "SZSE_EVENT_SAMPLE.jsonl",
    "VERIFICATION.json",
}
PROTECTED_AUTHORITY_FILES = {
    "datasets_json": "data/catalog/datasets.json",
    "source_registry": (
        "data/catalog/contracts/source_admission_v1/"
        "current_source_registry.json"
    ),
    "roadmap": "control/ROADMAP.md",
}
FORMAL_STORE_ROOTS = {
    "market_snapshots": "data/market/snapshots",
    "published": "data/published",
}
INVENTORY_MAX_ENTRIES = 10_000
INVENTORY_MAX_DEPTH = 8
INVENTORY_MAX_TOTAL_BYTES = 64 * 1024 * 1024


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_ref(workspace: Path, path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return {
        "path": resolved.relative_to(workspace.resolve()).as_posix(),
        "bytes": resolved.stat().st_size,
        "sha256": sha256(resolved),
    }


def require_report_path(path: Path, workspace: Path) -> None:
    report_parent = (workspace / "reports" / "data-governance").resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(report_parent)
    except ValueError as exc:
        raise ValueError(
            f"report output escapes reports/data-governance: {resolved}"
        ) from exc
    if resolved == report_parent:
        raise ValueError("report output must be a child of reports/data-governance")


def reject_unexpected_report_residuals(output: Path) -> None:
    if not output.exists():
        return
    if not output.is_dir():
        raise ValueError(f"report output is not a directory: {output}")
    residuals = sorted(
        item.name
        for item in output.iterdir()
        if not item.is_file() or item.name not in REPORT_FILES
    )
    if residuals:
        raise ValueError(
            "refusing replacement because unregistered report residuals exist: "
            f"{residuals}"
        )


def is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse_flag)


def stable_file_inventory_ref(path: Path, relative_path: str) -> dict[str, Any]:
    before = path.stat()
    digest = sha256(path)
    after = path.stat()
    stable_fields_before = (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    stable_fields_after = (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if stable_fields_before != stable_fields_after:
        raise RuntimeError(f"inventory file changed while hashing: {path}")
    return {
        "path": relative_path,
        "bytes": after.st_size,
        "sha256": digest,
    }


def controlled_inventory(
    workspace: Path,
    relative_root: str,
    *,
    max_entries: int = INVENTORY_MAX_ENTRIES,
    max_depth: int = INVENTORY_MAX_DEPTH,
    max_total_bytes: int = INVENTORY_MAX_TOTAL_BYTES,
) -> dict[str, Any]:
    """Hash a small, exact formal-store root with hard traversal limits."""

    root = workspace / relative_root
    if not root.exists():
        return {
            "path": relative_root,
            "exists": False,
            "file_count": 0,
            "directory_count": 0,
            "total_bytes": 0,
            "entries_sha256": hashlib.sha256(b"[]").hexdigest(),
        }
    if is_reparse_point(root):
        raise RuntimeError(f"formal-store root is a link/reparse point: {root}")
    if not root.is_dir():
        raise RuntimeError(f"formal-store root is not a directory: {root}")

    entries: list[dict[str, Any]] = []
    total_bytes = 0
    file_count = 0
    directory_count = 0

    def visit(directory: Path, depth: int) -> None:
        nonlocal total_bytes, file_count, directory_count
        if depth > max_depth:
            raise RuntimeError(
                f"formal-store inventory exceeded max depth {max_depth}: "
                f"{directory}"
            )
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            if len(entries) >= max_entries:
                raise RuntimeError(
                    f"formal-store inventory exceeded max entries {max_entries}: "
                    f"{root}"
                )
            if is_reparse_point(child):
                raise RuntimeError(
                    f"formal-store inventory rejects link/reparse point: {child}"
                )
            relative = child.relative_to(root).as_posix()
            if child.is_dir():
                directory_count += 1
                entries.append({"path": relative, "type": "directory"})
                visit(child, depth + 1)
            elif child.is_file():
                size = child.stat().st_size
                if total_bytes + size > max_total_bytes:
                    raise RuntimeError(
                        "formal-store inventory exceeded content-hash byte "
                        f"budget {max_total_bytes}: {root}"
                    )
                reference = stable_file_inventory_ref(child, relative)
                total_bytes += reference["bytes"]
                file_count += 1
                entries.append({"type": "file", **reference})
            else:
                raise RuntimeError(
                    f"formal-store inventory rejects non-file entry: {child}"
                )

    visit(root, 0)
    encoded = json.dumps(
        entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "path": relative_root,
        "exists": True,
        "file_count": file_count,
        "directory_count": directory_count,
        "total_bytes": total_bytes,
        "entries_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def capture_protected_state(workspace: Path) -> dict[str, Any]:
    return {
        "authority_files": {
            name: file_ref(workspace, workspace / relative_path)
            for name, relative_path in PROTECTED_AUTHORITY_FILES.items()
        },
        "formal_store_inventories": {
            name: controlled_inventory(workspace, relative_path)
            for name, relative_path in FORMAL_STORE_ROOTS.items()
        },
    }


def build_mutation_guard(
    pre: dict[str, Any], post: dict[str, Any]
) -> dict[str, Any]:
    authority_checks = {
        name: pre["authority_files"][name] == post["authority_files"][name]
        for name in PROTECTED_AUTHORITY_FILES
    }
    inventory_checks = {
        name: (
            pre["formal_store_inventories"][name]
            == post["formal_store_inventories"][name]
        )
        for name in FORMAL_STORE_ROOTS
    }
    changed = [
        f"authority_files.{name}"
        for name, passed in authority_checks.items()
        if not passed
    ] + [
        f"formal_store_inventories.{name}"
        for name, passed in inventory_checks.items()
        if not passed
    ]
    if changed:
        raise RuntimeError(
            "protected state changed during audit; failing closed: "
            + ", ".join(changed)
        )
    return {
        "status": "PASS__PROTECTED_STATE_PRE_EQUALS_POST",
        "policy": {
            "authority_files": list(PROTECTED_AUTHORITY_FILES.values()),
            "formal_store_roots": list(FORMAL_STORE_ROOTS.values()),
            "inventory_content_hashing": "ALL_REGULAR_FILES_WITHIN_LIMITS",
            "reparse_points_allowed": False,
            "max_entries_per_root": INVENTORY_MAX_ENTRIES,
            "max_depth": INVENTORY_MAX_DEPTH,
            "max_total_file_bytes_per_root": INVENTORY_MAX_TOTAL_BYTES,
            "limit_exceeded_behavior": "FAIL_CLOSED",
        },
        "pre": pre,
        "post": post,
        "checks": {
            "authority_files": authority_checks,
            "formal_store_inventories": inventory_checks,
            "all_pre_equals_post": True,
        },
        "residual_risk": (
            "The guard proves byte equality for the three authority files and "
            "content-inventory equality for the two exact formal-store roots "
            "across this audit before final report-only writes. It cannot prevent "
            "an unrelated external process from writing after the post capture."
        ),
    }


def manifest_entries(manifest_path: Path) -> list[dict[str, Any]]:
    manifest = read_json(manifest_path)
    return manifest["files"] if isinstance(manifest, dict) else manifest


def official_evidence_ref(
    workspace: Path,
    source_root: Path,
    manifest_path: Path,
    relative_path: str,
    *,
    source_id: str,
    evidence_role: str,
    applicable_date_range: dict[str, Any],
    boundary: str,
    official_url_override: str | None = None,
    retrieved_at_override: str | None = None,
    http_status_override: int | None = None,
) -> dict[str, Any]:
    entry = next(
        (
            item
            for item in manifest_entries(manifest_path)
            if (item.get("path") or item.get("relative_path")) == relative_path
        ),
        None,
    )
    if entry is None:
        raise KeyError(f"{relative_path} missing from {manifest_path}")
    local = file_ref(workspace, source_root / relative_path)
    if (
        local["bytes"] != int(entry["bytes"])
        or local["sha256"] != entry["sha256"]
    ):
        raise RuntimeError(f"Evidence hash mismatch for {relative_path}")
    return {
        "source_id": source_id,
        "evidence_role": evidence_role,
        "official_url": (
            official_url_override
            or entry.get("url")
            or entry.get("request_url")
            or entry.get("final_url")
        ),
        "retrieved_at": (
            retrieved_at_override
            or entry.get("retrieved_at")
            or entry.get("retrieved_at_asia_shanghai")
        ),
        "http_status": (
            http_status_override
            if http_status_override is not None
            else entry.get("http_status")
        ),
        "local_file": local,
        "applicable_date_range": applicable_date_range,
        "boundary": boundary,
    }


def normalize_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()[:10]
    digits = re.sub(r"\D", "", text)
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def verify_declared_file(
    workspace: Path,
    root: Path,
    relative_path: str,
    declared_bytes: int,
    declared_sha256: str,
) -> dict[str, Any]:
    path = root / relative_path
    actual_exists = path.is_file()
    actual_bytes = path.stat().st_size if actual_exists else None
    actual_sha256 = sha256(path) if actual_exists else None
    passed = (
        actual_exists
        and actual_bytes == int(declared_bytes)
        and actual_sha256 == declared_sha256
    )
    return {
        "path": path.resolve().relative_to(workspace.resolve()).as_posix(),
        "declared_bytes": int(declared_bytes),
        "actual_bytes": actual_bytes,
        "declared_sha256": declared_sha256,
        "actual_sha256": actual_sha256,
        "status": "PASS" if passed else "FAIL",
    }


def verify_source_manifest(
    workspace: Path, source_root: Path, manifest_path: Path, source_id: str
) -> dict[str, Any]:
    entries = manifest_entries(manifest_path)
    checks: list[dict[str, Any]] = []
    for entry in entries:
        relative = entry.get("path") or entry.get("relative_path")
        checks.append(
            verify_declared_file(
                workspace,
                source_root,
                relative,
                entry["bytes"],
                entry["sha256"],
            )
        )
    failures = [item for item in checks if item["status"] != "PASS"]
    return {
        "source_id": source_id,
        "manifest": file_ref(workspace, manifest_path),
        "declared_files": len(entries),
        "passed": len(entries) - len(failures),
        "failed": len(failures),
        "status": "PASS" if not failures else "FAIL",
        "checks": checks,
    }


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def clean_html(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value or "").strip()


def iso_from_epoch_ms(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, tz=SHANGHAI).isoformat()


def load_szse_master(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    # The official workbook declares an incorrect used range in read-only mode,
    # so normal mode is required to expose all 2,893 physical rows.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        workbook = openpyxl.load_workbook(path, read_only=False, data_only=True)
    sheet = workbook.active
    headers = [
        str(sheet.cell(1, column).value).strip()
        if sheet.cell(1, column).value is not None
        else ""
        for column in range(1, sheet.max_column + 1)
    ]
    rows: list[dict[str, Any]] = []
    for row_index in range(2, sheet.max_row + 1):
        row = {
            headers[column - 1]: sheet.cell(row_index, column).value
            for column in range(1, sheet.max_column + 1)
        }
        rows.append(row)
    return headers, rows


def build_sse_measurements(sse_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    derived = sse_root / "derived"
    main_current = read_csv_rows(derived / "005_listed_main_a__股票.csv")
    star_current = read_csv_rows(derived / "007_listed_star__股票.csv")
    main_delisted = read_csv_rows(derived / "008_delisted_main__终止上市公司.csv")
    star_delisted = read_csv_rows(derived / "009_delisted_star__终止上市公司.csv")
    current_rows = main_current + star_current
    delisted_rows = main_delisted + star_delisted
    current_codes = {row["A股代码"] for row in current_rows}
    delisted_codes = {row["原公司代码"] for row in delisted_rows}
    current_listing_dates = [
        date
        for row in current_rows
        if (date := normalize_date(row.get("上市日期")))
    ]
    delisted_listing_dates = [
        date
        for row in delisted_rows
        if (date := normalize_date(row.get("上市日期")))
    ]
    delisting_dates = [
        date
        for row in delisted_rows
        if (date := normalize_date(row.get("终止上市日期")))
    ]
    delisted_counts = Counter(row["原公司代码"] for row in delisted_rows)
    duplicate_codes = sorted(code for code, count in delisted_counts.items() if count > 1)
    ambiguity_rows: list[dict[str, Any]] = []
    for code in duplicate_codes:
        rows = [row for row in delisted_rows if row["原公司代码"] == code]
        ambiguity_rows.append(
            {
                "source_id": "SSE_OFFICIAL",
                "local_code": code,
                "official_rows": rows,
                "observation": (
                    "The official terminated-list workbook has multiple rows for the "
                    "same A-share code with the same name/delist date but different "
                    "listing dates. The row grain/share-class relation is not explicit."
                ),
                "adjudication": "QUARANTINE__DO_NOT_AUTO_MERGE_OR_SPLIT_SECURITY_ID",
            }
        )
    measurements = {
        "source_id": "SSE_OFFICIAL",
        "current_a_share_rows": len(current_rows),
        "current_a_share_unique_codes": len(current_codes),
        "current_board_rows": {
            "MAIN": len(main_current),
            "STAR": len(star_current),
        },
        "delisted_a_share_rows": len(delisted_rows),
        "delisted_a_share_unique_codes": len(delisted_codes),
        "delisted_board_rows": {
            "MAIN": len(main_delisted),
            "STAR": len(star_delisted),
        },
        "duplicate_delisted_code_count": len(duplicate_codes),
        "duplicate_delisted_codes": duplicate_codes,
        "current_delisted_code_overlap_count": len(current_codes & delisted_codes),
        "current_delisted_code_overlap": sorted(current_codes & delisted_codes),
        "current_listing_date_min": min(current_listing_dates),
        "current_listing_date_max": max(current_listing_dates),
        "delisted_listing_date_min": min(delisted_listing_dates),
        "delisted_listing_date_max": max(delisted_listing_dates),
        "delisting_date_min": min(delisting_dates),
        "delisting_date_max": max(delisting_dates),
        "permanent_security_id_present": False,
        "effective_dated_code_name_board_history_complete": False,
        "publication_revision_history_complete": False,
    }
    return measurements, ambiguity_rows


def build_szse_measurements(
    szse_root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    master_path = szse_root / "raw/identity/current_a_share_master.xlsx"
    headers, master_rows = load_szse_master(master_path)
    board_counts = Counter(str(row["板块"]).strip() for row in master_rows)
    a_codes = [
        str(row["A股代码"]).strip()
        for row in master_rows
        if row.get("A股代码") not in (None, "")
    ]
    listing_dates = [
        date
        for row in master_rows
        if (date := normalize_date(row.get("A股上市日期")))
    ]
    listing_path = szse_root / "raw/identity/listing_event_search_page1.json"
    delisting_path = szse_root / "raw/identity/delisting_event_search_page1.json"
    listing = read_json(listing_path)
    delisting = read_json(delisting_path)
    event_sample: list[dict[str, Any]] = []
    for kind, payload, raw_path in (
        ("LISTING", listing, listing_path),
        ("DELISTING", delisting, delisting_path),
    ):
        raw_hash = sha256(raw_path)
        for item in payload.get("data", []):
            event_sample.append(
                {
                    "source_id": "SZSE_OFFICIAL",
                    "event_kind": kind,
                    "document_id": str(item.get("id", "")),
                    "title": clean_html(item.get("doctitle", "")),
                    "publication_time_field": "docpubtime",
                    "published_at_candidate": iso_from_epoch_ms(item.get("docpubtime")),
                    "document_url": item.get("docpuburl"),
                    "document_json_url": item.get("docpubjsonurl"),
                    "raw_source_path": raw_path.as_posix(),
                    "raw_source_sha256": raw_hash,
                    "boundary": (
                        "Search metadata sample only; docpubtime has not been proven "
                        "to be first-publication or immutable revision time, and no "
                        "available_at is generated."
                    ),
                }
            )
    timestamps = [
        item["published_at_candidate"]
        for item in event_sample
        if item["published_at_candidate"]
    ]
    measurements = {
        "source_id": "SZSE_OFFICIAL",
        "current_master_rows": len(master_rows),
        "current_master_columns": len(headers),
        "current_a_share_nonblank_codes": len(a_codes),
        "current_a_share_unique_codes": len(set(a_codes)),
        "current_listing_date_min": min(listing_dates),
        "current_listing_date_max": max(listing_dates),
        "current_board_rows": dict(sorted(board_counts.items())),
        "listing_event_total_reported": int(listing.get("totalSize", 0)),
        "listing_event_rows_preserved": len(listing.get("data", [])),
        "delisting_event_total_reported": int(delisting.get("totalSize", 0)),
        "delisting_event_rows_preserved": len(delisting.get("data", [])),
        "event_rows_with_docpubtime": len(timestamps),
        "event_docpubtime_min": min(timestamps) if timestamps else None,
        "event_docpubtime_max": max(timestamps) if timestamps else None,
        "complete_event_pagination_preserved": False,
        "permanent_security_id_present": False,
        "effective_dated_code_name_board_history_complete": False,
        "publication_revision_history_complete": False,
    }
    return measurements, event_sample


def build_clause_decisions(
    baseline: dict[str, Any],
    evidence_refs: dict[str, dict[str, Any]],
    official_evidence: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    baseline_gates = baseline["gate_evaluations"]["targets"]["CORE-01"]
    baseline_subchecks = {
        gate["gate_id"]: {
            item["check_id"]: item["status"] for item in gate["subchecks"]
        }
        for gate in baseline_gates
    }

    def subcheck(
        gate_id: str,
        subcheck_id: str,
        *,
        strict_gate_status: str,
        closure_assessment: str,
        assertion: str,
        evidence_keys: list[str],
        supporting_local_keys: list[str] | None = None,
        assessment_note: str | None = None,
    ) -> dict[str, Any]:
        if closure_assessment not in {"PASS", "PARTIAL", "UNRESOLVED"}:
            raise ValueError(f"Invalid closure assessment: {closure_assessment}")
        if subcheck_id not in baseline_subchecks[gate_id]:
            raise KeyError(f"{gate_id}/{subcheck_id} is not a baseline subcheck")
        result = {
            "subcheck_id": subcheck_id,
            "baseline_status": baseline_subchecks[gate_id][subcheck_id],
            "strict_gate_status": strict_gate_status,
            "closure_assessment": closure_assessment,
            "assertion": assertion,
            "official_evidence": [
                official_evidence[key] for key in evidence_keys
            ],
            "supporting_local_evidence": [
                evidence_refs[key] for key in (supporting_local_keys or [])
            ],
        }
        if assessment_note:
            result["assessment_note"] = assessment_note
        return result

    current = [
        {
            "gate_id": "CORE01-Q1",
            "severity": "BLOCK",
            "baseline_status": "FAIL",
            "strict_gate_status": "FAIL",
            "closure_assessment": "UNRESOLVED",
            "rule": (
                "security_id globally unique; code/name changes must not create a "
                "new identity; uncertain sameness must be quarantined."
            ),
            "subchecks": [
                subcheck(
                    "CORE01-Q1",
                    "current_trd_co_code_unique",
                    strict_gate_status="UNKNOWN",
                    closure_assessment="PARTIAL",
                    assertion=(
                        "The preserved SSE and SZSE current A-share tables were parsed, "
                        "not merely located: each has unique nonblank local codes within "
                        "its venue snapshot. BSE has zero accepted business rows, so the "
                        "official three-venue check is incomplete."
                    ),
                    evidence_keys=[
                        "sse_current_main",
                        "sse_current_star",
                        "szse_current",
                        "bse_listing_preflight",
                    ],
                    supporting_local_keys=["baseline", "coverage"],
                ),
                subcheck(
                    "CORE01-Q1",
                    "local_code_is_not_permanent_identity",
                    strict_gate_status="FAIL",
                    closure_assessment="PASS",
                    assertion=(
                        "The baseline candidate contains a concrete local-code reuse "
                        "counterexample. Parsed official SSE/SZSE schemas expose local "
                        "codes but no cross-history permanent security identifier. This "
                        "closes the negative finding that local code must not be promoted "
                        "as permanent identity; it does not make Q1 pass."
                    ),
                    evidence_keys=[
                        "sse_current_main",
                        "sse_delisted_main",
                        "szse_current",
                    ],
                    supporting_local_keys=["baseline", "sse_identity"],
                    assessment_note=(
                        "PASS means the rejection of local code as permanent identity is "
                        "evidence-closed, not that a replacement identity has been built."
                    ),
                ),
                subcheck(
                    "CORE01-Q1",
                    "permanent_identity_contract_fields",
                    strict_gate_status="FAIL",
                    closure_assessment="UNRESOLVED",
                    assertion=(
                        "issuer_id, permanent security_id, listing_episode_no, complete "
                        "effective intervals, published_at/available_at and revision "
                        "lineage remain absent from the preserved official tables."
                    ),
                    evidence_keys=[
                        "sse_current_main",
                        "sse_delisted_main",
                        "szse_current",
                        "szse_listing_events",
                        "szse_delisting_events",
                    ],
                    supporting_local_keys=["contract", "baseline"],
                ),
                subcheck(
                    "CORE01-Q1",
                    "uncertain_mapping_quarantine",
                    strict_gate_status="UNKNOWN",
                    closure_assessment="PARTIAL",
                    assertion=(
                        "All six detected SSE same-code multi-row ambiguities plus the "
                        "incomplete SZSE and absent BSE histories are quarantined without "
                        "auto-merge or auto-split. Completeness of the uncertainty set "
                        "cannot be proven until all three venue histories exist."
                    ),
                    evidence_keys=[
                        "sse_delisted_main",
                        "sse_delisted_star",
                        "szse_listing_events",
                        "szse_delisting_events",
                        "bse_listing_preflight",
                    ],
                    supporting_local_keys=["ambiguity"],
                ),
            ],
            "evidence": [
                evidence_refs["sse_identity"],
                evidence_refs["szse_validation"],
                evidence_refs["ambiguity"],
            ],
        },
        {
            "gate_id": "CORE01-Q2",
            "severity": "BLOCK",
            "baseline_status": "FAIL",
            "strict_gate_status": "FAIL",
            "closure_assessment": "UNRESOLVED",
            "rule": (
                "Code and listing-status intervals for one permanent security_id may "
                "not overlap or have unexplained gaps."
            ),
            "subchecks": [
                subcheck(
                    "CORE01-Q2",
                    "code_and_listing_status_intervals_exist",
                    strict_gate_status="FAIL",
                    closure_assessment="UNRESOLVED",
                    assertion=(
                        "Current snapshots, SSE terminated lists and the bounded SZSE "
                        "event samples do not form full valid_from/valid_to or "
                        "effective_from/effective_to histories keyed by permanent ID."
                    ),
                    evidence_keys=[
                        "sse_current_main",
                        "sse_delisted_main",
                        "sse_current_star",
                        "sse_delisted_star",
                        "szse_current",
                        "szse_listing_events",
                        "szse_delisting_events",
                    ],
                    supporting_local_keys=["baseline", "coverage"],
                ),
                subcheck(
                    "CORE01-Q2",
                    "interval_overlap_and_gap_audit",
                    strict_gate_status="UNKNOWN",
                    closure_assessment="UNRESOLVED",
                    assertion=(
                        "A complete interval overlap/gap audit cannot run until a "
                        "permanent identity mapping and complete event chain exist; no "
                        "statistical inference or zero-fill is used."
                    ),
                    evidence_keys=[
                        "sse_delisted_main",
                        "sse_delisted_star",
                        "szse_listing_events",
                        "szse_delisting_events",
                        "bse_listing_preflight",
                    ],
                    supporting_local_keys=["baseline", "ambiguity"],
                ),
            ],
            "evidence": [
                evidence_refs["sse_identity"],
                evidence_refs["szse_coverage"],
            ],
        },
        {
            "gate_id": "CORE01-Q3",
            "severity": "BLOCK",
            "baseline_status": "UNKNOWN",
            "strict_gate_status": "FAIL",
            "closure_assessment": "UNRESOLVED",
            "status_change_scope": (
                "FAIL_CLOSED_WITHIN_THIS_SUPPLEMENTAL_EVIDENCE_PACKAGE_ONLY; "
                "NO_FORMAL_GATE_MUTATION"
            ),
            "rule": (
                "Complete current and delisted A-share coverage for SSE, SZSE and BSE, "
                "with year/venue/board coverage matrix."
            ),
            "subchecks": [
                subcheck(
                    "CORE01-Q3",
                    "three_venue_current_rows_observed",
                    strict_gate_status="FAIL",
                    closure_assessment="PARTIAL",
                    assertion=(
                        "SSE and SZSE current official tables were parsed and hash "
                        "verified. The BSE official preflight yielded only a WAF "
                        "self-redirect/403 hard stop and zero accepted business rows."
                    ),
                    evidence_keys=[
                        "sse_current_main",
                        "sse_current_star",
                        "szse_current",
                        "bse_listing_preflight",
                        "bse_legal_stop",
                    ],
                    supporting_local_keys=["coverage", "bse_identity"],
                ),
                subcheck(
                    "CORE01-Q3",
                    "current_and_delisted_a_share_completeness",
                    strict_gate_status="FAIL",
                    closure_assessment="UNRESOLVED",
                    assertion=(
                        "SSE has current and terminated-list files, but SZSE preserves "
                        "only the first 20 of 1,429 listing and first 20 of 146 delisting "
                        "search results, and BSE has no accepted business payload."
                    ),
                    evidence_keys=[
                        "sse_current_main",
                        "sse_current_star",
                        "sse_delisted_main",
                        "sse_delisted_star",
                        "szse_current",
                        "szse_listing_events",
                        "szse_delisting_events",
                        "bse_listing_preflight",
                    ],
                    supporting_local_keys=["coverage", "szse_coverage"],
                ),
                subcheck(
                    "CORE01-Q3",
                    "year_venue_board_coverage_matrix",
                    strict_gate_status="FAIL",
                    closure_assessment="UNRESOLVED",
                    assertion=(
                        "A partial SSE/SZSE venue-board matrix is measured from actual "
                        "rows, but missing SZSE pagination, missing BSE rows and absent "
                        "effective intervals prevent a complete year-by-venue-by-board "
                        "historical matrix."
                    ),
                    evidence_keys=[
                        "sse_current_main",
                        "sse_current_star",
                        "sse_delisted_main",
                        "sse_delisted_star",
                        "szse_current",
                        "szse_listing_events",
                        "szse_delisting_events",
                        "bse_listing_preflight",
                    ],
                    supporting_local_keys=["coverage"],
                ),
            ],
            "evidence": [
                evidence_refs["coverage"],
                evidence_refs["bse_identity"],
                evidence_refs["bse_unresolved"],
            ],
        },
        {
            "gate_id": "CORE01-Q4",
            "severity": "BLOCK",
            "baseline_status": "FAIL",
            "strict_gate_status": "FAIL",
            "closure_assessment": "UNRESOLVED",
            "rule": (
                "Every name/code/board/status history must have published_at and "
                "available_at evidence or an approved conservative availability rule."
            ),
            "subchecks": [
                subcheck(
                    "CORE01-Q4",
                    "history_publication_and_availability",
                    strict_gate_status="FAIL",
                    closure_assessment="UNRESOLVED",
                    assertion=(
                        "Forty parsed SZSE first-page event records expose docpubtime "
                        "candidates, but complete history, first-publication semantics, "
                        "available_at mapping and revision/supersedes chains are not "
                        "closed. No candidate timestamp is promoted."
                    ),
                    evidence_keys=[
                        "szse_listing_events",
                        "szse_delisting_events",
                        "szse_listing_sample",
                        "szse_delisting_sample",
                        "sse_delisted_main",
                        "sse_delisted_star",
                    ],
                    supporting_local_keys=["event_sample", "baseline"],
                )
            ],
            "evidence": [
                evidence_refs["event_sample"],
                evidence_refs["szse_coverage"],
                evidence_refs["sse_identity"],
            ],
        },
        {
            "gate_id": "CORE01-Q5",
            "severity": "REVIEW",
            "baseline_status": "UNKNOWN",
            "strict_gate_status": "UNKNOWN",
            "closure_assessment": "PARTIAL",
            "rule": (
                "Conflicts across official sources must coexist with recorded "
                "adjudication; silent overwrite is forbidden."
            ),
            "subchecks": [
                subcheck(
                    "CORE01-Q5",
                    "candidate_sources_preserved_separately",
                    strict_gate_status="PASS",
                    closure_assessment="PASS",
                    assertion=(
                        "Candidate and official evidence roots remain separate; all 47 "
                        "declared official-package files pass actual byte and SHA-256 "
                        "verification."
                    ),
                    evidence_keys=[
                        "sse_current_main",
                        "szse_current",
                        "bse_legal_stop",
                    ],
                    supporting_local_keys=["verification"],
                ),
                subcheck(
                    "CORE01-Q5",
                    "cross_official_conflict_adjudication",
                    strict_gate_status="UNKNOWN",
                    closure_assessment="PARTIAL",
                    assertion=(
                        "Known SSE ambiguities and missing SZSE/BSE histories are "
                        "quarantined, but incomplete official coverage prevents a "
                        "complete cross-official conflict register."
                    ),
                    evidence_keys=[
                        "sse_delisted_main",
                        "sse_delisted_star",
                        "szse_listing_events",
                        "szse_delisting_events",
                        "bse_listing_preflight",
                    ],
                    supporting_local_keys=["ambiguity"],
                ),
            ],
            "evidence": [
                evidence_refs["ambiguity"],
                evidence_refs["verification"],
            ],
        },
    ]
    stage_a_checks = {
        "phase_a_missing_items": "FAIL",
        "rights": "FAIL",
        "schema_semantics": "FAIL",
        "pit": "FAIL",
        "revision_chain": "FAIL",
        "official_cross_source_quality": "FAIL",
        "known_contract_blockers": "FAIL",
    }
    blocking_gates = [
        item["gate_id"]
        for item in current
        if item["severity"] == "BLOCK" and item["strict_gate_status"] != "PASS"
    ]
    return {
        "schema_version": 2,
        "run_id": RUN_ID,
        "scope": "CORE-01_OFFICIAL_IDENTITY_PRE_ADMISSION_EVIDENCE_ONLY",
        "baseline": {
            "source": evidence_refs["baseline"],
            "gates": baseline_gates,
        },
        "current_clause_decisions": current,
        "stage_a": {
            "status": "BLOCKED__PRE_ADMISSION_EVIDENCE_NOT_READY",
            "ready": False,
            "checks": stage_a_checks,
            "blocking_check_count": sum(
                1 for status in stage_a_checks.values() if status != "PASS"
            ),
            "blocking_quality_gates": blocking_gates,
            "blocking_quality_gate_count": len(blocking_gates),
            "total_blocking_reasons": (
                sum(1 for status in stage_a_checks.values() if status != "PASS")
                + len(blocking_gates)
            ),
        },
        "formal_mutations": {
            "catalog_writes": 0,
            "quality_promotions": 0,
            "formal_snapshots": 0,
            "gate_registry_mutations": 0,
            "research_runs": 0,
        },
    }


def render_readme(
    coverage: dict[str, Any],
    clause_decisions: dict[str, Any],
    rights: dict[str, Any],
    verification: dict[str, Any],
) -> str:
    gates = clause_decisions["current_clause_decisions"]
    rows = "\n".join(
        f"| {item['gate_id']} | {item['severity']} | "
        f"{item['baseline_status']} | {item['strict_gate_status']} | "
        f"{item['closure_assessment']} |"
        for item in gates
    )
    subcheck_sections: list[str] = []
    for gate in gates:
        if gate["gate_id"] not in {
            "CORE01-Q1",
            "CORE01-Q2",
            "CORE01-Q3",
            "CORE01-Q4",
        }:
            continue
        subcheck_sections.append(f"### {gate['gate_id']}")
        for item in gate["subchecks"]:
            subcheck_sections.append(
                f"\n#### `{item['subcheck_id']}`\n\n"
                f"- 基线状态：`{item['baseline_status']}`\n"
                f"- 严格门状态：`{item['strict_gate_status']}`\n"
                f"- 补证闭合度：`{item['closure_assessment']}`\n"
                f"- 判断：{item['assertion']}\n"
                f"- 官方原件（网页存在本身不构成 PASS）："
            )
            for evidence in item["official_evidence"]:
                date_range = evidence["applicable_date_range"]
                date_text = (
                    f"{date_range.get('start')} 至 {date_range.get('end')} "
                    f"({date_range.get('type')})"
                )
                local = evidence["local_file"]
                subcheck_sections.append(
                    f"  - [{evidence['source_id']} / "
                    f"{evidence['evidence_role']}]({evidence['official_url']})；"
                    f"本地 `{local['path']}`；SHA-256 "
                    f"`{local['sha256']}`；适用/观测范围：{date_text}；"
                    f"边界：{evidence['boundary']}"
                )
    subcheck_text = "\n".join(subcheck_sections)
    sse = coverage["venues"]["SSE"]
    szse = coverage["venues"]["SZSE"]
    bse = coverage["venues"]["BSE"]
    return f"""# CORE-01 官方永久身份闭合审计

运行：`{RUN_ID}`

## 结论

本包没有使 CORE-01 通过 Stage A。当前为
`BLOCKED__PRE_ADMISSION_EVIDENCE_NOT_READY`，共有 11 个阻断理由：
7 个通用 Stage-A 检查失败，`CORE01-Q1`～`Q4` 四个 BLOCK 门均未通过。
`CORE01-Q3` 在本补证包内由 `UNKNOWN` 收紧为 fail-closed `FAIL`，原因是北交所
官方包接受业务记录为 0，且深交所上市/退市事件只保留了第一页；这不是正式 gate
registry 变更。

| 门 | 严重度 | 基线 | 严格门状态 | 补证闭合度 |
|---|---|---|---|---|
{rows}

## Q1～Q4 原始 subcheck 证据映射

`PASS/PARTIAL/UNRESOLVED` 只描述本次补证的闭合度；严格质量门仍使用
`PASS/FAIL/UNKNOWN`。例如，“已证明本地代码不能当永久 ID”的补证可以是 PASS，
但这恰恰意味着永久 ID 门仍为 FAIL。所有判断基于实际解析的官方原件内容及哈希，
不会因网页可打开或搜索结果存在而判定通过。

{subcheck_text}

## 本包实际增加的确定性

- 上交所官方当前 A 股文件：{sse['current_a_share_rows']:,} 行、
  {sse['current_a_share_unique_codes']:,} 个唯一代码；官方终止上市文件
  {sse['delisted_a_share_rows']:,} 行、{sse['delisted_a_share_unique_codes']:,}
  个唯一代码。
- 上交所终止上市文件出现 {sse['duplicate_delisted_code_count']} 个同代码多行歧义，
  已逐项进入 `QUARANTINE__DO_NOT_AUTO_MERGE_OR_SPLIT_SECURITY_ID`，不自动解释为
  同一证券、重新上市或代码复用。
- 深交所官方当前 A 股文件：{szse['current_master_rows']:,} 行、
  {szse['current_a_share_unique_codes']:,} 个唯一代码；官方搜索报告上市事件
  {szse['listing_event_total_reported']:,} 条、退市事件
  {szse['delisting_event_total_reported']:,} 条，但本地只各保存前
  {szse['listing_event_rows_preserved']} / {szse['delisting_event_rows_preserved']} 条。
- 深交所样本中的 {szse['event_rows_with_docpubtime']} 条事件具有 `docpubtime`
  候选时间，但没有把它冒充首次发布时间，也没有生成未经证明的 `available_at`。
- 北交所仍为 `{bse['status']}`，接受业务记录 {bse['accepted_records']}。
- 三个交易所证据根始终分开保存；本包没有联网、没有重试北交所 WAF、没有复制或
  改写 raw。

## 为什么仍不能建立永久身份

1. 三交易所没有统一公开的、跨代码和名称变化稳定的证券永久 ID；
2. 当前列表与有限事件页不能组成完整 `valid_from/valid_to`、
   `effective_from/effective_to` 区间；
3. 上交所终止上市文件存在同代码多行但粒度/股份类别关系不完整；
4. 深交所事件归档未完整分页，北交所业务数据为 0；
5. `published_at/available_at/revision_id/supersedes` 历史链未闭合；
6. 来源权利仍失败：SSE 只支持本非商业 inspection 边界，SZSE 批量/正式产品权利
   未闭合，BSE 保存与内部研究权未知。

## 下一条最小闭合路径

- 取得北交所正式可保存的当前、历史、旧新代码和终止上市数据，且不得绕过 WAF；
- 对深交所上市/退市事件做获准的完整分页，并保留每条公告正文、发布时间和修订链；
- 对上交所同代码多行逐条取得官方公告或股份类别证据；
- 形成发行人 ID、证券 ID、代码/名称/板块/状态有效区间的人工可审计映射决策；
- 逐源补齐自动获取、本地保存、内部研究/共享和 raw 保留权利回执。

## 边界

- `purpose_ceiling = INSPECTION_ONLY`
- `formal_admission = false`
- `datasets.json`、source registry、Roadmap 的起止 bytes/SHA-256 完全一致
- 质量状态未晋级
- `data/market/snapshots` 与 `data/published` 的受控全文件内容 inventory 起止一致
- 未发布正式快照
- 未运行因子或研究

保护状态守卫：`{verification['mutation_guard']['status']}`。该守卫只遍历上述两个
精确正式存储根，拒绝链接/重解析点，并受深度、条目数和内容哈希总字节硬上限约束；
任何越界或前后不一致均失败关闭。

证据完整性：{verification['source_manifest_verification']['passed']} /
{verification['source_manifest_verification']['declared_files']} 个既有官方包文件验
长度与 SHA-256 通过；失败 {verification['source_manifest_verification']['failed']}。
来源权利总裁决：`{rights['overall_status']}`。
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=".")
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT,
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace only registered files in the selected report directory",
    )
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve(strict=True)
    output_dir = (workspace / args.output_dir).resolve()
    require_report_path(output_dir, workspace)
    protected_pre = capture_protected_state(workspace)
    if output_dir.exists():
        if not args.replace:
            raise FileExistsError(
                f"report already exists; pass --replace after review: {output_dir}"
            )
        reject_unexpected_report_residuals(output_dir)
    else:
        output_dir.mkdir(parents=True)

    official_root = (
        workspace
        / "tmp/d2a04b-source-closure/"
        "d2a04b-official-track-a-20260728-v1"
    )
    sse_root = official_root / "SSE"
    szse_root = official_root / "SZSE"
    bse_root = official_root / "BSE"
    baseline_path = (
        workspace
        / "reports/data-governance/"
        "core01-02-03-06-local-evidence-closure-20260730-v1/"
        "MEASUREMENTS.json"
    )
    contract_path = (
        workspace
        / "data/catalog/contracts/g_data_core_v1/"
        "core_01_08_market_reference.json"
    )

    source_verifications = [
        verify_source_manifest(
            workspace,
            sse_root,
            sse_root / "RAW_FILE_MANIFEST.json",
            "SSE_OFFICIAL",
        ),
        verify_source_manifest(
            workspace,
            szse_root,
            szse_root / "RAW_FILE_MANIFEST.json",
            "SZSE_OFFICIAL",
        ),
        verify_source_manifest(
            workspace,
            bse_root,
            bse_root / "RAW_FILE_MANIFEST.json",
            "BSE_OFFICIAL",
        ),
    ]
    manifest_declared = sum(item["declared_files"] for item in source_verifications)
    manifest_passed = sum(item["passed"] for item in source_verifications)
    manifest_failed = sum(item["failed"] for item in source_verifications)
    if manifest_failed:
        raise RuntimeError("Official input manifest verification failed closed")

    baseline = read_json(baseline_path)
    contract = read_json(contract_path)
    core01_contract = next(
        item for item in contract["datasets"] if item["plan_id"] == "CORE-01"
    )
    sse_measurements, ambiguity_rows = build_sse_measurements(sse_root)
    szse_measurements, event_sample = build_szse_measurements(szse_root)
    bse_identity_path = bse_root / "IDENTITY_COVERAGE.json"
    bse_identity = read_json(bse_identity_path)
    sse_identity_doc = read_json(sse_root / "IDENTITY_COVERAGE.json")
    sse_downloads = {
        item["role"]: item for item in sse_identity_doc["official_downloads"]
    }

    def sse_field_range(
        role: str, start_field: str, end_field: str | None = None
    ) -> dict[str, Any]:
        sheet = sse_downloads[role]["sheets"][0]
        start = normalize_date(sheet["date_ranges"][start_field]["min"])
        final_field = end_field or start_field
        end = normalize_date(sheet["date_ranges"][final_field]["max"])
        return {
            "type": "PARSED_ROW_FIELD_RANGE",
            "start": start,
            "end": end,
            "start_field": start_field,
            "end_field": final_field,
            "boundary": (
                "Observed row-field span only; it is not a complete "
                "effective-dated identity interval."
            ),
        }

    sse_manifest_path = sse_root / "RAW_FILE_MANIFEST.json"
    szse_manifest_path = szse_root / "RAW_FILE_MANIFEST.json"
    bse_manifest_path = bse_root / "RAW_FILE_MANIFEST.json"
    szse_event_range = {
        "type": "PRESERVED_FIRST_PAGE_DOCPUBTIME_CANDIDATE_RANGE",
        "start": szse_measurements["event_docpubtime_min"],
        "end": szse_measurements["event_docpubtime_max"],
        "field": "docpubtime",
        "boundary": (
            "Only the two preserved first pages; docpubtime is not proven "
            "first-publication time or available_at."
        ),
    }
    bse_uncovered_range = {
        "type": "REQUESTED_SCOPE_UNCOVERED",
        "start": bse_identity["scope_start_date"],
        "end": bse_identity["scope_end_date"],
        "boundary": (
            "Requested coverage interval only; zero official business records "
            "were accepted and no data applicability is claimed."
        ),
    }
    official_evidence = {
        "sse_current_main": official_evidence_ref(
            workspace,
            sse_root,
            sse_manifest_path,
            "raw/005_listed_main_a.xls",
            source_id="SSE_OFFICIAL",
            evidence_role="CURRENT_LISTED_MAIN_A_PARSED_TABLE",
            applicable_date_range=sse_field_range(
                "CURRENT_LISTED_MAIN_A", "上市日期"
            ),
            boundary=(
                "Actual official workbook rows and schema were parsed; page "
                "existence alone is not used as evidence."
            ),
        ),
        "sse_current_star": official_evidence_ref(
            workspace,
            sse_root,
            sse_manifest_path,
            "raw/007_listed_star.xls",
            source_id="SSE_OFFICIAL",
            evidence_role="CURRENT_LISTED_STAR_PARSED_TABLE",
            applicable_date_range=sse_field_range(
                "CURRENT_LISTED_STAR", "上市日期"
            ),
            boundary=(
                "Actual official workbook rows and schema were parsed; page "
                "existence alone is not used as evidence."
            ),
        ),
        "sse_delisted_main": official_evidence_ref(
            workspace,
            sse_root,
            sse_manifest_path,
            "raw/008_delisted_main.xls",
            source_id="SSE_OFFICIAL",
            evidence_role="DELISTED_MAIN_PARSED_TABLE",
            applicable_date_range=sse_field_range(
                "DELISTED_MAIN", "上市日期", "终止上市日期"
            ),
            boundary=(
                "Parsed terminated-list rows; they do not include permanent ID, "
                "publication lineage or complete status intervals."
            ),
        ),
        "sse_delisted_star": official_evidence_ref(
            workspace,
            sse_root,
            sse_manifest_path,
            "raw/009_delisted_star.xls",
            source_id="SSE_OFFICIAL",
            evidence_role="DELISTED_STAR_PARSED_TABLE",
            applicable_date_range=sse_field_range(
                "DELISTED_STAR", "上市日期", "终止上市日期"
            ),
            boundary=(
                "Parsed terminated-list rows; they do not include permanent ID, "
                "publication lineage or complete status intervals."
            ),
        ),
        "szse_current": official_evidence_ref(
            workspace,
            szse_root,
            szse_manifest_path,
            "raw/identity/current_a_share_master.xlsx",
            source_id="SZSE_OFFICIAL",
            evidence_role="CURRENT_A_SHARE_MASTER_PARSED_TABLE",
            applicable_date_range={
                "type": "PARSED_ROW_FIELD_RANGE",
                "start": szse_measurements["current_listing_date_min"],
                "end": szse_measurements["current_listing_date_max"],
                "field": "A股上市日期",
                "boundary": (
                    "Observed listing-date span in a current snapshot, not a "
                    "complete validity interval."
                ),
            },
            boundary=(
                "Actual official workbook rows and 22-column schema were parsed; "
                "page existence alone is not used as evidence."
            ),
        ),
        "szse_listing_events": official_evidence_ref(
            workspace,
            szse_root,
            szse_manifest_path,
            "raw/identity/listing_event_search_page1.json",
            source_id="SZSE_OFFICIAL",
            evidence_role="LISTING_EVENT_SEARCH_FIRST_PAGE_PARSED_JSON",
            applicable_date_range=szse_event_range,
            boundary=(
                "Twenty actual JSON records are preserved from a reported total "
                "of 1,429; this is not complete historical coverage."
            ),
        ),
        "szse_delisting_events": official_evidence_ref(
            workspace,
            szse_root,
            szse_manifest_path,
            "raw/identity/delisting_event_search_page1.json",
            source_id="SZSE_OFFICIAL",
            evidence_role="DELISTING_EVENT_SEARCH_FIRST_PAGE_PARSED_JSON",
            applicable_date_range=szse_event_range,
            boundary=(
                "Twenty actual JSON records are preserved from a reported total "
                "of 146; this is not complete historical coverage."
            ),
        ),
        "szse_listing_sample": official_evidence_ref(
            workspace,
            szse_root,
            szse_manifest_path,
            "raw/identity/2015_listing_002739.html",
            source_id="SZSE_OFFICIAL",
            evidence_role="LISTING_NOTICE_SAMPLE_BODY",
            applicable_date_range={
                "type": "SINGLE_NOTICE_DATE",
                "start": "2015-01-20",
                "end": "2015-01-20",
                "boundary": "One bounded notice sample, not a complete archive.",
            },
            boundary=(
                "The local official notice body is hash preserved; one sample "
                "cannot prove archive completeness or revision semantics."
            ),
        ),
        "szse_delisting_sample": official_evidence_ref(
            workspace,
            szse_root,
            szse_manifest_path,
            "raw/identity/2021_delisting_kangdexin.html",
            source_id="SZSE_OFFICIAL",
            evidence_role="DELISTING_NOTICE_SAMPLE_BODY",
            applicable_date_range={
                "type": "SINGLE_NOTICE_DATE",
                "start": "2021-04-06",
                "end": "2021-04-06",
                "boundary": "One bounded notice sample, not a complete archive.",
            },
            boundary=(
                "The local official notice body is hash preserved; one sample "
                "cannot prove archive completeness or revision semantics."
            ),
        ),
        "bse_listing_preflight": official_evidence_ref(
            workspace,
            bse_root,
            bse_manifest_path,
            "headers/0001_bse_listedcompany_302.headers.sanitized.txt",
            source_id="BSE_OFFICIAL",
            evidence_role="LISTED_COMPANY_PAGE_WAF_PREFLIGHT_HEADERS",
            applicable_date_range=bse_uncovered_range,
            boundary=(
                "HTTP 302 self-redirect headers only; no official business body "
                "or row was accepted, and this cannot be positive identity evidence."
            ),
            official_url_override="https://www.bse.cn/nq/listedcompany.html",
            retrieved_at_override="2026-07-28T22:59:57+08:00",
            http_status_override=302,
        ),
        "bse_legal_stop": official_evidence_ref(
            workspace,
            bse_root,
            bse_manifest_path,
            "raw/0002_bse_legal_disclaimer_403.html",
            source_id="BSE_OFFICIAL",
            evidence_role="LEGAL_DISCLAIMER_HTTP_403_HARD_STOP_BODY",
            applicable_date_range=bse_uncovered_range,
            boundary=(
                "HTTP 403 response body only; it proves the hard stop, not data "
                "coverage or use rights. No retry or bypass was attempted."
            ),
            official_url_override=(
                "https://www.bse.cn/about_neeq/legal_disclaimer.html"
            ),
            retrieved_at_override="2026-07-28T23:00:22+08:00",
            http_status_override=403,
        ),
    }

    event_sample_path = output_dir / "SZSE_EVENT_SAMPLE.jsonl"
    event_sample_path.write_text(
        "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
            for item in event_sample
        ),
        encoding="utf-8",
    )

    coverage = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "purpose_ceiling": "INSPECTION_ONLY",
        "formal_admission": False,
        "venues": {
            "SSE": sse_measurements,
            "SZSE": szse_measurements,
            "BSE": {
                "source_id": "BSE_OFFICIAL",
                "accepted_records": int(bse_identity["accepted_records"]),
                "status": bse_identity["status"],
                "missing": bse_identity["missing"],
            },
        },
        "matrix_status": (
            "PARTIAL__SSE_SZSE_CURRENT_AND_BOUNDED_HISTORY__"
            "BSE_ZERO_ACCEPTED_RECORDS"
        ),
        "complete_current_and_delisted_a_share_universe": False,
        "complete_year_venue_board_matrix": False,
    }
    coverage_path = output_dir / "OFFICIAL_COVERAGE_MATRIX.json"
    write_json(coverage_path, coverage)

    ambiguity = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "status": "OPEN_GAPS_QUARANTINED__NO_SILENT_IDENTITY_INFERENCE",
        "items": ambiguity_rows
        + [
            {
                "source_id": "SZSE_OFFICIAL",
                "item": "identity_event_archive",
                "observation": (
                    "Only page 1 of 1,429 listing and page 1 of 146 delisting "
                    "search results is preserved."
                ),
                "adjudication": "QUARANTINE__INCOMPLETE_HISTORY",
            },
            {
                "source_id": "BSE_OFFICIAL",
                "item": "all_identity_business_payloads",
                "observation": (
                    "No business record was accepted after source-level WAF/403 stop."
                ),
                "adjudication": "QUARANTINE_GAP__NO_RETRY_OR_NONOFFICIAL_SUBSTITUTE",
            },
        ],
        "auto_merge_count": 0,
        "auto_split_count": 0,
        "silent_overwrite_count": 0,
    }
    ambiguity_path = output_dir / "IDENTITY_AMBIGUITY_QUARANTINE.json"
    write_json(ambiguity_path, ambiguity)

    rights = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "overall_status": "FAIL__FORMAL_SOURCE_RIGHTS_NOT_CLOSED",
        "sources": {
            "SSE_OFFICIAL": read_json(sse_root / "RIGHTS_EVIDENCE.json"),
            "SZSE_OFFICIAL": read_json(szse_root / "COVERAGE_AND_GAPS.json")[
                "rights"
            ],
            "BSE_OFFICIAL": read_json(bse_root / "RIGHTS_EVIDENCE.json"),
        },
        "decision": (
            "No official source is promoted. Existing raw remains limited to its "
            "inspection package; no external redistribution is granted."
        ),
        "network_activity_by_this_audit": {
            "requests": 0,
            "reason": (
                "Existing hash-bound official packages already establish the "
                "remaining gaps; BSE retry is prohibited by the current fail-closed "
                "source decision."
            ),
        },
    }
    rights_path = output_dir / "RIGHTS_AND_ENDPOINT_DECISION.json"
    write_json(rights_path, rights)

    verification_stub = {
        "path": (
            "reports/data-governance/"
            "stage-a-core01-official-identity-closure-20260730-v1/"
            "VERIFICATION.json"
        )
    }
    evidence_refs = {
        "baseline": file_ref(workspace, baseline_path),
        "contract": file_ref(workspace, contract_path),
        "sse_identity": file_ref(workspace, sse_root / "IDENTITY_COVERAGE.json"),
        "szse_validation": file_ref(workspace, szse_root / "VALIDATION.json"),
        "szse_coverage": file_ref(
            workspace, szse_root / "COVERAGE_AND_GAPS.json"
        ),
        "bse_identity": file_ref(workspace, bse_identity_path),
        "bse_unresolved": file_ref(workspace, bse_root / "UNRESOLVED.json"),
        "coverage": file_ref(workspace, coverage_path),
        "ambiguity": file_ref(workspace, ambiguity_path),
        "event_sample": file_ref(workspace, event_sample_path),
        "verification": verification_stub,
    }
    clause_decisions = build_clause_decisions(
        baseline, evidence_refs, official_evidence
    )
    clause_decisions["contract"] = {
        "source": evidence_refs["contract"],
        "quality_gates": core01_contract["quality_gates"],
        "known_blockers": core01_contract["known_blockers"],
    }
    clause_path = output_dir / "CLAUSE_DECISIONS.json"
    write_json(clause_path, clause_decisions)
    q1_q4 = [
        item
        for item in clause_decisions["current_clause_decisions"]
        if item["gate_id"] in {
            "CORE01-Q1",
            "CORE01-Q2",
            "CORE01-Q3",
            "CORE01-Q4",
        }
    ]
    subcheck_matrix = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "status": "HASH_BOUND_OFFICIAL_EVIDENCE_MAPPED__NO_ADMISSION",
        "closure_assessment_enum": ["PASS", "PARTIAL", "UNRESOLVED"],
        "webpage_existence_is_pass": False,
        "clauses": [
            {
                "gate_id": gate["gate_id"],
                "strict_gate_status": gate["strict_gate_status"],
                "closure_assessment": gate["closure_assessment"],
                "subchecks": gate["subchecks"],
            }
            for gate in q1_q4
        ],
    }
    subcheck_matrix_path = output_dir / "SUBCHECK_EVIDENCE_MATRIX.json"
    write_json(subcheck_matrix_path, subcheck_matrix)

    protected_post = capture_protected_state(workspace)
    mutation_guard = build_mutation_guard(protected_pre, protected_post)
    verification = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "verified_at": datetime.now(SHANGHAI).isoformat(),
        "status": "PASS__INPUT_HASHES_AND_BOUNDARIES_VERIFIED__NO_ADMISSION",
        "source_manifest_verification": {
            "declared_files": manifest_declared,
            "passed": manifest_passed,
            "failed": manifest_failed,
            "sources": source_verifications,
        },
        "authority_inputs": {
            "baseline_measurements": file_ref(workspace, baseline_path),
            "core_contract": file_ref(workspace, contract_path),
            **protected_post["authority_files"],
        },
        "derived_artifacts": {
            "coverage_matrix": file_ref(workspace, coverage_path),
            "ambiguity_quarantine": file_ref(workspace, ambiguity_path),
            "szse_event_sample": file_ref(workspace, event_sample_path),
            "clause_decisions": file_ref(workspace, clause_path),
            "subcheck_evidence_matrix": file_ref(
                workspace, subcheck_matrix_path
            ),
            "rights_decision": file_ref(workspace, rights_path),
        },
        "semantic_checks": {
            "baseline_core01_gate_count": len(
                baseline["gate_evaluations"]["targets"]["CORE-01"]
            ),
            "current_core01_gate_count": len(
                clause_decisions["current_clause_decisions"]
            ),
            "current_block_gate_nonpass_count": len(
                clause_decisions["stage_a"]["blocking_quality_gates"]
            ),
            "stage_a_blocking_reason_count": clause_decisions["stage_a"][
                "total_blocking_reasons"
            ],
            "sse_duplicate_code_ambiguities_quarantined": len(ambiguity_rows),
            "szse_event_sample_rows": len(event_sample),
            "bse_accepted_business_records": int(bse_identity["accepted_records"]),
            "q1_q4_subcheck_count": sum(
                len(gate["subchecks"]) for gate in q1_q4
            ),
            "q1_q4_all_subchecks_have_exact_baseline_ids": all(
                item["subcheck_id"] in baseline_subchecks
                for gate in q1_q4
                for item in gate["subchecks"]
                for baseline_subchecks in [
                    {
                        subcheck["check_id"]
                        for baseline_gate in baseline[
                            "gate_evaluations"
                        ]["targets"]["CORE-01"]
                        if baseline_gate["gate_id"] == gate["gate_id"]
                        for subcheck in baseline_gate["subchecks"]
                    }
                ]
            ),
            "q1_q4_all_subchecks_have_allowed_closure_assessment": all(
                item["closure_assessment"]
                in {"PASS", "PARTIAL", "UNRESOLVED"}
                for gate in q1_q4
                for item in gate["subchecks"]
            ),
            "q1_q4_all_subchecks_have_hash_bound_official_evidence": all(
                bool(item["official_evidence"])
                and all(
                    evidence.get("official_url")
                    and evidence.get("local_file", {}).get("sha256")
                    and evidence.get("applicable_date_range", {}).get("type")
                    for evidence in item["official_evidence"]
                )
                for gate in q1_q4
                for item in gate["subchecks"]
            ),
        },
        "boundary_guards": {
            "purpose_ceiling": "INSPECTION_ONLY",
            "formal_admission": False,
            "catalog_writes": 0,
            "quality_promotions": 0,
            "formal_snapshots": 0,
            "source_registry_mutations": 0,
            "roadmap_mutations": 0,
            "network_requests": 0,
            "research_runs": 0,
        },
        "mutation_guard": mutation_guard,
    }
    verification_path = output_dir / "VERIFICATION.json"
    write_json(verification_path, verification)

    readme = render_readme(coverage, clause_decisions, rights, verification)
    readme_path = output_dir / "README.md"
    readme_path.write_text(readme, encoding="utf-8")

    report_artifacts = [
        readme_path,
        clause_path,
        subcheck_matrix_path,
        coverage_path,
        ambiguity_path,
        rights_path,
        event_sample_path,
        verification_path,
    ]
    report_manifest = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "created_at": datetime.now(SHANGHAI).isoformat(),
        "status": "PASS__REPORT_HASH_BOUND__STAGE_A_BLOCKED__NO_ADMISSION",
        "artifacts": [file_ref(workspace, path) for path in report_artifacts],
        "formal_state": {
            "core_id": "CORE-01",
            "stage_a_ready": False,
            "stage_a_blocking_reasons": 11,
            "formal_admission": False,
            "research_ready": False,
        },
    }
    write_json(output_dir / "REPORT_MANIFEST.json", report_manifest)

    print(
        json.dumps(
            {
                "run_id": RUN_ID,
                "output_dir": output_dir.as_posix(),
                "source_manifest_files": manifest_declared,
                "source_manifest_passed": manifest_passed,
                "stage_a_ready": False,
                "blocking_reasons": 11,
                "network_requests": 0,
                "formal_mutations": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
