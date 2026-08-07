#!/usr/bin/env python3
"""Seal and build a zero-loss RESSET DIST inspection candidate.

The only authorized source is the user-downloaded ``RESSET 股票_分配.zip``.
PLAN validates the complete source without writing. EXECUTE copies the exact
ZIP into a versioned quarantine intake, extracts the reviewed DIST member, and
builds an INSPECTION_ONLY / NOT_ADMITTED candidate below tmp.  The formal
catalog, planned catalog, quality states, snapshots and research inputs are
never modified.

Field names are preserved verbatim as VARCHAR.  This tool reports observed
shape, lexical dates, missingness, flag values and uniqueness profiles; it does
not invent vendor semantics, a business primary key, PIT availability, status
transitions, or revision relationships.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import uuid
import zipfile
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ZIP = Path.home() / "Downloads" / "RESSET 股票_分配.zip"
INTAKE_ROOT = (
    WORKSPACE_ROOT
    / "data"
    / "market"
    / "quarantine"
    / "resset-manual-export"
    / "intake-20260802-company-actions-v1"
)
CANDIDATE_ROOT = (
    WORKSPACE_ROOT
    / "tmp"
    / "local-dataset-build"
    / "resset-dist"
    / "intake-20260802-company-actions-v1-zero-loss-v1"
)
REPORT_ROOT = (
    WORKSPACE_ROOT
    / "reports"
    / "data-governance"
    / "resset-dist-company-actions-intake-20260802-v1"
)
FORMAL_CATALOG = WORKSPACE_ROOT / "data" / "catalog" / "datasets.json"
PLANNED_CATALOG = WORKSPACE_ROOT / "data" / "catalog" / "planned_datasets.json"

TOOL_ID = "parse_resset_dist_candidate.py/resset-dist-zero-loss-v1"
TABLE_ID = "DIST"
DATASET_ID = "resset-dist"
DATA_MEMBER = "RESSET_DIST_1.csv"
EXPECTED_ZIP_BYTES = 1_432_284
EXPECTED_ZIP_SHA256 = "fd64dd36b3f369e2826ba9d7119c471eff99674b3e60c4557b3fe78bdc527c18"
EXPECTED_MEMBER_BYTES = 10_025_449
EXPECTED_MEMBER_SHA256 = "df9e051b32302ee788136d568eb1b694530e6ac546f95a7b51c24b5cedaf729a"
EXPECTED_MEMBER_CRC32 = "17504c76"
EXPECTED_MEMBER_COUNT = 3
EXPECTED_ROWS = 57_287

BUSINESS_COLUMNS = [
    "nobs", "CompanyCode", "ComCd", "ISIN", "R_SecuCode", "StkCd",
    "StkCdoTrd", "LStkNm", "DistCd", "DclrDt", "BegDt", "ExDt",
    "Dividendfc", "DividendCNY", "DividendFlg", "ExFlgDiv", "StkSPRate",
    "StkSpFlg", "ShrCmPrSRate", "ShrCmprsFlg", "ExflgShrcmprs",
    "StkDRate", "StkDFlg", "CapIssuRate", "CapIssuFlg", "RigOffRate",
    "ARigOffShr", "RigOffPrFC", "RigOffPrCNY", "RigOffFlg",
    "ExFlgRigOff", "SNIPrFC", "SNIPrCNY", "SNIvol", "SNIFlg",
    "ExFlgSNI", "Ex", "Pcash", "Pshrrate", "pshrrate_C",
    "pshrrate_S", "Pcapissurate",
]
AUDIT_COLUMNS = [
    "_source_data_row_number",
    "_raw_line_sha256",
    "_normalized_business_row_sha256",
]
DATE_COLUMNS = ["DclrDt", "BegDt", "ExDt"]
IDENTITY_COLUMNS = [
    "nobs", "CompanyCode", "ComCd", "ISIN", "R_SecuCode", "StkCd",
    "StkCdoTrd", "LStkNm", "DistCd",
]
ECONOMIC_COLUMNS = [
    "Dividendfc", "DividendCNY", "DividendFlg", "ExFlgDiv", "StkSPRate",
    "StkSpFlg", "ShrCmPrSRate", "ShrCmprsFlg", "ExflgShrcmprs",
    "StkDRate", "StkDFlg", "CapIssuRate", "CapIssuFlg", "RigOffRate",
    "ARigOffShr", "RigOffPrFC", "RigOffPrCNY", "RigOffFlg",
    "ExFlgRigOff", "SNIPrFC", "SNIPrCNY", "SNIvol", "SNIFlg",
    "ExFlgSNI", "Ex", "Pcash", "Pshrrate", "pshrrate_C",
    "pshrrate_S", "Pcapissurate",
]
FLAG_COLUMNS = [
    "DividendFlg", "ExFlgDiv", "StkSpFlg", "ShrCmprsFlg", "StkDFlg",
    "CapIssuFlg", "RigOffFlg", "ExFlgRigOff", "SNIFlg", "ExFlgSNI",
]


class DistCandidateError(RuntimeError):
    """Fail-closed error for the DIST candidate pipeline."""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def json_bytes(value: Any, *, canonical: bool = False) -> bytes:
    options: dict[str, Any] = {"ensure_ascii": False}
    if canonical:
        options.update(sort_keys=True, separators=(",", ":"))
    else:
        options.update(indent=2, sort_keys=True)
    return (json.dumps(value, **options) + "\n").encode("utf-8")


def write_bytes_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)


def write_text_exclusive(path: Path, text: str) -> None:
    write_bytes_exclusive(path, text.encode("utf-8"))


def is_reparse_point(path: Path) -> bool:
    value = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(value & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def source_state(path: Path) -> dict[str, Any]:
    info = path.stat()
    return {
        "path": path.as_posix(),
        "bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "sha256": sha256_file(path),
    }


def validate_source() -> dict[str, Any]:
    resolved = SOURCE_ZIP.resolve(strict=True)
    if resolved != SOURCE_ZIP.resolve():
        raise DistCandidateError("source path resolution drifted")
    if is_reparse_point(resolved):
        raise DistCandidateError("source ZIP must not be a reparse point")
    state = source_state(resolved)
    if state["bytes"] != EXPECTED_ZIP_BYTES:
        raise DistCandidateError(f"source byte count drifted: {state['bytes']}")
    if state["sha256"] != EXPECTED_ZIP_SHA256:
        raise DistCandidateError(f"source hash drifted: {state['sha256']}")
    return state


def validate_zip(path: Path) -> tuple[bytes, list[dict[str, Any]]]:
    members: list[dict[str, Any]] = []
    seen: set[str] = set()
    data_payload: bytes | None = None
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) != EXPECTED_MEMBER_COUNT:
                raise DistCandidateError(f"member count drifted: {len(infos)}")
            for info in infos:
                member = PurePosixPath(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    raise DistCandidateError(f"unsafe ZIP member: {info.filename}")
                folded = info.filename.casefold()
                if folded in seen:
                    raise DistCandidateError(f"duplicate ZIP member: {info.filename}")
                seen.add(folded)
                if info.flag_bits & 0x1:
                    raise DistCandidateError(f"encrypted member: {info.filename}")
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise DistCandidateError(f"symlink member: {info.filename}")
                payload = archive.read(info)
                item = {
                    "path": info.filename,
                    "bytes": len(payload),
                    "compressed_bytes": info.compress_size,
                    "crc32": f"{info.CRC:08x}",
                    "sha256": sha256_bytes(payload),
                }
                members.append(item)
                if info.filename == DATA_MEMBER:
                    data_payload = payload
                    if item["bytes"] != EXPECTED_MEMBER_BYTES:
                        raise DistCandidateError("DIST member byte count drifted")
                    if item["crc32"] != EXPECTED_MEMBER_CRC32:
                        raise DistCandidateError("DIST member CRC drifted")
                    if item["sha256"] != EXPECTED_MEMBER_SHA256:
                        raise DistCandidateError("DIST member hash drifted")
            if archive.testzip() is not None:
                raise DistCandidateError("ZIP testzip reported a corrupt member")
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, DistCandidateError):
            raise
        raise DistCandidateError(f"ZIP validation failed: {exc}") from exc
    if data_payload is None:
        raise DistCandidateError(f"required member missing: {DATA_MEMBER}")
    return data_payload, members


def serialize_row(row: list[str]) -> str:
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="").writerow(row)
    return output.getvalue()


def validate_iso_date(value: str, column: str, row_number: int) -> None:
    if not value:
        return
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise DistCandidateError(
            f"invalid lexical date {column} at row {row_number}: {value!r}"
        ) from exc
    if parsed.isoformat() != value:
        raise DistCandidateError(
            f"non-canonical lexical date {column} at row {row_number}: {value!r}"
        )


def duplicate_profile(records: list[dict[str, str]], columns: list[str]) -> dict[str, Any]:
    counts = Counter(tuple(row[column] for column in columns) for row in records)
    duplicate_groups = [count for count in counts.values() if count > 1]
    return {
        "columns": columns,
        "observed_profile_only_not_vendor_declared_key": True,
        "unique_tuple_count": len(counts),
        "duplicate_row_count": len(records) - len(counts),
        "duplicate_group_count": len(duplicate_groups),
        "max_group_size": max(duplicate_groups, default=1),
    }


def parse_member(payload: bytes) -> dict[str, Any]:
    if payload.startswith(b"\xef\xbb\xbf"):
        raise DistCandidateError("DIST member unexpectedly has a BOM")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise DistCandidateError(f"DIST member is not strict UTF-8: {exc}") from exc
    lines = text.splitlines(keepends=True)
    if len(lines) != EXPECTED_ROWS + 1:
        raise DistCandidateError(f"physical line count drifted: {len(lines)}")
    header = next(csv.reader([lines[0].rstrip("\r\n")], strict=True))
    if header != BUSINESS_COLUMNS:
        raise DistCandidateError(f"42-column header drifted: {header}")

    records: list[dict[str, str]] = []
    arity = Counter()
    date_values: dict[str, list[str]] = {column: [] for column in DATE_COLUMNS}
    missing = Counter()
    nonblank = Counter()
    flag_values: dict[str, Counter[str]] = {column: Counter() for column in FLAG_COLUMNS}
    reconstruction = [lines[0]]

    for row_number, raw_line in enumerate(lines[1:], start=1):
        reconstruction.append(raw_line)
        raw_without_eol = raw_line.rstrip("\r\n")
        try:
            row = next(csv.reader([raw_without_eol], strict=True))
        except csv.Error as exc:
            raise DistCandidateError(f"strict CSV error at row {row_number}: {exc}") from exc
        arity[len(row)] += 1
        if len(row) != len(BUSINESS_COLUMNS):
            raise DistCandidateError(
                f"row {row_number} has {len(row)} fields, expected 42"
            )
        values = dict(zip(BUSINESS_COLUMNS, row, strict=True))
        for column in BUSINESS_COLUMNS:
            if values[column] == "":
                missing[column] += 1
            else:
                nonblank[column] += 1
        for column in DATE_COLUMNS:
            validate_iso_date(values[column], column, row_number)
            if values[column]:
                date_values[column].append(values[column])
        for column in FLAG_COLUMNS:
            flag_values[column][values[column]] += 1

        normalized = serialize_row(row)
        if next(csv.reader([normalized], strict=True)) != row:
            raise DistCandidateError(f"normalized row failed at row {row_number}")
        records.append(
            {
                **values,
                "_source_data_row_number": str(row_number),
                "_raw_line_sha256": sha256_bytes(raw_line.encode("utf-8")),
                "_normalized_business_row_sha256": sha256_bytes(
                    normalized.encode("utf-8")
                ),
            }
        )

    if "".join(reconstruction).encode("utf-8") != payload:
        raise DistCandidateError("source member byte reconstruction failed")
    if len(records) != EXPECTED_ROWS:
        raise DistCandidateError(f"row count drifted: {len(records)}")

    date_coverage = {
        column: {
            "nonblank": len(date_values[column]),
            "blank": missing[column],
            "min": min(date_values[column]),
            "max": max(date_values[column]),
        }
        for column in DATE_COLUMNS
    }
    metrics = {
        "source_record_count": len(records),
        "source_business_field_count": len(BUSINESS_COLUMNS),
        "strict_utf8": True,
        "bom_present": False,
        "source_member_byte_roundtrip": "PASS",
        "strict_field_arity_counts": {str(k): v for k, v in sorted(arity.items())},
        "date_coverage": date_coverage,
        "identity_missing_counts": {column: missing[column] for column in IDENTITY_COLUMNS},
        "economic_term_nonblank_counts": {
            column: nonblank[column] for column in ECONOMIC_COLUMNS
        },
        "flag_value_counts_observed_only": {
            column: dict(sorted(counts.items())) for column, counts in flag_values.items()
        },
        "uniqueness_profiles": [
            duplicate_profile(records, ["nobs"]),
            duplicate_profile(records, ["DistCd"]),
            duplicate_profile(records, ["StkCd", "DistCd"]),
            duplicate_profile(records, ["CompanyCode", "DistCd"]),
            duplicate_profile(records, ["StkCd", "DclrDt", "ExDt", "DistCd"]),
        ],
    }
    return {"records": records, "metrics": metrics}


def write_candidate_csv(path: Path, records: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", errors="strict", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=BUSINESS_COLUMNS + AUDIT_COLUMNS,
            extrasaction="raise",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(records)


def verify_candidate(path: Path, expected: list[dict[str, str]]) -> dict[str, Any]:
    count = 0
    with path.open("r", encoding="utf-8", errors="strict", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != BUSINESS_COLUMNS + AUDIT_COLUMNS:
            raise DistCandidateError("candidate header readback failed")
        for count, (actual, wanted) in enumerate(zip(reader, expected, strict=True), start=1):
            if actual != wanted:
                raise DistCandidateError(f"candidate readback differs at row {count}")
        if count != EXPECTED_ROWS:
            raise DistCandidateError(f"candidate readback row count: {count}")
    return {
        "status": "PASS",
        "row_count": count,
        "business_field_count": len(BUSINESS_COLUMNS),
        "audit_field_count": len(AUDIT_COLUMNS),
    }


def artifact(path: Path, root: Path, **extra: Any) -> dict[str, Any]:
    return {
        "relative_path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        **extra,
    }


def materialize_tree(staging: Path, source: dict[str, Any], members: list[dict[str, Any]], payload: bytes, parsed: dict[str, Any]) -> dict[str, Any]:
    raw_zip = staging / "raw" / "packages" / SOURCE_ZIP.name
    raw_member = staging / "raw" / "extracted" / DATA_MEMBER
    candidate = staging / "curated" / "candidate.csv"
    quality = staging / "quality" / "summary.json"
    provenance = staging / "provenance.json"
    report = staging / "REPORT.md"

    write_bytes_exclusive(raw_zip, SOURCE_ZIP.read_bytes())
    write_bytes_exclusive(raw_member, payload)
    write_candidate_csv(candidate, parsed["records"])
    roundtrip = verify_candidate(candidate, parsed["records"])

    gap_audit = {
        "schema_version": 1,
        "table": TABLE_ID,
        "status": "INSPECTION_ONLY",
        "admission_decision": "NOT_ADMITTED",
        "observed_fields": BUSINESS_COLUMNS,
        "pit_and_revision_audit": {
            "DclrDt_present": True,
            "BegDt_present": True,
            "ExDt_present": True,
            "InfoPubDt_present": False,
            "UpDt_present": False,
            "publication_time_present": False,
            "announcement_identity_present": False,
            "explicit_status_field_present": False,
            "correction_cancel_supersession_link_present": False,
            "available_at_closed": False,
            "revision_chain_closed": False,
            "note": "Date-like fields are preserved lexically; no unavailable vendor semantics are guessed.",
        },
        "core_candidate_delta": {
            "CORE-04": {
                "delta": "MATERIAL_CANDIDATE_EVIDENCE_ADDED",
                "added": "company-action dates, economic terms and observed action flags",
                "closed": False,
                "remaining": "vendor semantics, precise available_at, explicit status and correction/cancellation/supersession chain",
            },
            "CORE-05": {
                "delta": "PARTIAL_INPUT_ONLY",
                "added": "potential adjustment inputs retained without projection",
                "closed": False,
                "remaining": "CORE-04 semantic/PIT/revision closure plus verified adjustment projection and immutable snapshot",
            },
        },
    }
    quality_document = {
        **gap_audit,
        "metrics": parsed["metrics"],
        "candidate_roundtrip": roundtrip,
        "gates": {
            "source_hash": "PASS",
            "zip_crc_and_all_member_read": "PASS",
            "strict_utf8": "PASS",
            "row_count_57287": "PASS",
            "business_fields_42": "PASS",
            "all_rows_arity_42": "PASS",
            "formal_admission": "NOT_ATTEMPTED",
        },
    }
    write_bytes_exclusive(quality, json_bytes(quality_document))
    write_bytes_exclusive(
        provenance,
        json_bytes(
            {
                "schema_version": 1,
                "tool_id": TOOL_ID,
                "dataset_id": DATASET_ID,
                "table": TABLE_ID,
                "created_at": now_utc(),
                "source": {**source, "members": members},
                "transformation": {
                    "kind": "zero_loss_varchar_candidate",
                    "business_columns": BUSINESS_COLUMNS,
                    "business_field_count": len(BUSINESS_COLUMNS),
                    "source_member_retained_exactly": True,
                    "semantic_inference_performed": False,
                },
                "formal_effect": "NONE",
                "purpose_ceiling": "INSPECTION_ONLY",
                "admission_decision": "NOT_ADMITTED",
            }
        ),
    )
    dates = parsed["metrics"]["date_coverage"]
    write_text_exclusive(
        report,
        f"""# RESSET DIST zero-loss inspection candidate

- Status: `INSPECTION_ONLY / NOT_ADMITTED`
- Source ZIP SHA-256: `{EXPECTED_ZIP_SHA256}`
- Data member SHA-256: `{EXPECTED_MEMBER_SHA256}`
- Rows: `{EXPECTED_ROWS:,}`
- Business fields: `42`
- `DclrDt`: `{dates['DclrDt']['min']}` to `{dates['DclrDt']['max']}`; {dates['DclrDt']['nonblank']:,} nonblank / {dates['DclrDt']['blank']:,} blank
- `BegDt`: `{dates['BegDt']['min']}` to `{dates['BegDt']['max']}`; {dates['BegDt']['nonblank']:,} nonblank / {dates['BegDt']['blank']:,} blank
- `ExDt`: `{dates['ExDt']['min']}` to `{dates['ExDt']['max']}`; {dates['ExDt']['nonblank']:,} nonblank / {dates['ExDt']['blank']:,} blank

The original ZIP and CSV member are retained byte-for-byte.  All business
fields remain VARCHAR.  The table adds material company-action terms and date
evidence for CORE-04, and potential inputs for CORE-05, but it does not close
PIT availability, vendor semantics, status, correction/cancellation or the
revision chain.  It is not cataloged, not a formal snapshot, and not research
authorized.
""",
    )
    return {
        "raw_zip": artifact(raw_zip, staging),
        "raw_member": artifact(raw_member, staging),
        "candidate_csv": artifact(candidate, staging, row_count=EXPECTED_ROWS, business_field_count=42),
        "quality_summary": artifact(quality, staging),
        "provenance": artifact(provenance, staging),
        "report": artifact(report, staging),
    }


def build_intake(staging: Path, source: dict[str, Any], members: list[dict[str, Any]]) -> dict[str, Any]:
    raw_zip = staging / "raw" / "packages" / SOURCE_ZIP.name
    write_bytes_exclusive(raw_zip, SOURCE_ZIP.read_bytes())
    copied = source_state(raw_zip)
    if copied["sha256"] != source["sha256"] or copied["bytes"] != source["bytes"]:
        raise DistCandidateError("quarantine copy differs from source")
    manifest = {
        "schema_version": 1,
        "manifest_kind": "RESSET_MANUAL_EXPORT_INTAKE",
        "intake_id": INTAKE_ROOT.name,
        "created_at": now_utc(),
        "status": "SEALED_QUARANTINE_CANDIDATE_SOURCE",
        "source": source,
        "packages": [
            {
                "source_path": source["path"],
                "sealed_relative_path": raw_zip.relative_to(staging).as_posix(),
                "bytes": copied["bytes"],
                "sha256": copied["sha256"],
                "members": members,
            }
        ],
        "formal_effect": "NONE",
        "purpose_ceiling": "INSPECTION_ONLY",
        "admission_decision": "NOT_ADMITTED",
    }
    manifest_path = staging / "INTAKE_MANIFEST.json"
    write_bytes_exclusive(manifest_path, json_bytes(manifest))
    manifest_hash = sha256_file(manifest_path)
    write_text_exclusive(staging / "INTAKE_MANIFEST.sha256", f"{manifest_hash}  INTAKE_MANIFEST.json\n")
    receipt = {
        "schema_version": 1,
        "intake_id": INTAKE_ROOT.name,
        "created_at": now_utc(),
        "source_original_retained": True,
        "source_original_modified": False,
        "source_original_deleted": False,
        "source": source,
        "sealed_copy": copied,
        "hash_equal": copied["sha256"] == source["sha256"],
        "byte_count_equal": copied["bytes"] == source["bytes"],
    }
    write_bytes_exclusive(staging / "SOURCE_RETENTION_RECEIPT.json", json_bytes(receipt))
    return {"manifest_sha256": manifest_hash, "sealed_copy": copied}


def build_report(staging: Path, source: dict[str, Any], members: list[dict[str, Any]], parsed: dict[str, Any], intake: dict[str, Any], candidate_manifest_sha256: str, guards: dict[str, str]) -> dict[str, Any]:
    dates = parsed["metrics"]["date_coverage"]
    evidence = {
        "schema_version": 1,
        "report_id": REPORT_ROOT.name,
        "created_at": now_utc(),
        "status": "DIST_ZERO_LOSS_CANDIDATE_BUILT",
        "source": {**source, "members": members},
        "intake": {
            "path": INTAKE_ROOT.as_posix(),
            "manifest_sha256": intake["manifest_sha256"],
        },
        "candidate": {
            "path": CANDIDATE_ROOT.as_posix(),
            "manifest_sha256": candidate_manifest_sha256,
        },
        "metrics": parsed["metrics"],
        "pit_revision_gap": {
            "InfoPubDt": "ABSENT",
            "UpDt": "ABSENT",
            "publication_time": "ABSENT",
            "explicit_status": "ABSENT",
            "correction_cancel_supersession_link": "ABSENT",
            "available_at": "NOT_CLOSED",
            "revision_chain": "NOT_CLOSED",
        },
        "candidate_delta": {
            "CORE-04": "material company-action terms/date candidate added; still not closed",
            "CORE-05": "potential adjustment inputs only; still not closed",
        },
        "catalog_guards": guards,
        "formal_effect": "NONE",
        "purpose_ceiling": "INSPECTION_ONLY",
        "admission_decision": "NOT_ADMITTED",
    }
    evidence_path = staging / "EVIDENCE_REPORT.json"
    write_bytes_exclusive(evidence_path, json_bytes(evidence))
    verification = {
        "schema_version": 1,
        "status": "PASS",
        "checks": {
            "source_retained": True,
            "source_hash_stable": True,
            "sealed_copy_exact": True,
            "zip_all_members_crc_read": True,
            "strict_utf8": True,
            "rows": EXPECTED_ROWS,
            "business_fields": 42,
            "date_coverage": dates,
            "formal_catalog_unchanged": guards["datasets_sha256_before"] == guards["datasets_sha256_after"],
            "planned_catalog_unchanged": guards["planned_sha256_before"] == guards["planned_sha256_after"],
        },
    }
    verification_path = staging / "VERIFICATION.json"
    write_bytes_exclusive(verification_path, json_bytes(verification))
    readme = f"""# RESSET DIST company-action intake evidence

The user-downloaded DIST package was copied without modifying the C-drive
source.  The ZIP and all members passed full read/CRC validation.  The candidate
contains `{EXPECTED_ROWS:,}` rows and `42` preserved VARCHAR business fields.

This is candidate evidence only.  CORE-04 gains company-action terms and dates;
CORE-05 gains potential adjustment inputs.  Neither is formally closed because
precise `available_at`, field semantics, explicit status and a complete
correction/cancellation/supersession chain remain unresolved.
"""
    readme_path = staging / "README.md"
    write_text_exclusive(readme_path, readme)
    artifacts = {
        "readme": artifact(readme_path, staging),
        "evidence": artifact(evidence_path, staging),
        "verification": artifact(verification_path, staging),
    }
    report_manifest = {
        "schema_version": 1,
        "report_id": REPORT_ROOT.name,
        "created_at": now_utc(),
        "status": "SEALED",
        "artifacts": artifacts,
        "formal_effect": "NONE",
    }
    write_bytes_exclusive(staging / "REPORT_MANIFEST.json", json_bytes(report_manifest))
    return report_manifest


def execute(source: dict[str, Any], members: list[dict[str, Any]], payload: bytes, parsed: dict[str, Any]) -> dict[str, Any]:
    for target in (INTAKE_ROOT, CANDIDATE_ROOT, REPORT_ROOT):
        if target.exists():
            raise DistCandidateError(f"target already exists: {target}")
    source_before = source_state(SOURCE_ZIP)
    guards = {
        "datasets_sha256_before": sha256_file(FORMAL_CATALOG),
        "planned_sha256_before": sha256_file(PLANNED_CATALOG),
    }
    staging_paths = {
        "intake": INTAKE_ROOT.parent / f".{INTAKE_ROOT.name}.staging-{uuid.uuid4().hex}",
        "candidate": CANDIDATE_ROOT.parent / f".{CANDIDATE_ROOT.name}.staging-{uuid.uuid4().hex}",
        "report": REPORT_ROOT.parent / f".{REPORT_ROOT.name}.staging-{uuid.uuid4().hex}",
    }
    for path in staging_paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir(exist_ok=False)
    try:
        intake = build_intake(staging_paths["intake"], source, members)
        candidate_artifacts = materialize_tree(
            staging_paths["candidate"], source, members, payload, parsed
        )
        candidate_manifest = {
            "schema_version": 1,
            "manifest_kind": "RESSET_DIST_ZERO_LOSS_CANDIDATE",
            "tool_id": TOOL_ID,
            "dataset_id": DATASET_ID,
            "table": TABLE_ID,
            "run_id": CANDIDATE_ROOT.name,
            "created_at": now_utc(),
            "status": "ZERO_LOSS_CANDIDATE_MATERIALIZED",
            "source": {**source, "members": members},
            "metrics": parsed["metrics"],
            "artifacts": candidate_artifacts,
            "content_hash": sha256_bytes(
                json_bytes(
                    {
                        "source_zip_sha256": source["sha256"],
                        "source_member_sha256": EXPECTED_MEMBER_SHA256,
                        "candidate_csv_sha256": candidate_artifacts["candidate_csv"]["sha256"],
                        "quality_sha256": candidate_artifacts["quality_summary"]["sha256"],
                    },
                    canonical=True,
                )
            ),
            "safety": {
                "formal_effect": "NONE",
                "purpose_ceiling": "INSPECTION_ONLY",
                "admission_decision": "NOT_ADMITTED",
                "catalog_registered": False,
                "quality_promotion": False,
                "snapshot_created": False,
                "research_ready": False,
            },
        }
        candidate_manifest_path = staging_paths["candidate"] / "manifest.json"
        write_bytes_exclusive(candidate_manifest_path, json_bytes(candidate_manifest))
        candidate_manifest_sha256 = sha256_file(candidate_manifest_path)

        source_after = source_state(SOURCE_ZIP)
        if source_after != source_before:
            raise DistCandidateError("C-drive source changed during execution")
        guards.update(
            datasets_sha256_after=sha256_file(FORMAL_CATALOG),
            planned_sha256_after=sha256_file(PLANNED_CATALOG),
        )
        if guards["datasets_sha256_before"] != guards["datasets_sha256_after"]:
            raise DistCandidateError("formal catalog changed during execution")
        if guards["planned_sha256_before"] != guards["planned_sha256_after"]:
            raise DistCandidateError("planned catalog changed during execution")

        build_report(
            staging_paths["report"], source, members, parsed, intake,
            candidate_manifest_sha256, guards,
        )
        os.replace(staging_paths["intake"], INTAKE_ROOT)
        os.replace(staging_paths["candidate"], CANDIDATE_ROOT)
        os.replace(staging_paths["report"], REPORT_ROOT)
        return {
            "status": "PASS",
            "write_performed": True,
            "source": source_after,
            "members": members,
            "intake_path": INTAKE_ROOT.as_posix(),
            "intake_manifest_sha256": intake["manifest_sha256"],
            "candidate_path": CANDIDATE_ROOT.as_posix(),
            "candidate_manifest_sha256": candidate_manifest_sha256,
            "report_path": REPORT_ROOT.as_posix(),
            "metrics": parsed["metrics"],
            "catalog_guards": guards,
            "formal_effect": "NONE",
            "purpose_ceiling": "INSPECTION_ONLY",
            "admission_decision": "NOT_ADMITTED",
        }
    except Exception:
        for path in staging_paths.values():
            if path.exists():
                shutil.rmtree(path)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        source = validate_source()
        payload, members = validate_zip(SOURCE_ZIP)
        parsed = parse_member(payload)
        if args.execute:
            result = execute(source, members, payload, parsed)
        else:
            result = {
                "status": "PLAN_VALIDATED_WRITE_FREE",
                "write_performed": False,
                "source": source,
                "members": members,
                "metrics": parsed["metrics"],
                "targets": {
                    "intake": INTAKE_ROOT.as_posix(),
                    "candidate": CANDIDATE_ROOT.as_posix(),
                    "report": REPORT_ROOT.as_posix(),
                },
                "targets_exist": {
                    "intake": INTAKE_ROOT.exists(),
                    "candidate": CANDIDATE_ROOT.exists(),
                    "report": REPORT_ROOT.exists(),
                },
                "formal_effect": "NONE",
                "purpose_ceiling": "INSPECTION_ONLY",
                "admission_decision": "NOT_ADMITTED",
            }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "FAIL_CLOSED",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "write_performed": False,
                    "formal_effect": "NONE",
                    "purpose_ceiling": "INSPECTION_ONLY",
                    "admission_decision": "NOT_ADMITTED",
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
