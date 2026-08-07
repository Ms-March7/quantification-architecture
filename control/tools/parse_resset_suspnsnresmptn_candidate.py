#!/usr/bin/env python3
"""Build a zero-loss RESSET SUSPNSNRESMPTN inspection candidate.

This parser is hard-bound to the sealed 2026-08-02 RESSET intake.  The source
member contains 81,535 one-line records and 22 documented business fields.
Eight records contain unescaped literal quotes inside ``SuspnsnReason``.  A
strict CSV reader rejects those records even though a permissive reader still
returns 22 values.  Permissive parsing is not used as the repair because it
silently changes the positions of the literal quotes.

For each exceptional record this tool deterministically:

1. strictly parses the first eleven fields;
2. locates the unique reason-field closing boundary whose suffix strictly
   parses as the remaining ten fields;
3. keeps every literal quote in the reason text at its original position; and
4. serializes the recovered 22 values with a standards-compliant CSV writer.

The original ZIP, original member, every raw source line and their hashes are
retained.  ``ResmptnDt`` remains VARCHAR, including the observed 1900-01-01
sentinel.  The default mode is a complete write-free PLAN.  ``--execute`` may
write only below ``tmp/local-dataset-build/resset-suspnsnresmptn``.  Outputs
are always INSPECTION_ONLY / NOT_ADMITTED and never update the formal catalog.
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
SEALED_INTAKE_ROOT = (
    WORKSPACE_ROOT
    / "data"
    / "market"
    / "quarantine"
    / "resset-manual-export"
    / "intake-20260802-04pkg-v1"
)
SOURCE_ZIP = SEALED_INTAKE_ROOT / "raw" / "packages" / "RESSET 股票_停复牌.zip"
INTAKE_MANIFEST = SEALED_INTAKE_ROOT / "INTAKE_MANIFEST.json"
FORMAL_CATALOG = WORKSPACE_ROOT / "data" / "catalog" / "datasets.json"
OUTPUT_BASE = (
    WORKSPACE_ROOT / "tmp" / "local-dataset-build" / "resset-suspnsnresmptn"
)

DATASET_ID = "resset-suspnsnresmptn"
TABLE_ID = "SUSPNSNRESMPTN"
DEFAULT_RUN_ID = "intake-20260802-04pkg-v1-zero-loss-v1"
TOOL_ID = (
    "parse_resset_suspnsnresmptn_candidate.py/"
    "resset-suspnsnresmptn-zero-loss-candidate-v1"
)

EXPECTED_ZIP_BYTES = 1_794_834
EXPECTED_ZIP_SHA256 = "cb2760ac788468e3b4c1c6eb6a92ab72a3aa9458e84c770e12e75edc40d3016d"
EXPECTED_INTAKE_MANIFEST_SHA256 = (
    "d7a8bdacbe8ed8d4876179a0e0c0be293b2a15a0fab39daf8ccb2ffea7d0c969"
)
DATA_MEMBER = "RESSET_SUSPNSNRESMPTN_1.csv"
EXPECTED_MEMBER_BYTES = 17_375_204
EXPECTED_MEMBER_SHA256 = "ef142fedfe890905daa44e151ec143ba613e78e53082b911d8aa5691d5443ce2"
EXPECTED_MEMBER_CRC32 = "215f0b7a"
EXPECTED_RECORDS = 81_535
EXPECTED_STRICT_QUOTE_ERRORS = 8
EXPECTED_SENTINEL = "1900-01-01"

BUSINESS_COLUMNS = [
    "CompanyCode",
    "ComCd",
    "ISIN",
    "R_SecuCode",
    "StkCd",
    "StkCdoTrd",
    "LStkNm",
    "CsrcIcCd1",
    "CsrcIcCd2",
    "SuspnsnDt",
    "SuspnsnTime",
    "SuspnsnReason",
    "SuspnsnDesc",
    "SuspnsnMat",
    "SuspnsnType",
    "ResmptnDt",
    "ResmptnTime",
    "ResmptnDesc",
    "InfoPubDt",
    "InfoSourceCd",
    "PubDt",
    "UpDt",
]

AUDIT_COLUMNS = [
    "_source_data_row_number",
    "_source_physical_line_number",
    "_repair_class",
    "_raw_line_sha256",
    "_normalized_business_row_sha256",
    "_raw_line_text",
    "_normalized_business_row_text",
]

DATE_COLUMNS = ["SuspnsnDt", "InfoPubDt", "PubDt", "UpDt"]
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class RessetSuspensionCandidateError(RuntimeError):
    """Fail-closed candidate parser error."""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def json_bytes(value: Any, *, canonical: bool = False) -> bytes:
    options: dict[str, Any] = {"ensure_ascii": False}
    if canonical:
        options.update(sort_keys=True, separators=(",", ":"))
    else:
        options.update(indent=2, sort_keys=True)
    return (json.dumps(value, **options) + "\n").encode("utf-8")


def is_reparse_point(path: Path) -> bool:
    value = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(value & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def source_state(path: Path) -> dict[str, Any]:
    info = path.stat()
    return {
        "bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "sha256": sha256_file(path),
    }


def validate_source_binding() -> dict[str, Any]:
    try:
        resolved = SOURCE_ZIP.resolve(strict=True)
        sealed_root = SEALED_INTAKE_ROOT.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RessetSuspensionCandidateError(
            f"sealed intake source is missing: {exc}"
        ) from exc
    if not resolved.is_relative_to(sealed_root):
        raise RessetSuspensionCandidateError("source escaped sealed intake root")
    cursor = resolved.parent
    while cursor != sealed_root.parent:
        if is_reparse_point(cursor):
            raise RessetSuspensionCandidateError(
                f"reparse point is not allowed in sealed source path: {cursor}"
            )
        if cursor == sealed_root:
            break
        cursor = cursor.parent
    if is_reparse_point(resolved):
        raise RessetSuspensionCandidateError("source ZIP is a reparse point")

    state = source_state(resolved)
    if state["bytes"] != EXPECTED_ZIP_BYTES:
        raise RessetSuspensionCandidateError(
            f"source ZIP byte count drifted: {state['bytes']}"
        )
    if state["sha256"] != EXPECTED_ZIP_SHA256:
        raise RessetSuspensionCandidateError(
            f"source ZIP hash drifted: {state['sha256']}"
        )
    manifest_hash = sha256_file(INTAKE_MANIFEST)
    if manifest_hash != EXPECTED_INTAKE_MANIFEST_SHA256:
        raise RessetSuspensionCandidateError(
            f"sealed intake manifest hash drifted: {manifest_hash}"
        )
    return {
        "path": resolved.as_posix(),
        **state,
        "intake_manifest": INTAKE_MANIFEST.as_posix(),
        "intake_manifest_sha256": manifest_hash,
    }


def validate_zip_and_read_member(path: Path) -> tuple[bytes, list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) != 3:
                raise RessetSuspensionCandidateError(
                    f"reviewed ZIP member count changed: {len(infos)}"
                )
            data_payload: bytes | None = None
            for info in infos:
                member = PurePosixPath(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    raise RessetSuspensionCandidateError(
                        f"unsafe ZIP member path: {info.filename}"
                    )
                folded = info.filename.casefold()
                if folded in seen:
                    raise RessetSuspensionCandidateError(
                        f"duplicate ZIP member path: {info.filename}"
                    )
                seen.add(folded)
                if info.flag_bits & 0x1:
                    raise RessetSuspensionCandidateError(
                        f"encrypted ZIP member is forbidden: {info.filename}"
                    )
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise RessetSuspensionCandidateError(
                        f"ZIP symlink member is forbidden: {info.filename}"
                    )
                payload = archive.read(info)
                item = {
                    "path": info.filename,
                    "bytes": len(payload),
                    "compressed_bytes": info.compress_size,
                    "crc32": f"{info.CRC:08x}",
                    "sha256": sha256_bytes(payload),
                }
                evidence.append(item)
                if info.filename == DATA_MEMBER:
                    data_payload = payload
                    if item["bytes"] != EXPECTED_MEMBER_BYTES:
                        raise RessetSuspensionCandidateError(
                            f"data member byte count drifted: {item['bytes']}"
                        )
                    if item["crc32"] != EXPECTED_MEMBER_CRC32:
                        raise RessetSuspensionCandidateError(
                            f"data member CRC drifted: {item['crc32']}"
                        )
                    if item["sha256"] != EXPECTED_MEMBER_SHA256:
                        raise RessetSuspensionCandidateError(
                            f"data member hash drifted: {item['sha256']}"
                        )
            if data_payload is None:
                raise RessetSuspensionCandidateError(
                    f"required member is missing: {DATA_MEMBER}"
                )
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, RessetSuspensionCandidateError):
            raise
        raise RessetSuspensionCandidateError(f"ZIP validation failed: {exc}") from exc
    return data_payload, evidence


def parse_strict_row(line_without_eol: str) -> list[str]:
    reader = csv.reader([line_without_eol], strict=True)
    row = next(reader)
    if len(row) != len(BUSINESS_COLUMNS):
        raise RessetSuspensionCandidateError(
            f"strict row has {len(row)} fields, expected {len(BUSINESS_COLUMNS)}"
        )
    return row


def parse_leading_fields(raw: str, count: int) -> tuple[list[str], int]:
    """Strictly parse *count* leading CSV fields and return next position."""

    values: list[str] = []
    pos = 0
    for _ in range(count):
        if pos >= len(raw):
            raise RessetSuspensionCandidateError("truncated leading-field prefix")
        if raw[pos] == '"':
            pos += 1
            chunks: list[str] = []
            while pos < len(raw):
                char = raw[pos]
                if char != '"':
                    chunks.append(char)
                    pos += 1
                    continue
                if pos + 1 < len(raw) and raw[pos + 1] == '"':
                    chunks.append('"')
                    pos += 2
                    continue
                pos += 1
                if pos >= len(raw) or raw[pos] != ",":
                    raise RessetSuspensionCandidateError(
                        "quoted leading field did not end at a delimiter"
                    )
                pos += 1
                values.append("".join(chunks))
                break
            else:
                raise RessetSuspensionCandidateError("unterminated leading field")
        else:
            end = raw.find(",", pos)
            if end < 0:
                raise RessetSuspensionCandidateError("missing leading-field delimiter")
            values.append(raw[pos:end])
            pos = end + 1
    return values, pos


def serialize_business_row(row: list[str]) -> str:
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="").writerow(row)
    return output.getvalue()


def recover_bare_quotes(raw: str) -> tuple[list[str], dict[str, Any]]:
    """Recover one known-class malformed SuspnsnReason record losslessly."""

    prefix, reason_start = parse_leading_fields(raw, 11)
    if reason_start >= len(raw) or raw[reason_start] != '"':
        raise RessetSuspensionCandidateError(
            "strict anomaly is not a quoted SuspnsnReason field"
        )

    candidates: list[tuple[int, list[str]]] = []
    search_pos = reason_start + 1
    while True:
        closing = raw.find('",', search_pos)
        if closing < 0:
            break
        suffix_raw = raw[closing + 2 :]
        try:
            suffix = next(csv.reader([suffix_raw], strict=True))
        except csv.Error:
            search_pos = closing + 1
            continue
        if len(suffix) == 10:
            candidates.append((closing, suffix))
        search_pos = closing + 1

    if len(candidates) != 1:
        raise RessetSuspensionCandidateError(
            f"bare-quote repair boundary is not unique: {len(candidates)} candidates"
        )
    closing, suffix = candidates[0]
    reason = raw[reason_start + 1 : closing]
    if '"' not in reason:
        raise RessetSuspensionCandidateError(
            "strict anomaly contains no recoverable literal quote"
        )
    if '""' in reason:
        raise RessetSuspensionCandidateError(
            "unexpected doubled quote inside reviewed malformed reason"
        )

    recovered = prefix + [reason] + suffix
    if len(recovered) != len(BUSINESS_COLUMNS):
        raise RessetSuspensionCandidateError(
            f"recovered row has {len(recovered)} fields"
        )
    normalized = serialize_business_row(recovered)
    reparsed = parse_strict_row(normalized)
    if reparsed != recovered:
        raise RessetSuspensionCandidateError("normalized row did not strict-roundtrip")
    return recovered, {
        "reason_start_offset": reason_start,
        "reason_close_offset": closing,
        "literal_quote_count": reason.count('"'),
        "repair_rule": (
            "strict_prefix_11 + unique_strict_suffix_10; preserve each bare "
            "quote as a literal SuspnsnReason character; csv.writer doubles it"
        ),
        "normalized_business_row_text": normalized,
    }


def validate_iso_date(value: str, column: str, row_number: int) -> None:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise RessetSuspensionCandidateError(
            f"invalid {column} at source row {row_number}: {value!r}"
        ) from exc
    if parsed.isoformat() != value:
        raise RessetSuspensionCandidateError(
            f"non-canonical {column} at source row {row_number}: {value!r}"
        )


def parse_member(payload: bytes) -> dict[str, Any]:
    if payload.startswith(b"\xef\xbb\xbf"):
        raise RessetSuspensionCandidateError("reviewed member unexpectedly gained a BOM")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RessetSuspensionCandidateError(f"member is not strict UTF-8: {exc}") from exc

    lines = text.splitlines(keepends=True)
    if len(lines) != EXPECTED_RECORDS + 1:
        raise RessetSuspensionCandidateError(
            f"physical line count drifted: {len(lines)}"
        )
    header_text = lines[0].rstrip("\r\n")
    header = next(csv.reader([header_text], strict=True))
    if header != BUSINESS_COLUMNS:
        raise RessetSuspensionCandidateError(
            f"reviewed 22-column header drifted: {header}"
        )

    records: list[dict[str, str]] = []
    repairs: list[dict[str, Any]] = []
    class_counts: Counter[str] = Counter()
    sentinel_count = 0
    reconstruction = [lines[0]]

    for data_row_number, raw_line in enumerate(lines[1:], start=1):
        physical_line_number = data_row_number + 1
        reconstruction.append(raw_line)
        raw_without_eol = raw_line.rstrip("\r\n")
        if "\r" in raw_without_eol or "\n" in raw_without_eol:
            raise RessetSuspensionCandidateError(
                f"unexpected embedded line break at row {data_row_number}"
            )
        repair_details: dict[str, Any] | None = None
        strict_error: str | None = None
        try:
            row = parse_strict_row(raw_without_eol)
            repair_class = "STRICT_STANDARD_RECORD"
        except csv.Error as exc:
            strict_error = str(exc)
            row, repair_details = recover_bare_quotes(raw_without_eol)
            repair_class = "BARE_QUOTE_IN_SUSPNSNREASON_NORMALIZED"

        if len(row) != len(BUSINESS_COLUMNS):
            raise RessetSuspensionCandidateError(
                f"row {data_row_number} has {len(row)} business fields"
            )
        values = dict(zip(BUSINESS_COLUMNS, row, strict=True))
        for column in DATE_COLUMNS:
            validate_iso_date(values[column], column, data_row_number)
        # ResmptnDt is deliberately validated only as a lexical ISO date and
        # retained as VARCHAR; 1900-01-01 is evidence, not converted to null.
        validate_iso_date(values["ResmptnDt"], "ResmptnDt", data_row_number)
        if values["ResmptnDt"] == EXPECTED_SENTINEL:
            sentinel_count += 1

        normalized = serialize_business_row(row)
        if parse_strict_row(normalized) != row:
            raise RessetSuspensionCandidateError(
                f"normalized row {data_row_number} failed strict roundtrip"
            )
        raw_hash = sha256_bytes(raw_line.encode("utf-8"))
        normalized_hash = sha256_bytes(normalized.encode("utf-8"))
        candidate = {
            **values,
            "_source_data_row_number": str(data_row_number),
            "_source_physical_line_number": str(physical_line_number),
            "_repair_class": repair_class,
            "_raw_line_sha256": raw_hash,
            "_normalized_business_row_sha256": normalized_hash,
            "_raw_line_text": raw_line,
            "_normalized_business_row_text": normalized,
        }
        records.append(candidate)
        class_counts[repair_class] += 1

        if repair_details is not None:
            permissive_row = next(csv.reader([raw_without_eol], strict=False))
            repairs.append(
                {
                    "source_data_row_number": data_row_number,
                    "source_physical_line_number": physical_line_number,
                    "strict_error": strict_error,
                    "repair_class": repair_class,
                    "raw_line_sha256": raw_hash,
                    "normalized_business_row_sha256": normalized_hash,
                    "raw_line_text": raw_line,
                    "recovered_suspnsn_reason": row[11],
                    "permissive_parser_suspnsn_reason": permissive_row[11],
                    "permissive_parser_not_used": True,
                    **repair_details,
                }
            )

    reconstructed = "".join(reconstruction).encode("utf-8")
    if reconstructed != payload:
        raise RessetSuspensionCandidateError("raw member byte reconstruction failed")
    if len(records) != EXPECTED_RECORDS:
        raise RessetSuspensionCandidateError(
            f"record count drifted: {len(records)}"
        )
    if len(repairs) != EXPECTED_STRICT_QUOTE_ERRORS:
        raise RessetSuspensionCandidateError(
            f"strict quote error count drifted: {len(repairs)}"
        )
    if class_counts["STRICT_STANDARD_RECORD"] != (
        EXPECTED_RECORDS - EXPECTED_STRICT_QUOTE_ERRORS
    ):
        raise RessetSuspensionCandidateError("standard record count drifted")

    metrics = {
        "source_header_column_count": len(BUSINESS_COLUMNS),
        "source_record_count": len(records),
        "source_physical_data_line_count": len(lines) - 1,
        "source_member_byte_roundtrip": "PASS",
        "all_business_rows_strict_after_normalization": True,
        "business_field_count_min": 22,
        "business_field_count_max": 22,
        "strict_quote_error_count": len(repairs),
        "repair_class_counts": dict(sorted(class_counts.items())),
        "resmptn_dt_storage_type": "VARCHAR",
        "resmptn_dt_1900_01_01_sentinel_count": sentinel_count,
        "raw_line_hash_count": len(records),
        "normalized_row_hash_count": len(records),
    }
    return {
        "header": header,
        "records": records,
        "repairs": repairs,
        "metrics": metrics,
    }


def write_bytes_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)


def write_text_exclusive(path: Path, text: str) -> None:
    write_bytes_exclusive(path, text.encode("utf-8"))


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


def verify_candidate_csv(
    path: Path, expected: list[dict[str, str]]
) -> dict[str, Any]:
    count = 0
    repairs = 0
    sentinel_count = 0
    with path.open("r", encoding="utf-8", errors="strict", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != BUSINESS_COLUMNS + AUDIT_COLUMNS:
            raise RessetSuspensionCandidateError("candidate header readback failed")
        for count, (actual, original) in enumerate(
            zip(reader, expected, strict=False), start=1
        ):
            if actual != original:
                raise RessetSuspensionCandidateError(
                    f"candidate readback differs at row {count}"
                )
            raw = actual["_raw_line_text"]
            if sha256_bytes(raw.encode("utf-8")) != actual["_raw_line_sha256"]:
                raise RessetSuspensionCandidateError(
                    f"raw line hash readback failed at row {count}"
                )
            normalized = actual["_normalized_business_row_text"]
            if (
                sha256_bytes(normalized.encode("utf-8"))
                != actual["_normalized_business_row_sha256"]
            ):
                raise RessetSuspensionCandidateError(
                    f"normalized row hash readback failed at row {count}"
                )
            normalized_values = parse_strict_row(normalized)
            if normalized_values != [actual[column] for column in BUSINESS_COLUMNS]:
                raise RessetSuspensionCandidateError(
                    f"22-field normalized readback failed at row {count}"
                )
            if actual["_repair_class"] != "STRICT_STANDARD_RECORD":
                repairs += 1
            if actual["ResmptnDt"] == EXPECTED_SENTINEL:
                sentinel_count += 1
        extra = next(reader, None)
        if extra is not None:
            raise RessetSuspensionCandidateError("candidate has unexpected extra rows")
    if count != len(expected) or count != EXPECTED_RECORDS:
        raise RessetSuspensionCandidateError(
            f"candidate row count readback failed: {count}"
        )
    if repairs != EXPECTED_STRICT_QUOTE_ERRORS:
        raise RessetSuspensionCandidateError(
            f"candidate repair count readback failed: {repairs}"
        )
    return {
        "status": "PASS",
        "row_count": count,
        "business_field_count": 22,
        "strict_normalized_row_count": count,
        "raw_line_hash_roundtrip_count": count,
        "normalized_row_hash_roundtrip_count": count,
        "repair_count": repairs,
        "resmptn_dt_1900_01_01_sentinel_count": sentinel_count,
    }


def artifact(path: Path, root: Path, **extra: Any) -> dict[str, Any]:
    return {
        "relative_path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        **extra,
    }


def build_plan(source: dict[str, Any], members: list[dict[str, Any]], parsed: dict[str, Any], target: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "tool_id": TOOL_ID,
        "mode": "PLAN",
        "status": "PLAN_VALIDATED_WRITE_FREE",
        "write_performed": False,
        "dataset_id": DATASET_ID,
        "table": TABLE_ID,
        "candidate_path": target.as_posix(),
        "candidate_exists": target.exists(),
        "source": {**source, "members": members},
        "metrics": parsed["metrics"],
        "planned_outputs": [
            "raw/<sealed ZIP>",
            f"raw/extracted/{DATA_MEMBER}",
            "curated/candidate.csv",
            "quality/repair-ledger.json",
            "quality/summary.json",
            "provenance.json",
            "REPORT.md",
            "manifest.json",
        ],
        "safety": {
            "formal_effect": "NONE",
            "purpose_ceiling": "INSPECTION_ONLY",
            "admission_decision": "NOT_ADMITTED",
            "catalog_registered": False,
            "quality_promotion": False,
            "research_ready": False,
            "source_zip_modified": False,
        },
    }


def execute(
    run_id: str,
    source: dict[str, Any],
    members: list[dict[str, Any]],
    member_payload: bytes,
    parsed: dict[str, Any],
) -> dict[str, Any]:
    target = OUTPUT_BASE / run_id
    if target.exists():
        raise RessetSuspensionCandidateError(f"candidate already exists: {target}")
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    staging = OUTPUT_BASE / f".{run_id}.staging-{uuid.uuid4().hex}"
    if staging.exists():
        raise RessetSuspensionCandidateError(f"staging path exists: {staging}")

    source_before = source_state(SOURCE_ZIP)
    catalog_before = sha256_file(FORMAL_CATALOG)
    artifacts: dict[str, Any] = {}
    try:
        staging.mkdir(parents=False, exist_ok=False)
        raw_zip = staging / "raw" / SOURCE_ZIP.name
        raw_member = staging / "raw" / "extracted" / DATA_MEMBER
        candidate = staging / "curated" / "candidate.csv"
        repair_ledger_path = staging / "quality" / "repair-ledger.json"
        quality_summary_path = staging / "quality" / "summary.json"
        provenance_path = staging / "provenance.json"
        report_path = staging / "REPORT.md"

        write_bytes_exclusive(raw_zip, SOURCE_ZIP.read_bytes())
        write_bytes_exclusive(raw_member, member_payload)
        write_candidate_csv(candidate, parsed["records"])
        readback = verify_candidate_csv(candidate, parsed["records"])

        repair_ledger = {
            "schema_version": 1,
            "dataset_id": DATASET_ID,
            "table": TABLE_ID,
            "status": "INSPECTION_ONLY",
            "admission_decision": "NOT_ADMITTED",
            "normalization_contract": {
                "standard_rows": "strict csv.reader; exact 22 fields",
                "exception_rows": (
                    "strict first 11 fields + unique strict final 10 fields; "
                    "middle SuspnsnReason retains all literal quote characters; "
                    "csv.writer creates reproducible standards-compliant escaping"
                ),
                "permissive_parser_is_not_repair_authority": True,
                "raw_line_preserved_for_every_record": True,
                "silent_field_drop_allowed": False,
                "resmptn_dt_type": "VARCHAR",
                "resmptn_dt_sentinel_preserved": EXPECTED_SENTINEL,
            },
            "metrics": parsed["metrics"],
            "repairs": parsed["repairs"],
        }
        write_bytes_exclusive(repair_ledger_path, json_bytes(repair_ledger))

        quality_summary = {
            "schema_version": 1,
            "dataset_id": DATASET_ID,
            "table": TABLE_ID,
            "quality_status": "INSPECTION_ONLY",
            "admission_decision": "NOT_ADMITTED",
            "metrics": parsed["metrics"],
            "candidate_roundtrip": readback,
            "gates": {
                "sealed_source_hash": "PASS",
                "zip_crc_and_member_hash": "PASS",
                "record_count_81535": "PASS",
                "business_columns_22": "PASS",
                "strict_quote_errors_8_accounted": "PASS",
                "all_rows_strict_after_normalization": "PASS",
                "raw_line_and_hash_retained": "PASS",
                "resmptn_dt_sentinel_as_varchar": "PASS",
                "formal_admission": "NOT_ATTEMPTED",
            },
        }
        write_bytes_exclusive(quality_summary_path, json_bytes(quality_summary))

        provenance = {
            "schema_version": 1,
            "tool_id": TOOL_ID,
            "dataset_id": DATASET_ID,
            "table": TABLE_ID,
            "run_id": run_id,
            "created_at": now_utc(),
            "source": {**source, "members": members},
            "transformation": {
                "kind": "lossless_csv_quote_normalization_candidate",
                "business_columns": BUSINESS_COLUMNS,
                "business_column_count": 22,
                "date_storage": {
                    "ResmptnDt": "VARCHAR",
                    "other_date_fields": "lexically validated ISO-8601 strings",
                },
                "repair_ledger": "quality/repair-ledger.json",
            },
            "formal_effect": "NONE",
            "purpose_ceiling": "INSPECTION_ONLY",
            "admission_decision": "NOT_ADMITTED",
        }
        write_bytes_exclusive(provenance_path, json_bytes(provenance))

        report = f"""# RESSET SUSPNSNRESMPTN zero-loss candidate

- Status: `INSPECTION_ONLY / NOT_ADMITTED`
- Source: sealed intake `{SOURCE_ZIP.as_posix()}`
- Source ZIP SHA-256: `{EXPECTED_ZIP_SHA256}`
- Data member SHA-256: `{EXPECTED_MEMBER_SHA256}`
- Records: `{EXPECTED_RECORDS:,}`
- Documented business fields: `22`
- Strict quote anomalies: `{EXPECTED_STRICT_QUOTE_ERRORS}`
- ResmptnDt sentinel `{EXPECTED_SENTINEL}` retained as VARCHAR: `{parsed['metrics']['resmptn_dt_1900_01_01_sentinel_count']:,}` rows

The eight malformed rows were normalized only after deterministic boundary
recovery.  Every literal quote, raw source line, raw-line hash, normalized row
and normalized-row hash is retained.  This candidate is not cataloged, not a
formal snapshot, and not authorized for research.
"""
        write_text_exclusive(report_path, report)

        artifacts = {
            "raw_zip": artifact(raw_zip, staging),
            "raw_csv_member": artifact(raw_member, staging),
            "candidate_csv": artifact(
                candidate, staging, row_count=EXPECTED_RECORDS, business_field_count=22
            ),
            "repair_ledger": artifact(
                repair_ledger_path,
                staging,
                strict_quote_error_count=EXPECTED_STRICT_QUOTE_ERRORS,
            ),
            "quality_summary": artifact(quality_summary_path, staging),
            "provenance": artifact(provenance_path, staging),
            "report": artifact(report_path, staging),
        }
        content_hash = sha256_bytes(
            json_bytes(
                {
                    "source_zip_sha256": EXPECTED_ZIP_SHA256,
                    "source_member_sha256": EXPECTED_MEMBER_SHA256,
                    "candidate_csv_sha256": artifacts["candidate_csv"]["sha256"],
                    "repair_ledger_sha256": artifacts["repair_ledger"]["sha256"],
                    "metrics": parsed["metrics"],
                },
                canonical=True,
            )
        )

        source_after = source_state(SOURCE_ZIP)
        if source_after != source_before:
            raise RessetSuspensionCandidateError("sealed source changed during execution")
        catalog_after = sha256_file(FORMAL_CATALOG)
        if catalog_after != catalog_before:
            raise RessetSuspensionCandidateError("formal catalog changed during execution")

        manifest = {
            "schema_version": 1,
            "manifest_kind": "RESSET_SUSPNSNRESMPTN_ZERO_LOSS_CANDIDATE",
            "tool_id": TOOL_ID,
            "dataset_id": DATASET_ID,
            "table": TABLE_ID,
            "run_id": run_id,
            "created_at": now_utc(),
            "status": "ZERO_LOSS_CANDIDATE_MATERIALIZED",
            "write_performed": True,
            "source": {**source, "members": members},
            "metrics": parsed["metrics"],
            "candidate_roundtrip": readback,
            "artifacts": artifacts,
            "content_hash_algorithm": (
                "sha256-canonical-json-resset-suspnsnresmptn-zero-loss-v1"
            ),
            "content_hash": content_hash,
            "catalog_guard": {
                "path": FORMAL_CATALOG.as_posix(),
                "sha256_before": catalog_before,
                "sha256_after": catalog_after,
                "write_authorized": False,
            },
            "safety": {
                "formal_effect": "NONE",
                "purpose_ceiling": "INSPECTION_ONLY",
                "admission_decision": "NOT_ADMITTED",
                "catalog_registered": False,
                "quality_promotion": False,
                "research_ready": False,
                "source_admission": False,
                "source_zip_modified": False,
            },
        }
        write_bytes_exclusive(staging / "manifest.json", json_bytes(manifest))
        os.replace(staging, target)
        return {**manifest, "candidate_path": target.as_posix()}
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="validate without writing")
    mode.add_argument("--execute", action="store_true", help="materialize candidate")
    args = parser.parse_args()
    if not RUN_ID_RE.fullmatch(args.run_id):
        parser.error("--run-id contains unsupported characters")
    return args


def main() -> int:
    args = parse_args()
    try:
        source = validate_source_binding()
        member_payload, members = validate_zip_and_read_member(SOURCE_ZIP)
        parsed = parse_member(member_payload)
        target = OUTPUT_BASE / args.run_id
        if args.execute:
            result = execute(
                args.run_id, source, members, member_payload, parsed
            )
        else:
            result = build_plan(source, members, parsed, target)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:  # fail closed with a machine-readable receipt
        result = {
            "status": "FAIL_CLOSED",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "write_performed": False,
            "formal_effect": "NONE",
            "purpose_ceiling": "INSPECTION_ONLY",
            "admission_decision": "NOT_ADMITTED",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
