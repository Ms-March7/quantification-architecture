#!/usr/bin/env python3
"""Plan or build an inspection-only dataset candidate from local CSV/ZIP files.

The default operation is a write-free plan.  ``--execute`` copies the original
download packages into an isolated ``tmp`` candidate, validates and types one
logical table with DuckDB, writes single-file or year/month Parquet partitions, and emits a
five-document candidate contract plus a human-readable report.

This tool never downloads data, writes ``data/market``, changes
``data/catalog/datasets.json``, admits a source, or promotes data quality.
"""

from __future__ import annotations

import argparse
import codecs
import csv
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable


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


TOOL_ID = "build_downloaded_dataset.py/local-download-candidate-v1"
HASH_ALGORITHM = "sha256-local-download-candidate-v1"
MODE = "local_download_candidate"
PURPOSE_CEILING = "INSPECTION_ONLY"
RESERVED_LINEAGE_COLUMN = "__qdata_source_file"
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}$")
SAFE_TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
SAFE_TYPE = re.compile(
    r"^(?:VARCHAR|BOOLEAN|TINYINT|SMALLINT|INTEGER|BIGINT|"
    r"UTINYINT|USMALLINT|UINTEGER|UBIGINT|REAL|FLOAT|DOUBLE|DATE|"
    r"TIMESTAMP|TIMESTAMP_MS|TIMESTAMP_US|TIMESTAMP_NS|"
    r"DECIMAL\((?:[1-9]|[1-2][0-9]|3[0-8]),(?:0|[1-9]|[1-2][0-9]|"
    r"3[0-8])\))$"
)
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
REQUEST_KEYS = {
    "schema_version",
    "mode",
    "dataset_id",
    "run_id",
    "source_id",
    "dataset_schema_version",
    "authorized_local_research_download",
    "source_access_note",
    "include_patterns",
    "zip_member_patterns",
    "table",
    "limits",
}
TABLE_KEYS = {
    "table_name",
    "expected_columns",
    "column_types",
    "date_column",
    "key_columns",
    "required_columns",
    "delimiter",
    "encoding",
    "allow_extra_columns",
    "partition_mode",
}
LIMIT_KEYS = {
    "max_packages",
    "max_csv_sources",
    "max_archive_members",
    "max_total_input_bytes",
    "max_total_unpacked_bytes",
    "max_compression_ratio",
    "max_rows",
}
DEFAULT_LIMITS = {
    "max_packages": 10_000,
    "max_csv_sources": 100_000,
    "max_archive_members": 200_000,
    "max_total_input_bytes": 2 * 1024**4,
    "max_total_unpacked_bytes": 4 * 1024**4,
    "max_compression_ratio": 1_000,
    "max_rows": 10_000_000_000,
}
ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "utf-16")
DELIMITERS = (",", "\t", ";", "|")


class CandidateBuildError(RuntimeError):
    """A controlled fail-closed rejection."""


@dataclass(frozen=True)
class CsvSource:
    package_relative_path: str
    member_name: str | None
    source_ref: str
    uncompressed_bytes: int
    compressed_bytes: int
    encoding: str
    delimiter: str
    header: list[str]


@dataclass(frozen=True)
class InputPackage:
    path: Path
    relative_path: str
    kind: str
    bytes: int
    mtime_ns: int
    csv_sources: list[CsvSource]
    archive_member_count: int | None


@dataclass(frozen=True)
class MaterializedCsv:
    path: Path
    source_ref: str
    delimiter: str
    header: list[str]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            value,
            handle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def strict_json(path: Path) -> dict[str, Any]:
    try:
        return load_json_object(path)
    except SnapshotContractError as exc:
        raise CandidateBuildError(str(exc)) from exc


def validate_component(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_COMPONENT.fullmatch(value):
        raise CandidateBuildError(f"{label} must be a safe path component")
    if value in {".", ".."} or value.rstrip(" .") != value:
        raise CandidateBuildError(f"{label} has an unsafe Windows path form")
    if value.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        raise CandidateBuildError(f"{label} uses a reserved Windows device name")
    return value


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CandidateBuildError(f"{label} must be a non-empty string")
    return value.strip()


def require_string_list(value: Any, label: str, *, nonempty: bool = True) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise CandidateBuildError(f"{label} must be a non-empty string array")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(require_string(item, f"{label}[{index}]"))
    if len(set(result)) != len(result):
        raise CandidateBuildError(f"{label} contains duplicates")
    return result


def normalized_type(value: Any, label: str) -> str:
    result = require_string(value, label).upper().replace(" ", "")
    if not SAFE_TYPE.fullmatch(result):
        raise CandidateBuildError(f"{label} has an unsupported DuckDB type: {value!r}")
    if result.startswith("DECIMAL("):
        precision, scale = (
            int(part)
            for part in result.removeprefix("DECIMAL(").removesuffix(")").split(",")
        )
        if scale > precision:
            raise CandidateBuildError(f"{label} decimal scale exceeds precision")
    return result


def validate_request(request: dict[str, Any], *, for_execute: bool) -> dict[str, Any]:
    unexpected = sorted(set(request) - REQUEST_KEYS)
    if unexpected:
        raise CandidateBuildError(
            f"request contains unsupported keys: {', '.join(unexpected)}"
        )
    required = {
        "schema_version",
        "mode",
        "dataset_id",
        "run_id",
        "source_id",
        "dataset_schema_version",
        "authorized_local_research_download",
        "source_access_note",
        "table",
    }
    missing = sorted(required - set(request))
    if missing:
        raise CandidateBuildError(f"request is missing keys: {', '.join(missing)}")
    if request["schema_version"] != 1 or request["mode"] != MODE:
        raise CandidateBuildError(
            f"request must use schema_version=1 and mode={MODE!r}"
        )
    dataset_id = validate_component(request["dataset_id"], "dataset_id")
    run_id = validate_component(request["run_id"], "run_id")
    source_id = validate_component(request["source_id"], "source_id")
    schema_version = validate_component(
        request["dataset_schema_version"], "dataset_schema_version"
    )
    authorized = request["authorized_local_research_download"]
    if not isinstance(authorized, bool):
        raise CandidateBuildError(
            "authorized_local_research_download must be a boolean"
        )
    if for_execute and not authorized:
        raise CandidateBuildError(
            "execute requires authorized_local_research_download=true"
        )
    source_access_note = require_string(
        request["source_access_note"], "source_access_note"
    )

    include_patterns = require_string_list(
        request.get(
            "include_patterns",
            ["*.csv", "**/*.csv", "*.zip", "**/*.zip"],
        ),
        "include_patterns",
    )
    zip_member_patterns = require_string_list(
        request.get("zip_member_patterns", ["*.csv", "**/*.csv"]),
        "zip_member_patterns",
    )

    table = request["table"]
    if not isinstance(table, dict):
        raise CandidateBuildError("table must be an object")
    unexpected_table = sorted(set(table) - TABLE_KEYS)
    if unexpected_table:
        raise CandidateBuildError(
            f"table contains unsupported keys: {', '.join(unexpected_table)}"
        )
    table_required = {
        "table_name",
        "expected_columns",
        "column_types",
        "date_column",
        "key_columns",
        "required_columns",
    }
    missing_table = sorted(table_required - set(table))
    if missing_table:
        raise CandidateBuildError(
            f"table is missing keys: {', '.join(missing_table)}"
        )
    table_name = require_string(table["table_name"], "table.table_name")
    if not SAFE_TABLE_NAME.fullmatch(table_name):
        raise CandidateBuildError("table.table_name is not a safe SQL identifier")
    expected_columns = require_string_list(
        table["expected_columns"], "table.expected_columns"
    )
    if RESERVED_LINEAGE_COLUMN in expected_columns:
        raise CandidateBuildError(
            f"{RESERVED_LINEAGE_COLUMN} is reserved for local lineage"
        )
    column_types_raw = table["column_types"]
    if not isinstance(column_types_raw, dict):
        raise CandidateBuildError("table.column_types must be an object")
    if set(column_types_raw) != set(expected_columns):
        raise CandidateBuildError(
            "table.column_types must cover exactly table.expected_columns"
        )
    column_types = {
        column: normalized_type(column_types_raw[column], f"column_types.{column}")
        for column in expected_columns
    }
    partition_mode = table.get("partition_mode", "year_month")
    if partition_mode not in {"year_month", "single"}:
        raise CandidateBuildError(
            "table.partition_mode must be 'year_month' or 'single'"
        )
    raw_date_column = table["date_column"]
    if raw_date_column is None:
        date_column: str | None = None
    else:
        date_column = require_string(raw_date_column, "table.date_column")
    if partition_mode == "year_month" and date_column is None:
        raise CandidateBuildError(
            "year_month partitioning requires a non-null table.date_column"
        )
    key_columns = require_string_list(table["key_columns"], "table.key_columns")
    required_columns = require_string_list(
        table["required_columns"], "table.required_columns"
    )
    expected_set = set(expected_columns)
    declared_column_groups = [
        ("key_columns", key_columns),
        ("required_columns", required_columns),
    ]
    if date_column is not None:
        declared_column_groups.insert(0, ("date_column", [date_column]))
    for label, columns in declared_column_groups:
        unknown = sorted(set(columns) - expected_set)
        if unknown:
            raise CandidateBuildError(
                f"table.{label} contains undeclared columns: {', '.join(unknown)}"
            )
    if not set(key_columns).issubset(required_columns):
        raise CandidateBuildError("all key_columns must also be required_columns")
    if date_column is not None and column_types[date_column] != "DATE":
        raise CandidateBuildError(
            "a non-null date_column must have DuckDB type DATE"
        )
    if partition_mode == "year_month" and date_column not in required_columns:
        raise CandidateBuildError(
            "year_month date_column must also be a required_column"
        )
    delimiter = table.get("delimiter")
    if delimiter is not None and delimiter not in DELIMITERS:
        raise CandidateBuildError(
            r"table.delimiter must be one of comma, tab, semicolon, or pipe"
        )
    encoding = table.get("encoding")
    if encoding is not None and encoding not in ENCODINGS:
        raise CandidateBuildError(
            f"table.encoding must be one of: {', '.join(ENCODINGS)}"
        )
    allow_extra = table.get("allow_extra_columns", False)
    if not isinstance(allow_extra, bool):
        raise CandidateBuildError("table.allow_extra_columns must be boolean")
    limits_raw = request.get("limits", {})
    if not isinstance(limits_raw, dict):
        raise CandidateBuildError("limits must be an object")
    unexpected_limits = sorted(set(limits_raw) - LIMIT_KEYS)
    if unexpected_limits:
        raise CandidateBuildError(
            f"limits contains unsupported keys: {', '.join(unexpected_limits)}"
        )
    limits = dict(DEFAULT_LIMITS)
    for key, value in limits_raw.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
        ):
            raise CandidateBuildError(f"limits.{key} must be a positive integer")
        limits[key] = value

    return {
        "schema_version": 1,
        "mode": MODE,
        "dataset_id": dataset_id,
        "run_id": run_id,
        "source_id": source_id,
        "dataset_schema_version": schema_version,
        "authorized_local_research_download": authorized,
        "source_access_note": source_access_note,
        "include_patterns": include_patterns,
        "zip_member_patterns": zip_member_patterns,
        "table": {
            "table_name": table_name,
            "expected_columns": expected_columns,
            "column_types": column_types,
            "date_column": date_column,
            "key_columns": key_columns,
            "required_columns": required_columns,
            "delimiter": delimiter,
            "encoding": encoding,
            "allow_extra_columns": allow_extra,
            "partition_mode": partition_mode,
        },
        "limits": limits,
    }


def resolved_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except ValueError:
        return False
    return True


def reject_reparse_ancestors(path: Path, stop: Path | None = None) -> None:
    current = path.resolve(strict=False)
    stop_resolved = stop.resolve(strict=False) if stop is not None else None
    chain: list[Path] = []
    while True:
        chain.append(current)
        if stop_resolved is not None and current == stop_resolved:
            break
        if current.parent == current:
            break
        current = current.parent
    for item in reversed(chain):
        if item.exists() and is_reparse_point(item):
            raise CandidateBuildError(f"symlink/junction/reparse point is forbidden: {item}")


def validate_input_root(path: Path) -> Path:
    root = path.resolve()
    if not root.is_dir():
        raise CandidateBuildError(f"inbox is not a directory: {root}")
    if root == Path(root.anchor):
        raise CandidateBuildError("refusing to scan an entire filesystem root")
    if is_reparse_point(root):
        raise CandidateBuildError(f"inbox cannot be a reparse point: {root}")
    return root


def validate_output_root(path: Path, workspace_root: Path) -> Path:
    workspace_root = workspace_root.resolve()
    allowed = (workspace_root / "tmp").resolve(strict=False)
    output = path.resolve(strict=False)
    if not resolved_under(output, allowed):
        raise CandidateBuildError(
            f"output root must stay under the workspace tmp directory: {allowed}"
        )
    reject_reparse_ancestors(output, stop=workspace_root)
    return output


def matches_any(relative_path: str, patterns: list[str]) -> bool:
    normalized = relative_path.replace("\\", "/")
    lowered = normalized.casefold()
    return any(
        fnmatch.fnmatchcase(lowered, pattern.replace("\\", "/").casefold())
        for pattern in patterns
    )


def iter_input_files(root: Path) -> Iterable[tuple[Path, str]]:
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(
                os.scandir(directory),
                key=lambda item: item.name.casefold(),
                reverse=True,
            )
        except OSError as exc:
            raise CandidateBuildError(f"cannot scan inbox directory {directory}: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink() or is_reparse_point(path):
                raise CandidateBuildError(
                    f"reparse points are forbidden inside the inbox: {path}"
                )
            if entry.is_dir(follow_symlinks=False):
                stack.append(path)
            elif entry.is_file(follow_symlinks=False):
                yield path, path.relative_to(root).as_posix()


def decode_probe(sample: bytes, encoding: str) -> str:
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    return decoder.decode(sample, final=False)


def detect_encoding(sample: bytes, requested: str | None) -> tuple[str, str]:
    if requested is not None:
        try:
            return requested, decode_probe(sample, requested)
        except UnicodeError as exc:
            raise CandidateBuildError(
                f"CSV sample does not decode as requested encoding {requested}"
            ) from exc
    candidates: list[str]
    if sample.startswith(codecs.BOM_UTF8):
        candidates = ["utf-8-sig"]
    elif sample.startswith(codecs.BOM_UTF16_LE) or sample.startswith(codecs.BOM_UTF16_BE):
        candidates = ["utf-16"]
    else:
        candidates = ["utf-8", "gb18030"]
    for encoding in candidates:
        try:
            return encoding, decode_probe(sample, encoding)
        except UnicodeError:
            continue
    raise CandidateBuildError("CSV encoding is not UTF-8, GB18030, or UTF-16")


def inspect_csv_header(
    stream: BinaryIO,
    *,
    requested_encoding: str | None,
    requested_delimiter: str | None,
    label: str,
) -> tuple[str, str, list[str]]:
    sample = stream.read(1024 * 1024)
    if not sample:
        raise CandidateBuildError(f"empty CSV file: {label}")
    encoding, decoded = detect_encoding(sample, requested_encoding)
    delimiter = requested_delimiter
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(decoded, delimiters="".join(DELIMITERS)).delimiter
        except csv.Error:
            delimiter = ","
    try:
        reader = csv.reader(io.StringIO(decoded, newline=""), delimiter=delimiter, strict=True)
        header = next(reader)
    except (csv.Error, StopIteration) as exc:
        raise CandidateBuildError(f"cannot parse CSV header for {label}: {exc}") from exc
    if not header:
        raise CandidateBuildError(f"CSV header is empty: {label}")
    if any(not column or column != column.strip() for column in header):
        raise CandidateBuildError(
            f"CSV header has blank or surrounding-whitespace column names: {label}"
        )
    if len(set(header)) != len(header):
        raise CandidateBuildError(f"CSV header has duplicate columns: {label}")
    if RESERVED_LINEAGE_COLUMN in header:
        raise CandidateBuildError(
            f"CSV uses reserved column {RESERVED_LINEAGE_COLUMN}: {label}"
        )
    return encoding, delimiter, header


def safe_zip_member_name(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    member = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in member.parts)
    ):
        raise CandidateBuildError(f"unsafe ZIP member path: {name!r}")
    return member


def is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_IFMT(unix_mode) == stat.S_IFLNK


def inspect_zip(
    path: Path,
    relative_path: str,
    config: dict[str, Any],
) -> tuple[list[CsvSource], int]:
    limits = config["limits"]
    selected: list[CsvSource] = []
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise CandidateBuildError(f"invalid ZIP package {relative_path}: {exc}") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > limits["max_archive_members"]:
            raise CandidateBuildError(
                f"ZIP member count exceeds reviewed cap: {relative_path}"
            )
        seen: set[str] = set()
        for info in infos:
            member = safe_zip_member_name(info.filename)
            folded = member.as_posix().casefold()
            if folded in seen:
                raise CandidateBuildError(
                    f"ZIP contains duplicate case-insensitive path: {relative_path}!{info.filename}"
                )
            seen.add(folded)
            if info.flag_bits & 0x1:
                raise CandidateBuildError(
                    f"encrypted ZIP members are unsupported: {relative_path}!{info.filename}"
                )
            if is_zip_symlink(info):
                raise CandidateBuildError(
                    f"ZIP symlinks are forbidden: {relative_path}!{info.filename}"
                )
            if info.is_dir() or member.suffix.casefold() != ".csv":
                continue
            if not matches_any(member.as_posix(), config["zip_member_patterns"]):
                continue
            if info.file_size and not info.compress_size:
                raise CandidateBuildError(
                    f"ZIP member has invalid zero compressed size: {relative_path}!{info.filename}"
                )
            ratio = info.file_size / max(info.compress_size, 1)
            if ratio > limits["max_compression_ratio"]:
                raise CandidateBuildError(
                    f"ZIP compression ratio exceeds reviewed cap: {relative_path}!{info.filename}"
                )
            with archive.open(info, "r") as stream:
                encoding, delimiter, header = inspect_csv_header(
                    stream,
                    requested_encoding=config["table"]["encoding"],
                    requested_delimiter=config["table"]["delimiter"],
                    label=f"{relative_path}!{info.filename}",
                )
            selected.append(
                CsvSource(
                    package_relative_path=relative_path,
                    member_name=member.as_posix(),
                    source_ref=f"zip:{relative_path}!{member.as_posix()}",
                    uncompressed_bytes=info.file_size,
                    compressed_bytes=info.compress_size,
                    encoding=encoding,
                    delimiter=delimiter,
                    header=header,
                )
            )
    return selected, len(infos)


def inspect_direct_csv(
    path: Path,
    relative_path: str,
    config: dict[str, Any],
) -> CsvSource:
    with path.open("rb") as stream:
        encoding, delimiter, header = inspect_csv_header(
            stream,
            requested_encoding=config["table"]["encoding"],
            requested_delimiter=config["table"]["delimiter"],
            label=relative_path,
        )
    size = path.stat().st_size
    return CsvSource(
        package_relative_path=relative_path,
        member_name=None,
        source_ref=f"csv:{relative_path}",
        uncompressed_bytes=size,
        compressed_bytes=size,
        encoding=encoding,
        delimiter=delimiter,
        header=header,
    )


def validate_headers(sources: list[CsvSource], config: dict[str, Any]) -> list[str]:
    if not sources:
        raise CandidateBuildError("no CSV inputs matched the request")
    first = sources[0].header
    for source in sources[1:]:
        if source.header != first:
            raise CandidateBuildError(
                "selected CSV sources have different headers; split them into separate dataset requests"
            )
    expected = config["table"]["expected_columns"]
    if config["table"]["allow_extra_columns"]:
        missing = [column for column in expected if column not in first]
        if missing:
            raise CandidateBuildError(
                f"CSV header is missing expected columns: {', '.join(missing)}"
            )
    elif first != expected:
        raise CandidateBuildError(
            "CSV header does not exactly match table.expected_columns"
        )
    return first


def discover_packages(inbox: Path, config: dict[str, Any]) -> list[InputPackage]:
    packages: list[InputPackage] = []
    total_bytes = 0
    for path, relative in iter_input_files(inbox):
        if not matches_any(relative, config["include_patterns"]):
            continue
        suffix = path.suffix.casefold()
        if suffix not in {".csv", ".zip"}:
            continue
        stat_result = path.stat()
        total_bytes += stat_result.st_size
        if total_bytes > config["limits"]["max_total_input_bytes"]:
            raise CandidateBuildError("total input bytes exceed the reviewed cap")
        if suffix == ".csv":
            sources = [inspect_direct_csv(path, relative, config)]
            archive_members = None
            kind = "csv"
        else:
            sources, archive_members = inspect_zip(path, relative, config)
            kind = "zip"
        if not sources:
            continue
        packages.append(
            InputPackage(
                path=path,
                relative_path=relative,
                kind=kind,
                bytes=stat_result.st_size,
                mtime_ns=stat_result.st_mtime_ns,
                csv_sources=sources,
                archive_member_count=archive_members,
            )
        )
        if len(packages) > config["limits"]["max_packages"]:
            raise CandidateBuildError("package count exceeds the reviewed cap")
    sources = [source for package in packages for source in package.csv_sources]
    if len(sources) > config["limits"]["max_csv_sources"]:
        raise CandidateBuildError("CSV source count exceeds the reviewed cap")
    total_unpacked = sum(source.uncompressed_bytes for source in sources)
    if total_unpacked > config["limits"]["max_total_unpacked_bytes"]:
        raise CandidateBuildError("selected unpacked bytes exceed the reviewed cap")
    validate_headers(sources, config)
    return packages


def catalog_path(workspace_root: Path) -> Path:
    path = workspace_root / "data" / "catalog" / "datasets.json"
    if not path.is_file():
        raise CandidateBuildError(f"workspace catalog is missing: {path}")
    return path


def plan_value(
    *,
    workspace_root: Path,
    inbox: Path,
    output_root: Path,
    request_path: Path,
    request: dict[str, Any],
    config: dict[str, Any],
    packages: list[InputPackage],
) -> dict[str, Any]:
    sources = [source for package in packages for source in package.csv_sources]
    target = output_root / config["dataset_id"] / config["run_id"]
    input_bytes = sum(package.bytes for package in packages)
    unpacked_bytes = sum(source.uncompressed_bytes for source in sources)
    return {
        "schema_version": 1,
        "tool_id": TOOL_ID,
        "operation": "PLAN",
        "status": "PLAN_READY",
        "write_performed": False,
        "workspace_root": workspace_root.as_posix(),
        "request_path": request_path.resolve().as_posix(),
        "request_sha256": sha256_bytes(canonical_bytes(request)),
        "inbox": inbox.as_posix(),
        "target_candidate_path": target.as_posix(),
        "dataset_id": config["dataset_id"],
        "run_id": config["run_id"],
        "package_count": len(packages),
        "csv_source_count": len(sources),
        "input_package_bytes": input_bytes,
        "selected_unpacked_csv_bytes": unpacked_bytes,
        "estimated_peak_working_bytes": input_bytes + unpacked_bytes * 2,
        "estimate_note": (
            "upper-bound planning arithmetic only: raw copy plus temporary CSV materialization "
            "plus uncompressed-size allowance for Parquet; not a measured capacity percentile"
        ),
        "packages": [
            {
                "relative_path": package.relative_path,
                "kind": package.kind,
                "bytes": package.bytes,
                "archive_member_count": package.archive_member_count,
                "csv_sources": [asdict(source) for source in package.csv_sources],
            }
            for package in packages
        ],
        "catalog": {
            "path": catalog_path(workspace_root).as_posix(),
            "sha256": sha256_file(catalog_path(workspace_root)),
            "write_authorized": False,
            "will_be_modified": False,
        },
        "execution_allowed_by_request": config[
            "authorized_local_research_download"
        ],
        "hard_boundaries": {
            "downloads_data": False,
            "writes_formal_data_root": False,
            "changes_catalog": False,
            "source_admission": False,
            "quality_promotion": False,
            "bulk_backfill": False,
            "purpose_ceiling": PURPOSE_CEILING,
        },
    }


def copy_and_hash_package(
    package: InputPackage,
    raw_root: Path,
    index: int,
) -> tuple[Path, dict[str, Any]]:
    before = package.path.stat()
    safe_name = package.path.name
    destination = raw_root / f"{index:05d}-{safe_name}"
    digest = hashlib.sha256()
    with package.path.open("rb") as source, destination.open("xb") as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            target.write(chunk)
        target.flush()
        os.fsync(target.fileno())
    after = package.path.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_size != destination.stat().st_size
    ):
        raise CandidateBuildError(
            f"input package changed while being copied: {package.path}"
        )
    return destination, {
        "source_path": package.path.resolve().as_posix(),
        "source_relative_path": package.relative_path,
        "raw_relative_path": destination.relative_to(raw_root.parent).as_posix(),
        "bytes": destination.stat().st_size,
        "sha256": digest.hexdigest(),
        "source_mtime_ns": before.st_mtime_ns,
        "kind": package.kind,
    }


def transcode_csv(
    source: BinaryIO,
    destination: Path,
    *,
    encoding: str,
    delimiter: str,
    expected_header: list[str],
) -> None:
    with io.TextIOWrapper(source, encoding=encoding, errors="strict", newline="") as text:
        reader = csv.reader(text, delimiter=delimiter, strict=True)
        try:
            header = next(reader)
        except (csv.Error, StopIteration) as exc:
            raise CandidateBuildError(f"cannot read CSV header during transcode: {exc}") from exc
        if header != expected_header:
            raise CandidateBuildError("CSV header changed between plan and execution")
        with destination.open("x", encoding="utf-8", newline="") as output:
            writer = csv.writer(output, delimiter=",", lineterminator="\n")
            writer.writerow(header)
            for row_number, row in enumerate(reader, start=2):
                if len(row) != len(header):
                    raise CandidateBuildError(
                        f"CSV row {row_number} has {len(row)} fields; expected {len(header)}"
                    )
                writer.writerow(row)


def materialize_csv_sources(
    *,
    packages: list[InputPackage],
    copied: dict[str, Path],
    temp_root: Path,
) -> list[MaterializedCsv]:
    result: list[MaterializedCsv] = []
    index = 0
    for package in packages:
        raw_path = copied[package.relative_path]
        if package.kind == "csv":
            source = package.csv_sources[0]
            if source.encoding in {"utf-8", "utf-8-sig"}:
                materialized = raw_path
                delimiter = source.delimiter
            else:
                materialized = temp_root / f"{index:06d}.csv"
                with raw_path.open("rb") as stream:
                    transcode_csv(
                        stream,
                        materialized,
                        encoding=source.encoding,
                        delimiter=source.delimiter,
                        expected_header=source.header,
                    )
                delimiter = ","
            result.append(
                MaterializedCsv(
                    path=materialized,
                    source_ref=source.source_ref,
                    delimiter=delimiter,
                    header=source.header,
                )
            )
            index += 1
            continue

        with zipfile.ZipFile(raw_path) as archive:
            info_by_name = {info.filename.replace("\\", "/"): info for info in archive.infolist()}
            for source in package.csv_sources:
                assert source.member_name is not None
                info = info_by_name.get(source.member_name)
                if info is None:
                    raise CandidateBuildError(
                        f"ZIP member disappeared during execution: {source.source_ref}"
                    )
                materialized = temp_root / f"{index:06d}.csv"
                with archive.open(info, "r") as stream:
                    if source.encoding in {"utf-8", "utf-8-sig"}:
                        with materialized.open("xb") as output:
                            shutil.copyfileobj(stream, output, length=1024 * 1024)
                    else:
                        transcode_csv(
                            stream,
                            materialized,
                            encoding=source.encoding,
                            delimiter=source.delimiter,
                            expected_header=source.header,
                        )
                result.append(
                    MaterializedCsv(
                        path=materialized,
                        source_ref=source.source_ref,
                        delimiter=source.delimiter
                        if source.encoding in {"utf-8", "utf-8-sig"}
                        else ",",
                        header=source.header,
                    )
                )
                index += 1
    return result


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def import_duckdb() -> Any:
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise CandidateBuildError(
            "DuckDB is required for --execute. Build and verify the locked "
            "control/runtime/data-engineering runtime first."
        ) from exc
    return duckdb


def inventory_file(path: Path, root: Path, *, layer: str) -> dict[str, Any]:
    return {
        "layer": layer,
        "relative_path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def build_parquet(
    *,
    materialized: list[MaterializedCsv],
    stage_root: Path,
    final_root: Path,
    config: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    duckdb = import_duckdb()
    table = config["table"]
    headers = materialized[0].header
    connection = duckdb.connect(database=":memory:")
    try:
        selects: list[str] = []
        for item in materialized:
            path = item.path.resolve().as_posix()
            columns = ", ".join(sql_identifier(column) for column in headers)
            selects.append(
                "SELECT "
                f"{columns}, {sql_string(item.source_ref)} AS "
                f"{sql_identifier(RESERVED_LINEAGE_COLUMN)} "
                f"FROM read_csv({sql_string(path)}, header=true, "
                f"all_varchar=true, delim={sql_string(item.delimiter)})"
            )
        connection.execute("CREATE TABLE _raw AS " + " UNION ALL ".join(selects))

        typed_expressions: list[str] = []
        expected_set = set(table["expected_columns"])
        for column in headers:
            source = sql_identifier(column)
            if column in expected_set:
                data_type = table["column_types"][column]
                if data_type == "VARCHAR":
                    expression = source
                else:
                    expression = (
                        f"TRY_CAST(NULLIF(TRIM({source}), '') AS {data_type})"
                    )
            else:
                expression = source
            typed_expressions.append(f"{expression} AS {source}")
        typed_expressions.append(sql_identifier(RESERVED_LINEAGE_COLUMN))
        connection.execute(
            "CREATE TABLE _typed AS SELECT "
            + ", ".join(typed_expressions)
            + " FROM _raw"
        )

        row_count = int(connection.execute("SELECT COUNT(*) FROM _typed").fetchone()[0])
        if row_count <= 0:
            raise CandidateBuildError("selected CSV inputs contain no data rows")
        if row_count > config["limits"]["max_rows"]:
            raise CandidateBuildError("row count exceeds the reviewed cap")

        cast_failures: dict[str, int] = {}
        for column, data_type in table["column_types"].items():
            if data_type == "VARCHAR":
                cast_failures[column] = 0
                continue
            ident = sql_identifier(column)
            count = connection.execute(
                "SELECT COUNT(*) FROM _raw "
                f"WHERE {ident} IS NOT NULL AND TRIM({ident}) <> '' "
                f"AND TRY_CAST(TRIM({ident}) AS {data_type}) IS NULL"
            ).fetchone()[0]
            cast_failures[column] = int(count)
        failed_casts = {key: value for key, value in cast_failures.items() if value}
        if failed_casts:
            raise CandidateBuildError(
                f"type conversion failures detected: {json.dumps(failed_casts, sort_keys=True)}"
            )

        required_nulls: dict[str, int] = {}
        for column in table["required_columns"]:
            ident = sql_identifier(column)
            count = connection.execute(
                f"SELECT COUNT(*) FROM _typed WHERE {ident} IS NULL"
            ).fetchone()[0]
            required_nulls[column] = int(count)
        failed_required = {key: value for key, value in required_nulls.items() if value}
        if failed_required:
            raise CandidateBuildError(
                f"required-column nulls detected: {json.dumps(failed_required, sort_keys=True)}"
            )

        key_sql = ", ".join(sql_identifier(column) for column in table["key_columns"])
        duplicate = connection.execute(
            "SELECT COALESCE(SUM(c - 1), 0), COUNT(*) FROM "
            f"(SELECT {key_sql}, COUNT(*) AS c FROM _typed "
            f"GROUP BY {key_sql} HAVING COUNT(*) > 1)"
        ).fetchone()
        duplicate_rows = int(duplicate[0])
        duplicate_groups = int(duplicate[1])
        if duplicate_rows:
            raise CandidateBuildError(
                f"duplicate business keys detected: {duplicate_groups} groups, "
                f"{duplicate_rows} duplicate rows"
            )

        if table["date_column"] is None:
            date_ident = None
            date_min = None
            date_max = None
        else:
            date_ident = sql_identifier(table["date_column"])
            date_min, date_max = connection.execute(
                f"SELECT MIN({date_ident}), MAX({date_ident}) FROM _typed"
            ).fetchone()
        partition_records: list[dict[str, Any]] = []
        order_sql = ", ".join(
            sql_identifier(column) for column in table["key_columns"]
        )
        if table["partition_mode"] == "year_month":
            assert date_ident is not None
            partitions = connection.execute(
                f"SELECT YEAR({date_ident}), MONTH({date_ident}), COUNT(*) "
                f"FROM _typed GROUP BY 1, 2 ORDER BY 1, 2"
            ).fetchall()
            for year, month, count in partitions:
                relative = (
                    Path("curated")
                    / f"schema={config['dataset_schema_version']}"
                    / f"year={int(year):04d}"
                    / f"month={int(month):02d}"
                    / "part-000.parquet"
                )
                destination = stage_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                connection.execute(
                    "COPY (SELECT * FROM _typed "
                    f"WHERE YEAR({date_ident}) = {int(year)} "
                    f"AND MONTH({date_ident}) = {int(month)} "
                    f"ORDER BY {order_sql}) "
                    f"TO {sql_string(destination.resolve().as_posix())} "
                    "(FORMAT PARQUET, COMPRESSION ZSTD)"
                )
                written_rows = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM read_parquet("
                        f"{sql_string(destination.resolve().as_posix())})"
                    ).fetchone()[0]
                )
                if written_rows != int(count):
                    raise CandidateBuildError(
                        f"Parquet row-count mismatch for {relative.as_posix()}"
                    )
                partition_records.append(
                    {
                        "year": int(year),
                        "month": int(month),
                        "row_count": written_rows,
                        "relative_path": relative.as_posix(),
                        "bytes": destination.stat().st_size,
                        "sha256": sha256_file(destination),
                    }
                )
            final_glob = (
                final_root
                / "curated"
                / f"schema={config['dataset_schema_version']}"
                / "year=*"
                / "month=*"
                / "*.parquet"
            ).as_posix()
            hive_partitioning = "true"
        else:
            relative = (
                Path("curated")
                / f"schema={config['dataset_schema_version']}"
                / "part-000.parquet"
            )
            destination = stage_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            connection.execute(
                f"COPY (SELECT * FROM _typed ORDER BY {order_sql}) "
                f"TO {sql_string(destination.resolve().as_posix())} "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            written_rows = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM read_parquet("
                    f"{sql_string(destination.resolve().as_posix())})"
                ).fetchone()[0]
            )
            if written_rows != row_count:
                raise CandidateBuildError(
                    f"Parquet row-count mismatch for {relative.as_posix()}"
                )
            partition_records.append(
                {
                    "partition": "all",
                    "row_count": written_rows,
                    "relative_path": relative.as_posix(),
                    "bytes": destination.stat().st_size,
                    "sha256": sha256_file(destination),
                }
            )
            final_glob = (
                final_root
                / "curated"
                / f"schema={config['dataset_schema_version']}"
                / "*.parquet"
            ).as_posix()
            hive_partitioning = "false"
        query_sql = stage_root / "query.sql"
        query_sql.write_text(
            f"-- Candidate only; not catalog-admitted or research-ready.\n"
            f"CREATE OR REPLACE VIEW {sql_identifier(table['table_name'])} AS\n"
            f"SELECT * FROM read_parquet({sql_string(final_glob)}, "
            f"hive_partitioning={hive_partitioning}, union_by_name=false);\n",
            encoding="utf-8",
            newline="\n",
        )
        quality = {
            "row_count": row_count,
            "column_count": len(headers) + 1,
            "columns": headers + [RESERVED_LINEAGE_COLUMN],
            "cast_failures": cast_failures,
            "required_null_counts": required_nulls,
            "duplicate_key_groups": duplicate_groups,
            "duplicate_rows": duplicate_rows,
            "date_min": date_min.isoformat() if date_min is not None else None,
            "date_max": date_max.isoformat() if date_max is not None else None,
            "partition_count": len(partition_records),
            "mechanical_checks_passed": True,
        }
        return quality, partition_records
    except CandidateBuildError:
        raise
    except Exception as exc:
        raise CandidateBuildError(f"DuckDB build failed: {exc}") from exc
    finally:
        connection.close()


def candidate_contract_hash(
    *,
    request: dict[str, Any],
    manifest: dict[str, Any],
    provenance: dict[str, Any],
    partition_manifest: dict[str, Any],
    quality: dict[str, Any],
) -> str:
    manifest_value = dict(manifest)
    manifest_value.pop("content_hash", None)
    return sha256_bytes(
        canonical_bytes(
            {
                "hash_algorithm": HASH_ALGORITHM,
                "request": request,
                "manifest": manifest_value,
                "provenance": provenance,
                "partition_manifest": partition_manifest,
                "quality": quality,
            }
        )
    )


def report_text(
    *,
    config: dict[str, Any],
    final_root: Path,
    manifest: dict[str, Any],
    quality: dict[str, Any],
) -> str:
    return f"""# Local downloaded dataset candidate report

- Dataset: `{config["dataset_id"]}`
- Run: `{config["run_id"]}`
- Candidate path: `{final_root.as_posix()}`
- Mechanical validation: `{quality["mechanical_status"]}`
- Rows: `{quality["metrics"]["row_count"]}`
- Date range: `{quality["metrics"]["date_min"]}` to `{quality["metrics"]["date_max"]}`
- Parquet partitions: `{quality["metrics"]["partition_count"]}`
- Content hash: `{manifest["content_hash"]}`

## Mandatory boundary

This output is an `INSPECTION_ONLY` candidate, not a formal database snapshot.
The tool did not download data, change `data/catalog/datasets.json`, admit a
source, validate PIT or field semantics, or promote quality. A mechanical PASS
does not replace those formal gates.

## Local query

Run `query.sql` with DuckDB to create the
`{config["table"]["table_name"]}` view. The query is pinned to this explicit
candidate path. Do not treat this candidate as `latest` or bypass qdata/catalog
for research use.

## Next gate

Formal shared-database use still requires source-rights evidence, field and unit
semantics, PIT and revision-chain validation, coverage and cross-source checks,
approved atomic publication, and catalog registration.
"""


def execute_build(
    *,
    workspace_root: Path,
    inbox: Path,
    output_root: Path,
    request_path: Path,
    request: dict[str, Any],
    config: dict[str, Any],
    packages: list[InputPackage],
) -> dict[str, Any]:
    duckdb = import_duckdb()
    del duckdb
    final_root = output_root / config["dataset_id"] / config["run_id"]
    if final_root.exists():
        raise CandidateBuildError(f"refusing to overwrite candidate: {final_root}")
    parent = final_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    reject_reparse_ancestors(parent, stop=workspace_root)
    stage_root = parent / f".{config['run_id']}.partial-{uuid.uuid4().hex}"
    if stage_root.exists():
        raise CandidateBuildError(f"unexpected staging collision: {stage_root}")

    catalog = catalog_path(workspace_root)
    catalog_hash_before = sha256_file(catalog)
    created_stage = False
    try:
        stage_root.mkdir()
        created_stage = True
        raw_root = stage_root / "raw"
        raw_root.mkdir()
        copied_paths: dict[str, Path] = {}
        raw_inventory: list[dict[str, Any]] = []
        input_provenance: list[dict[str, Any]] = []
        for index, package in enumerate(packages):
            copied_path, record = copy_and_hash_package(package, raw_root, index)
            copied_paths[package.relative_path] = copied_path
            raw_inventory.append(inventory_file(copied_path, stage_root, layer="raw"))
            record["csv_sources"] = [asdict(source) for source in package.csv_sources]
            input_provenance.append(record)

        with tempfile.TemporaryDirectory(
            prefix="qdata-local-csv-", dir=stage_root
        ) as temp_dir:
            materialized = materialize_csv_sources(
                packages=packages,
                copied=copied_paths,
                temp_root=Path(temp_dir),
            )
            metrics, partitions = build_parquet(
                materialized=materialized,
                stage_root=stage_root,
                final_root=final_root,
                config=config,
            )

        if sha256_file(catalog) != catalog_hash_before:
            raise CandidateBuildError(
                "datasets.json changed concurrently; refusing to seal a candidate "
                "against an unstable catalog fingerprint"
            )
        curated_inventory = [
            inventory_file(stage_root / item["relative_path"], stage_root, layer="curated")
            for item in partitions
        ]
        query_inventory = [
            inventory_file(stage_root / "query.sql", stage_root, layer="query")
        ]
        candidate_id = f"{config['dataset_id']}@{config['run_id']}"
        provenance = {
            "schema_version": 1,
            "candidate_id": candidate_id,
            "dataset_id": config["dataset_id"],
            "run_id": config["run_id"],
            "source_id": config["source_id"],
            "tool_id": TOOL_ID,
            "created_at": utc_now(),
            "input_root": inbox.as_posix(),
            "source_access_note": config["source_access_note"],
            "authorization_basis": "USER_ASSERTED_LOCAL_RESEARCH_DOWNLOAD",
            "authorization_independently_verified": False,
            "input_packages": input_provenance,
            "transformations": [
                "copy original downloaded package bytes into candidate raw/",
                "decode UTF-8 directly; transcode declared/detected GB18030 or UTF-16 CSV to UTF-8 staging",
                "strict single-table header gate",
                "explicit DuckDB TRY_CAST using request column_types",
                "required-null and unique-business-key gates",
                f"{config['table']['partition_mode']} Parquet partitioning with ZSTD compression",
            ],
            "catalog_guard": {
                "path": catalog.as_posix(),
                "sha256_before": catalog_hash_before,
                "sha256_preseal": sha256_file(catalog),
                "write_attempted": False,
            },
        }
        partition_manifest = {
            "schema_version": 1,
            "candidate_id": candidate_id,
            "dataset_id": config["dataset_id"],
            "run_id": config["run_id"],
            "partition_scheme": (
                ["year", "month"]
                if config["table"]["partition_mode"] == "year_month"
                else ["single"]
            ),
            "date_column": config["table"]["date_column"],
            "partitions": partitions,
            "total_rows": metrics["row_count"],
        }
        quality = {
            "schema_version": 1,
            "candidate_id": candidate_id,
            "dataset_id": config["dataset_id"],
            "run_id": config["run_id"],
            "mechanical_status": "PASS",
            "admission_decision": "NOT_ADMITTED",
            "quality_status": "REVIEW",
            "purpose_ceiling": PURPOSE_CEILING,
            "source_admission": False,
            "catalog_registered": False,
            "quality_promotion": False,
            "research_ready": False,
            "metrics": metrics,
            "unresolved_blockers": [
                "source rights and retention scope are user-asserted, not independently admitted",
                "field meaning, unit, code mapping and corporate-action semantics are not formally admitted",
                "PIT availability, revision chain and as-of behavior are not validated",
                "coverage, cross-source conflicts and formal quality thresholds are not evaluated",
                "candidate is absent from data/catalog/datasets.json",
            ],
        }
        manifest = {
            "schema_version": 1,
            "candidate_id": candidate_id,
            "dataset_id": config["dataset_id"],
            "run_id": config["run_id"],
            "dataset_schema_version": config["dataset_schema_version"],
            "root_path": final_root.as_posix(),
            "hash_algorithm": HASH_ALGORITHM,
            "content_hash": None,
            "lifecycle": "candidate",
            "mutability": "immutable_after_seal",
            "purpose_ceiling": PURPOSE_CEILING,
            "catalog_registered": False,
            "source_admission": False,
            "research_ready": False,
            "payload_inventory": raw_inventory + curated_inventory + query_inventory,
            "authority_paths": [
                "request.json",
                "manifest.json",
                "provenance.json",
                "partition-manifest.json",
                "quality/summary.json",
            ],
        }
        manifest["content_hash"] = candidate_contract_hash(
            request=request,
            manifest=manifest,
            provenance=provenance,
            partition_manifest=partition_manifest,
            quality=quality,
        )
        write_json(stage_root / "request.json", request)
        write_json(stage_root / "manifest.json", manifest)
        write_json(stage_root / "provenance.json", provenance)
        write_json(stage_root / "partition-manifest.json", partition_manifest)
        write_json(stage_root / "quality" / "summary.json", quality)
        (stage_root / "REPORT.md").write_text(
            report_text(
                config=config,
                final_root=final_root,
                manifest=manifest,
                quality=quality,
            ),
            encoding="utf-8",
            newline="\n",
        )

        for entry in manifest["payload_inventory"]:
            payload = stage_root / entry["relative_path"]
            if (
                not payload.is_file()
                or payload.stat().st_size != entry["bytes"]
                or sha256_file(payload) != entry["sha256"]
            ):
                raise CandidateBuildError(
                    f"payload verification failed before seal: {entry['relative_path']}"
                )
        if sha256_file(catalog) != catalog_hash_before:
            raise CandidateBuildError(
                "datasets.json changed before candidate seal; no candidate committed"
            )
        os.replace(stage_root, final_root)
        created_stage = False
        return {
            "schema_version": 1,
            "tool_id": TOOL_ID,
            "operation": "EXECUTE",
            "status": "CANDIDATE_BUILT",
            "dataset_id": config["dataset_id"],
            "run_id": config["run_id"],
            "candidate_path": final_root.as_posix(),
            "content_hash": manifest["content_hash"],
            "row_count": metrics["row_count"],
            "partition_count": metrics["partition_count"],
            "purpose_ceiling": PURPOSE_CEILING,
            "source_admission": False,
            "catalog_registered": False,
            "quality_promotion": False,
            "catalog_sha256_before": catalog_hash_before,
            "catalog_sha256_after": sha256_file(catalog),
        }
    finally:
        if created_stage and stage_root.exists():
            if not resolved_under(stage_root, output_root):
                raise CandidateBuildError(
                    f"refusing unsafe staging cleanup outside output root: {stage_root}"
                )
            shutil.rmtree(stage_root)


def run(
    *,
    workspace_root: Path,
    request_path: Path,
    inbox: Path,
    output_root: Path | None,
    execute: bool,
) -> dict[str, Any]:
    workspace_root = workspace_root.resolve()
    if not workspace_root.is_dir():
        raise CandidateBuildError(f"workspace root is not a directory: {workspace_root}")
    request_path = request_path.resolve()
    request = strict_json(request_path)
    config = validate_request(request, for_execute=execute)
    inbox = validate_input_root(inbox)
    if output_root is None:
        output_root = workspace_root / "tmp" / "local-dataset-build"
    output_root = validate_output_root(output_root, workspace_root)
    packages = discover_packages(inbox, config)
    plan = plan_value(
        workspace_root=workspace_root,
        inbox=inbox,
        output_root=output_root,
        request_path=request_path,
        request=request,
        config=config,
        packages=packages,
    )
    if not execute:
        return plan
    return execute_build(
        workspace_root=workspace_root,
        inbox=inbox,
        output_root=output_root,
        request_path=request_path,
        request=request,
        config=config,
        packages=packages,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, default=WORKSPACE_ROOT)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Build an isolated tmp candidate. Without this flag the command is write-free.",
    )
    args = parser.parse_args()
    try:
        result = run(
            workspace_root=args.workspace_root,
            request_path=args.request,
            inbox=args.inbox,
            output_root=args.output_root,
            execute=args.execute,
        )
        return_code = 0
    except (CandidateBuildError, OSError, csv.Error, zipfile.BadZipFile) as exc:
        result = {
            "schema_version": 1,
            "tool_id": TOOL_ID,
            "operation": "EXECUTE" if args.execute else "PLAN",
            "status": "FAIL",
            "write_to_formal_data_attempted": False,
            "catalog_write_attempted": False,
            "source_admission": False,
            "quality_promotion": False,
            "errors": [str(exc)],
        }
        return_code = 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
