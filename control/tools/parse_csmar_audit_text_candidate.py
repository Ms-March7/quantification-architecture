#!/usr/bin/env python3
"""Repair two malformed CSMAR audit-text CSV exports without losing source bytes.

The source ZIP must already be sealed below ``data/market/quarantine``.  The
default mode performs a complete, write-free inspection.  ``--execute`` writes
only below the workspace ``tmp`` tree:

* an RFC-4180-compatible UTF-8-SIG CSV;
* a one-line-per-source-record repair sidecar;
* an inspection-only manifest and quality summary;
* a deterministic provenance ZIP containing the repaired data and all repair
  evidence; and
* a request that makes the existing local downloaded-dataset builder consume
  that whole provenance ZIP as its raw payload.

The parser never edits the source ZIP, writes the formal catalog, publishes a
snapshot, or promotes data quality.  Every byte after the source header is
assigned to exactly one anchored business record.  Every repair is reversible:
the sidecar records source byte offsets, source hashes, and exact quote
insertions, while execution verifies that removing those insertions recreates
the original record body byte-for-byte.
"""

from __future__ import annotations

import argparse
import codecs
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator, TextIO


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOOL_ID = "parse_csmar_audit_text_candidate.py/csmar-audit-text-v2"
PURPOSE_CEILING = "INSPECTION_ONLY"
PROVENANCE_PACKAGE_FORMAT = "csmar-audit-text-provenance-zip-v1"
DETERMINISTIC_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}$")
DATE_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
QUOTE_RUN = re.compile(r'"+')


class AuditTextParseError(RuntimeError):
    """A controlled fail-closed rejection."""


@dataclass(frozen=True)
class TableSpec:
    name: str
    member_name: str
    dataset_id: str
    table_name: str
    columns: tuple[str, ...]
    column_types: dict[str, str]
    key_columns: tuple[str, ...]
    required_columns: tuple[str, ...]
    anchor: re.Pattern[bytes]
    ambiguous_left_fixed: int | None = None
    ambiguous_right_fixed: int | None = None


AUDIT_COLUMNS = (
    "Stkcd",
    "Stknme",
    "Accper",
    "Annodt",
    "Audittyp",
    "Auditor",
    "Dadtunit",
    "Iadtunit",
    "Dcost",
    "Currency1",
    "Fcost",
    "Currency2",
    "Othercst",
    "Currency3",
    "Tcost",
    "Currency4",
    "Adtremark",
    "Cstremark",
    "DadtunitID",
    "IadtunitID",
)
AUDIT_DETAIL_COLUMNS = (
    "Stkcd",
    "Accper",
    "Stknme",
    "ItemNo",
    "Classification",
    "Description",
    "Method",
    "Location",
)


TABLE_SPECS = {
    "FIN_Audit": TableSpec(
        name="FIN_Audit",
        member_name="FIN_Audit.csv",
        dataset_id="csmar-fin-audit",
        table_name="fin_audit_candidate",
        columns=AUDIT_COLUMNS,
        column_types={
            column: (
                "DATE"
                if column in {"Accper", "Annodt"}
                else "DECIMAL(20,2)"
                if column in {"Dcost", "Fcost", "Othercst", "Tcost"}
                else "VARCHAR"
            )
            for column in AUDIT_COLUMNS
        },
        key_columns=("Stkcd", "Accper"),
        required_columns=("Stkcd", "Accper"),
        anchor=re.compile(
            rb'(?m)^"\d{6}","[^"\r\n]*","\d{4}-\d{2}-\d{2}",'
        ),
        # The final four fields are Adtremark, Cstremark, DadtunitID,
        # IadtunitID.  If narrative text itself contains the literal CSV
        # separator `","`, keep the right-most narrative boundary and merge
        # surplus pieces back into Adtremark.  The sidecar flags this as
        # semantic-review-required rather than silently pretending certainty.
        ambiguous_left_fixed=16,
        ambiguous_right_fixed=3,
    ),
    "FIN_AuditDetail": TableSpec(
        name="FIN_AuditDetail",
        member_name="FIN_AuditDetail.csv",
        dataset_id="csmar-fin-auditdetail",
        table_name="fin_auditdetail_candidate",
        columns=AUDIT_DETAIL_COLUMNS,
        column_types={
            column: (
                "DATE"
                if column == "Accper"
                else "INTEGER"
                if column == "ItemNo"
                else "VARCHAR"
            )
            for column in AUDIT_DETAIL_COLUMNS
        },
        key_columns=("Stkcd", "Accper", "ItemNo"),
        required_columns=("Stkcd", "Accper", "ItemNo"),
        anchor=re.compile(rb'(?m)^"\d{6}","\d{4}-\d{2}-\d{2}",'),
    ),
}


@dataclass(frozen=True)
class FramedRecord:
    ordinal: int
    start_byte: int
    end_byte: int
    region: bytes
    body: bytes
    separator: bytes


@dataclass
class OutputSinks:
    csv_file: TextIO
    sidecar_file: TextIO


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


def json_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def write_deterministic_zip(
    path: Path,
    members: list[tuple[str, Path | bytes]],
) -> list[dict[str, Any]]:
    """Write a byte-deterministic ZIP and return its verified member inventory."""

    seen: set[str] = set()
    inventory: list[dict[str, Any]] = []
    with zipfile.ZipFile(
        path,
        "x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for member_name, source in sorted(members, key=lambda item: item[0]):
            if (
                not member_name
                or member_name.startswith("/")
                or "\\" in member_name
                or ".." in Path(member_name).parts
                or member_name in seen
            ):
                raise AuditTextParseError(
                    f"unsafe or duplicate provenance ZIP member: {member_name!r}"
                )
            seen.add(member_name)
            info = zipfile.ZipInfo(member_name, date_time=DETERMINISTIC_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.flag_bits |= 0x800
            digest = hashlib.sha256()
            size = 0
            with archive.open(info, "w", force_zip64=True) as target:
                if isinstance(source, bytes):
                    digest.update(source)
                    size = len(source)
                    target.write(source)
                else:
                    with source.open("rb") as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(block)
                            size += len(block)
                            target.write(block)
            inventory.append(
                {
                    "member": member_name,
                    "bytes": size,
                    "sha256": digest.hexdigest(),
                }
            )

    with zipfile.ZipFile(path) as archive:
        corrupt_member = archive.testzip()
        if corrupt_member is not None:
            raise AuditTextParseError(
                f"provenance ZIP CRC failure for member {corrupt_member!r}"
            )
        infos = {info.filename: info for info in archive.infolist()}
        if set(infos) != {entry["member"] for entry in inventory}:
            raise AuditTextParseError("provenance ZIP member inventory changed")
        for entry in inventory:
            info = infos[entry["member"]]
            if info.file_size != entry["bytes"]:
                raise AuditTextParseError(
                    f"provenance ZIP member size mismatch: {entry['member']}"
                )
            digest = hashlib.sha256()
            with archive.open(info) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != entry["sha256"]:
                raise AuditTextParseError(
                    f"provenance ZIP member hash mismatch: {entry['member']}"
                )
    return inventory


def require_safe_component(value: str, label: str) -> str:
    if not SAFE_COMPONENT.fullmatch(value):
        raise AuditTextParseError(f"unsafe {label}: {value!r}")
    return value


def superseded_run_id(run_id: str) -> str | None:
    if run_id.endswith("-v2"):
        return run_id[:-3] + "-v1"
    return None


def is_below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def require_input_location(workspace_root: Path, zip_path: Path) -> Path:
    workspace = workspace_root.resolve()
    quarantine = (workspace / "data" / "market" / "quarantine").resolve()
    resolved = zip_path.resolve(strict=True)
    if not resolved.is_file() or resolved.suffix.casefold() != ".zip":
        raise AuditTextParseError(f"source is not a ZIP file: {resolved}")
    if zip_path.is_symlink() or not is_below(resolved, quarantine):
        raise AuditTextParseError(
            "source ZIP must be a non-symlink file below data/market/quarantine"
        )
    return resolved


def require_output_location(
    workspace_root: Path,
    output_root: Path | None,
    dataset_id: str,
    run_id: str,
) -> tuple[Path, Path]:
    workspace = workspace_root.resolve()
    tmp_root = (workspace / "tmp").resolve()
    base = (
        (tmp_root / "csmar-audit-text-candidate")
        if output_root is None
        else output_root.resolve()
    )
    if not is_below(base, tmp_root):
        raise AuditTextParseError("output root must stay below the workspace tmp tree")
    target = base / dataset_id / run_id
    if not is_below(target.resolve(), tmp_root):
        raise AuditTextParseError("candidate target escaped the workspace tmp tree")
    return base, target


def split_final_record_separator(region: bytes) -> tuple[bytes, bytes]:
    body = region
    separator = bytearray()
    while body and not body.endswith(b'"'):
        if body.endswith(b"\r\n"):
            separator[:0] = b"\r\n"
            body = body[:-2]
        elif body.endswith(b"\n"):
            separator[:0] = b"\n"
            body = body[:-1]
        elif body.endswith(b"\r"):
            separator[:0] = b"\r"
            body = body[:-1]
        else:
            break
    if not body.startswith(b'"') or not body.endswith(b'"'):
        raise AuditTextParseError(
            "anchored source record does not have a quoted outer boundary"
        )
    return body, bytes(separator)


def frame_records(raw: bytes, spec: TableSpec) -> tuple[list[str], list[FramedRecord]]:
    if not raw.startswith(codecs.BOM_UTF8):
        raise AuditTextParseError("source CSV must be UTF-8-SIG")
    matches = list(spec.anchor.finditer(raw))
    if not matches:
        raise AuditTextParseError(f"no business record anchors found for {spec.name}")

    header_bytes = raw[: matches[0].start()]
    try:
        header_text = header_bytes.decode("utf-8-sig")
        parsed_header = list(
            csv.reader(io.StringIO(header_text, newline=""), strict=True)
        )
    except (UnicodeError, csv.Error) as exc:
        raise AuditTextParseError(f"cannot parse source header: {exc}") from exc
    if len(parsed_header) != 1:
        raise AuditTextParseError(
            f"source header region contains {len(parsed_header)} logical rows"
        )
    header = parsed_header[0]
    if header != list(spec.columns):
        raise AuditTextParseError(
            f"source header mismatch for {spec.name}: {header!r}"
        )

    records: list[FramedRecord] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        region = raw[match.start() : end]
        body, separator = split_final_record_separator(region)
        records.append(
            FramedRecord(
                ordinal=index + 1,
                start_byte=match.start(),
                end_byte=end,
                region=region,
                body=body,
                separator=separator,
            )
        )
    if records[0].start_byte != len(header_bytes):
        raise AuditTextParseError("unaccounted bytes exist between header and first record")
    if records[-1].end_byte != len(raw):
        raise AuditTextParseError("unaccounted bytes exist after the final record")
    return header, records


def decode_quote_runs(
    raw_content: str,
    *,
    field_index: int,
    column: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Decode CSV quote pairs and explicitly repair each odd quote run."""

    logical_parts: list[str] = []
    actions: list[dict[str, Any]] = []
    raw_cursor = 0
    canonical_cursor = 0
    for match in QUOTE_RUN.finditer(raw_content):
        prefix = raw_content[raw_cursor : match.start()]
        logical_parts.append(prefix)
        canonical_cursor += len(prefix)
        run_length = match.end() - match.start()
        logical_quote_count = (run_length + 1) // 2
        logical_parts.append('"' * logical_quote_count)
        canonical_run_length = logical_quote_count * 2
        if run_length % 2:
            actions.append(
                {
                    "action": "INSERT_QUOTE_TO_ESCAPE_UNESCAPED_QUOTE",
                    "field_index": field_index,
                    "column": column,
                    "raw_field_char_offset": match.start(),
                    "canonical_field_char_offset": canonical_cursor + run_length,
                    "raw_run_length": run_length,
                    "canonical_run_length": canonical_run_length,
                    "inserted_text": '"',
                }
            )
        canonical_cursor += canonical_run_length
        raw_cursor = match.end()
    logical_parts.append(raw_content[raw_cursor:])
    return "".join(logical_parts), actions


def canonical_csv_body(values: list[str]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(
        buffer,
        delimiter=",",
        quotechar='"',
        doublequote=True,
        quoting=csv.QUOTE_ALL,
        lineterminator="\n",
    )
    writer.writerow(values)
    result = buffer.getvalue()
    if not result.endswith("\n"):
        raise AuditTextParseError("canonical CSV writer omitted record terminator")
    return result[:-1]


def reverse_repaired_body(
    values: list[str],
    actions: list[dict[str, Any]],
) -> str:
    """Reverse quote insertions and reproduce the source record body."""

    raw_contents = [value.replace('"', '""') for value in values]
    quote_actions = [
        action
        for action in actions
        if action["action"] == "INSERT_QUOTE_TO_ESCAPE_UNESCAPED_QUOTE"
    ]
    by_field: dict[int, list[dict[str, Any]]] = {}
    for action in quote_actions:
        by_field.setdefault(int(action["field_index"]), []).append(action)
    for field_index, field_actions in by_field.items():
        content = raw_contents[field_index]
        for action in sorted(
            field_actions,
            key=lambda item: int(item["canonical_field_char_offset"]),
            reverse=True,
        ):
            offset = int(action["canonical_field_char_offset"])
            if offset >= len(content) or content[offset] != '"':
                raise AuditTextParseError(
                    "repair action cannot be reversed at its declared quote offset"
                )
            content = content[:offset] + content[offset + 1 :]
        raw_contents[field_index] = content
    return '"' + '","'.join(raw_contents) + '"'


def parse_one_record(
    framed: FramedRecord,
    spec: TableSpec,
) -> tuple[list[str], dict[str, Any], bool]:
    try:
        body_text = framed.body.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise AuditTextParseError(
            f"record {framed.ordinal} is not strict UTF-8: {exc}"
        ) from exc

    strict_valid = False
    try:
        strict_rows = list(
            csv.reader(io.StringIO(body_text, newline=""), strict=True)
        )
        strict_valid = (
            len(strict_rows) == 1
            and len(strict_rows[0]) == len(spec.columns)
        )
    except csv.Error:
        strict_valid = False

    if not body_text.startswith('"') or not body_text.endswith('"'):
        raise AuditTextParseError(
            f"record {framed.ordinal} lacks quoted field boundaries"
        )
    raw_parts = body_text[1:-1].split('","')
    original_structural_width = len(raw_parts)
    actions: list[dict[str, Any]] = []
    semantic_review_required = False

    if len(raw_parts) != len(spec.columns):
        if (
            spec.ambiguous_left_fixed is None
            or spec.ambiguous_right_fixed is None
            or len(raw_parts) <= len(spec.columns)
        ):
            raise AuditTextParseError(
                f"record {framed.ordinal} has structural width "
                f"{len(raw_parts)}; expected {len(spec.columns)}"
            )
        left = spec.ambiguous_left_fixed
        right = spec.ambiguous_right_fixed
        middle_end = len(raw_parts) - right
        merged = '","'.join(raw_parts[left:middle_end])
        raw_parts = raw_parts[:left] + [merged] + raw_parts[middle_end:]
        if len(raw_parts) != len(spec.columns):
            raise AuditTextParseError(
                f"record {framed.ordinal} delimiter repair did not restore "
                f"{len(spec.columns)} fields"
            )
        semantic_review_required = True
        actions.append(
            {
                "action": "MERGE_AMBIGUOUS_LITERAL_FIELD_SEPARATOR",
                "field_index": left,
                "column": spec.columns[left],
                "original_structural_width": original_structural_width,
                "repaired_structural_width": len(raw_parts),
                "resolution_rule": "FIXED_LEFT_FIELDS_AND_RIGHTMOST_NARRATIVE_BOUNDARY",
                "semantic_review_required": True,
            }
        )

    values: list[str] = []
    for field_index, (column, raw_content) in enumerate(
        zip(spec.columns, raw_parts, strict=True)
    ):
        value, quote_actions = decode_quote_runs(
            raw_content,
            field_index=field_index,
            column=column,
        )
        values.append(value)
        actions.extend(quote_actions)

    quote_repairs = sum(
        action["action"] == "INSERT_QUOTE_TO_ESCAPE_UNESCAPED_QUOTE"
        for action in actions
    )
    if strict_valid and actions:
        raise AuditTextParseError(
            f"record {framed.ordinal} was strict-valid but repair actions were inferred"
        )
    if not strict_valid and not actions:
        raise AuditTextParseError(
            f"record {framed.ordinal} was strict-invalid with no explicit repair"
        )

    for column in spec.required_columns:
        value = values[spec.columns.index(column)]
        if value == "":
            raise AuditTextParseError(
                f"record {framed.ordinal} has blank required column {column}"
            )
    accounting_date = values[spec.columns.index("Accper")]
    if not DATE_VALUE.fullmatch(accounting_date):
        raise AuditTextParseError(
            f"record {framed.ordinal} has invalid Accper {accounting_date!r}"
        )
    if spec.name == "FIN_AuditDetail":
        item_number = values[spec.columns.index("ItemNo")]
        if not item_number.isdigit():
            raise AuditTextParseError(
                f"record {framed.ordinal} has invalid ItemNo {item_number!r}"
            )

    canonical_body = canonical_csv_body(values)
    try:
        canonical_rows = list(
            csv.reader(io.StringIO(canonical_body, newline=""), strict=True)
        )
    except csv.Error as exc:
        raise AuditTextParseError(
            f"record {framed.ordinal} canonical CSV is invalid: {exc}"
        ) from exc
    if canonical_rows != [values]:
        raise AuditTextParseError(
            f"record {framed.ordinal} canonical round trip changed values"
        )
    reconstructed = reverse_repaired_body(values, actions)
    if reconstructed != body_text:
        raise AuditTextParseError(
            f"record {framed.ordinal} repair is not byte-reversible"
        )

    if not actions:
        parse_status = "CLEAN_STRICT"
    elif semantic_review_required:
        parse_status = "REPAIRED_QUOTES_AND_AMBIGUOUS_DELIMITER__REVIEW_REQUIRED"
    else:
        parse_status = "REPAIRED_UNESCAPED_QUOTES"

    key = {
        column: values[spec.columns.index(column)]
        for column in spec.key_columns
    }
    sidecar = {
        "record_ordinal": framed.ordinal,
        "source_start_byte": framed.start_byte,
        "source_end_byte": framed.end_byte,
        "source_record_sha256": sha256_bytes(framed.region),
        "source_body_sha256": sha256_bytes(framed.body),
        "source_separator_hex": framed.separator.hex(),
        "canonical_body_sha256": sha256_bytes(canonical_body.encode("utf-8")),
        "parse_status": parse_status,
        "strict_source_parse": strict_valid,
        "source_structural_width": original_structural_width,
        "output_field_count": len(values),
        "empty_source_field_count": sum(value == "" for value in values),
        "quote_repair_count": quote_repairs,
        "semantic_review_required": semantic_review_required,
        "key": key,
        "actions": actions,
    }
    return values, sidecar, strict_valid


def newline_counts(raw: bytes) -> dict[str, int]:
    crlf = raw.count(b"\r\n")
    cr = raw.count(b"\r")
    lf = raw.count(b"\n")
    lone_cr = cr - crlf
    lone_lf = lf - crlf
    terminators = crlf + lone_cr + lone_lf
    physical_lines = terminators + (
        0 if raw.endswith((b"\r", b"\n")) else 1
    )
    return {
        "crlf": crlf,
        "lone_cr": lone_cr,
        "lone_lf": lone_lf,
        "physical_lines_including_header": physical_lines,
        "physical_data_lines_after_header": physical_lines - 1,
    }


def parse_archive(
    *,
    zip_path: Path,
    spec: TableSpec,
    sinks: OutputSinks | None,
) -> dict[str, Any]:
    package_sha256 = sha256_file(zip_path)
    package_bytes = zip_path.stat().st_size
    with zipfile.ZipFile(zip_path) as archive:
        corrupt_member = archive.testzip()
        if corrupt_member is not None:
            raise AuditTextParseError(
                f"ZIP CRC failure for archive member {corrupt_member!r}"
            )
        try:
            info = archive.getinfo(spec.member_name)
        except KeyError as exc:
            raise AuditTextParseError(
                f"ZIP does not contain {spec.member_name}"
            ) from exc
        if info.flag_bits & 0x1:
            raise AuditTextParseError("encrypted CSV members are not supported")
        raw = archive.read(info)

    header, records = frame_records(raw, spec)
    metrics: dict[str, Any] = {
        "anchored_record_count": len(records),
        "accounted_record_count": 0,
        "output_record_count": 0,
        "strict_valid_record_count": 0,
        "repaired_record_count": 0,
        "semantic_review_record_count": 0,
        "unescaped_quote_character_count": 0,
        "empty_source_field_count": 0,
        "duplicate_key_count": 0,
        "date_min": None,
        "date_max": None,
    }
    first_repaired_ordinals: list[int] = []
    semantic_review_records: list[dict[str, Any]] = []
    keys: set[tuple[str, ...]] = set()
    dates: list[str] = []

    if sinks is not None:
        sinks.csv_file.write(",".join(header) + "\n")

    for framed in records:
        values, sidecar, strict_valid = parse_one_record(framed, spec)
        metrics["accounted_record_count"] += 1
        metrics["strict_valid_record_count"] += int(strict_valid)
        repaired = sidecar["parse_status"] != "CLEAN_STRICT"
        metrics["repaired_record_count"] += int(repaired)
        metrics["semantic_review_record_count"] += int(
            sidecar["semantic_review_required"]
        )
        metrics["unescaped_quote_character_count"] += int(
            sidecar["quote_repair_count"]
        )
        metrics["empty_source_field_count"] += int(
            sidecar["empty_source_field_count"]
        )
        if repaired and len(first_repaired_ordinals) < 20:
            first_repaired_ordinals.append(framed.ordinal)
        if sidecar["semantic_review_required"]:
            semantic_review_records.append(
                {
                    "record_ordinal": sidecar["record_ordinal"],
                    "key": sidecar["key"],
                    "parse_status": sidecar["parse_status"],
                    "source_start_byte": sidecar["source_start_byte"],
                    "source_end_byte": sidecar["source_end_byte"],
                    "source_record_sha256": sidecar["source_record_sha256"],
                    "source_body_sha256": sidecar["source_body_sha256"],
                    "canonical_body_sha256": sidecar["canonical_body_sha256"],
                    "source_structural_width": sidecar[
                        "source_structural_width"
                    ],
                    "output_field_count": sidecar["output_field_count"],
                    "quote_repair_count": sidecar["quote_repair_count"],
                    "actions": sidecar["actions"],
                }
            )

        key = tuple(values[spec.columns.index(column)] for column in spec.key_columns)
        if key in keys:
            metrics["duplicate_key_count"] += 1
        keys.add(key)
        dates.append(values[spec.columns.index("Accper")])

        if sinks is not None:
            sinks.csv_file.write(canonical_csv_body(values) + "\n")
            sinks.sidecar_file.write(
                json.dumps(
                    sidecar,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            )
        metrics["output_record_count"] += 1

    if not (
        metrics["anchored_record_count"]
        == metrics["accounted_record_count"]
        == metrics["output_record_count"]
    ):
        raise AuditTextParseError("record accounting invariant failed")
    if metrics["duplicate_key_count"]:
        raise AuditTextParseError(
            f"found {metrics['duplicate_key_count']} duplicate business keys"
        )
    metrics["date_min"] = min(dates)
    metrics["date_max"] = max(dates)
    metrics["first_repaired_record_ordinals"] = first_repaired_ordinals
    metrics["newline_counts"] = newline_counts(raw)

    return {
        "tool_id": TOOL_ID,
        "table": spec.name,
        "dataset_id": spec.dataset_id,
        "source": {
            "zip_path": zip_path.as_posix(),
            "zip_filename": zip_path.name,
            "zip_bytes": package_bytes,
            "zip_sha256": package_sha256,
            "zip_crc_read_status": "PASS",
            "csv_member": spec.member_name,
            "csv_member_bytes": info.file_size,
            "csv_member_compressed_bytes": info.compress_size,
            "csv_member_crc32": f"{info.CRC:08x}",
            "csv_member_sha256": sha256_bytes(raw),
            "encoding": "utf-8-sig",
            "header_columns": len(header),
        },
        "metrics": metrics,
        "semantic_review": {
            "required": bool(semantic_review_records),
            "record_count": len(semantic_review_records),
            "records": semantic_review_records,
        },
        "safety": {
            "purpose_ceiling": PURPOSE_CEILING,
            "admission_decision": "NOT_ADMITTED",
            "catalog_registered": False,
            "quality_promotion": False,
            "research_ready": False,
            "unknown_value_policy": (
                "Source empty fields remain empty CSV cells and MUST map to SQL NULL "
                "downstream; no value is filled, inferred, or replaced with zero."
            ),
            "source_zip_modified": False,
        },
    }


def quality_summary(result: dict[str, Any]) -> dict[str, Any]:
    semantic_review = result["semantic_review"]
    return {
        "schema_version": 2,
        "mechanical_status": (
            "PASS_WITH_REPAIRS__SEMANTIC_REVIEW_REQUIRED"
            if semantic_review["required"]
            else "PASS_WITH_REVERSIBLE_QUOTE_REPAIRS"
        ),
        "anchored_record_count": result["metrics"]["anchored_record_count"],
        "accounted_record_count": result["metrics"]["accounted_record_count"],
        "output_record_count": result["metrics"]["output_record_count"],
        "repaired_record_count": result["metrics"]["repaired_record_count"],
        "unescaped_quote_character_count": result["metrics"][
            "unescaped_quote_character_count"
        ],
        "duplicate_key_count": result["metrics"]["duplicate_key_count"],
        "semantic_review": semantic_review,
        "unknown_value_policy": result["safety"]["unknown_value_policy"],
        "admission_decision": "NOT_ADMITTED",
        "purpose_ceiling": PURPOSE_CEILING,
        "research_ready": False,
    }


def parser_manifest(
    *,
    result: dict[str, Any],
    run_id: str,
    candidate_path: Path,
    parser_script_path: Path,
    parser_script_sha256: str,
    catalog_sha256: str | None,
    repaired_csv: dict[str, Any],
    repair_sidecar: dict[str, Any],
    quality_artifact: dict[str, Any],
    catalog_path: Path,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "manifest_kind": "CSMAR_AUDIT_TEXT_PARSER_MANIFEST",
        "package_format": PROVENANCE_PACKAGE_FORMAT,
        "tool_id": TOOL_ID,
        "parser_script": {
            "path": parser_script_path.as_posix(),
            "sha256": parser_script_sha256,
        },
        "table": result["table"],
        "dataset_id": result["dataset_id"],
        "run_id": run_id,
        "supersedes_run_id": superseded_run_id(run_id),
        "candidate_path": candidate_path.as_posix(),
        "source": result["source"],
        "metrics": result["metrics"],
        "semantic_review": result["semantic_review"],
        "artifacts": {
            "repaired_csv": repaired_csv,
            "repair_sidecar": repair_sidecar,
            "quality_summary": quality_artifact,
        },
        "catalog_guard": {
            "path": catalog_path.as_posix(),
            "sha256": catalog_sha256,
            "write_authorized": False,
        },
        "safety": result["safety"],
        "self_reference_boundary": (
            "This parser manifest intentionally excludes the provenance ZIP hash. "
            "The external run manifest records the completed package hash."
        ),
    }


def upstream_provenance_receipt(
    *,
    result: dict[str, Any],
    run_id: str,
    candidate_path: Path,
    parser_script_path: Path,
    parser_script_sha256: str,
    parser_manifest_artifact: dict[str, Any],
    repaired_csv: dict[str, Any],
    repair_sidecar: dict[str, Any],
    quality_artifact: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "receipt_kind": "CSMAR_AUDIT_TEXT_UPSTREAM_PROVENANCE",
        "package_format": PROVENANCE_PACKAGE_FORMAT,
        "dataset_id": result["dataset_id"],
        "table": result["table"],
        "run_id": run_id,
        "supersedes_run_id": superseded_run_id(run_id),
        "candidate_path": candidate_path.as_posix(),
        "source": {
            "zip_path": result["source"]["zip_path"],
            "zip_bytes": result["source"]["zip_bytes"],
            "zip_sha256": result["source"]["zip_sha256"],
            "csv_member": result["source"]["csv_member"],
            "csv_member_bytes": result["source"]["csv_member_bytes"],
            "csv_member_crc32": result["source"]["csv_member_crc32"],
            "csv_member_sha256": result["source"]["csv_member_sha256"],
        },
        "parser": {
            "tool_id": TOOL_ID,
            "script_path": parser_script_path.as_posix(),
            "script_sha256": parser_script_sha256,
        },
        "artifacts": {
            "parser_manifest": parser_manifest_artifact,
            "repair_sidecar": repair_sidecar,
            "repaired_csv": repaired_csv,
            "quality_summary": quality_artifact,
        },
        "semantic_review": result["semantic_review"],
        "binding_contract": {
            "source_to_sidecar": (
                "Each sidecar row binds source byte offsets and source record/body "
                "SHA-256 to the canonical repaired row SHA-256."
            ),
            "sidecar_to_repaired_csv": (
                "The parser manifest and this receipt bind the complete sidecar "
                "and repaired CSV hashes and row count."
            ),
            "package_to_downstream": (
                "The completed provenance ZIP hash is recorded outside this "
                "package in manifest.json and downstream-build.request.json."
            ),
            "admission_decision": "NOT_ADMITTED",
            "purpose_ceiling": PURPOSE_CEILING,
        },
    }


def downstream_request(
    *,
    spec: TableSpec,
    run_id: str,
    source: dict[str, Any],
    row_count: int,
    provenance_package: dict[str, Any],
    parser_manifest_artifact: dict[str, Any],
    repair_sidecar: dict[str, Any],
    semantic_review: dict[str, Any],
) -> dict[str, Any]:
    semantic_ordinals = [
        str(record["record_ordinal"]) for record in semantic_review["records"]
    ]
    semantic_note = (
        ",".join(semantic_ordinals) if semantic_ordinals else "none"
    )
    return {
        "schema_version": 1,
        "mode": "local_download_candidate",
        "dataset_id": spec.dataset_id,
        "run_id": f"{run_id}-parquet-v1",
        "source_id": "CSMAR_MANUAL_EXPORT_REPAIRED_TEXT_PROVENANCE_ZIP_V2",
        "dataset_schema_version": (
            f"csmar-{spec.name.lower()}-observed-{len(spec.columns)}col-"
            "repaired-text-candidate-v1"
        ),
        "authorized_local_research_download": True,
        "source_access_note": (
            "Inspection-only downstream request for a deterministic provenance "
            f"ZIP containing repaired {spec.name} data and repair evidence. "
            f"Provenance ZIP SHA-256={provenance_package['sha256']}; parser "
            f"manifest SHA-256={parser_manifest_artifact['sha256']}; repair "
            f"sidecar SHA-256={repair_sidecar['sha256']} with "
            f"{repair_sidecar['row_count']} rows; raw ZIP "
            f"SHA-256={source['zip_sha256']}; raw member "
            f"SHA-256={source['csv_member_sha256']}; semantic-review record "
            f"count={semantic_review['record_count']}, ordinals={semantic_note}. "
            "The generic builder must retain this complete provenance ZIP as its "
            "raw payload. Empty source cells represent unknown values and must "
            "remain NULL. This "
            "does not assert source admission, PIT closure, quality promotion, "
            "redistribution rights, catalog registration, or research readiness."
        ),
        "include_patterns": [provenance_package["relative_path"]],
        "zip_member_patterns": [f"repaired/{spec.member_name}"],
        "table": {
            "table_name": spec.table_name,
            "expected_columns": list(spec.columns),
            "column_types": spec.column_types,
            "date_column": "Accper",
            "key_columns": list(spec.key_columns),
            "required_columns": list(spec.required_columns),
            "delimiter": ",",
            "encoding": "utf-8-sig",
            "allow_extra_columns": False,
            "partition_mode": "year_month",
        },
        "limits": {
            "max_packages": 1,
            "max_csv_sources": 1,
            "max_archive_members": len(provenance_package["members"]),
            "max_total_input_bytes": max(
                1_000_000, provenance_package["bytes"] * 2
            ),
            "max_total_unpacked_bytes": max(
                2_000_000,
                sum(member["bytes"] for member in provenance_package["members"])
                * 2,
            ),
            "max_compression_ratio": 1000,
            "max_rows": max(1000, row_count + 1),
        },
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_bytes(json_bytes(value))


def catalog_hash(workspace_root: Path) -> str | None:
    path = workspace_root / "data" / "catalog" / "datasets.json"
    return sha256_file(path) if path.is_file() else None


def run(
    *,
    workspace_root: Path,
    zip_path: Path,
    table: str,
    run_id: str,
    output_root: Path | None,
    execute: bool,
) -> dict[str, Any]:
    workspace = workspace_root.resolve()
    if table not in TABLE_SPECS:
        raise AuditTextParseError(f"unsupported table: {table}")
    spec = TABLE_SPECS[table]
    require_safe_component(run_id, "run_id")
    require_safe_component(spec.dataset_id, "dataset_id")
    source_zip = require_input_location(workspace, zip_path)
    _, target = require_output_location(
        workspace,
        output_root,
        spec.dataset_id,
        run_id,
    )
    before_catalog = catalog_hash(workspace)

    if not execute:
        result = parse_archive(zip_path=source_zip, spec=spec, sinks=None)
        result.update(
            {
                "status": "INSPECTION_COMPLETE",
                "write_performed": False,
                "candidate_path": target.as_posix(),
                "catalog_sha256_before": before_catalog,
                "catalog_sha256_after": catalog_hash(workspace),
            }
        )
        if result["catalog_sha256_after"] != before_catalog:
            raise AuditTextParseError("catalog changed during read-only inspection")
        return result

    if target.exists():
        raise AuditTextParseError(f"candidate target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{run_id}.partial-{uuid.uuid4().hex}"
    if stage.exists():
        raise AuditTextParseError(f"staging target already exists: {stage}")
    stage.mkdir()
    try:
        repaired_dir = stage / "repaired"
        repair_dir = stage / "repair"
        quality_dir = stage / "quality"
        repaired_dir.mkdir()
        repair_dir.mkdir()
        quality_dir.mkdir()
        repaired_csv = repaired_dir / spec.member_name
        sidecar = repair_dir / "records.jsonl"
        with repaired_csv.open(
            "x", encoding="utf-8-sig", errors="strict", newline=""
        ) as csv_file, sidecar.open(
            "x", encoding="utf-8", errors="strict", newline="\n"
        ) as sidecar_file:
            result = parse_archive(
                zip_path=source_zip,
                spec=spec,
                sinks=OutputSinks(csv_file=csv_file, sidecar_file=sidecar_file),
            )

        repaired_artifact = {
            "relative_path": f"repaired/{spec.member_name}",
            "package_member": f"repaired/{spec.member_name}",
            "bytes": repaired_csv.stat().st_size,
            "sha256": sha256_file(repaired_csv),
        }
        sidecar_artifact = {
            "relative_path": "repair/records.jsonl",
            "package_member": "repair/records.jsonl",
            "bytes": sidecar.stat().st_size,
            "sha256": sha256_file(sidecar),
            "row_count": result["metrics"]["output_record_count"],
        }

        quality = quality_summary(result)
        quality_path = stage / "quality" / "summary.json"
        write_json(quality_path, quality)
        quality_artifact = {
            "relative_path": "quality/summary.json",
            "package_member": "quality/summary.json",
            "bytes": quality_path.stat().st_size,
            "sha256": sha256_file(quality_path),
        }

        parser_script_path = Path(__file__).resolve()
        parser_script_sha256 = sha256_file(parser_script_path)
        internal_manifest = parser_manifest(
            result=result,
            run_id=run_id,
            candidate_path=target,
            parser_script_path=parser_script_path,
            parser_script_sha256=parser_script_sha256,
            catalog_sha256=before_catalog,
            repaired_csv=repaired_artifact,
            repair_sidecar=sidecar_artifact,
            quality_artifact=quality_artifact,
            catalog_path=workspace / "data" / "catalog" / "datasets.json",
        )
        parser_manifest_path = stage / "parser-manifest.json"
        write_json(parser_manifest_path, internal_manifest)
        parser_manifest_artifact = {
            "relative_path": "parser-manifest.json",
            "package_member": "parser-manifest.json",
            "bytes": parser_manifest_path.stat().st_size,
            "sha256": sha256_file(parser_manifest_path),
        }

        receipt = upstream_provenance_receipt(
            result=result,
            run_id=run_id,
            candidate_path=target,
            parser_script_path=parser_script_path,
            parser_script_sha256=parser_script_sha256,
            parser_manifest_artifact=parser_manifest_artifact,
            repaired_csv=repaired_artifact,
            repair_sidecar=sidecar_artifact,
            quality_artifact=quality_artifact,
        )
        receipt_path = stage / "upstream_provenance_receipt.json"
        write_json(receipt_path, receipt)
        receipt_artifact = {
            "relative_path": "upstream_provenance_receipt.json",
            "package_member": "upstream_provenance_receipt.json",
            "bytes": receipt_path.stat().st_size,
            "sha256": sha256_file(receipt_path),
        }

        provenance_dir = stage / "provenance"
        provenance_dir.mkdir()
        package_name = f"{spec.dataset_id}-{run_id}-provenance.zip"
        package_path = provenance_dir / package_name
        package_members = write_deterministic_zip(
            package_path,
            [
                (repaired_artifact["package_member"], repaired_csv),
                (sidecar_artifact["package_member"], sidecar),
                (parser_manifest_artifact["package_member"], parser_manifest_path),
                (quality_artifact["package_member"], quality_path),
                (receipt_artifact["package_member"], receipt_path),
            ],
        )
        provenance_package = {
            "relative_path": f"provenance/{package_name}",
            "bytes": package_path.stat().st_size,
            "sha256": sha256_file(package_path),
            "format": PROVENANCE_PACKAGE_FORMAT,
            "members": package_members,
        }

        result["outputs"] = {
            "repaired_csv": repaired_artifact,
            "repair_sidecar": sidecar_artifact,
            "parser_manifest": parser_manifest_artifact,
            "quality_summary": quality_artifact,
            "upstream_provenance_receipt": receipt_artifact,
            "provenance_zip": provenance_package,
        }
        result.update(
            {
                "status": "REPAIRED_PROVENANCE_CANDIDATE_MATERIALIZED",
                "write_performed": True,
                "created_at": utc_now(),
                "run_id": run_id,
                "supersedes_run_id": superseded_run_id(run_id),
                "candidate_path": target.as_posix(),
                "catalog_sha256_before": before_catalog,
                "catalog_sha256_after": catalog_hash(workspace),
            }
        )
        if result["catalog_sha256_after"] != before_catalog:
            raise AuditTextParseError("catalog changed during candidate execution")

        request = downstream_request(
            spec=spec,
            run_id=run_id,
            source=result["source"],
            row_count=result["metrics"]["output_record_count"],
            provenance_package=provenance_package,
            parser_manifest_artifact=parser_manifest_artifact,
            repair_sidecar=sidecar_artifact,
            semantic_review=result["semantic_review"],
        )
        write_json(stage / "manifest.json", result)
        write_json(stage / "downstream-build.request.json", request)
        if catalog_hash(workspace) != before_catalog:
            raise AuditTextParseError("catalog changed before candidate seal")
        os.replace(stage, target)
        return result
    except Exception:
        if stage.exists() and is_below(stage.resolve(), (workspace / "tmp").resolve()):
            shutil.rmtree(stage)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or repair malformed CSMAR audit text CSV exports."
    )
    parser.add_argument("--zip", required=True, type=Path, dest="zip_path")
    parser.add_argument("--table", required=True, choices=sorted(TABLE_SPECS))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--inspect",
        action="store_true",
        help="perform a complete write-free inspection (default)",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="materialize an inspection-only repaired candidate below tmp",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(
            workspace_root=WORKSPACE_ROOT,
            zip_path=args.zip_path,
            table=args.table,
            run_id=args.run_id,
            output_root=args.output_root,
            execute=bool(args.execute),
        )
    except (AuditTextParseError, OSError, UnicodeError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "REJECTED", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
