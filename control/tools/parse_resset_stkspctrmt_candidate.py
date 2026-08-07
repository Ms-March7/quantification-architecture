#!/usr/bin/env python3
"""Build a zero-loss RESSET STKSPCTRMT inspection candidate.

The reviewed RESSET export is not a conventional rectangular CSV: free-text
tails contain bare commas, bare quotes, and embedded newlines.  A normal
``csv.reader`` therefore returns 4,103 rows, including 76 rows whose arity is
not the documented 15 columns.  The website export itself carries a stronger
record boundary: each record begins with ``UpDt,"nobs",`` and the reviewed
file contains the complete sequence ``nobs=1..4105``.

This tool uses that boundary only to frame records.  It parses the first 13
fields exactly, preserves the complete raw record and untrusted tail, and
assigns the last two fields only when the framed record is an unambiguous
15-column CSV row.  Every exceptional decision is written to a repair ledger.

The default operation is a complete, write-free PLAN.  ``--execute`` writes
only below ``tmp/local-dataset-build/resset-stkspctrmt``.  The source ZIP is
never modified.  All outputs remain ``INSPECTION_ONLY/NOT_ADMITTED``.
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
TOOL_ID = (
    "parse_resset_stkspctrmt_candidate.py/"
    "resset-stkspctrmt-zero-loss-candidate-v1"
)
PURPOSE_CEILING = "INSPECTION_ONLY"
ADMISSION_DECISION = "NOT_ADMITTED"
DATASET_ID = "resset-stkspctrmt"
TABLE_NAME = "STKSPCTRMT"
DEFAULT_RUN_ID = "manual-export-20260802-zero-loss-v1"

EXPECTED_ZIP_NAME = "RESSET 股票_股票特别处理与撤销.zip"
EXPECTED_ZIP_BYTES = 739_790
EXPECTED_ZIP_SHA256 = (
    "963e36bcbc8c3dc305932cf169829865d4c3d5375430c70f57e6f217533bcbdb"
)
CSV_MEMBER = "RESSET_STKSPCTRMT_1.csv"
EXPECTED_CSV_BYTES = 2_006_008
EXPECTED_CSV_SHA256 = (
    "76a089fbde2b67f22987497219b80b0f3b6697216426066638777a07cd391537"
)
EXPECTED_CSV_CRC32 = "1bd8755a"

EXPECTED_RECORD_COUNT = 4_105
EXPECTED_STANDARD_OUTPUT_COUNT = 4_103
EXPECTED_STANDARD_ARITY_ANOMALIES = 76
EXPECTED_MULTILINE_RECORD_COUNT = 53

EXPECTED_COLUMNS = (
    "UpDt",
    "nobs",
    "CompanyCode",
    "ComCd",
    "ISIN",
    "R_SecuCode",
    "StkCd",
    "StkCdOTrd",
    "LStkNm",
    "InfoPubDt",
    "SecuAbbr",
    "TrmtType",
    "ImpDt",
    "TrmtDes",
    "TrmtRea",
)
PREFIX_COLUMNS = EXPECTED_COLUMNS[:13]
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}$")
RECORD_BOUNDARY = re.compile(
    r'^\d{4}-\d{2}-\d{2},"(?P<nobs>\d+)",'
)

CANDIDATE_COLUMNS = (
    *PREFIX_COLUMNS,
    "TrmtDes_candidate",
    "TrmtRea_candidate",
    "tail_parse_status",
    "repair_class",
    "physical_line_count",
    "source_start_line",
    "source_end_line",
    "framed_parser_row_count",
    "framed_parser_field_counts_json",
    "framed_parser_rows_json",
    "prefix_raw_text",
    "tail_raw_text",
    "raw_record_text",
    "raw_record_sha256",
    "tail_sha256",
)


class RessetCandidateError(RuntimeError):
    """A controlled, fail-closed candidate rejection."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_bytes(value: Any, *, canonical: bool = False) -> bytes:
    options: dict[str, Any] = {
        "ensure_ascii": False,
        "sort_keys": True,
        "allow_nan": False,
    }
    if canonical:
        options["separators"] = (",", ":")
    else:
        options["indent"] = 2
    return (json.dumps(value, **options) + "\n").encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.write_bytes(json_bytes(value))


def is_below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def is_reparse_point(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def safe_zip_member(name: str) -> None:
    normalized = name.replace("\\", "/")
    member = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in member.parts)
    ):
        raise RessetCandidateError(f"unsafe ZIP member path: {name!r}")


def validate_source_path(zip_path: Path, input_root: Path) -> Path:
    root = input_root.resolve(strict=True)
    source = zip_path.resolve(strict=True)
    if not root.is_dir():
        raise RessetCandidateError(f"input root is not a directory: {root}")
    if not source.is_file() or source.suffix.casefold() != ".zip":
        raise RessetCandidateError(f"source is not a ZIP file: {source}")
    if (
        zip_path.is_symlink()
        or is_reparse_point(zip_path)
        or not is_below(source, root)
    ):
        raise RessetCandidateError(
            "source ZIP must be a non-reparse file below the explicit input root"
        )
    if source.name != EXPECTED_ZIP_NAME:
        raise RessetCandidateError(
            f"unexpected source filename: {source.name!r}"
        )
    return source


def inspect_archive(source: Path) -> tuple[dict[str, Any], bytes]:
    source_stat = source.stat()
    source_hash = sha256_file(source)
    if source_stat.st_size != EXPECTED_ZIP_BYTES:
        raise RessetCandidateError("reviewed ZIP byte length changed")
    if source_hash != EXPECTED_ZIP_SHA256:
        raise RessetCandidateError("reviewed ZIP SHA-256 changed")

    members: list[dict[str, Any]] = []
    member_payloads: dict[str, bytes] = {}
    seen: set[str] = set()
    with zipfile.ZipFile(source) as archive:
        if len(archive.infolist()) > 100:
            raise RessetCandidateError("unexpectedly large ZIP member count")
        for info in archive.infolist():
            safe_zip_member(info.filename)
            folded = info.filename.casefold()
            if folded in seen:
                raise RessetCandidateError(
                    f"duplicate case-insensitive ZIP member: {info.filename}"
                )
            seen.add(folded)
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_IFMT(unix_mode) == stat.S_IFLNK:
                raise RessetCandidateError(
                    f"ZIP symlink is forbidden: {info.filename}"
                )
            if info.flag_bits & 0x1:
                raise RessetCandidateError(
                    f"encrypted ZIP member is forbidden: {info.filename}"
                )
            payload = archive.read(info)
            member_payloads[info.filename] = payload
            members.append(
                {
                    "path": info.filename,
                    "bytes": len(payload),
                    "compressed_bytes": int(info.compress_size),
                    "crc32": f"{info.CRC:08x}",
                    "sha256": sha256_bytes(payload),
                }
            )

    if CSV_MEMBER not in member_payloads:
        raise RessetCandidateError(f"missing reviewed CSV member: {CSV_MEMBER}")
    csv_payload = member_payloads[CSV_MEMBER]
    csv_member = next(item for item in members if item["path"] == CSV_MEMBER)
    if len(csv_payload) != EXPECTED_CSV_BYTES:
        raise RessetCandidateError("reviewed CSV member byte length changed")
    if sha256_bytes(csv_payload) != EXPECTED_CSV_SHA256:
        raise RessetCandidateError("reviewed CSV member SHA-256 changed")
    if csv_member["crc32"] != EXPECTED_CSV_CRC32:
        raise RessetCandidateError("reviewed CSV member CRC-32 changed")

    source_info = {
        "path": source.as_posix(),
        "filename": source.name,
        "bytes": int(source_stat.st_size),
        "sha256": source_hash,
        "last_write_time_ns": int(source_stat.st_mtime_ns),
        "members": members,
        "crc_read_status": "PASS",
    }
    return source_info, csv_payload


def parse_prefix(record: str) -> tuple[list[str], str, str]:
    """Parse exactly 13 CSV fields, returning values, raw prefix, raw tail."""

    values: list[str] = []
    position = 0
    for field_index in range(len(PREFIX_COLUMNS)):
        if position >= len(record):
            raise RessetCandidateError(
                f"record ended inside prefix field {field_index + 1}"
            )
        if record[position] == '"':
            position += 1
            value: list[str] = []
            closed = False
            while position < len(record):
                char = record[position]
                if char == '"':
                    if position + 1 < len(record) and record[position + 1] == '"':
                        value.append('"')
                        position += 2
                        continue
                    position += 1
                    closed = True
                    break
                value.append(char)
                position += 1
            if not closed:
                raise RessetCandidateError(
                    f"unterminated quote inside prefix field {field_index + 1}"
                )
            parsed = "".join(value)
        else:
            delimiter = record.find(",", position)
            if delimiter < 0:
                raise RessetCandidateError(
                    f"missing delimiter after prefix field {field_index + 1}"
                )
            parsed = record[position:delimiter]
            position = delimiter

        if position >= len(record) or record[position] != ",":
            raise RessetCandidateError(
                f"missing exact comma after prefix field {field_index + 1}"
            )
        position += 1
        values.append(parsed)
    return values, record[:position], record[position:]


def standard_parser_baseline(text: str) -> dict[str, Any]:
    stream = io.StringIO(text, newline="")
    reader = csv.reader(stream, strict=False)
    header = next(reader)
    if tuple(header) != EXPECTED_COLUMNS:
        raise RessetCandidateError("standard-parser header differs from contract")

    previous_line = reader.line_num
    output_count = 0
    multiline_reads = 0
    arity_counts: Counter[int] = Counter()
    anomalies: list[dict[str, Any]] = []
    for row in reader:
        output_count += 1
        consumed_lines = reader.line_num - previous_line
        previous_line = reader.line_num
        if consumed_lines > 1:
            multiline_reads += 1
        arity_counts[len(row)] += 1
        if len(row) != len(EXPECTED_COLUMNS):
            anomalies.append(
                {
                    "parser_output_index": output_count,
                    "reader_end_line": int(reader.line_num),
                    "physical_lines_consumed": int(consumed_lines),
                    "field_count": len(row),
                    "nobs_hint": row[1] if len(row) > 1 else None,
                    "parsed_row_sha256": sha256_bytes(
                        json_bytes(row, canonical=True)
                    ),
                }
            )

    if output_count != EXPECTED_STANDARD_OUTPUT_COUNT:
        raise RessetCandidateError(
            "standard-parser output count differs from reviewed baseline"
        )
    if len(anomalies) != EXPECTED_STANDARD_ARITY_ANOMALIES:
        raise RessetCandidateError(
            "standard-parser anomaly count differs from reviewed baseline"
        )
    return {
        "output_record_count": output_count,
        "arity_anomaly_count": len(anomalies),
        "field_count_distribution": {
            str(key): value for key, value in sorted(arity_counts.items())
        },
        "multiline_reads": multiline_reads,
        "anomalies": anomalies,
    }


def validate_iso_date(value: str, label: str, nobs: int) -> None:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise RessetCandidateError(
            f"invalid {label} in nobs={nobs}: {value!r}"
        ) from exc


def frame_records(csv_payload: bytes) -> dict[str, Any]:
    # The reviewed RESSET member is plain UTF-8 without a BOM.  Bind that
    # encoding fact explicitly so a silently re-exported variant cannot pass
    # the byte-roundtrip gate under different decoding semantics.
    if csv_payload.startswith(b"\xef\xbb\xbf"):
        raise RessetCandidateError("reviewed CSV member unexpectedly gained a UTF-8 BOM")
    text = csv_payload.decode("utf-8", errors="strict")
    lines = text.splitlines(keepends=True)
    if not lines:
        raise RessetCandidateError("reviewed CSV member is empty")

    header_reader = list(csv.reader([lines[0].rstrip("\r\n")], strict=True))
    if len(header_reader) != 1 or tuple(header_reader[0]) != EXPECTED_COLUMNS:
        raise RessetCandidateError("source header differs from reviewed contract")

    boundaries: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = RECORD_BOUNDARY.match(line)
        if match:
            boundaries.append((index, int(match.group("nobs"))))
    if len(boundaries) != EXPECTED_RECORD_COUNT:
        raise RessetCandidateError(
            f"website boundary count is {len(boundaries)}, expected "
            f"{EXPECTED_RECORD_COUNT}"
        )
    if boundaries[0][0] != 1:
        raise RessetCandidateError("unexpected bytes between header and first record")
    observed_nobs = [item[1] for item in boundaries]
    expected_nobs = list(range(1, EXPECTED_RECORD_COUNT + 1))
    if observed_nobs != expected_nobs:
        raise RessetCandidateError("nobs sequence is not exactly 1..4105")

    baseline = standard_parser_baseline(text)
    candidate_rows: list[dict[str, Any]] = []
    multiline_ledger: list[dict[str, Any]] = []
    recovered_repairs: list[dict[str, Any]] = []
    repair_counts: Counter[str] = Counter()
    framed_arity_counts: Counter[int] = Counter()
    ambiguous_tail_count = 0

    for offset, (line_index, boundary_nobs) in enumerate(boundaries):
        next_index = (
            boundaries[offset + 1][0]
            if offset + 1 < len(boundaries)
            else len(lines)
        )
        raw_record = "".join(lines[line_index:next_index])
        raw_bytes = raw_record.encode("utf-8")
        physical_line_count = next_index - line_index
        prefix_values, prefix_raw, tail_raw = parse_prefix(raw_record)
        parsed_nobs = int(prefix_values[1])
        if parsed_nobs != boundary_nobs:
            raise RessetCandidateError(
                f"prefix nobs differs from boundary nobs={boundary_nobs}"
            )
        validate_iso_date(prefix_values[0], "UpDt", parsed_nobs)
        validate_iso_date(prefix_values[9], "InfoPubDt", parsed_nobs)
        validate_iso_date(prefix_values[12], "ImpDt", parsed_nobs)

        framed_rows = list(
            csv.reader(io.StringIO(raw_record, newline=""), strict=False)
        )
        field_counts = [len(row) for row in framed_rows]
        for field_count in field_counts:
            framed_arity_counts[field_count] += 1
        unambiguous = len(framed_rows) == 1 and field_counts == [15]
        is_multiline = physical_line_count > 1
        is_arity_anomaly = not unambiguous
        if is_multiline and is_arity_anomaly:
            repair_class = "MULTILINE_AND_TAIL_ANOMALY_PRESERVED"
        elif is_multiline:
            repair_class = "MULTILINE_BOUNDARY_RECOVERY"
        elif is_arity_anomaly:
            repair_class = "TAIL_ARITY_ANOMALY_PRESERVED"
        else:
            repair_class = "STANDARD_RECORD"
        repair_counts[repair_class] += 1

        if unambiguous:
            trmt_des = framed_rows[0][13]
            trmt_rea = framed_rows[0][14]
            tail_parse_status = "UNAMBIGUOUS_15_COLUMN_PARSE"
        else:
            trmt_des = ""
            trmt_rea = ""
            tail_parse_status = "AMBIGUOUS_TAIL_PRESERVED_ONLY"
            ambiguous_tail_count += 1

        row: dict[str, Any] = {
            column: value
            for column, value in zip(PREFIX_COLUMNS, prefix_values, strict=True)
        }
        row.update(
            {
                "TrmtDes_candidate": trmt_des,
                "TrmtRea_candidate": trmt_rea,
                "tail_parse_status": tail_parse_status,
                "repair_class": repair_class,
                "physical_line_count": str(physical_line_count),
                "source_start_line": str(line_index + 1),
                "source_end_line": str(next_index),
                "framed_parser_row_count": str(len(framed_rows)),
                "framed_parser_field_counts_json": json.dumps(
                    field_counts, separators=(",", ":")
                ),
                "framed_parser_rows_json": json.dumps(
                    framed_rows,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "prefix_raw_text": prefix_raw,
                "tail_raw_text": tail_raw,
                "raw_record_text": raw_record,
                "raw_record_sha256": sha256_bytes(raw_bytes),
                "tail_sha256": sha256_bytes(tail_raw.encode("utf-8")),
            }
        )
        candidate_rows.append(row)

        if is_multiline:
            multiline_ledger.append(
                {
                    "nobs": parsed_nobs,
                    "source_start_line": line_index + 1,
                    "source_end_line": next_index,
                    "physical_line_count": physical_line_count,
                    "raw_record_sha256": row["raw_record_sha256"],
                    "repair_class": repair_class,
                }
            )
        if repair_class != "STANDARD_RECORD":
            recovered_repairs.append(
                {
                    "nobs": parsed_nobs,
                    "repair_class": repair_class,
                    "physical_line_count": physical_line_count,
                    "framed_parser_row_count": len(framed_rows),
                    "framed_parser_field_counts": field_counts,
                    "raw_record_sha256": row["raw_record_sha256"],
                    "tail_sha256": row["tail_sha256"],
                    "resolution": (
                        "FIRST_13_FIELDS_EXACT__RAW_RECORD_AND_TAIL_PRESERVED"
                    ),
                }
            )

    if len(multiline_ledger) != EXPECTED_MULTILINE_RECORD_COUNT:
        raise RessetCandidateError(
            "multiline record count differs from reviewed website boundary"
        )
    if len(candidate_rows) != EXPECTED_RECORD_COUNT:
        raise RessetCandidateError("candidate row count differs from boundary count")
    if [int(row["nobs"]) for row in candidate_rows] != expected_nobs:
        raise RessetCandidateError("candidate nobs sequence is incomplete")

    reconstructed_text = lines[0] + "".join(
        row["raw_record_text"] for row in candidate_rows
    )
    reconstructed_bytes = reconstructed_text.encode("utf-8")
    if reconstructed_bytes != csv_payload:
        raise RessetCandidateError("raw-record reconstruction is not byte exact")

    repair_ledger = {
        "schema_version": 1,
        "ledger_kind": "RESSET_STKSPCTRMT_ZERO_LOSS_REPAIR_LEDGER",
        "table": TABLE_NAME,
        "standard_parser_baseline": {
            key: value for key, value in baseline.items() if key != "anomalies"
        },
        "standard_parser_anomalies": baseline["anomalies"],
        "website_boundary_recovery": {
            "record_count": len(candidate_rows),
            "nobs_first": 1,
            "nobs_last": EXPECTED_RECORD_COUNT,
            "nobs_unique_and_contiguous": True,
            "multiline_record_count": len(multiline_ledger),
            "repair_class_counts": dict(sorted(repair_counts.items())),
            "ambiguous_tail_record_count": ambiguous_tail_count,
            "framed_field_count_distribution": {
                str(key): value
                for key, value in sorted(framed_arity_counts.items())
            },
        },
        "multiline_records": multiline_ledger,
        "recovered_record_repairs": recovered_repairs,
        "loss_policy": {
            "prefix_columns_parsed_exactly": list(PREFIX_COLUMNS),
            "raw_record_preserved_for_every_row": True,
            "raw_tail_preserved_for_every_row": True,
            "ambiguous_last_two_fields_left_unassigned": True,
            "silent_drop": False,
        },
    }
    metrics = {
        "source_record_count": len(candidate_rows),
        "nobs_first": 1,
        "nobs_last": EXPECTED_RECORD_COUNT,
        "nobs_unique_count": len({row["nobs"] for row in candidate_rows}),
        "nobs_complete": True,
        "prefix_column_count": len(PREFIX_COLUMNS),
        "prefix_parse_failures": 0,
        "standard_parser_output_count": baseline["output_record_count"],
        "standard_parser_arity_anomaly_count": baseline[
            "arity_anomaly_count"
        ],
        "standard_parser_multiline_reads": baseline["multiline_reads"],
        "website_boundary_multiline_record_count": len(multiline_ledger),
        "framed_ambiguous_tail_count": ambiguous_tail_count,
        "repair_class_counts": dict(sorted(repair_counts.items())),
        "source_member_byte_roundtrip": "PASS",
    }
    return {
        "header_raw_text": lines[0],
        "candidate_rows": candidate_rows,
        "repair_ledger": repair_ledger,
        "metrics": metrics,
    }


def artifact(path: Path, root: Path, *, row_count: int | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "relative_path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    if row_count is not None:
        value["row_count"] = row_count
    return value


def write_candidate_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8", errors="strict", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=CANDIDATE_COLUMNS,
            extrasaction="raise",
            lineterminator="\n",
            quoting=csv.QUOTE_MINIMAL,
        )
        writer.writeheader()
        writer.writerows(rows)


def verify_candidate_csv(
    path: Path,
    source_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    with path.open("r", encoding="utf-8", errors="strict", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != CANDIDATE_COLUMNS:
            raise RessetCandidateError("candidate CSV header changed")
        observed = list(reader)
    if len(observed) != EXPECTED_RECORD_COUNT:
        raise RessetCandidateError("candidate CSV row count mismatch")
    if [int(row["nobs"]) for row in observed] != list(
        range(1, EXPECTED_RECORD_COUNT + 1)
    ):
        raise RessetCandidateError("candidate CSV nobs sequence mismatch")
    for expected, actual in zip(source_rows, observed, strict=True):
        if actual["raw_record_text"] != expected["raw_record_text"]:
            raise RessetCandidateError(
                f"candidate raw record changed at nobs={expected['nobs']}"
            )
        if sha256_bytes(actual["raw_record_text"].encode("utf-8")) != actual[
            "raw_record_sha256"
        ]:
            raise RessetCandidateError(
                f"candidate raw record hash mismatch at nobs={expected['nobs']}"
            )
        if actual["tail_raw_text"] != expected["tail_raw_text"]:
            raise RessetCandidateError(
                f"candidate raw tail changed at nobs={expected['nobs']}"
            )
    return {
        "row_count": len(observed),
        "nobs_unique_and_contiguous": True,
        "raw_record_roundtrip_count": len(observed),
        "raw_tail_roundtrip_count": len(observed),
        "status": "PASS",
    }


def catalog_hash(workspace: Path) -> str:
    catalog = workspace / "data" / "catalog" / "datasets.json"
    if not catalog.is_file():
        raise RessetCandidateError(f"formal catalog is missing: {catalog}")
    return sha256_file(catalog)


def validate_output_target(workspace: Path, run_id: str) -> tuple[Path, Path]:
    if not SAFE_COMPONENT.fullmatch(run_id):
        raise RessetCandidateError(f"unsafe run-id: {run_id!r}")
    base = (
        workspace / "tmp" / "local-dataset-build" / DATASET_ID
    ).resolve()
    target = (base / run_id).resolve()
    if not is_below(target, base):
        raise RessetCandidateError("candidate target escaped its dataset root")
    return base, target


def quality_summary(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "PASS_ZERO_LOSS_INSPECTION_ONLY",
        "table": TABLE_NAME,
        "metrics": metrics,
        "checks": {
            "source_zip_hash_bound": "PASS",
            "source_member_hash_crc_bound": "PASS",
            "website_record_boundary_4105": "PASS",
            "nobs_1_through_4105_unique_complete": "PASS",
            "first_13_fields_exact": "PASS",
            "standard_parser_76_anomalies_accounted": "PASS",
            "website_boundary_53_multiline_records_accounted": "PASS",
            "raw_record_and_tail_preserved": "PASS",
            "source_member_byte_roundtrip": "PASS",
        },
        "open_gaps": [
            "Ambiguous TrmtDes/TrmtRea tails are preserved but not assigned.",
            "Field semantics, PIT availability, revisions, rights admission, and cross-source calibration remain open.",
        ],
        "purpose_ceiling": PURPOSE_CEILING,
        "admission_decision": ADMISSION_DECISION,
        "formal_effect": "NONE",
    }


def render_report(
    *,
    source: dict[str, Any],
    metrics: dict[str, Any],
    target: Path,
) -> str:
    return f"""# RESSET STKSPCTRMT zero-loss candidate

- Source ZIP: `{source['filename']}`
- Source SHA-256: `{source['sha256']}`
- Candidate: `{target.as_posix()}`
- Website-bound records: **{metrics['source_record_count']:,}**
- `nobs`: **1..{metrics['nobs_last']}**, unique and complete
- Standard CSV baseline: **{metrics['standard_parser_output_count']:,}** rows, **{metrics['standard_parser_arity_anomaly_count']}** arity anomalies
- Website-bound multiline records: **{metrics['website_boundary_multiline_record_count']}**
- Framed records with ambiguous tails: **{metrics['framed_ambiguous_tail_count']}**
- First 13 fields: exact parse; failures **{metrics['prefix_parse_failures']}**
- Raw source-record and tail preservation: **PASS**

The final two documented fields are populated only for unambiguous 15-column
records.  For every ambiguous record, the complete source record, raw tail,
parser output, line span, and hashes remain in the candidate and repair ledger.

This is `INSPECTION_ONLY / NOT_ADMITTED`.  It is not catalog-registered,
quality-promoted, formally admitted, snapshot-published, or research-ready.
"""


def plan_result(
    *,
    source: dict[str, Any],
    parsed: dict[str, Any],
    target: Path,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": "PLAN",
        "status": "PLAN_VALIDATED_WRITE_FREE",
        "write_performed": False,
        "tool_id": TOOL_ID,
        "dataset_id": DATASET_ID,
        "table": TABLE_NAME,
        "source": source,
        "candidate_path": target.as_posix(),
        "candidate_exists": target.exists(),
        "metrics": parsed["metrics"],
        "planned_outputs": [
            "raw/<original ZIP>",
            f"raw/extracted/{CSV_MEMBER}",
            "curated/candidate.csv",
            "quality/repair-ledger.json",
            "quality/summary.json",
            "provenance.json",
            "REPORT.md",
            "manifest.json",
        ],
        "safety": {
            "purpose_ceiling": PURPOSE_CEILING,
            "admission_decision": ADMISSION_DECISION,
            "formal_effect": "NONE",
            "catalog_registered": False,
            "source_admission": False,
            "quality_promotion": False,
            "research_ready": False,
            "source_zip_modified": False,
        },
    }


def execute_candidate(
    *,
    workspace: Path,
    source_path: Path,
    source: dict[str, Any],
    csv_payload: bytes,
    parsed: dict[str, Any],
    run_id: str,
    base: Path,
    target: Path,
) -> dict[str, Any]:
    if target.exists():
        raise RessetCandidateError(f"candidate already exists: {target}")
    base.mkdir(parents=True, exist_ok=True)
    stage = (base / f".staging-{run_id}-{uuid.uuid4().hex}").resolve()
    if not is_below(stage, base):
        raise RessetCandidateError("staging path escaped candidate root")
    before_catalog = catalog_hash(workspace)
    source_before = {
        "bytes": source_path.stat().st_size,
        "mtime_ns": source_path.stat().st_mtime_ns,
        "sha256": sha256_file(source_path),
    }
    stage.mkdir()
    try:
        raw_dir = stage / "raw"
        extracted_dir = raw_dir / "extracted"
        curated_dir = stage / "curated"
        quality_dir = stage / "quality"
        for directory in (raw_dir, extracted_dir, curated_dir, quality_dir):
            directory.mkdir()

        raw_copy = raw_dir / source_path.name
        shutil.copyfile(source_path, raw_copy)
        if sha256_file(raw_copy) != EXPECTED_ZIP_SHA256:
            raise RessetCandidateError("raw ZIP copy hash mismatch")
        extracted_csv = extracted_dir / CSV_MEMBER
        extracted_csv.write_bytes(csv_payload)
        if sha256_file(extracted_csv) != EXPECTED_CSV_SHA256:
            raise RessetCandidateError("extracted CSV member hash mismatch")

        candidate_csv = curated_dir / "candidate.csv"
        write_candidate_csv(candidate_csv, parsed["candidate_rows"])
        roundtrip = verify_candidate_csv(
            candidate_csv, parsed["candidate_rows"]
        )

        repair_path = quality_dir / "repair-ledger.json"
        write_json(repair_path, parsed["repair_ledger"])
        quality = quality_summary(parsed["metrics"])
        quality["candidate_roundtrip"] = roundtrip
        quality_path = quality_dir / "summary.json"
        write_json(quality_path, quality)

        parser_path = Path(__file__).resolve()
        provenance = {
            "schema_version": 1,
            "provenance_kind": "RESSET_MANUAL_EXPORT_ZERO_LOSS_CANDIDATE",
            "created_at": utc_now(),
            "tool": {
                "path": parser_path.as_posix(),
                "sha256": sha256_file(parser_path),
                "tool_id": TOOL_ID,
            },
            "source": source,
            "source_access": {
                "route": "Tsinghua institutional RESSET manual export",
                "credentials_persisted": False,
                "rights_admission_closed": False,
            },
            "record_contract": {
                "expected_columns": list(EXPECTED_COLUMNS),
                "exact_prefix_columns": list(PREFIX_COLUMNS),
                "record_boundary": RECORD_BOUNDARY.pattern,
                "nobs_expected": "1..4105",
                "ambiguous_tail_policy": "PRESERVE_RAW_DO_NOT_ASSIGN",
            },
            "purpose_ceiling": PURPOSE_CEILING,
            "admission_decision": ADMISSION_DECISION,
            "formal_effect": "NONE",
        }
        provenance_path = stage / "provenance.json"
        write_json(provenance_path, provenance)
        report_path = stage / "REPORT.md"
        report_path.write_text(
            render_report(
                source=source,
                metrics=parsed["metrics"],
                target=target,
            ),
            encoding="utf-8",
            newline="\n",
        )

        artifacts = {
            "raw_zip": artifact(raw_copy, stage),
            "raw_csv_member": artifact(extracted_csv, stage),
            "candidate_csv": artifact(
                candidate_csv, stage, row_count=EXPECTED_RECORD_COUNT
            ),
            "repair_ledger": artifact(repair_path, stage),
            "quality_summary": artifact(quality_path, stage),
            "provenance": artifact(provenance_path, stage),
            "report": artifact(report_path, stage),
        }
        after_catalog = catalog_hash(workspace)
        if after_catalog != before_catalog:
            raise RessetCandidateError("formal catalog changed during execution")
        source_after = {
            "bytes": source_path.stat().st_size,
            "mtime_ns": source_path.stat().st_mtime_ns,
            "sha256": sha256_file(source_path),
        }
        if source_after != source_before:
            raise RessetCandidateError("source ZIP changed during execution")

        content_binding = {
            "tool_id": TOOL_ID,
            "dataset_id": DATASET_ID,
            "run_id": run_id,
            "source_sha256": source["sha256"],
            "source_csv_sha256": EXPECTED_CSV_SHA256,
            "metrics": parsed["metrics"],
            "artifacts": artifacts,
            "purpose_ceiling": PURPOSE_CEILING,
            "admission_decision": ADMISSION_DECISION,
        }
        manifest = {
            "schema_version": 1,
            "manifest_kind": "RESSET_STKSPCTRMT_ZERO_LOSS_CANDIDATE",
            "status": "ZERO_LOSS_CANDIDATE_MATERIALIZED",
            "write_performed": True,
            "created_at": utc_now(),
            "tool_id": TOOL_ID,
            "dataset_id": DATASET_ID,
            "table": TABLE_NAME,
            "run_id": run_id,
            "candidate_path": target.as_posix(),
            "source": source,
            "metrics": parsed["metrics"],
            "candidate_roundtrip": roundtrip,
            "artifacts": artifacts,
            "content_hash_algorithm": (
                "sha256-canonical-json-resset-stkspctrmt-zero-loss-v1"
            ),
            "content_hash": sha256_bytes(
                json_bytes(content_binding, canonical=True)
            ),
            "catalog_guard": {
                "path": (
                    workspace / "data" / "catalog" / "datasets.json"
                ).as_posix(),
                "sha256_before": before_catalog,
                "sha256_after": after_catalog,
                "write_authorized": False,
            },
            "safety": {
                "purpose_ceiling": PURPOSE_CEILING,
                "admission_decision": ADMISSION_DECISION,
                "formal_effect": "NONE",
                "catalog_registered": False,
                "source_admission": False,
                "quality_promotion": False,
                "research_ready": False,
                "source_zip_modified": False,
            },
        }
        write_json(stage / "manifest.json", manifest)
        if catalog_hash(workspace) != before_catalog:
            raise RessetCandidateError("catalog changed before atomic seal")
        if target.exists():
            raise RessetCandidateError("candidate target appeared during build")
        os.replace(stage, target)
        return manifest
    except Exception:
        resolved_tmp = (workspace / "tmp").resolve()
        if stage.exists() and is_below(stage.resolve(), resolved_tmp):
            shutil.rmtree(stage)
        raise


def run(
    *,
    workspace: Path,
    zip_path: Path,
    input_root: Path,
    run_id: str,
    execute: bool,
) -> dict[str, Any]:
    workspace = workspace.resolve(strict=True)
    source_path = validate_source_path(zip_path, input_root)
    source, csv_payload = inspect_archive(source_path)
    parsed = frame_records(csv_payload)
    base, target = validate_output_target(workspace, run_id)
    if not execute:
        return plan_result(source=source, parsed=parsed, target=target)
    return execute_candidate(
        workspace=workspace,
        source_path=source_path,
        source=source,
        csv_payload=csv_payload,
        parsed=parsed,
        run_id=run_id,
        base=base,
        target=target,
    )


def default_downloads() -> Path:
    return Path.home() / "Downloads"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or materialize the bound RESSET STKSPCTRMT zero-loss "
            "inspection candidate."
        )
    )
    parser.add_argument(
        "--zip",
        dest="zip_path",
        type=Path,
        default=default_downloads() / EXPECTED_ZIP_NAME,
        help="reviewed RESSET ZIP (defaults to the exact Downloads filename)",
    )
    parser.add_argument(
        "--input-root",
        type=Path,
        default=default_downloads(),
        help="explicit inbox root (default: current user's Downloads)",
    )
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan",
        action="store_true",
        help="complete write-free plan (default)",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="write the isolated INSPECTION_ONLY/NOT_ADMITTED candidate",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(
            workspace=WORKSPACE_ROOT,
            zip_path=args.zip_path,
            input_root=args.input_root,
            run_id=args.run_id,
            execute=bool(args.execute),
        )
    except (RessetCandidateError, OSError, UnicodeError, zipfile.BadZipFile) as exc:
        print(
            json.dumps(
                {
                    "status": "FAIL_CLOSED",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "write_performed": False,
                    "purpose_ceiling": PURPOSE_CEILING,
                    "admission_decision": ADMISSION_DECISION,
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
