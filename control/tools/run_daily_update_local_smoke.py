#!/usr/bin/env python3
"""Build an inspection-only local daily incremental candidate.

The tool intentionally has no network client, credential handling, scheduler,
formal catalog writer, source admission or quality-promotion path.  It accepts
already normalized UTF-8 CSV/JSONL deltas, optionally merges a previously
sealed candidate from the same engine, validates schema/key/date/revision
semantics, writes Parquet to a sibling staging directory and seals the
candidate with ``os.replace``.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "data" / "catalog" / "contracts" / "local_daily_update_v1"
REQUEST_SCHEMA_PATH = CONTRACT_ROOT / "request.schema.json"
FORMAL_CATALOG_REL = Path("data/catalog/datasets.json")
DEFAULT_OUTPUT_REL = Path("tmp/local-daily-update")

ENGINE_ID = "run_daily_update_local_smoke.py/local-daily-update-candidate-v1"
HASH_ALGORITHM = "sha256-local-daily-update-candidate-v1"
PURPOSE_CEILING = "INSPECTION_ONLY"
MODE = "local_daily_update_candidate"
SUPPORTED_SCOPES = {"synthetic_fixture", "registered_inspection"}
SUPPORTED_TYPES = {
    "VARCHAR",
    "DATE",
    "TIMESTAMP_TZ",
    "DOUBLE",
    "BIGINT",
    "BOOLEAN",
}
COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}$")
TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
INTEGER_RE = re.compile(r"^[+-]?\d+$")
ALLOWED_SUFFIXES = {".csv", ".jsonl"}
CANDIDATE_AUTHORITY_PATHS = (
    "request.json",
    "manifest.json",
    "provenance.json",
    "partition-manifest.json",
    "quality/summary.json",
    "receipts/update.json",
)


class DailyUpdateError(RuntimeError):
    """Fail-closed local update error."""

    def __init__(self, message: str, *, code: str = "LOCAL_UPDATE_REJECTED"):
        super().__init__(message)
        self.code = code


class InjectedFailure(DailyUpdateError):
    """Synthetic test-only failure after staging has started."""


@dataclass(frozen=True)
class InputFile:
    path: Path
    relative_path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class UpdateResult:
    status: str
    dataset_id: str
    run_id: str
    candidate_path: str | None
    quarantine_path: str | None
    request_fingerprint: str | None
    row_count: int | None
    new_row_count: int | None
    duplicate_row_count: int | None
    partition_count: int | None
    new_partition_count: int | None
    purpose_ceiling: str = PURPOSE_CEILING
    source_admission: bool = False
    catalog_registered: bool = False
    quality_promotion: bool = False
    research_ready: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "engine_id": ENGINE_ID,
            "status": self.status,
            "dataset_id": self.dataset_id,
            "run_id": self.run_id,
            "candidate_path": self.candidate_path,
            "quarantine_path": self.quarantine_path,
            "request_fingerprint": self.request_fingerprint,
            "row_count": self.row_count,
            "new_row_count": self.new_row_count,
            "duplicate_row_count": self.duplicate_row_count,
            "partition_count": self.partition_count,
            "new_partition_count": self.new_partition_count,
            "purpose_ceiling": self.purpose_ceiling,
            "source_admission": self.source_admission,
            "catalog_registered": self.catalog_registered,
            "quality_promotion": self.quality_promotion,
            "research_ready": self.research_ready,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DailyUpdateError(f"invalid JSON file {path}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.write_text(payload, encoding="utf-8", newline="\n")


def is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def has_reparse_flag(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise DailyUpdateError(f"cannot stat path {path}: {exc}") from exc
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse)


def reject_reparse_tree(root: Path) -> None:
    if has_reparse_flag(root):
        raise DailyUpdateError(f"reparse/symlink input root is forbidden: {root}")
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directory_names + file_names:
            candidate = current_path / name
            if has_reparse_flag(candidate):
                raise DailyUpdateError(
                    f"reparse/symlink entry is forbidden: {candidate}"
                )


def validate_component(value: Any, label: str) -> str:
    if not isinstance(value, str) or COMPONENT_RE.fullmatch(value) is None:
        raise DailyUpdateError(f"{label} is not a safe component")
    return value


def require_string_list(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise DailyUpdateError(f"{label} must be a unique non-empty string array")
    return value


def strict_request(path: Path) -> dict[str, Any]:
    request = read_json(path)
    if not isinstance(request, dict):
        raise DailyUpdateError("request must be a JSON object")
    allowed = {
        "schema_version",
        "mode",
        "dataset_scope",
        "dataset_id",
        "run_id",
        "source_id",
        "dataset_schema_version",
        "authorization_note",
        "include_patterns",
        "base_candidate",
        "table",
        "limits",
    }
    required = allowed
    missing = sorted(required - set(request))
    unsupported = sorted(set(request) - allowed)
    if missing or unsupported:
        raise DailyUpdateError(
            "request field contract mismatch; "
            f"missing={missing}, unsupported={unsupported}"
        )
    if request["schema_version"] != 1:
        raise DailyUpdateError("request.schema_version must be 1")
    if request["mode"] != MODE:
        raise DailyUpdateError(f"request.mode must be {MODE}")
    if request["dataset_scope"] not in SUPPORTED_SCOPES:
        raise DailyUpdateError(
            f"request.dataset_scope must be one of {sorted(SUPPORTED_SCOPES)}"
        )
    for field in ("dataset_id", "run_id", "source_id", "dataset_schema_version"):
        validate_component(request[field], f"request.{field}")
    if (
        not isinstance(request["authorization_note"], str)
        or not request["authorization_note"].strip()
    ):
        raise DailyUpdateError("request.authorization_note must be non-empty")
    require_string_list(request["include_patterns"], "request.include_patterns")
    base = request["base_candidate"]
    if base is not None and (not isinstance(base, str) or not base.strip()):
        raise DailyUpdateError("request.base_candidate must be null or non-empty")
    validate_table(request["table"])
    validate_limits(request["limits"])
    if request["dataset_scope"] == "synthetic_fixture":
        source = request["source_id"].upper()
        if "SYNTHETIC" not in source and "FIXTURE" not in source:
            raise DailyUpdateError(
                "synthetic_fixture requires a SYNTHETIC/FIXTURE source_id"
            )
    return request


def validate_table(table: Any) -> dict[str, Any]:
    if not isinstance(table, dict):
        raise DailyUpdateError("request.table must be an object")
    allowed = {
        "table_name",
        "expected_columns",
        "column_types",
        "date_column",
        "primary_key_columns",
        "required_columns",
        "delimiter",
        "revision",
    }
    missing = sorted(allowed - set(table))
    unsupported = sorted(set(table) - allowed)
    if missing or unsupported:
        raise DailyUpdateError(
            "table field contract mismatch; "
            f"missing={missing}, unsupported={unsupported}"
        )
    if not isinstance(table["table_name"], str) or TABLE_RE.fullmatch(
        table["table_name"]
    ) is None:
        raise DailyUpdateError("table.table_name is invalid")
    columns = require_string_list(
        table["expected_columns"], "table.expected_columns"
    )
    column_set = set(columns)
    types = table["column_types"]
    if (
        not isinstance(types, dict)
        or set(types) != column_set
        or any(value not in SUPPORTED_TYPES for value in types.values())
    ):
        raise DailyUpdateError(
            "table.column_types must exactly cover expected_columns with supported types"
        )
    date_column = table["date_column"]
    if date_column not in column_set or types[date_column] != "DATE":
        raise DailyUpdateError(
            "table.date_column must be an expected DATE column"
        )
    primary = require_string_list(
        table["primary_key_columns"], "table.primary_key_columns"
    )
    required = require_string_list(
        table["required_columns"], "table.required_columns"
    )
    outside = sorted((set(primary) | set(required)) - column_set)
    if outside:
        raise DailyUpdateError(
            "table key/required columns outside expected_columns: "
            + ", ".join(outside)
        )
    if not set(primary) <= set(required):
        raise DailyUpdateError(
            "table.primary_key_columns must be required columns"
        )
    if table["delimiter"] not in {",", "\t", ";", "|"}:
        raise DailyUpdateError("table.delimiter is unsupported")
    revision = table["revision"]
    if revision is not None:
        validate_revision(revision, table)
    return table


def validate_revision(revision: Any, table: dict[str, Any]) -> None:
    if not isinstance(revision, dict):
        raise DailyUpdateError("table.revision must be null or an object")
    allowed = {
        "business_key_columns",
        "revision_column",
        "available_at_column",
        "policy",
    }
    missing = sorted(allowed - set(revision))
    unsupported = sorted(set(revision) - allowed)
    if missing or unsupported:
        raise DailyUpdateError(
            "revision field contract mismatch; "
            f"missing={missing}, unsupported={unsupported}"
        )
    business = require_string_list(
        revision["business_key_columns"],
        "table.revision.business_key_columns",
    )
    columns = set(table["expected_columns"])
    if not set(business) <= columns:
        raise DailyUpdateError("revision business keys are outside schema")
    revision_column = revision["revision_column"]
    available_column = revision["available_at_column"]
    if (
        revision_column not in columns
        or table["column_types"][revision_column] != "BIGINT"
        or revision_column not in table["primary_key_columns"]
    ):
        raise DailyUpdateError(
            "revision_column must be a BIGINT primary-key column"
        )
    if (
        available_column not in columns
        or table["column_types"][available_column] != "TIMESTAMP_TZ"
        or available_column not in table["required_columns"]
    ):
        raise DailyUpdateError(
            "available_at_column must be a required TIMESTAMP_TZ column"
        )
    if revision["policy"] != "APPEND_ONLY_STRICT":
        raise DailyUpdateError("only APPEND_ONLY_STRICT revision policy is allowed")


def validate_limits(limits: Any) -> dict[str, int]:
    if not isinstance(limits, dict):
        raise DailyUpdateError("request.limits must be an object")
    allowed = {"max_files", "max_total_input_bytes", "max_rows"}
    missing = sorted(allowed - set(limits))
    unsupported = sorted(set(limits) - allowed)
    if missing or unsupported:
        raise DailyUpdateError(
            "limits field contract mismatch; "
            f"missing={missing}, unsupported={unsupported}"
        )
    for field in sorted(allowed):
        value = limits[field]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise DailyUpdateError(f"limits.{field} must be a positive integer")
    return limits


def validate_workspace(workspace_root: Path) -> tuple[Path, Path, str, dict[str, Any]]:
    workspace_root = workspace_root.resolve()
    if not workspace_root.is_dir():
        raise DailyUpdateError(f"workspace root is not a directory: {workspace_root}")
    catalog_path = workspace_root / FORMAL_CATALOG_REL
    if not catalog_path.is_file():
        raise DailyUpdateError(f"formal catalog is missing: {catalog_path}")
    catalog_hash = sha256_file(catalog_path)
    catalog = read_json(catalog_path)
    if not isinstance(catalog, dict) or not isinstance(catalog.get("datasets"), list):
        raise DailyUpdateError("formal catalog shape is invalid")
    return workspace_root, catalog_path, catalog_hash, catalog


def validate_dataset_scope(request: dict[str, Any], catalog: dict[str, Any]) -> None:
    if request["dataset_scope"] != "registered_inspection":
        return
    matches = [
        item
        for item in catalog["datasets"]
        if isinstance(item, dict) and item.get("dataset_id") == request["dataset_id"]
    ]
    if len(matches) != 1:
        raise DailyUpdateError(
            "registered_inspection requires exactly one formal catalog dataset identity",
            code="REGISTERED_DATASET_IDENTITY_NOT_FOUND",
        )


def resolve_output_root(workspace_root: Path, output_root: Path | None) -> Path:
    target = (
        (workspace_root / DEFAULT_OUTPUT_REL)
        if output_root is None
        else output_root.resolve()
    )
    if not is_under(target, workspace_root):
        raise DailyUpdateError("output root must remain below workspace root")
    target_relative = target.resolve().relative_to(workspace_root)
    if not target_relative.parts or target_relative.parts[0].lower() != "tmp":
        raise DailyUpdateError("output root must remain below workspace tmp/")
    return target


def validate_inbox(workspace_root: Path, inbox: Path) -> Path:
    inbox = inbox.resolve()
    if not inbox.is_dir():
        raise DailyUpdateError(f"inbox is not a directory: {inbox}")
    if not is_under(inbox, workspace_root):
        raise DailyUpdateError("inbox must remain below workspace root")
    reject_reparse_tree(inbox)
    return inbox


def discover_inputs(
    inbox: Path,
    patterns: Sequence[str],
    limits: dict[str, int],
) -> list[InputFile]:
    discovered: dict[str, Path] = {}
    for pattern in patterns:
        for path in inbox.glob(pattern):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if not is_under(resolved, inbox):
                raise DailyUpdateError(f"discovered input escapes inbox: {path}")
            relative = resolved.relative_to(inbox).as_posix()
            discovered[relative.casefold()] = resolved
    files: list[InputFile] = []
    for _, path in sorted(discovered.items()):
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            raise DailyUpdateError(f"unsupported input suffix: {path}")
        relative = path.relative_to(inbox).as_posix()
        files.append(
            InputFile(
                path=path,
                relative_path=relative,
                bytes=path.stat().st_size,
                sha256=sha256_file(path),
            )
        )
    if not files:
        raise DailyUpdateError("no matching CSV/JSONL inputs discovered")
    if len(files) > limits["max_files"]:
        raise DailyUpdateError(
            f"input file count {len(files)} exceeds {limits['max_files']}"
        )
    total_bytes = sum(item.bytes for item in files)
    if total_bytes > limits["max_total_input_bytes"]:
        raise DailyUpdateError(
            f"input bytes {total_bytes} exceed {limits['max_total_input_bytes']}"
        )
    return files


def parse_datetime_tz(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise DailyUpdateError(f"{label} must be an RFC3339 timestamp")
    text = value.strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise DailyUpdateError(f"{label} is not a valid timestamp: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DailyUpdateError(f"{label} must include a timezone offset")
    return parsed


def cast_value(value: Any, expected_type: str, label: str) -> Any:
    if value is None or value == "":
        return None
    if expected_type == "VARCHAR":
        if not isinstance(value, str):
            value = str(value)
        return value
    if expected_type == "DATE":
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if not isinstance(value, str):
            raise DailyUpdateError(f"{label} must be an ISO date")
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise DailyUpdateError(f"{label} is not a valid ISO date: {value}") from exc
    if expected_type == "TIMESTAMP_TZ":
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise DailyUpdateError(f"{label} must be timezone-aware")
            return value
        return parse_datetime_tz(value, label)
    if expected_type == "DOUBLE":
        if isinstance(value, bool):
            raise DailyUpdateError(f"{label} must be a finite number")
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise DailyUpdateError(f"{label} must be a finite number") from exc
        if not math.isfinite(result):
            raise DailyUpdateError(f"{label} must be a finite number")
        return result
    if expected_type == "BIGINT":
        if isinstance(value, bool):
            raise DailyUpdateError(f"{label} must be an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and INTEGER_RE.fullmatch(value.strip()):
            return int(value.strip())
        raise DailyUpdateError(f"{label} must be an integer")
    if expected_type == "BOOLEAN":
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1"}:
                return True
            if normalized in {"false", "0"}:
                return False
        raise DailyUpdateError(f"{label} must be boolean")
    raise DailyUpdateError(f"unsupported type {expected_type}")


def normalize_record(
    raw: Any,
    *,
    table: dict[str, Any],
    source: str,
    row_number: int,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DailyUpdateError(f"{source} row {row_number} is not an object")
    expected = table["expected_columns"]
    expected_set = set(expected)
    missing = sorted(expected_set - set(raw))
    extra = sorted(set(raw) - expected_set)
    if missing or extra:
        raise DailyUpdateError(
            f"{source} row {row_number} schema mismatch; "
            f"missing={missing}, extra={extra}",
            code="BUSINESS_SCHEMA_DRIFT",
        )
    normalized = {
        field: cast_value(
            raw[field],
            table["column_types"][field],
            f"{source} row {row_number} field {field}",
        )
        for field in expected
    }
    null_required = [
        field for field in table["required_columns"] if normalized[field] is None
    ]
    if null_required:
        raise DailyUpdateError(
            f"{source} row {row_number} has null required fields: {null_required}",
            code="REQUIRED_VALUE_MISSING",
        )
    return normalized


def iter_input_rows(
    input_file: InputFile,
    table: dict[str, Any],
) -> Iterable[dict[str, Any]]:
    if input_file.path.suffix.lower() == ".csv":
        try:
            with input_file.path.open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                reader = csv.DictReader(handle, delimiter=table["delimiter"])
                if reader.fieldnames != table["expected_columns"]:
                    raise DailyUpdateError(
                        f"{input_file.relative_path} CSV header mismatch; "
                        f"expected={table['expected_columns']}, actual={reader.fieldnames}",
                        code="BUSINESS_SCHEMA_DRIFT",
                    )
                for row_number, row in enumerate(reader, start=2):
                    yield normalize_record(
                        row,
                        table=table,
                        source=input_file.relative_path,
                        row_number=row_number,
                    )
        except UnicodeError as exc:
            raise DailyUpdateError(
                f"{input_file.relative_path} is not valid UTF-8: {exc}"
            ) from exc
        return
    try:
        with input_file.path.open("r", encoding="utf-8") as handle:
            for row_number, line in enumerate(handle, start=1):
                if not line.strip():
                    raise DailyUpdateError(
                        f"{input_file.relative_path} row {row_number} is blank"
                    )
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise DailyUpdateError(
                        f"{input_file.relative_path} row {row_number} invalid JSON: {exc}"
                    ) from exc
                yield normalize_record(
                    raw,
                    table=table,
                    source=input_file.relative_path,
                    row_number=row_number,
                )
    except UnicodeError as exc:
        raise DailyUpdateError(
            f"{input_file.relative_path} is not valid UTF-8: {exc}"
        ) from exc


def serializable_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return (
            value.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    if isinstance(value, date):
        return value.isoformat()
    return value


def serializable_row(row: dict[str, Any], columns: Sequence[str]) -> dict[str, Any]:
    return {field: serializable_value(row[field]) for field in columns}


def row_hash(row: dict[str, Any], columns: Sequence[str]) -> str:
    return sha256_bytes(canonical_bytes(serializable_row(row, columns)))


def key_for(row: dict[str, Any], fields: Sequence[str]) -> tuple[Any, ...]:
    return tuple(serializable_value(row[field]) for field in fields)


def import_duckdb():
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise DailyUpdateError(
            "DuckDB is required; use the locked central data-engineering runtime"
        ) from exc
    return duckdb


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def quote_sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def verify_payload_inventory(root: Path, inventory: Any) -> None:
    if not isinstance(inventory, list):
        raise DailyUpdateError("manifest.payload_inventory must be an array")
    seen: set[str] = set()
    for entry in inventory:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"relative_path", "bytes", "sha256", "layer"}
            or not isinstance(entry["relative_path"], str)
            or not isinstance(entry["bytes"], int)
            or not isinstance(entry["sha256"], str)
            or not isinstance(entry["layer"], str)
            or not entry["layer"]
        ):
            raise DailyUpdateError("invalid payload inventory entry")
        relative = Path(entry["relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise DailyUpdateError("payload inventory path escapes candidate")
        key = relative.as_posix().casefold()
        if key in seen:
            raise DailyUpdateError("duplicate payload inventory path")
        seen.add(key)
        path = (root / relative).resolve()
        if not is_under(path, root) or not path.is_file():
            raise DailyUpdateError(
                f"candidate payload is missing: {entry['relative_path']}"
            )
        if path.stat().st_size != entry["bytes"] or sha256_file(path) != entry["sha256"]:
            raise DailyUpdateError(
                f"candidate payload verification failed: {entry['relative_path']}"
            )


def verify_candidate_file_set(root: Path, manifest: dict[str, Any]) -> None:
    if manifest.get("authority_paths") != list(CANDIDATE_AUTHORITY_PATHS):
        raise DailyUpdateError("candidate authority path contract is invalid")
    inventory = manifest.get("payload_inventory")
    if not isinstance(inventory, list):
        raise DailyUpdateError("manifest.payload_inventory must be an array")

    expected: dict[str, str] = {}
    for relative_path in CANDIDATE_AUTHORITY_PATHS:
        expected[relative_path.casefold()] = relative_path
    for entry in inventory:
        if not isinstance(entry, dict) or not isinstance(
            entry.get("relative_path"), str
        ):
            raise DailyUpdateError("invalid payload inventory entry")
        relative_path = Path(entry["relative_path"]).as_posix()
        key = relative_path.casefold()
        if key in expected:
            raise DailyUpdateError(
                f"candidate path is declared more than once: {relative_path}"
            )
        expected[key] = relative_path

    actual: dict[str, str] = {}
    for current, _directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in file_names:
            path = current_path / name
            if not path.is_file():
                raise DailyUpdateError(f"candidate entry is not a regular file: {path}")
            relative_path = path.relative_to(root).as_posix()
            key = relative_path.casefold()
            if key in actual:
                raise DailyUpdateError(
                    f"case-insensitive duplicate candidate path: {relative_path}"
                )
            actual[key] = relative_path

    missing = sorted(
        (expected[key] for key in expected.keys() - actual.keys()),
        key=str.casefold,
    )
    unexpected = sorted(
        (actual[key] for key in actual.keys() - expected.keys()),
        key=str.casefold,
    )
    if missing or unexpected:
        raise DailyUpdateError(
            "candidate file set differs from sealed manifest; "
            f"missing={missing}, unexpected={unexpected}"
        )


def resolve_base_candidate(
    *,
    request: dict[str, Any],
    workspace_root: Path,
    output_root: Path,
) -> tuple[Path | None, dict[str, Any] | None]:
    value = request["base_candidate"]
    if value is None:
        return None, None
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise DailyUpdateError("base_candidate must be a safe workspace-relative path")
    base = (workspace_root / relative).resolve()
    if not is_under(base, output_root):
        raise DailyUpdateError(
            "base_candidate must be under the local daily update output root"
        )
    if "quarantine" in {
        part.lower() for part in base.relative_to(output_root).parts
    }:
        raise DailyUpdateError("quarantine cannot be used as a base candidate")
    if not base.is_dir() or has_reparse_flag(base):
        raise DailyUpdateError(f"base candidate is invalid: {base}")
    manifest = verify_candidate(base)
    if (
        manifest.get("dataset_id") != request["dataset_id"]
        or manifest.get("dataset_schema_version")
        != request["dataset_schema_version"]
    ):
        raise DailyUpdateError("base candidate contract is incompatible")
    return base, manifest


def load_base_rows(
    *,
    base: Path | None,
    table: dict[str, Any],
    max_rows: int,
) -> list[dict[str, Any]]:
    if base is None:
        return []
    manifest = verify_candidate(base)
    duckdb = import_duckdb()
    parquet_paths: list[Path] = []
    for entry in manifest["payload_inventory"]:
        relative_path = Path(entry["relative_path"])
        if entry["layer"] == "curated":
            if relative_path.suffix.lower() != ".parquet":
                raise DailyUpdateError(
                    "base candidate contains a non-Parquet curated payload: "
                    f"{entry['relative_path']}"
                )
            parquet_paths.append(base / relative_path)
    parquet_paths.sort(key=lambda item: item.as_posix().casefold())
    if not parquet_paths:
        raise DailyUpdateError("base candidate has no Parquet payload")
    path_list = "[" + ",".join(
        quote_sql_string(path.as_posix()) for path in parquet_paths
    ) + "]"
    columns = table["expected_columns"]
    connection = duckdb.connect(database=":memory:")
    try:
        description = connection.execute(
            f"SELECT * FROM read_parquet({path_list}, "
            "hive_partitioning=false, union_by_name=false) LIMIT 0"
        ).description
        actual_columns = [item[0] for item in description]
        if actual_columns != columns:
            raise DailyUpdateError(
                f"base Parquet schema mismatch; expected={columns}, actual={actual_columns}",
                code="BASE_SCHEMA_DRIFT",
            )
        count = connection.execute(
            f"SELECT count(*) FROM read_parquet({path_list}, "
            "hive_partitioning=false, union_by_name=false)"
        ).fetchone()[0]
        if count > max_rows:
            raise DailyUpdateError(
                f"base row count {count} exceeds request limit {max_rows}"
            )
        select_columns = ", ".join(quote_identifier(field) for field in columns)
        values = connection.execute(
            f"SELECT {select_columns} "
            f"FROM read_parquet({path_list}, "
            "hive_partitioning=false, union_by_name=false)"
        ).fetchall()
    finally:
        connection.close()
    rows: list[dict[str, Any]] = []
    for index, values_row in enumerate(values, start=1):
        rows.append(
            normalize_record(
                dict(zip(columns, values_row, strict=True)),
                table=table,
                source="base_candidate",
                row_number=index,
            )
        )
    return rows


def merge_rows(
    *,
    base_rows: list[dict[str, Any]],
    incoming_rows: list[dict[str, Any]],
    table: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    columns = table["expected_columns"]
    primary = table["primary_key_columns"]
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    hashes: dict[tuple[Any, ...], str] = {}
    for row in base_rows:
        key = key_for(row, primary)
        digest = row_hash(row, columns)
        if key in merged and hashes[key] != digest:
            raise DailyUpdateError(
                f"base candidate has conflicting primary key {key}",
                code="BASE_PRIMARY_KEY_CONFLICT",
            )
        merged[key] = row
        hashes[key] = digest
    base_keys = set(merged)
    duplicate_rows = 0
    new_rows = 0
    new_unique: list[dict[str, Any]] = []
    for row in incoming_rows:
        key = key_for(row, primary)
        digest = row_hash(row, columns)
        if key in merged:
            if hashes[key] != digest:
                raise DailyUpdateError(
                    f"incoming row conflicts with primary key {key}",
                    code="PRIMARY_KEY_CONTENT_CONFLICT",
                )
            duplicate_rows += 1
            continue
        merged[key] = row
        hashes[key] = digest
        new_rows += 1
        new_unique.append(row)
    revision = table["revision"]
    if revision is not None:
        validate_revision_rows(
            base_rows=base_rows,
            new_rows=new_unique,
            all_rows=list(merged.values()),
            revision=revision,
        )
    elif len({key_for(row, primary) for row in merged.values()}) != len(merged):
        raise DailyUpdateError("primary key uniqueness failure")
    ordered = sorted(
        merged.values(),
        key=lambda row: tuple(
            "" if value is None else str(value)
            for value in key_for(row, primary)
        ),
    )
    return ordered, {
        "base_rows": len(base_rows),
        "incoming_rows": len(incoming_rows),
        "new_rows": new_rows,
        "duplicate_rows": duplicate_rows,
        "merged_rows": len(ordered),
        "base_primary_keys": len(base_keys),
    }


def validate_revision_rows(
    *,
    base_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
    all_rows: list[dict[str, Any]],
    revision: dict[str, Any],
) -> None:
    business_fields = revision["business_key_columns"]
    revision_field = revision["revision_column"]
    available_field = revision["available_at_column"]
    base_max: dict[tuple[Any, ...], int] = {}
    for row in base_rows:
        business = key_for(row, business_fields)
        current = base_max.get(business)
        value = row[revision_field]
        base_max[business] = value if current is None else max(current, value)
    for row in new_rows:
        business = key_for(row, business_fields)
        if business in base_max and row[revision_field] <= base_max[business]:
            raise DailyUpdateError(
                f"revision {row[revision_field]} is not append-only for {business}",
                code="REVISION_NOT_APPEND_ONLY",
            )
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in all_rows:
        grouped[key_for(row, business_fields)].append(row)
    for business, rows in grouped.items():
        rows.sort(key=lambda item: item[revision_field])
        previous_revision: int | None = None
        previous_available: datetime | None = None
        for row in rows:
            current_revision = row[revision_field]
            current_available = row[available_field]
            if (
                previous_revision is not None
                and current_revision <= previous_revision
            ):
                raise DailyUpdateError(
                    f"revision order is not strict for {business}",
                    code="REVISION_ORDER_INVALID",
                )
            if (
                previous_available is not None
                and current_available <= previous_available
            ):
                raise DailyUpdateError(
                    f"available_at order is not strict for {business}",
                    code="REVISION_AVAILABLE_AT_INVALID",
                )
            previous_revision = current_revision
            previous_available = current_available


def partition_key(row: dict[str, Any], date_column: str) -> tuple[int, int]:
    value = row[date_column]
    if not isinstance(value, date):
        raise DailyUpdateError("date column was not normalized to date")
    return value.year, value.month


def request_fingerprint(
    *,
    request: dict[str, Any],
    inputs: list[InputFile],
    base_manifest: dict[str, Any] | None,
) -> str:
    return sha256_bytes(
        canonical_bytes(
            {
                "engine_id": ENGINE_ID,
                "request": request,
                "inputs": [
                    {
                        "relative_path": item.relative_path,
                        "bytes": item.bytes,
                        "sha256": item.sha256,
                    }
                    for item in inputs
                ],
                "base_content_hash": (
                    base_manifest.get("content_hash")
                    if base_manifest is not None
                    else None
                ),
            }
        )
    )


def inventory_file(path: Path, root: Path, layer: str) -> dict[str, Any]:
    return {
        "relative_path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "layer": layer,
    }


def duckdb_type(value: str) -> str:
    return {
        "VARCHAR": "VARCHAR",
        "DATE": "DATE",
        "TIMESTAMP_TZ": "TIMESTAMPTZ",
        "DOUBLE": "DOUBLE",
        "BIGINT": "BIGINT",
        "BOOLEAN": "BOOLEAN",
    }[value]


def write_parquet_partitions(
    *,
    rows: list[dict[str, Any]],
    stage_root: Path,
    final_root: Path,
    request: dict[str, Any],
) -> list[dict[str, Any]]:
    duckdb = import_duckdb()
    table = request["table"]
    columns = table["expected_columns"]
    column_sql = ", ".join(
        f"{quote_identifier(field)} {duckdb_type(table['column_types'][field])}"
        for field in columns
    )
    connection = duckdb.connect(database=":memory:")
    try:
        connection.execute(f"CREATE TABLE merged ({column_sql})")
        placeholders = ", ".join("?" for _ in columns)
        connection.executemany(
            f"INSERT INTO merged VALUES ({placeholders})",
            [[row[field] for field in columns] for row in rows],
        )
        select_columns = ", ".join(quote_identifier(field) for field in columns)
        order_columns = ", ".join(
            quote_identifier(field) for field in table["primary_key_columns"]
        )
        date_column = quote_identifier(table["date_column"])
        partitions: list[dict[str, Any]] = []
        for year, month in sorted(
            {partition_key(row, table["date_column"]) for row in rows}
        ):
            relative = (
                Path("curated")
                / f"schema={request['dataset_schema_version']}"
                / f"year={year:04d}"
                / f"month={month:02d}"
                / "part-000.parquet"
            )
            output_path = stage_root / relative
            output_path.parent.mkdir(parents=True, exist_ok=True)
            connection.execute(
                "COPY ("
                f"SELECT {select_columns} FROM merged "
                f"WHERE year({date_column})={year} AND month({date_column})={month} "
                f"ORDER BY {order_columns}"
                ") TO "
                f"{quote_sql_string(output_path.as_posix())} "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            count = connection.execute(
                "SELECT count(*) FROM merged "
                f"WHERE year({date_column})={year} AND month({date_column})={month}"
            ).fetchone()[0]
            partitions.append(
                {
                    "relative_path": relative.as_posix(),
                    "final_path": (final_root / relative).as_posix(),
                    "year": year,
                    "month": month,
                    "row_count": count,
                    "bytes": output_path.stat().st_size,
                    "sha256": sha256_file(output_path),
                }
            )
        return partitions
    finally:
        connection.close()


def merged_payload_hash(rows: list[dict[str, Any]], columns: Sequence[str]) -> str:
    return sha256_bytes(
        b"\n".join(canonical_bytes(serializable_row(row, columns)) for row in rows)
    )


def candidate_content_hash(
    *,
    request: dict[str, Any],
    manifest: dict[str, Any],
    provenance: dict[str, Any],
    partition_manifest: dict[str, Any],
    quality: dict[str, Any],
    receipt: dict[str, Any],
) -> str:
    manifest_copy = copy.deepcopy(manifest)
    manifest_copy.pop("content_hash", None)
    return sha256_bytes(
        canonical_bytes(
            {
                "hash_algorithm": HASH_ALGORITHM,
                "request": request,
                "manifest": manifest_copy,
                "provenance": provenance,
                "partition_manifest": partition_manifest,
                "quality": quality,
                "receipt": receipt,
            }
        )
    )


def verify_candidate(root: Path) -> dict[str, Any]:
    if not root.is_dir() or has_reparse_flag(root):
        raise DailyUpdateError(f"candidate root is invalid: {root}")
    reject_reparse_tree(root)
    manifest = read_json(root / "manifest.json")
    if (
        not isinstance(manifest, dict)
        or manifest.get("engine_id") != ENGINE_ID
        or manifest.get("sealed") is not True
        or manifest.get("purpose_ceiling") != PURPOSE_CEILING
        or manifest.get("catalog_registered") is not False
        or manifest.get("source_admission") is not False
    ):
        raise DailyUpdateError("sealed candidate manifest is invalid")
    verify_payload_inventory(root, manifest.get("payload_inventory"))
    verify_candidate_file_set(root, manifest)
    request = read_json(root / "request.json")
    provenance = read_json(root / "provenance.json")
    partitions = read_json(root / "partition-manifest.json")
    quality = read_json(root / "quality" / "summary.json")
    receipt = read_json(root / "receipts" / "update.json")
    expected = candidate_content_hash(
        request=request,
        manifest=manifest,
        provenance=provenance,
        partition_manifest=partitions,
        quality=quality,
        receipt=receipt,
    )
    if expected != manifest.get("content_hash"):
        raise DailyUpdateError("candidate content hash is invalid")
    return manifest


def quarantine_failure(
    *,
    output_root: Path,
    request: dict[str, Any],
    error: DailyUpdateError,
    fingerprint: str | None,
    catalog_hash_before: str,
    catalog_hash_after: str,
) -> Path:
    quarantine_root = output_root / "quarantine"
    quarantine_root.mkdir(parents=True, exist_ok=True)
    final = (
        quarantine_root
        / request["dataset_id"]
        / f"{request['run_id']}-{uuid.uuid4().hex}"
    )
    final.parent.mkdir(parents=True, exist_ok=True)
    stage = final.parent / f".{final.name}.partial"
    if stage.exists() or final.exists():
        raise DailyUpdateError("unexpected quarantine path collision")
    stage.mkdir()
    write_json(
        stage / "failure.json",
        {
            "schema_version": 1,
            "engine_id": ENGINE_ID,
            "status": "QUARANTINED",
            "dataset_id": request["dataset_id"],
            "run_id": request["run_id"],
            "reason": error.code,
            "message": str(error),
            "request_fingerprint": fingerprint,
            "failed_at": utc_now(),
            "rollback": {
                "candidate_published": False,
                "formal_catalog_written": False,
                "staging_removed": True,
            },
            "formal_catalog_guard": {
                "sha256_before": catalog_hash_before,
                "sha256_after": catalog_hash_after,
                "unchanged": catalog_hash_before == catalog_hash_after,
            },
            "purpose_ceiling": PURPOSE_CEILING,
            "source_admission": False,
            "catalog_registered": False,
            "quality_promotion": False,
            "research_ready": False,
        },
    )
    os.replace(stage, final)
    return final


def build_report(
    *,
    request: dict[str, Any],
    final_root: Path,
    quality: dict[str, Any],
) -> str:
    metrics = quality["metrics"]
    return f"""# Local daily update candidate

- Dataset: `{request["dataset_id"]}`
- Run: `{request["run_id"]}`
- Candidate: `{final_root.as_posix()}`
- Status: `CANDIDATE_PUBLISHED`
- Rows: `{metrics["merged_rows"]}`
- Incoming rows: `{metrics["incoming_rows"]}`
- New rows: `{metrics["new_rows"]}`
- Idempotent duplicate rows: `{metrics["duplicate_rows"]}`
- Partitions: `{metrics["partition_count"]}`
- New partition keys: `{metrics["new_partition_keys"]}`
- Content hash: see the sealed `manifest.json`

## Boundary

This is an immutable `INSPECTION_ONLY` local candidate.  It did not access the
network, use a credential, mutate `data/catalog/datasets.json`, admit a source,
promote quality, create a scheduler or publish a research snapshot.

The candidate can be inspected with `query.sql`.  It must not be resolved by a
direct path for research and must not be treated as `latest`.
"""


def execute_candidate(
    *,
    workspace_root: Path,
    catalog_path: Path,
    catalog_hash_before: str,
    request_path: Path,
    request: dict[str, Any],
    inbox: Path,
    inputs: list[InputFile],
    output_root: Path,
    base: Path | None,
    base_manifest: dict[str, Any] | None,
    merged_rows: list[dict[str, Any]],
    metrics: dict[str, int],
    fingerprint: str,
    failpoint: str | None,
) -> UpdateResult:
    final_root = output_root / request["dataset_id"] / request["run_id"]
    final_root.parent.mkdir(parents=True, exist_ok=True)
    if final_root.exists():
        existing = verify_candidate(final_root)
        if existing.get("request_fingerprint") == fingerprint:
            return UpdateResult(
                status="NO_CHANGE",
                dataset_id=request["dataset_id"],
                run_id=request["run_id"],
                candidate_path=final_root.as_posix(),
                quarantine_path=None,
                request_fingerprint=fingerprint,
                row_count=existing["row_count"],
                new_row_count=existing["new_row_count"],
                duplicate_row_count=existing["duplicate_row_count"],
                partition_count=existing["partition_count"],
                new_partition_count=existing["new_partition_count"],
            )
        error = DailyUpdateError(
            "refusing to reuse dataset_id + run_id for changed content",
            code="CANDIDATE_IDENTITY_CONFLICT",
        )
        quarantine = quarantine_failure(
            output_root=output_root,
            request=request,
            error=error,
            fingerprint=fingerprint,
            catalog_hash_before=catalog_hash_before,
            catalog_hash_after=sha256_file(catalog_path),
        )
        return UpdateResult(
            status="QUARANTINED",
            dataset_id=request["dataset_id"],
            run_id=request["run_id"],
            candidate_path=None,
            quarantine_path=quarantine.as_posix(),
            request_fingerprint=fingerprint,
            row_count=None,
            new_row_count=None,
            duplicate_row_count=None,
            partition_count=None,
            new_partition_count=None,
        )
    stage_root = (
        final_root.parent / f".{request['run_id']}.partial-{uuid.uuid4().hex}"
    )
    if stage_root.exists():
        raise DailyUpdateError("unexpected staging collision")
    created_stage = False
    try:
        stage_root.mkdir()
        created_stage = True
        raw_inventory: list[dict[str, Any]] = []
        for index, item in enumerate(inputs):
            relative = Path("raw") / "incoming" / f"{index:05d}" / item.relative_path
            target = stage_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item.path, target)
            if target.stat().st_size != item.bytes or sha256_file(target) != item.sha256:
                raise DailyUpdateError(
                    f"raw input copy verification failed: {item.relative_path}"
                )
            raw_inventory.append(inventory_file(target, stage_root, "raw"))
        partitions = write_parquet_partitions(
            rows=merged_rows,
            stage_root=stage_root,
            final_root=final_root,
            request=request,
        )
        if failpoint == "after_payload":
            raise InjectedFailure(
                "injected failure after payload materialization",
                code="INJECTED_AFTER_PAYLOAD",
            )
        table = request["table"]
        final_glob = (
            final_root
            / "curated"
            / f"schema={request['dataset_schema_version']}"
            / "year=*"
            / "month=*"
            / "*.parquet"
        ).as_posix()
        query_sql = (
            "-- Candidate only; not catalog-admitted or research-ready.\n"
            f"CREATE OR REPLACE VIEW {quote_identifier(table['table_name'])} AS\n"
            f"SELECT * FROM read_parquet({quote_sql_string(final_glob)}, "
            "hive_partitioning=false, union_by_name=false);\n"
        )
        (stage_root / "query.sql").write_text(
            query_sql, encoding="utf-8", newline="\n"
        )
        input_records = [
            {
                "relative_path": item.relative_path,
                "bytes": item.bytes,
                "sha256": item.sha256,
            }
            for item in inputs
        ]
        base_partition_keys = sorted(
            {
                f"{partition_key(row, table['date_column'])[0]:04d}-"
                f"{partition_key(row, table['date_column'])[1]:02d}"
                for row in load_base_rows(
                    base=base,
                    table=table,
                    max_rows=request["limits"]["max_rows"],
                )
            }
        )
        all_partition_keys = sorted(
            {f"{year:04d}-{month:02d}" for year, month in {
                partition_key(row, table["date_column"]) for row in merged_rows
            }}
        )
        new_partition_keys = sorted(set(all_partition_keys) - set(base_partition_keys))
        metrics_value: dict[str, Any] = {
            **metrics,
            "partition_count": len(partitions),
            "base_partition_keys": base_partition_keys,
            "partition_keys": all_partition_keys,
            "new_partition_keys": new_partition_keys,
            "date_min": min(
                row[table["date_column"]] for row in merged_rows
            ).isoformat(),
            "date_max": max(
                row[table["date_column"]] for row in merged_rows
            ).isoformat(),
            "merged_payload_sha256": merged_payload_hash(
                merged_rows, table["expected_columns"]
            ),
        }
        provenance = {
            "schema_version": 1,
            "engine_id": ENGINE_ID,
            "dataset_id": request["dataset_id"],
            "run_id": request["run_id"],
            "source_id": request["source_id"],
            "dataset_scope": request["dataset_scope"],
            "created_at": utc_now(),
            "input_root": inbox.as_posix(),
            "input_files": input_records,
            "base_candidate": (
                {
                    "path": base.as_posix(),
                    "content_hash": base_manifest["content_hash"],
                }
                if base is not None and base_manifest is not None
                else None
            ),
            "authorization_note": request["authorization_note"],
            "transformations": [
                "discover explicit normalized UTF-8 CSV/JSONL delta files",
                "exact schema and explicit scalar casts",
                "required-value and primary-key conflict gate",
                "append-only revision and timezone-aware available_at gate when configured",
                "idempotent exact-row deduplication",
                "self-contained merged Parquet candidate with ZSTD compression",
                "same-volume staging verification and atomic directory seal",
            ],
            "formal_catalog_guard": {
                "path": catalog_path.as_posix(),
                "sha256_before": catalog_hash_before,
                "write_attempted": False,
            },
        }
        partition_manifest = {
            "schema_version": 1,
            "engine_id": ENGINE_ID,
            "dataset_id": request["dataset_id"],
            "run_id": request["run_id"],
            "partition_scheme": ["year", "month"],
            "date_column": table["date_column"],
            "partitions": partitions,
            "total_rows": metrics["merged_rows"],
        }
        quality = {
            "schema_version": 1,
            "engine_id": ENGINE_ID,
            "dataset_id": request["dataset_id"],
            "run_id": request["run_id"],
            "mechanical_status": "PASS",
            "admission_decision": "NOT_ADMITTED",
            "purpose_ceiling": PURPOSE_CEILING,
            "source_admission": False,
            "catalog_registered": False,
            "quality_promotion": False,
            "research_ready": False,
            "checks": {
                "input_reparse_free": True,
                "input_hashes_bound": True,
                "schema_exact": True,
                "required_non_null": True,
                "scalar_types_valid": True,
                "date_valid": True,
                "primary_key_conflicts": 0,
                "revision_append_only": True,
                "base_candidate_hashes_verified": base is not None,
                "formal_catalog_unchanged_preseal": True,
            },
            "metrics": metrics_value,
            "limitations": [
                "normalized input does not independently prove source semantics",
                "authorization note is not a formal source-rights admission",
                "available_at validation is mechanical and does not prove first-publication completeness",
                "candidate is not registered in data/catalog/datasets.json",
                "candidate is not a formal immutable research snapshot",
            ],
        }
        receipt = {
            "schema_version": 1,
            "engine_id": ENGINE_ID,
            "status": "CANDIDATE_PUBLISHED",
            "dataset_id": request["dataset_id"],
            "run_id": request["run_id"],
            "request_fingerprint": fingerprint,
            "base_candidate": base.as_posix() if base is not None else None,
            "discovered_files": input_records,
            "metrics": metrics_value,
            "atomicity": {
                "staging_directory": "sibling_same_volume",
                "seal_operation": "os.replace",
                "formal_catalog_write": False,
            },
            "purpose_ceiling": PURPOSE_CEILING,
            "source_admission": False,
            "catalog_registered": False,
            "quality_promotion": False,
            "research_ready": False,
        }
        (stage_root / "REPORT.md").write_text(
            build_report(
                request=request,
                final_root=final_root,
                quality=quality,
            ),
            encoding="utf-8",
            newline="\n",
        )
        payload_inventory = (
            raw_inventory
            + [
                inventory_file(
                    stage_root / item["relative_path"], stage_root, "curated"
                )
                for item in partitions
            ]
            + [
                inventory_file(stage_root / "query.sql", stage_root, "query"),
                inventory_file(stage_root / "REPORT.md", stage_root, "report"),
            ]
        )
        manifest = {
            "schema_version": 1,
            "engine_id": ENGINE_ID,
            "hash_algorithm": HASH_ALGORITHM,
            "dataset_id": request["dataset_id"],
            "run_id": request["run_id"],
            "dataset_schema_version": request["dataset_schema_version"],
            "root_path": final_root.as_posix(),
            "request_fingerprint": fingerprint,
            "content_hash": None,
            "sealed": True,
            "lifecycle": "candidate",
            "mutability": "immutable_after_seal",
            "purpose_ceiling": PURPOSE_CEILING,
            "catalog_registered": False,
            "source_admission": False,
            "quality_promotion": False,
            "research_ready": False,
            "row_count": metrics["merged_rows"],
            "new_row_count": metrics["new_rows"],
            "duplicate_row_count": metrics["duplicate_rows"],
            "partition_count": len(partitions),
            "new_partition_count": len(new_partition_keys),
            "payload_inventory": payload_inventory,
            "authority_paths": list(CANDIDATE_AUTHORITY_PATHS),
        }
        manifest["content_hash"] = candidate_content_hash(
            request=request,
            manifest=manifest,
            provenance=provenance,
            partition_manifest=partition_manifest,
            quality=quality,
            receipt=receipt,
        )
        write_json(stage_root / "request.json", request)
        write_json(stage_root / "manifest.json", manifest)
        write_json(stage_root / "provenance.json", provenance)
        write_json(stage_root / "partition-manifest.json", partition_manifest)
        write_json(stage_root / "quality" / "summary.json", quality)
        write_json(stage_root / "receipts" / "update.json", receipt)
        verify_payload_inventory(stage_root, payload_inventory)
        if sha256_file(catalog_path) != catalog_hash_before:
            raise DailyUpdateError(
                "formal catalog changed concurrently; refusing candidate seal",
                code="FORMAL_CATALOG_CHANGED",
            )
        if failpoint == "before_seal":
            raise InjectedFailure(
                "injected failure before atomic candidate seal",
                code="INJECTED_BEFORE_SEAL",
            )
        os.replace(stage_root, final_root)
        created_stage = False
        verified_manifest = verify_candidate(final_root)
        if sha256_file(catalog_path) != catalog_hash_before:
            raise DailyUpdateError(
                "formal catalog changed after candidate seal",
                code="FORMAL_CATALOG_CHANGED_AFTER_SEAL",
            )
        return UpdateResult(
            status="CANDIDATE_PUBLISHED",
            dataset_id=request["dataset_id"],
            run_id=request["run_id"],
            candidate_path=final_root.as_posix(),
            quarantine_path=None,
            request_fingerprint=fingerprint,
            row_count=verified_manifest["row_count"],
            new_row_count=verified_manifest["new_row_count"],
            duplicate_row_count=verified_manifest["duplicate_row_count"],
            partition_count=verified_manifest["partition_count"],
            new_partition_count=verified_manifest["new_partition_count"],
        )
    except DailyUpdateError as exc:
        if created_stage and stage_root.exists():
            if not is_under(stage_root, output_root):
                raise DailyUpdateError(
                    "refusing unsafe staging cleanup outside output root"
                ) from exc
            shutil.rmtree(stage_root)
            created_stage = False
        quarantine = quarantine_failure(
            output_root=output_root,
            request=request,
            error=exc,
            fingerprint=fingerprint,
            catalog_hash_before=catalog_hash_before,
            catalog_hash_after=sha256_file(catalog_path),
        )
        if isinstance(exc, InjectedFailure):
            raise InjectedFailure(
                f"{exc}; receipt={quarantine}",
                code=exc.code,
            ) from exc
        return UpdateResult(
            status="QUARANTINED",
            dataset_id=request["dataset_id"],
            run_id=request["run_id"],
            candidate_path=None,
            quarantine_path=quarantine.as_posix(),
            request_fingerprint=fingerprint,
            row_count=None,
            new_row_count=None,
            duplicate_row_count=None,
            partition_count=None,
            new_partition_count=None,
        )
    finally:
        if created_stage and stage_root.exists():
            if not is_under(stage_root, output_root):
                raise DailyUpdateError(
                    "refusing unsafe staging cleanup outside output root"
                )
            shutil.rmtree(stage_root)


def run(
    *,
    workspace_root: Path,
    request_path: Path,
    inbox: Path,
    output_root: Path | None = None,
    execute: bool = False,
    failpoint: str | None = None,
) -> UpdateResult:
    workspace_root, catalog_path, catalog_hash_before, catalog = validate_workspace(
        workspace_root
    )
    request_path = request_path.resolve()
    if not request_path.is_file():
        raise DailyUpdateError(f"request file is missing: {request_path}")
    request = strict_request(request_path)
    output_root = resolve_output_root(workspace_root, output_root)
    try:
        validate_dataset_scope(request, catalog)
        if failpoint is not None and request["dataset_scope"] != "synthetic_fixture":
            raise DailyUpdateError(
                "failpoints are allowed only for synthetic fixtures"
            )
        inbox = validate_inbox(workspace_root, inbox)
        inputs = discover_inputs(
            inbox, request["include_patterns"], request["limits"]
        )
        base, base_manifest = resolve_base_candidate(
            request=request,
            workspace_root=workspace_root,
            output_root=output_root,
        )
        base_rows = load_base_rows(
            base=base,
            table=request["table"],
            max_rows=request["limits"]["max_rows"],
        )
        incoming_rows: list[dict[str, Any]] = []
        for item in inputs:
            for row in iter_input_rows(item, request["table"]):
                incoming_rows.append(row)
                if len(base_rows) + len(incoming_rows) > request["limits"]["max_rows"]:
                    raise DailyUpdateError(
                        f"merged rows exceed request limit {request['limits']['max_rows']}"
                    )
        merged_rows, metrics = merge_rows(
            base_rows=base_rows,
            incoming_rows=incoming_rows,
            table=request["table"],
        )
    except DailyUpdateError as exc:
        if not execute:
            raise
        quarantine = quarantine_failure(
            output_root=output_root,
            request=request,
            error=exc,
            fingerprint=None,
            catalog_hash_before=catalog_hash_before,
            catalog_hash_after=sha256_file(catalog_path),
        )
        return UpdateResult(
            status="QUARANTINED",
            dataset_id=request["dataset_id"],
            run_id=request["run_id"],
            candidate_path=None,
            quarantine_path=quarantine.as_posix(),
            request_fingerprint=None,
            row_count=None,
            new_row_count=None,
            duplicate_row_count=None,
            partition_count=None,
            new_partition_count=None,
        )
    fingerprint = request_fingerprint(
        request=request,
        inputs=inputs,
        base_manifest=base_manifest,
    )
    all_partitions = {
        partition_key(row, request["table"]["date_column"]) for row in merged_rows
    }
    base_partitions = {
        partition_key(row, request["table"]["date_column"]) for row in base_rows
    }
    if not execute:
        return UpdateResult(
            status="PLAN",
            dataset_id=request["dataset_id"],
            run_id=request["run_id"],
            candidate_path=(
                output_root / request["dataset_id"] / request["run_id"]
            ).as_posix(),
            quarantine_path=None,
            request_fingerprint=fingerprint,
            row_count=metrics["merged_rows"],
            new_row_count=metrics["new_rows"],
            duplicate_row_count=metrics["duplicate_rows"],
            partition_count=len(all_partitions),
            new_partition_count=len(all_partitions - base_partitions),
        )
    return execute_candidate(
        workspace_root=workspace_root,
        catalog_path=catalog_path,
        catalog_hash_before=catalog_hash_before,
        request_path=request_path,
        request=request,
        inbox=inbox,
        inputs=inputs,
        output_root=output_root,
        base=base,
        base_manifest=base_manifest,
        merged_rows=merged_rows,
        metrics=metrics,
        fingerprint=fingerprint,
        failpoint=failpoint,
    )


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan or build an inspection-only local daily update candidate"
    )
    parser.add_argument("--workspace-root", type=Path, default=ROOT)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--failpoint",
        choices=["after_payload", "before_seal"],
        help="Synthetic-fixture test only.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        result = run(
            workspace_root=args.workspace_root,
            request_path=args.request,
            inbox=args.inbox,
            output_root=args.output_root,
            execute=args.execute,
            failpoint=args.failpoint,
        )
    except DailyUpdateError as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "engine_id": ENGINE_ID,
                    "status": "REJECTED",
                    "reason": exc.code,
                    "message": str(exc),
                    "purpose_ceiling": PURPOSE_CEILING,
                    "source_admission": False,
                    "catalog_registered": False,
                    "quality_promotion": False,
                    "research_ready": False,
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status != "QUARANTINED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
