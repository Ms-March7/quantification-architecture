#!/usr/bin/env python3
"""Build zero-drop inspection candidates for two malformed CSMAR BDT exports.

The default operation is a complete, write-free plan.  ``--execute`` may write
only below ``tmp/csmar-bdt-zero-loss-candidate`` and never edits the downloaded
ZIP, the formal catalog, the source registry, or any roadmap.

The two reviewed exports contain valid multiline narrative text plus literal,
unescaped double quotes.  ``BDT_Trdchg.Content`` also contains six literal
``","`` sequences which a generic CSV reader mistakes for field boundaries.
This adapter frames records from table-specific business anchors, accounts for
every source byte, applies only reversible quote insertions and the explicit
Content-boundary rule, and emits one sidecar object per source record.  It then
materializes Parquet and requires zero rows in both CSV-to-Parquet ``EXCEPT
ALL`` directions.

All outputs remain ``INSPECTION_ONLY/NOT_ADMITTED``.
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
import stat
import sys
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, TextIO


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOOL_ID = (
    "parse_csmar_bdt_zero_loss_candidate.py/"
    "csmar-bdt-zero-loss-candidate-v1"
)
PURPOSE_CEILING = "INSPECTION_ONLY"
ADMISSION_DECISION = "NOT_ADMITTED"
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}$")
DATE_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
QUOTE_RUN = re.compile(r'"+')
PARQUET_NULL_SENTINEL = "__QDATA_BDT_ZERO_LOSS_NEVER_NULL_8D74F732__"


class BdtZeroLossError(RuntimeError):
    """A controlled, fail-closed parser rejection."""


@dataclass(frozen=True)
class TableSpec:
    name: str
    member_name: str
    descriptor_member_name: str
    dataset_id: str
    table_name: str
    columns: tuple[str, ...]
    key_columns: tuple[str, ...]
    required_columns: tuple[str, ...]
    date_columns: tuple[str, ...]
    binary_flag_columns: tuple[str, ...]
    anchor: re.Pattern[bytes]
    expected_record_count: int
    merge_field_index: int | None = None
    merge_right_fixed: int | None = None


BDT_CO_COLUMNS = (
    "Symbol",
    "ShortName",
    "ListedDate",
    "InstitutionID",
    "CoFullName",
    "EstablishDate",
    "DelistedDate",
    "IndustryCode",
    "IndustryName",
    "IndustryCode1",
    "IndustryName1",
    "ListingExange",
    "Crcd",
    "FirstSTDate",
    "FirstSTReason",
    "SurvivalTimeofFirstST",
    "TimeConsumingofUncap",
    "IsManyTimesST",
    "HasOverseaSubsidiary",
    "RegisteredCapital",
    "BusinessScope",
)

BDT_TRDCHG_COLUMNS = (
    "Symbol",
    "Chgtype",
    "Chgreas",
    "ShortNamebc",
    "ShortNameac",
    "Annoudt",
    "Execudt",
    "Chgrsdis",
    "Content",
    "TypeAuditOpinbc",
    "ISSustManaNonStandExpl",
)


TABLE_SPECS = {
    "BDT_CO": TableSpec(
        name="BDT_CO",
        member_name="BDT_CO.csv",
        descriptor_member_name="BDT_CO[DES][csv].txt",
        dataset_id="csmar-bdt-co-zero-loss",
        table_name="bdt_co_zero_loss_candidate",
        columns=BDT_CO_COLUMNS,
        # Symbol 600018 is legitimately reused across two listing episodes.
        key_columns=("Symbol", "ListedDate"),
        required_columns=(
            "Symbol",
            "ShortName",
            "ListedDate",
            "InstitutionID",
            "CoFullName",
            "EstablishDate",
        ),
        date_columns=("ListedDate", "EstablishDate"),
        binary_flag_columns=("IsManyTimesST", "HasOverseaSubsidiary"),
        anchor=re.compile(
            rb'(?m)^"\d{6}","[^"\r\n]*","(?:\d{4}-\d{2}-\d{2}|)",'
        ),
        expected_record_count=5_991,
    ),
    "BDT_Trdchg": TableSpec(
        name="BDT_Trdchg",
        member_name="BDT_Trdchg.csv",
        descriptor_member_name="BDT_Trdchg[DES][csv].txt",
        dataset_id="csmar-bdt-trdchg-zero-loss",
        table_name="bdt_trdchg_zero_loss_candidate",
        columns=BDT_TRDCHG_COLUMNS,
        key_columns=("Symbol", "Chgtype", "Annoudt", "Execudt"),
        required_columns=(
            "Symbol",
            "Chgtype",
            "Chgreas",
            "ShortNamebc",
            "ShortNameac",
            "Annoudt",
            "Execudt",
            "Chgrsdis",
            "ISSustManaNonStandExpl",
        ),
        date_columns=("Annoudt", "Execudt"),
        binary_flag_columns=("ISSustManaNonStandExpl",),
        anchor=re.compile(rb'(?m)^"\d{6}","[A-Z]{2}",'),
        expected_record_count=4_253,
        # Eight fixed fields precede Content and two fixed fields follow it.
        merge_field_index=8,
        merge_right_fixed=2,
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


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def pretty_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.write_bytes(pretty_json_bytes(value))


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


def require_safe_component(value: str, label: str) -> str:
    if not SAFE_COMPONENT.fullmatch(value):
        raise BdtZeroLossError(f"unsafe {label}: {value!r}")
    return value


def require_input_location(zip_path: Path, input_root: Path) -> Path:
    root = input_root.resolve(strict=True)
    resolved = zip_path.resolve(strict=True)
    if not root.is_dir():
        raise BdtZeroLossError(f"input root is not a directory: {root}")
    if not resolved.is_file() or resolved.suffix.casefold() != ".zip":
        raise BdtZeroLossError(f"source is not a ZIP file: {resolved}")
    if (
        zip_path.is_symlink()
        or is_reparse_point(zip_path)
        or not is_below(resolved, root)
    ):
        raise BdtZeroLossError(
            "source ZIP must be a non-reparse file below the explicit input root"
        )
    return resolved


def require_output_location(
    workspace_root: Path,
    output_root: Path | None,
    dataset_id: str,
    run_id: str,
) -> Path:
    workspace = workspace_root.resolve()
    tmp_root = (workspace / "tmp").resolve()
    base = (
        tmp_root / "csmar-bdt-zero-loss-candidate"
        if output_root is None
        else output_root.resolve()
    )
    if not is_below(base, tmp_root):
        raise BdtZeroLossError("output root must stay below the workspace tmp tree")
    target = base / dataset_id / run_id
    if not is_below(target.resolve(), tmp_root):
        raise BdtZeroLossError("candidate target escaped the workspace tmp tree")
    return target


def safe_zip_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members: dict[str, zipfile.ZipInfo] = {}
    folded: set[str] = set()
    for info in archive.infolist():
        pure = PurePosixPath(info.filename)
        folded_name = info.filename.casefold()
        if (
            not info.filename
            or info.filename.startswith(("/", "\\"))
            or "\\" in info.filename
            or ".." in pure.parts
            or pure.is_absolute()
        ):
            raise BdtZeroLossError(
                f"unsafe ZIP member path: {info.filename!r}"
            )
        if folded_name in folded:
            raise BdtZeroLossError(
                f"duplicate case-insensitive ZIP member: {info.filename!r}"
            )
        if info.flag_bits & 0x1:
            raise BdtZeroLossError(
                f"encrypted ZIP member is unsupported: {info.filename!r}"
            )
        folded.add(folded_name)
        members[info.filename] = info
    return members


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
        raise BdtZeroLossError(
            "anchored source record does not have a quoted outer boundary"
        )
    return body, bytes(separator)


def frame_records(
    raw: bytes,
    spec: TableSpec,
) -> tuple[list[str], bytes, list[FramedRecord]]:
    if not raw.startswith(codecs.BOM_UTF8):
        raise BdtZeroLossError("source CSV must be UTF-8-SIG")
    matches = list(spec.anchor.finditer(raw))
    if not matches:
        raise BdtZeroLossError(f"no business record anchors found for {spec.name}")

    header_bytes = raw[: matches[0].start()]
    try:
        header_rows = list(
            csv.reader(
                io.StringIO(
                    header_bytes.decode("utf-8-sig", errors="strict"),
                    newline="",
                ),
                strict=True,
            )
        )
    except (UnicodeError, csv.Error) as exc:
        raise BdtZeroLossError(f"cannot parse source header: {exc}") from exc
    if header_rows != [list(spec.columns)]:
        raise BdtZeroLossError(
            f"source header mismatch for {spec.name}: {header_rows!r}"
        )

    records: list[FramedRecord] = []
    expected_start = len(header_bytes)
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        if start != expected_start:
            raise BdtZeroLossError(
                f"unaccounted bytes before record {index + 1}"
            )
        region = raw[start:end]
        body, separator = split_final_record_separator(region)
        if body + separator != region:
            raise BdtZeroLossError(
                f"record {index + 1} body/separator accounting failed"
            )
        records.append(
            FramedRecord(
                ordinal=index + 1,
                start_byte=start,
                end_byte=end,
                region=region,
                body=body,
                separator=separator,
            )
        )
        expected_start = end
    if expected_start != len(raw):
        raise BdtZeroLossError("unaccounted bytes exist after the final record")
    accounted = len(header_bytes) + sum(len(record.region) for record in records)
    if accounted != len(raw):
        raise BdtZeroLossError("source byte accounting invariant failed")
    return list(spec.columns), header_bytes, records


def decode_quote_runs(
    raw_content: str,
    *,
    field_index: int,
    column: str,
) -> tuple[str, list[dict[str, Any]]]:
    logical_parts: list[str] = []
    actions: list[dict[str, Any]] = []
    raw_cursor = 0
    canonical_cursor = 0
    for match in QUOTE_RUN.finditer(raw_content):
        prefix = raw_content[raw_cursor : match.start()]
        logical_parts.append(prefix)
        canonical_cursor += len(prefix)
        raw_run_length = match.end() - match.start()
        logical_quote_count = (raw_run_length + 1) // 2
        logical_parts.append('"' * logical_quote_count)
        canonical_run_length = logical_quote_count * 2
        if raw_run_length % 2:
            actions.append(
                {
                    "action": "INSERT_QUOTE_TO_ESCAPE_UNESCAPED_QUOTE",
                    "field_index": field_index,
                    "column": column,
                    "raw_field_char_offset": match.start(),
                    "canonical_field_char_offset": (
                        canonical_cursor + raw_run_length
                    ),
                    "raw_run_length": raw_run_length,
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
        raise BdtZeroLossError("canonical CSV writer omitted row terminator")
    return result[:-1]


def reverse_repaired_body(
    values: list[str],
    actions: list[dict[str, Any]],
) -> str:
    raw_contents = [value.replace('"', '""') for value in values]
    by_field: dict[int, list[dict[str, Any]]] = {}
    for action in actions:
        if action["action"] != "INSERT_QUOTE_TO_ESCAPE_UNESCAPED_QUOTE":
            continue
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
                raise BdtZeroLossError(
                    "repair action cannot be reversed at its declared offset"
                )
            content = content[:offset] + content[offset + 1 :]
        raw_contents[field_index] = content
    return '"' + '","'.join(raw_contents) + '"'


def validate_values(
    values: list[str],
    *,
    framed: FramedRecord,
    spec: TableSpec,
) -> None:
    for column in spec.required_columns:
        if values[spec.columns.index(column)] == "":
            raise BdtZeroLossError(
                f"record {framed.ordinal} has blank required column {column}"
            )
    for column in spec.date_columns:
        value = values[spec.columns.index(column)]
        if not DATE_VALUE.fullmatch(value):
            raise BdtZeroLossError(
                f"record {framed.ordinal} has invalid {column}: {value!r}"
            )
    for column in spec.binary_flag_columns:
        value = values[spec.columns.index(column)]
        if value not in {"0", "1"}:
            raise BdtZeroLossError(
                f"record {framed.ordinal} has non-binary {column}: {value!r}"
            )
    if any(PARQUET_NULL_SENTINEL in value for value in values):
        raise BdtZeroLossError(
            f"record {framed.ordinal} contains the reserved null sentinel"
        )


def parse_one_record(
    framed: FramedRecord,
    spec: TableSpec,
) -> tuple[list[str], dict[str, Any], bool]:
    try:
        body_text = framed.body.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise BdtZeroLossError(
            f"record {framed.ordinal} is not strict UTF-8: {exc}"
        ) from exc
    if not body_text.startswith('"') or not body_text.endswith('"'):
        raise BdtZeroLossError(
            f"record {framed.ordinal} lacks quoted field boundaries"
        )

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

    raw_parts = body_text[1:-1].split('","')
    original_structural_width = len(raw_parts)
    actions: list[dict[str, Any]] = []
    delimiter_merge_required = False
    if len(raw_parts) != len(spec.columns):
        left = spec.merge_field_index
        right = spec.merge_right_fixed
        if (
            left is None
            or right is None
            or len(raw_parts) <= len(spec.columns)
        ):
            raise BdtZeroLossError(
                f"record {framed.ordinal} has structural width "
                f"{len(raw_parts)}; expected {len(spec.columns)}"
            )
        middle_end = len(raw_parts) - right
        merged = '","'.join(raw_parts[left:middle_end])
        raw_parts = raw_parts[:left] + [merged] + raw_parts[middle_end:]
        if len(raw_parts) != len(spec.columns):
            raise BdtZeroLossError(
                f"record {framed.ordinal} delimiter repair did not restore "
                f"{len(spec.columns)} fields"
            )
        delimiter_merge_required = True
        actions.append(
            {
                "action": "MERGE_LITERAL_FIELD_SEPARATORS_IN_CONTENT",
                "field_index": left,
                "column": spec.columns[left],
                "original_structural_width": original_structural_width,
                "repaired_structural_width": len(raw_parts),
                "literal_separator_count": (
                    original_structural_width - len(spec.columns)
                ),
                "resolution_rule": "FIXED_FIRST_8_FIELDS_AND_LAST_2_FIELDS",
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

    if strict_valid and actions:
        raise BdtZeroLossError(
            f"record {framed.ordinal} was strict-valid but repairs were inferred"
        )
    if not strict_valid and not actions:
        raise BdtZeroLossError(
            f"record {framed.ordinal} was strict-invalid with no repair rule"
        )
    validate_values(values, framed=framed, spec=spec)

    canonical_body = canonical_csv_body(values)
    try:
        canonical_rows = list(
            csv.reader(io.StringIO(canonical_body, newline=""), strict=True)
        )
    except csv.Error as exc:
        raise BdtZeroLossError(
            f"record {framed.ordinal} canonical CSV is invalid: {exc}"
        ) from exc
    if canonical_rows != [values]:
        raise BdtZeroLossError(
            f"record {framed.ordinal} canonical CSV roundtrip changed values"
        )
    reconstructed = reverse_repaired_body(values, actions)
    if reconstructed != body_text:
        raise BdtZeroLossError(
            f"record {framed.ordinal} repair is not byte-reversible"
        )

    quote_repairs = sum(
        action["action"] == "INSERT_QUOTE_TO_ESCAPE_UNESCAPED_QUOTE"
        for action in actions
    )
    if not actions:
        parse_status = "CLEAN_STRICT"
    elif delimiter_merge_required:
        parse_status = (
            "REPAIRED_QUOTES_AND_CONTENT_SEPARATORS__REVIEW_REQUIRED"
        )
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
        "source_region_bytes": len(framed.region),
        "source_body_bytes": len(framed.body),
        "source_record_sha256": sha256_bytes(framed.region),
        "source_body_sha256": sha256_bytes(framed.body),
        "source_separator_hex": framed.separator.hex(),
        "canonical_body_sha256": sha256_bytes(
            canonical_body.encode("utf-8")
        ),
        "parse_status": parse_status,
        "strict_source_parse": strict_valid,
        "source_structural_width": original_structural_width,
        "output_field_count": len(values),
        "empty_source_field_count": sum(value == "" for value in values),
        "quote_repair_count": quote_repairs,
        "literal_separator_merge_count": int(delimiter_merge_required),
        "semantic_review_required": delimiter_merge_required,
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
            raise BdtZeroLossError(
                f"ZIP CRC failure for archive member {corrupt_member!r}"
            )
        members = safe_zip_members(archive)
        for required_member in (
            spec.member_name,
            spec.descriptor_member_name,
        ):
            if required_member not in members:
                raise BdtZeroLossError(
                    f"ZIP does not contain {required_member}"
                )
        data_info = members[spec.member_name]
        descriptor_info = members[spec.descriptor_member_name]
        raw = archive.read(data_info)
        descriptor = archive.read(descriptor_info)

    header, header_bytes, records = frame_records(raw, spec)
    if len(records) != spec.expected_record_count:
        raise BdtZeroLossError(
            f"{spec.name} anchored record count {len(records)} does not match "
            f"reviewed expectation {spec.expected_record_count}"
        )

    metrics: dict[str, Any] = {
        "expected_record_count": spec.expected_record_count,
        "anchored_record_count": len(records),
        "accounted_record_count": 0,
        "output_record_count": 0,
        "strict_valid_record_count": 0,
        "repaired_record_count": 0,
        "semantic_review_record_count": 0,
        "unescaped_quote_character_count": 0,
        "literal_separator_merge_record_count": 0,
        "literal_separator_count_recovered": 0,
        "empty_source_field_count": 0,
        "duplicate_key_count": 0,
        "date_ranges": {},
        "source_csv_bytes": len(raw),
        "header_bytes": len(header_bytes),
        "accounted_source_bytes": (
            len(header_bytes) + sum(len(record.region) for record in records)
        ),
        "unaccounted_source_bytes": 0,
        "logical_block_accounting_ratio": 1.0,
    }
    keys: set[tuple[str, ...]] = set()
    dates = {column: [] for column in spec.date_columns}
    first_repaired_ordinals: list[int] = []
    semantic_records: list[dict[str, Any]] = []

    if sinks is not None:
        sinks.csv_file.write(canonical_csv_body(header) + "\n")

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
        metrics["literal_separator_merge_record_count"] += int(
            sidecar["literal_separator_merge_count"]
        )
        metrics["literal_separator_count_recovered"] += sum(
            int(action.get("literal_separator_count", 0))
            for action in sidecar["actions"]
        )
        metrics["empty_source_field_count"] += int(
            sidecar["empty_source_field_count"]
        )
        if repaired and len(first_repaired_ordinals) < 20:
            first_repaired_ordinals.append(framed.ordinal)
        if sidecar["semantic_review_required"]:
            semantic_records.append(
                {
                    "record_ordinal": sidecar["record_ordinal"],
                    "key": sidecar["key"],
                    "source_start_byte": sidecar["source_start_byte"],
                    "source_end_byte": sidecar["source_end_byte"],
                    "source_record_sha256": sidecar["source_record_sha256"],
                    "canonical_body_sha256": sidecar[
                        "canonical_body_sha256"
                    ],
                    "actions": sidecar["actions"],
                }
            )

        key = tuple(
            values[spec.columns.index(column)]
            for column in spec.key_columns
        )
        if key in keys:
            metrics["duplicate_key_count"] += 1
        keys.add(key)
        for column in spec.date_columns:
            dates[column].append(values[spec.columns.index(column)])

        if sinks is not None:
            sinks.csv_file.write(canonical_csv_body(values) + "\n")
            sinks.sidecar_file.write(
                json.dumps(
                    sidecar,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n"
            )
        metrics["output_record_count"] += 1

    if not (
        metrics["expected_record_count"]
        == metrics["anchored_record_count"]
        == metrics["accounted_record_count"]
        == metrics["output_record_count"]
    ):
        raise BdtZeroLossError("logical record accounting invariant failed")
    if metrics["accounted_source_bytes"] != metrics["source_csv_bytes"]:
        raise BdtZeroLossError("source byte accounting invariant failed")
    if metrics["duplicate_key_count"]:
        raise BdtZeroLossError(
            f"found {metrics['duplicate_key_count']} duplicate business keys"
        )
    metrics["date_ranges"] = {
        column: {"min": min(values), "max": max(values)}
        for column, values in dates.items()
    }
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
            "zip_member_count": len(members),
            "csv_member": spec.member_name,
            "csv_member_bytes": data_info.file_size,
            "csv_member_compressed_bytes": data_info.compress_size,
            "csv_member_crc32": f"{data_info.CRC:08x}",
            "csv_member_sha256": sha256_bytes(raw),
            "descriptor_member": spec.descriptor_member_name,
            "descriptor_member_bytes": descriptor_info.file_size,
            "descriptor_member_crc32": f"{descriptor_info.CRC:08x}",
            "descriptor_member_sha256": sha256_bytes(descriptor),
            "encoding": "utf-8-sig",
            "header_columns": len(header),
            "header_sha256": sha256_bytes(header_bytes),
        },
        "metrics": metrics,
        "repair_contract": {
            "quote_rule": (
                "Every odd source quote run inside a framed field is made "
                "RFC-4180-compatible by inserting exactly one quote; removing "
                "declared insertions must reproduce the source body exactly."
            ),
            "content_separator_rule": (
                "BDT_Trdchg fixes the first eight and last two fields, then "
                "merges surplus literal field separators into Content. "
                "Affected records require semantic review."
            ),
            "source_body_reverse_verification": "PASS_ALL_RECORDS",
        },
        "semantic_review": {
            "required": bool(semantic_records),
            "record_count": len(semantic_records),
            "records": semantic_records,
        },
        "safety": {
            "purpose_ceiling": PURPOSE_CEILING,
            "admission_decision": ADMISSION_DECISION,
            "catalog_registered": False,
            "source_admission": False,
            "quality_promotion": False,
            "research_ready": False,
            "source_zip_modified": False,
            "unknown_value_policy": (
                "Empty source fields remain empty strings in this lossless "
                "mechanical candidate. No value is filled, inferred, or "
                "replaced with zero; a future admitted semantic layer must map "
                "declared unknowns to NULL explicitly."
            ),
        },
    }


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def import_duckdb() -> Any:
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise BdtZeroLossError(
            "DuckDB is required for --execute; use the locked central runtime"
        ) from exc
    return duckdb


def build_and_verify_parquet(
    *,
    repaired_csv: Path,
    parquet_path: Path,
    spec: TableSpec,
) -> dict[str, Any]:
    duckdb = import_duckdb()
    connection = duckdb.connect(database=":memory:")
    columns = ", ".join(sql_identifier(column) for column in spec.columns)
    key_sql = ", ".join(
        sql_identifier(column) for column in spec.key_columns
    )
    csv_path = repaired_csv.resolve().as_posix()
    parquet_target = parquet_path.resolve().as_posix()
    try:
        connection.execute(
            "CREATE TABLE _csv AS SELECT "
            f"{columns} FROM read_csv("
            f"{sql_string(csv_path)}, header=true, all_varchar=true, "
            "delim=',', quote='\"', escape='\"', strict_mode=true, "
            f"nullstr={sql_string(PARQUET_NULL_SENTINEL)})"
        )
        csv_rows = int(
            connection.execute("SELECT COUNT(*) FROM _csv").fetchone()[0]
        )
        duplicate = connection.execute(
            "SELECT COALESCE(SUM(c - 1), 0), COUNT(*) FROM "
            f"(SELECT {key_sql}, COUNT(*) AS c FROM _csv "
            f"GROUP BY {key_sql} HAVING COUNT(*) > 1)"
        ).fetchone()
        duplicate_rows = int(duplicate[0])
        duplicate_groups = int(duplicate[1])
        if duplicate_rows:
            raise BdtZeroLossError(
                "canonical CSV contains duplicate business keys: "
                f"{duplicate_groups} groups, {duplicate_rows} rows"
            )
        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        connection.execute(
            f"COPY (SELECT {columns} FROM _csv ORDER BY {key_sql}) "
            f"TO {sql_string(parquet_target)} "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        connection.execute(
            "CREATE VIEW _parquet AS SELECT "
            f"{columns} FROM read_parquet({sql_string(parquet_target)})"
        )
        parquet_rows = int(
            connection.execute("SELECT COUNT(*) FROM _parquet").fetchone()[0]
        )
        csv_minus_parquet = int(
            connection.execute(
                "SELECT COUNT(*) FROM ("
                f"(SELECT {columns} FROM _csv) "
                "EXCEPT ALL "
                f"(SELECT {columns} FROM _parquet)"
                ")"
            ).fetchone()[0]
        )
        parquet_minus_csv = int(
            connection.execute(
                "SELECT COUNT(*) FROM ("
                f"(SELECT {columns} FROM _parquet) "
                "EXCEPT ALL "
                f"(SELECT {columns} FROM _csv)"
                ")"
            ).fetchone()[0]
        )
        if (
            csv_rows != parquet_rows
            or csv_minus_parquet
            or parquet_minus_csv
        ):
            raise BdtZeroLossError(
                "CSV/Parquet roundtrip mismatch: "
                f"csv_rows={csv_rows}, parquet_rows={parquet_rows}, "
                f"csv_minus_parquet={csv_minus_parquet}, "
                f"parquet_minus_csv={parquet_minus_csv}"
            )
        parquet_types = connection.execute(
            "DESCRIBE SELECT * FROM _parquet"
        ).fetchall()
        non_varchar = [
            row for row in parquet_types if str(row[1]).upper() != "VARCHAR"
        ]
        if non_varchar:
            raise BdtZeroLossError(
                f"Parquet lossless layer inferred non-VARCHAR fields: {non_varchar}"
            )
        return {
            "status": "PASS_ZERO_BIDIRECTIONAL_DIFFERENCE",
            "csv_row_count": csv_rows,
            "parquet_row_count": parquet_rows,
            "csv_minus_parquet_rows": csv_minus_parquet,
            "parquet_minus_csv_rows": parquet_minus_csv,
            "duplicate_key_groups": duplicate_groups,
            "duplicate_key_rows": duplicate_rows,
            "column_count": len(spec.columns),
            "all_columns_varchar": True,
            "empty_string_preservation": (
                "CSV nullstr is a reserved impossible sentinel; empty source "
                "strings remain empty strings in Parquet."
            ),
        }
    except BdtZeroLossError:
        raise
    except Exception as exc:
        raise BdtZeroLossError(f"DuckDB Parquet build failed: {exc}") from exc
    finally:
        connection.close()


def artifact(path: Path, root: Path, *, row_count: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "relative_path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    if row_count is not None:
        result["row_count"] = row_count
    return result


def catalog_hash(workspace_root: Path) -> str | None:
    path = workspace_root / "data" / "catalog" / "datasets.json"
    return sha256_file(path) if path.is_file() else None


def quality_summary(
    result: dict[str, Any],
    parquet_verification: dict[str, Any],
) -> dict[str, Any]:
    semantic = result["semantic_review"]
    return {
        "schema_version": 1,
        "mechanical_status": (
            "PASS_WITH_REVERSIBLE_REPAIRS__SEMANTIC_REVIEW_REQUIRED"
            if semantic["required"]
            else "PASS_WITH_REVERSIBLE_QUOTE_REPAIRS"
            if result["metrics"]["repaired_record_count"]
            else "PASS_NO_REPAIRS_REQUIRED"
        ),
        "table": result["table"],
        "dataset_id": result["dataset_id"],
        "metrics": result["metrics"],
        "parquet_roundtrip": parquet_verification,
        "semantic_review": semantic,
        "repair_contract": result["repair_contract"],
        "purpose_ceiling": PURPOSE_CEILING,
        "admission_decision": ADMISSION_DECISION,
        "catalog_registered": False,
        "research_ready": False,
    }


def run(
    *,
    workspace_root: Path,
    zip_path: Path,
    input_root: Path,
    table: str,
    run_id: str,
    output_root: Path | None,
    execute: bool,
) -> dict[str, Any]:
    workspace = workspace_root.resolve()
    if table not in TABLE_SPECS:
        raise BdtZeroLossError(f"unsupported table: {table}")
    spec = TABLE_SPECS[table]
    require_safe_component(run_id, "run_id")
    source_zip = require_input_location(zip_path, input_root)
    target = require_output_location(
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
                "status": "PLAN_COMPLETE__NO_WRITES",
                "write_performed": False,
                "candidate_path": target.as_posix(),
                "catalog_sha256_before": before_catalog,
                "catalog_sha256_after": catalog_hash(workspace),
            }
        )
        if result["catalog_sha256_after"] != before_catalog:
            raise BdtZeroLossError("catalog changed during read-only plan")
        return result

    if target.exists():
        raise BdtZeroLossError(f"candidate target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{run_id}.partial-{uuid.uuid4().hex}"
    if stage.exists():
        raise BdtZeroLossError(f"staging target already exists: {stage}")
    stage.mkdir()
    try:
        raw_dir = stage / "raw"
        repaired_dir = stage / "repaired"
        repair_dir = stage / "repair"
        curated_dir = stage / "curated"
        quality_dir = stage / "quality"
        for directory in (
            raw_dir,
            repaired_dir,
            repair_dir,
            curated_dir,
            quality_dir,
        ):
            directory.mkdir()

        raw_copy = raw_dir / source_zip.name
        shutil.copyfile(source_zip, raw_copy)
        if sha256_file(raw_copy) != sha256_file(source_zip):
            raise BdtZeroLossError("raw ZIP copy hash differs from source")

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
                sinks=OutputSinks(
                    csv_file=csv_file,
                    sidecar_file=sidecar_file,
                ),
            )

        parquet_path = curated_dir / "part-000.parquet"
        parquet_verification = build_and_verify_parquet(
            repaired_csv=repaired_csv,
            parquet_path=parquet_path,
            spec=spec,
        )
        if (
            parquet_verification["csv_row_count"]
            != result["metrics"]["output_record_count"]
        ):
            raise BdtZeroLossError("parser and Parquet row counts differ")

        query_sql = stage / "query.sql"
        final_parquet_path = (
            target / "curated" / "part-000.parquet"
        ).as_posix()
        query_sql.write_text(
            "-- INSPECTION_ONLY/NOT_ADMITTED; not a research input.\n"
            f"CREATE OR REPLACE VIEW {sql_identifier(spec.table_name)} AS\n"
            "SELECT * FROM read_parquet("
            f"{sql_string(final_parquet_path)});\n",
            encoding="utf-8",
            newline="\n",
        )

        quality = quality_summary(result, parquet_verification)
        quality_path = quality_dir / "summary.json"
        write_json(quality_path, quality)

        row_count = result["metrics"]["output_record_count"]
        outputs = {
            "raw_zip": artifact(raw_copy, stage),
            "repaired_csv": artifact(
                repaired_csv, stage, row_count=row_count
            ),
            "record_sidecar": artifact(
                sidecar, stage, row_count=row_count
            ),
            "parquet": artifact(
                parquet_path, stage, row_count=row_count
            ),
            "quality_summary": artifact(quality_path, stage),
            "query_sql": artifact(query_sql, stage),
        }
        parser_script = Path(__file__).resolve()
        parser_script_sha256 = sha256_file(parser_script)
        after_catalog = catalog_hash(workspace)
        if after_catalog != before_catalog:
            raise BdtZeroLossError("catalog changed during candidate execution")

        content_binding = {
            "tool_id": TOOL_ID,
            "table": spec.name,
            "dataset_id": spec.dataset_id,
            "run_id": run_id,
            "source": result["source"],
            "parser_script_sha256": parser_script_sha256,
            "outputs": outputs,
            "metrics": result["metrics"],
            "parquet_roundtrip": parquet_verification,
            "purpose_ceiling": PURPOSE_CEILING,
            "admission_decision": ADMISSION_DECISION,
        }
        content_hash = sha256_bytes(canonical_json_bytes(content_binding))
        result.update(
            {
                "schema_version": 1,
                "manifest_kind": "CSMAR_BDT_ZERO_LOSS_CANDIDATE",
                "status": "ZERO_LOSS_CANDIDATE_MATERIALIZED",
                "write_performed": True,
                "created_at": utc_now(),
                "run_id": run_id,
                "candidate_path": target.as_posix(),
                "parser_script": {
                    "path": parser_script.as_posix(),
                    "sha256": parser_script_sha256,
                },
                "outputs": outputs,
                "parquet_roundtrip": parquet_verification,
                "content_hash_algorithm": (
                    "sha256-canonical-json-csmar-bdt-zero-loss-v1"
                ),
                "content_hash": content_hash,
                "catalog_guard": {
                    "path": (
                        workspace / "data" / "catalog" / "datasets.json"
                    ).as_posix(),
                    "sha256_before": before_catalog,
                    "sha256_after": after_catalog,
                    "write_authorized": False,
                },
            }
        )
        write_json(stage / "manifest.json", result)
        if catalog_hash(workspace) != before_catalog:
            raise BdtZeroLossError("catalog changed before candidate seal")
        os.replace(stage, target)
        return result
    except Exception:
        tmp_root = (workspace / "tmp").resolve()
        if stage.exists() and is_below(stage.resolve(), tmp_root):
            shutil.rmtree(stage)
        raise


def default_downloads() -> Path:
    return Path.home() / "Downloads"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or materialize zero-drop inspection candidates for "
            "CSMAR BDT_CO and BDT_Trdchg exports."
        )
    )
    parser.add_argument("--zip", required=True, type=Path, dest="zip_path")
    parser.add_argument(
        "--input-root",
        type=Path,
        default=default_downloads(),
        help="explicit local inbox root (default: the current user's Downloads)",
    )
    parser.add_argument("--table", required=True, choices=sorted(TABLE_SPECS))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan",
        action="store_true",
        help="perform the complete write-free plan (default)",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="materialize an INSPECTION_ONLY candidate below tmp",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(
            workspace_root=WORKSPACE_ROOT,
            zip_path=args.zip_path,
            input_root=args.input_root,
            table=args.table,
            run_id=args.run_id,
            output_root=args.output_root,
            execute=bool(args.execute),
        )
    except (BdtZeroLossError, OSError, UnicodeError, zipfile.BadZipFile) as exc:
        print(
            json.dumps(
                {"status": "REJECTED", "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
