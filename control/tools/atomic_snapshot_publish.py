#!/usr/bin/env python3
"""Prepare, verify, and atomically register an isolated synthetic snapshot.

L1-D12A is an infrastructure-only package.  The publisher refuses to run
outside a sentinel-marked temporary workspace and never downloads real data.
The only discovery commit point is a same-directory ``os.replace`` of the
isolated ``datasets.json`` catalog.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator


CATALOG_LIB = Path(__file__).resolve().parents[2] / "data" / "catalog"
if str(CATALOG_LIB) not in sys.path:
    sys.path.insert(0, str(CATALOG_LIB))

from snapshot_contract import (  # noqa: E402
    AUTHORITY_RELATIVE_PATHS,
    HASH_ALGORITHM,
    SnapshotContractError,
    canonical_bytes,
    is_reparse_point,
    load_json_object,
    request_contract_hash,
    sha256_bytes,
    sha256_file,
    snapshot_contract_hash,
    verify_snapshot_contract,
)


SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RFC3339_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RFC3339_DATETIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)
LOCK_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
TEST_SENTINEL = ".qdata-d12a-synthetic-workspace"
TEST_SENTINEL_CONTENT = "D12A_SYNTHETIC_ONLY\n"
PUBLISHER_ID = "atomic_snapshot_publish.py/d12a-v1"
REQUEST_SCHEMA_PATH = (
    CATALOG_LIB / "contracts" / "atomic_publish_v1" / "request.schema.json"
)
ALLOWED_FAILPOINTS = {
    "after_snapshot_finalize",
    "before_catalog_replace",
    "after_catalog_replace",
    "hard_crash_after_snapshot_finalize",
    "hard_crash_after_catalog_replace",
}
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class PublishError(RuntimeError):
    """A controlled fail-closed publication rejection."""


class InjectedFailure(RuntimeError):
    """A deterministic exception used only by isolated tests."""


@dataclass(frozen=True)
class PublishResult:
    status: str
    dataset_id: str
    snapshot_id: str
    run_id: str
    content_hash: str | None
    snapshot_path: str | None
    quarantine_path: str | None
    catalog_hash_before: str
    catalog_hash_after: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def catalog_path_string(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def load_json(path: Path) -> dict[str, Any]:
    try:
        return load_json_object(path)
    except SnapshotContractError as exc:
        raise PublishError(str(exc)) from exc


def validate_component(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_COMPONENT.fullmatch(value):
        raise PublishError(f"{label} must be a safe path component: {value!r}")
    if value in {".", ".."} or value.rstrip(" .") != value:
        raise PublishError(f"{label} has an unsafe Windows path form")
    if value.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        raise PublishError(f"{label} uses a reserved Windows device name: {value}")
    return value


def parse_iso_datetime(value: Any, label: str) -> str:
    if not isinstance(value, str) or not RFC3339_DATETIME_PATTERN.fullmatch(value):
        raise PublishError(f"{label} must be an RFC 3339 date-time")
    try:
        parsed = datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublishError(f"{label} is not a valid RFC 3339 date-time: {value!r}") from exc
    if parsed.tzinfo is None:
        raise PublishError(f"{label} must contain an explicit timezone")
    return value


def parse_iso_date(value: Any, label: str) -> str:
    if not isinstance(value, str) or not RFC3339_DATE_PATTERN.fullmatch(value):
        raise PublishError(f"{label} must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise PublishError(f"{label} is not a valid date: {value!r}") from exc
    return value


@lru_cache(maxsize=1)
def load_request_schema() -> dict[str, Any]:
    """Load the checked-in request schema through the strict JSON reader."""

    schema = load_json(REQUEST_SCHEMA_PATH)
    validate_supported_schema(schema, "request.schema")
    return schema


SUPPORTED_SCHEMA_KEYWORDS = {
    "$schema",
    "$id",
    "$defs",
    "$ref",
    "title",
    "description",
    "type",
    "additionalProperties",
    "required",
    "properties",
    "const",
    "enum",
    "pattern",
    "format",
    "minItems",
    "uniqueItems",
    "items",
}


def validate_supported_schema(schema: dict[str, Any], path: str) -> None:
    """Reject contract keywords this zero-dependency validator cannot execute."""

    unsupported = sorted(set(schema) - SUPPORTED_SCHEMA_KEYWORDS)
    if unsupported:
        raise PublishError(
            f"{path} contains unsupported schema keywords: {', '.join(unsupported)}"
        )
    definitions = schema.get("$defs", {})
    if not isinstance(definitions, dict):
        raise PublishError(f"{path}.$defs must be an object")
    for name, child in definitions.items():
        if not isinstance(child, dict):
            raise PublishError(f"{path}.$defs.{name} must be an object")
        validate_supported_schema(child, f"{path}.$defs.{name}")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise PublishError(f"{path}.properties must be an object")
    for name, child in properties.items():
        if not isinstance(child, dict):
            raise PublishError(f"{path}.properties.{name} must be an object")
        validate_supported_schema(child, f"{path}.properties.{name}")
    items = schema.get("items")
    if items is not None:
        if not isinstance(items, dict):
            raise PublishError(f"{path}.items must be an object")
        validate_supported_schema(items, f"{path}.items")
    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        raise PublishError(
            f"{path}.additionalProperties must be a boolean in the supported subset"
        )


def json_schema_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without treating booleans as integers."""

    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


def resolve_local_schema_ref(root: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise PublishError(f"request schema contains unsupported reference: {reference}")
    value: Any = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or part not in value:
            raise PublishError(f"request schema reference does not resolve: {reference}")
        value = value[part]
    if not isinstance(value, dict):
        raise PublishError(f"request schema reference is not an object: {reference}")
    return value


def matches_json_schema_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise PublishError(f"request schema contains unsupported type: {expected}")


def validate_json_schema_value(
    value: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    path: str,
) -> None:
    """Validate the JSON-Schema subset used by atomic request contract v1."""

    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str):
            raise PublishError(f"{path}: schema $ref must be a string")
        validate_json_schema_value(
            value,
            resolve_local_schema_ref(root_schema, reference),
            root_schema,
            path,
        )

    expected_type = schema.get("type")
    if expected_type is not None:
        if not isinstance(expected_type, str):
            raise PublishError(f"{path}: schema type must be a string")
        if not matches_json_schema_type(value, expected_type):
            raise PublishError(f"{path} must have JSON type {expected_type}")

    if "const" in schema and not json_schema_equal(value, schema["const"]):
        raise PublishError(f"{path} must equal {schema['const']!r}")

    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list):
            raise PublishError(f"{path}: schema enum must be an array")
        if not any(json_schema_equal(value, candidate) for candidate in enum):
            raise PublishError(f"{path} must be one of {enum!r}")

    if isinstance(value, str):
        pattern = schema.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str):
                raise PublishError(f"{path}: schema pattern must be a string")
            try:
                matched = re.search(pattern, value)
            except re.error as exc:
                raise PublishError(f"{path}: invalid schema pattern: {pattern!r}") from exc
            if matched is None:
                raise PublishError(f"{path} does not match schema pattern {pattern!r}")
        value_format = schema.get("format")
        if value_format == "date":
            parse_iso_date(value, path)
        elif value_format == "date-time":
            parse_iso_datetime(value, path)
        elif value_format is not None:
            raise PublishError(f"{path}: unsupported schema format {value_format!r}")

    if isinstance(value, list):
        minimum = schema.get("minItems")
        if minimum is not None:
            if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
                raise PublishError(f"{path}: schema minItems must be a non-negative integer")
            if len(value) < minimum:
                raise PublishError(f"{path} must contain at least {minimum} items")
        if schema.get("uniqueItems") is True:
            for left_index, left in enumerate(value):
                if any(json_schema_equal(left, right) for right in value[:left_index]):
                    raise PublishError(f"{path} must not contain duplicate items")
        item_schema = schema.get("items")
        if item_schema is not None:
            if not isinstance(item_schema, dict):
                raise PublishError(f"{path}: schema items must be an object")
            for index, item in enumerate(value):
                validate_json_schema_value(
                    item,
                    item_schema,
                    root_schema,
                    f"{path}[{index}]",
                )

    if isinstance(value, dict):
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise PublishError(f"{path}: schema required must be an array of strings")
        missing = sorted(set(required) - value.keys())
        if missing:
            raise PublishError(f"{path} is missing fields: {', '.join(missing)}")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise PublishError(f"{path}: schema properties must be an object")
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - properties.keys())
            if extras:
                raise PublishError(f"{path} contains unsupported fields: {', '.join(extras)}")
        for key, item in value.items():
            property_schema = properties.get(key)
            if property_schema is not None:
                if not isinstance(property_schema, dict):
                    raise PublishError(f"{path}.{key}: property schema must be an object")
                validate_json_schema_value(
                    item,
                    property_schema,
                    root_schema,
                    f"{path}.{key}",
                )


def validate_request(request: dict[str, Any]) -> dict[str, Any]:
    schema = load_request_schema()
    validate_json_schema_value(request, schema, schema, "request")
    for field in (
        "run_id",
        "dataset_id",
        "snapshot_id",
        "source_id",
        "owner_project",
        "store_id",
        "contract_id",
        "dataset_schema_version",
    ):
        validate_component(request.get(field), f"request.{field}")
    for index, segment in enumerate(request["partition_segments"]):
        validate_component(segment, f"request.partition_segments[{index}]")
    if "latest" in request["snapshot_id"].lower():
        raise PublishError("snapshot_id must never use latest")
    return request


def immutable_request_contract(request: dict[str, Any]) -> dict[str, Any]:
    """Remove operational retry fields while retaining every snapshot policy field."""
    excluded = {"run_id", "expected_payload_hash"}
    return {key: copy.deepcopy(value) for key, value in request.items() if key not in excluded}


def assert_isolated_synthetic_workspace(workspace_root: Path, catalog_path: Path) -> None:
    sentinel = assert_path_under_workspace(
        workspace_root / TEST_SENTINEL,
        workspace_root,
        "synthetic workspace sentinel",
    )
    if not sentinel.is_file() or is_reparse_point(sentinel):
        raise PublishError("synthetic workspace sentinel must be a regular file")
    try:
        content = sentinel.read_text(encoding="utf-8")
    except OSError as exc:
        raise PublishError(
            f"synthetic publisher requires an isolated workspace containing {TEST_SENTINEL}"
        ) from exc
    if content != TEST_SENTINEL_CONTENT:
        raise PublishError(f"invalid synthetic workspace sentinel: {sentinel}")
    expected_catalog = workspace_root / "data" / "catalog" / "datasets.json"
    catalog_path = assert_path_under_workspace(
        catalog_path,
        workspace_root,
        "catalog_path",
    )
    if not catalog_path.is_file() or is_reparse_point(catalog_path):
        raise PublishError("synthetic catalog must be an existing regular file")
    if Path(os.path.abspath(catalog_path)) != Path(os.path.abspath(expected_catalog)):
        raise PublishError("synthetic publisher may only update the isolated workspace datasets.json")


def assert_path_under_workspace(path: Path, workspace_root: Path, label: str) -> Path:
    lexical_root = Path(os.path.abspath(workspace_root))
    lexical_path = Path(os.path.abspath(path))
    try:
        if Path(os.path.commonpath([lexical_root, lexical_path])) != lexical_root:
            raise PublishError(f"{label} escapes the isolated workspace")
    except ValueError as exc:
        raise PublishError(f"{label} is on another volume") from exc
    relative = lexical_path.relative_to(lexical_root)
    current = lexical_root
    if is_reparse_point(current):
        raise PublishError(f"workspace root must not be a reparse point: {current}")
    for part in relative.parts:
        current /= part
        if current.exists() and is_reparse_point(current):
            raise PublishError(f"{label} traverses a symlink/junction/reparse point: {current}")
    resolved_root = lexical_root.resolve()
    resolved_path = lexical_path.resolve(strict=False)
    try:
        if Path(os.path.commonpath([resolved_root, resolved_path])) != resolved_root:
            raise PublishError(f"{label} resolves outside the isolated workspace")
    except ValueError as exc:
        raise PublishError(f"{label} resolves to another volume") from exc
    return lexical_path


def validate_payload_relative_path(relative: Path, label: str) -> None:
    if relative.is_absolute() or not relative.parts:
        raise PublishError(f"{label} has an invalid relative path: {relative}")
    for component in relative.parts:
        if component in {"", ".", ".."} or component.rstrip(" .") != component:
            raise PublishError(f"{label} contains an unsafe path component: {relative}")
        if component.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
            raise PublishError(f"{label} contains a reserved Windows name: {relative}")


def ensure_regular_tree(root: Path, label: str) -> list[Path]:
    root = Path(os.path.abspath(root))
    if not root.is_dir() or is_reparse_point(root):
        raise PublishError(f"{label} must be a regular directory: {root}")
    files: list[Path] = []
    seen: set[str] = set()
    for current_raw, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(current_raw)
        if is_reparse_point(current):
            raise PublishError(f"{label} contains a reparse directory: {current}")
        for name in sorted(dirnames + filenames):
            path = current / name
            relative = path.relative_to(root)
            validate_payload_relative_path(relative, label)
            folded = relative.as_posix().casefold()
            if folded in seen:
                raise PublishError(f"{label} contains a Windows case-fold collision: {relative}")
            seen.add(folded)
            if is_reparse_point(path):
                raise PublishError(f"{label} contains a symlink/junction/reparse point: {path}")
        for name in sorted(filenames):
            path = current / name
            if not path.is_file():
                raise PublishError(f"{label} contains a non-regular file: {path}")
            files.append(path)
    if not files:
        raise PublishError(f"{label} must not be empty")
    return sorted(files, key=lambda value: value.relative_to(root).as_posix())


def collect_layer_entries(root: Path, layer: str) -> list[dict[str, Any]]:
    root = Path(os.path.abspath(root))
    result: list[dict[str, Any]] = []
    for path in ensure_regular_tree(root, f"{layer}_dir"):
        result.append(
            {
                "layer": layer,
                "relative_path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return result


def collect_payload_entries(raw_dir: Path, curated_dir: Path) -> list[dict[str, Any]]:
    entries = collect_layer_entries(raw_dir, "raw") + collect_layer_entries(curated_dir, "curated")
    return sorted(entries, key=lambda item: (item["layer"], item["relative_path"]))


def payload_digest(entries: list[dict[str, Any]]) -> str:
    return sha256_bytes(canonical_bytes(entries))


def copy_regular_tree(source: Path, destination: Path, expected: list[dict[str, Any]], layer: str) -> None:
    if destination.exists():
        raise PublishError(f"destination already exists: {destination}")
    destination.mkdir(parents=True)
    source = Path(os.path.abspath(source))
    for entry in (value for value in expected if value["layer"] == layer):
        relative = Path(entry["relative_path"])
        validate_payload_relative_path(relative, f"{layer}_dir")
        input_path = source / relative
        if is_reparse_point(input_path) or not input_path.is_file():
            raise PublishError(f"source changed while sealing staging: {input_path}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(input_path, target, follow_symlinks=False)
    verify_tree(destination, expected, layer)


def verify_tree(root: Path, entries: list[dict[str, Any]], layer: str) -> None:
    actual = collect_layer_entries(root, layer)
    expected = sorted(
        [entry for entry in entries if entry["layer"] == layer],
        key=lambda item: (item["layer"], item["relative_path"]),
    )
    if actual != expected:
        raise PublishError(f"sealed {layer} tree differs from its inventory")


def plan_hash(plan: dict[str, Any]) -> str:
    value = dict(plan)
    value.pop("plan_hash", None)
    return sha256_bytes(canonical_bytes(value))


def load_verified_plan(path: Path, expected_kind: str) -> dict[str, Any]:
    plan = load_json(path)
    if plan.get("kind") != expected_kind:
        raise PublishError(f"unexpected plan kind in {path}")
    if plan.get("plan_hash") != plan_hash(plan):
        raise PublishError(f"plan seal mismatch: {path}")
    return plan


def safe_remove_owned_stage(stage_dir: Path, staging_root: Path, run_id: str) -> None:
    if not stage_dir.exists():
        return
    resolved_stage = stage_dir.resolve()
    resolved_root = staging_root.resolve()
    try:
        if Path(os.path.commonpath([resolved_stage, resolved_root])) != resolved_root:
            raise PublishError(f"refusing to remove out-of-scope staging directory: {stage_dir}")
    except ValueError as exc:
        raise PublishError(f"invalid staging cleanup scope: {stage_dir}") from exc
    if resolved_stage == resolved_root:
        raise PublishError("refusing to remove staging root")
    owner = load_json(stage_dir / "owner.json")
    if owner.get("run_id") != run_id or owner.get("publisher_id") != PUBLISHER_ID:
        raise PublishError(f"staging ownership mismatch: {stage_dir}")
    shutil.rmtree(stage_dir)


def try_remove_owned_stage(
    stage_dir: Path,
    staging_root: Path,
    run_id: str,
) -> str | None:
    try:
        safe_remove_owned_stage(stage_dir, staging_root, run_id)
    except (OSError, PublishError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def inspect_windows_process(pid: int) -> tuple[str, str | None]:
    """Return (state, creation token) using kernel wait state on Windows."""

    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    error_access_denied = 5
    error_invalid_parameter = 87
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102

    class FileTime(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(
        process_query_limited_information | synchronize,
        False,
        pid,
    )
    if not handle:
        error = ctypes.get_last_error()
        if error == error_invalid_parameter:
            return "dead", None
        if error == error_access_denied:
            return "unknown", None
        return "unknown", None
    try:
        wait_state = kernel32.WaitForSingleObject(handle, 0)
        if wait_state == wait_object_0:
            return "dead", None
        if wait_state != wait_timeout:
            return "unknown", None
        creation = FileTime()
        exit_time = FileTime()
        kernel_time = FileTime()
        user_time = FileTime()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return "alive", None
        creation_value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return "alive", f"windows-filetime:{creation_value}"
    finally:
        kernel32.CloseHandle(handle)


def inspect_posix_process(pid: int) -> tuple[str, str | None]:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead", None
    except PermissionError:
        return "unknown", None
    except OSError:
        return "unknown", None
    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.is_file():
        return "alive", None
    try:
        raw = stat_path.read_text(encoding="utf-8")
        remainder = raw[raw.rfind(")") + 2 :].split()
        start_ticks = remainder[19]
    except (OSError, IndexError, ValueError):
        return "alive", None
    return "alive", f"linux-proc-start-ticks:{start_ticks}"


def inspect_process(pid: int) -> tuple[str, str | None]:
    if pid <= 0:
        return "dead", None
    if os.name == "nt":
        return inspect_windows_process(pid)
    return inspect_posix_process(pid)


def process_start_token(pid: int) -> str | None:
    state, token = inspect_process(pid)
    if state != "alive":
        return None
    return token


def process_is_alive(pid: int, expected_start_token: str | None = None) -> bool:
    state, current_start_token = inspect_process(pid)
    if state == "dead":
        return False
    if state == "unknown":
        return True
    if (
        expected_start_token is not None
        and current_start_token is not None
        and expected_start_token != current_start_token
    ):
        return False
    return True


@contextmanager
def catalog_transition_guard(catalog_path: Path) -> Iterator[None]:
    """Serialize writer-lock acquisition and stale-lock replacement.

    The operating-system byte lock is released automatically if the process
    terminates, so the transition guard itself cannot become stale.
    """

    guard_path = catalog_path.with_name(f".{catalog_path.name}.transition.guard")
    guard_path.parent.mkdir(parents=True, exist_ok=True)
    handle = guard_path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PublishError("catalog lock transition is already in progress") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def lock_binding_from_prepared(prepared: dict[str, Any]) -> dict[str, str]:
    binding = {
        "run_id": prepared.get("run_id"),
        "dataset_id": prepared.get("dataset_id"),
        "snapshot_id": prepared.get("snapshot_id"),
        "stage_path": prepared.get("stage_path"),
        "prepared_plan_hash": prepared.get("plan_hash"),
        "operational_request_hash": prepared.get("operational_request_hash"),
    }
    for field in ("run_id", "dataset_id", "snapshot_id"):
        validate_component(binding[field], f"prepared.{field}")
    if not isinstance(binding["stage_path"], str) or not binding["stage_path"]:
        raise PublishError("prepared.stage_path must be a non-empty path string")
    for field in ("prepared_plan_hash", "operational_request_hash"):
        value = binding[field]
        if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
            raise PublishError(f"prepared.{field} must be a lowercase SHA-256")
    return binding


def read_confirmed_stale_lock(
    lock_path: Path,
    run_id: str,
    confirm_sha256: str,
) -> tuple[dict[str, Any], bytes]:
    if not lock_path.is_file():
        raise PublishError("no catalog lock exists to recover")
    payload = lock_path.read_bytes()
    if sha256_bytes(payload) != confirm_sha256:
        raise PublishError("stale-lock confirmation SHA-256 does not match")
    lock = load_json(lock_path)
    if lock_path.read_bytes() != payload:
        raise PublishError("catalog lock changed while it was being inspected")
    if lock.get("schema_version") != 1 or lock.get("publisher_id") != PUBLISHER_ID:
        raise PublishError("catalog lock contract is invalid")
    if lock.get("run_id") != run_id:
        raise PublishError("stale-lock recovery requires the exact run_id")
    if lock.get("hostname") != socket.gethostname():
        raise PublishError("lock belongs to another host; manual review is required")
    lock_id = lock.get("lock_id")
    if not isinstance(lock_id, str) or not LOCK_ID_PATTERN.fullmatch(lock_id):
        raise PublishError("lock_id is missing or invalid")
    for field in ("dataset_id", "snapshot_id"):
        validate_component(lock.get(field), f"lock.{field}")
    if not isinstance(lock.get("stage_path"), str) or not lock["stage_path"]:
        raise PublishError("lock.stage_path is missing or invalid")
    for field in ("prepared_plan_hash", "operational_request_hash"):
        value = lock.get(field)
        if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
            raise PublishError(f"lock.{field} is missing or invalid")
    pid = lock.get("pid")
    expected_start_token = lock.get("process_start_token")
    if expected_start_token is not None and not isinstance(expected_start_token, str):
        raise PublishError("lock process_start_token is invalid")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or process_is_alive(pid, expected_start_token)
    ):
        raise PublishError("lock owner is alive or its PID is invalid")
    return lock, payload


def archive_confirmed_stale_lock(
    lock_path: Path,
    market_root: Path,
    lock: dict[str, Any],
    payload: bytes,
) -> Path:
    lock_id = lock["lock_id"]
    run_id = lock["run_id"]
    destination = market_root / "transactions" / "stale-locks" / f"{run_id}-{lock_id}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        if destination.read_bytes() != payload:
            raise PublishError(f"stale-lock audit destination conflicts: {destination}")
    else:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    if not lock_path.is_file() or lock_path.read_bytes() != payload:
        raise PublishError("catalog lock changed before stale-lock archival")
    lock_path.unlink()
    return destination


def create_catalog_lock_file(catalog_path: Path, prepared: dict[str, Any]) -> str:
    lock_path = catalog_path.with_name(f".{catalog_path.name}.lock")
    lock_id = uuid.uuid4().hex
    owner_start_token = process_start_token(os.getpid())
    binding = lock_binding_from_prepared(prepared)
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as exc:
        raise PublishError(f"catalog writer lock already exists: {lock_path}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        lock_value = {
            "schema_version": 1,
            "publisher_id": PUBLISHER_ID,
            **binding,
            "lock_id": lock_id,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "created_at": utc_now(),
        }
        if owner_start_token is not None:
            lock_value["process_start_token"] = owner_start_token
        json.dump(
            lock_value,
            handle,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return lock_id


def remove_owned_catalog_lock(catalog_path: Path, lock_id: str) -> None:
    lock_path = catalog_path.with_name(f".{catalog_path.name}.lock")
    if not lock_path.exists():
        return
    try:
        current = load_json(lock_path)
    except PublishError:
        current = {}
    if current.get("lock_id") == lock_id:
        lock_path.unlink()


@contextmanager
def catalog_lock(catalog_path: Path, prepared: dict[str, Any]) -> Iterator[None]:
    lock_path = catalog_path.with_name(f".{catalog_path.name}.lock")
    with catalog_transition_guard(catalog_path):
        if lock_path.exists():
            raise PublishError(f"catalog writer lock already exists: {lock_path}")
        lock_id = create_catalog_lock_file(catalog_path, prepared)
    try:
        yield
    finally:
        remove_owned_catalog_lock(catalog_path, lock_id)


def validate_catalog_for_workspace(catalog: dict[str, Any], workspace_root: Path) -> None:
    raw_root = catalog.get("workspace_root")
    if not isinstance(raw_root, str):
        raise PublishError("catalog.workspace_root must be a path string")
    if Path(raw_root.replace("/", os.sep)).resolve() != workspace_root.resolve():
        raise PublishError("catalog.workspace_root does not match the isolated workspace")
    if catalog.get("schema_version") != 1 or catalog.get("catalog_mode") != "federated_logical_catalog":
        raise PublishError("catalog is not an admitted federated logical catalog")
    for field in ("stores", "datasets", "retired_datasets", "publication_contracts"):
        if not isinstance(catalog.get(field), list):
            raise PublishError(f"catalog.{field} must be an array")


def validate_catalog_candidate(catalog: dict[str, Any]) -> None:
    tools_dir = Path(__file__).resolve().parent
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    try:
        from validate_dataset_catalog import validate_catalog
    except ImportError as exc:
        raise PublishError(f"cannot load catalog validator: {exc}") from exc
    errors = validate_catalog(catalog)
    if errors:
        raise PublishError("candidate catalog validation failed: " + " | ".join(errors))


def find_catalog_item(catalog: dict[str, Any], dataset_id: str, snapshot_id: str) -> dict[str, Any] | None:
    matches = [
        item
        for item in catalog.get("datasets", [])
        if item.get("dataset_id") == dataset_id and item.get("snapshot_id") == snapshot_id
    ]
    if len(matches) > 1:
        raise PublishError(f"catalog contains duplicate identity: {dataset_id}@{snapshot_id}")
    return matches[0] if matches else None


def transaction_path(market_root: Path, run_id: str) -> Path:
    return market_root / "transactions" / f"{run_id}.json"


def write_transaction(market_root: Path, run_id: str, state: str, **fields: Any) -> None:
    validate_component(run_id, "transaction.run_id")
    for field in ("dataset_id", "snapshot_id"):
        validate_component(fields.get(field), f"transaction.{field}")
    operational_hash = fields.get("operational_request_hash")
    if not isinstance(operational_hash, str) or not SHA256_PATTERN.fullmatch(operational_hash):
        raise PublishError("transaction.operational_request_hash must be a lowercase SHA-256")
    stage_path = fields.get("stage_path")
    if not isinstance(stage_path, str) or not stage_path:
        raise PublishError("transaction.stage_path must be a non-empty path string")
    prepared_hash = fields.get("prepared_plan_hash")
    if prepared_hash is not None and (
        not isinstance(prepared_hash, str) or not SHA256_PATTERN.fullmatch(prepared_hash)
    ):
        raise PublishError("transaction.prepared_plan_hash must be null or a lowercase SHA-256")
    value = {
        "schema_version": 1,
        "publisher_id": PUBLISHER_ID,
        "run_id": run_id,
        "state": state,
        "updated_at": utc_now(),
        **fields,
    }
    path = transaction_path(market_root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    identity_fields = (
        "publisher_id",
        "run_id",
        "dataset_id",
        "snapshot_id",
        "operational_request_hash",
        "stage_path",
    )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        existing = load_json(path)
        mismatches = [
            field
            for field in identity_fields
            if existing.get(field) != value.get(field)
        ]
        old_prepared_hash = existing.get("prepared_plan_hash")
        if (
            old_prepared_hash is not None
            or prepared_hash is not None
        ) and old_prepared_hash != prepared_hash:
            mismatches.append("prepared_plan_hash")
        if mismatches:
            raise PublishError(
                "run_id is already bound to another transaction: "
                + ", ".join(sorted(set(mismatches)))
            )
        write_json_atomic(path, value)
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
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


def prepared_transaction_fields(prepared: dict[str, Any]) -> dict[str, str]:
    binding = lock_binding_from_prepared(prepared)
    return {
        "dataset_id": binding["dataset_id"],
        "snapshot_id": binding["snapshot_id"],
        "stage_path": binding["stage_path"],
        "prepared_plan_hash": binding["prepared_plan_hash"],
        "operational_request_hash": binding["operational_request_hash"],
    }


def assert_record_binds_prepared(
    record: dict[str, Any],
    prepared: dict[str, Any],
    label: str,
) -> None:
    binding = lock_binding_from_prepared(prepared)
    mismatches = [
        field
        for field, expected in binding.items()
        if record.get(field) != expected
    ]
    if mismatches:
        raise PublishError(
            f"{label} does not bind the prepared plan: "
            + ", ".join(sorted(mismatches))
        )


def load_prepared_from_record(
    *,
    workspace_root: Path,
    record: dict[str, Any],
    run_id: str,
    label: str,
) -> dict[str, Any]:
    stage_raw = record.get("stage_path")
    if not isinstance(stage_raw, str):
        raise PublishError(f"{label}.stage_path is missing")
    stage_dir = assert_path_under_workspace(
        Path(stage_raw.replace("/", os.sep)),
        workspace_root,
        f"{label}.stage_path",
    )
    prepared = load_verified_plan(
        stage_dir / "prepared-plan.json",
        "PREPARED_SNAPSHOT",
    )
    validate_prepared_for_workspace(
        workspace_root,
        prepared,
        expected_run_id=run_id,
    )
    assert_record_binds_prepared(record, prepared, label)
    return prepared


def workspace_relative(path: Path, workspace_root: Path) -> str:
    return path.resolve().relative_to(workspace_root.resolve()).as_posix()


def build_paths(workspace_root: Path, request: dict[str, Any]) -> dict[str, Path]:
    market_root = workspace_root / "data" / "market"
    raw_final = (
        market_root
        / "raw"
        / request["source_id"]
        / request["dataset_id"]
        / f"ingest_date={request['ingest_date']}"
        / f"snapshot={request['snapshot_id']}"
    )
    curated_final = market_root / "curated" / request["dataset_id"] / f"schema={request['dataset_schema_version']}"
    for segment in request["partition_segments"]:
        curated_final /= segment
    curated_final /= f"snapshot={request['snapshot_id']}"
    snapshot_final = market_root / "snapshots" / request["dataset_id"] / request["snapshot_id"]
    stage_dir = market_root / ".staging" / request["dataset_id"] / request["run_id"]
    return {
        "market_root": market_root,
        "raw_final": raw_final,
        "curated_final": curated_final,
        "snapshot_final": snapshot_final,
        "stage_dir": stage_dir,
    }


def build_catalog_item(request: dict[str, Any], snapshot_path: Path, content_hash: str) -> dict[str, Any]:
    return {
        "dataset_id": request["dataset_id"],
        "snapshot_id": request["snapshot_id"],
        "store_id": request["store_id"],
        "owner_project": request["owner_project"],
        "physical_path": catalog_path_string(snapshot_path),
        "lifecycle": "review",
        "mutability": "immutable",
        "purpose_ceiling": request["purpose_ceiling"],
        "license_status": request["license_status"],
        "quality_status": request["quality_status"],
        "authority_paths": [
            catalog_path_string(snapshot_path / relative) for relative in AUTHORITY_RELATIVE_PATHS
        ],
        "content_hash": content_hash,
        "hash_algorithm": HASH_ALGORITHM,
        "consumers": request["consumers"],
        "known_limits": [
            "L1-D12A isolated synthetic fixture; infrastructure verification only",
            "Forbidden for factor research, backtests, Shadow, live use, or investment conclusions",
            "Does not admit or approve any real external data source",
        ],
        "notes": "Synthetic fixture registered only inside an isolated test catalog.",
    }


def build_snapshot_documents(
    request: dict[str, Any],
    paths: dict[str, Path],
    entries: list[dict[str, Any]],
    workspace_root: Path,
) -> tuple[dict[str, dict[str, Any]], str]:
    request_contract = immutable_request_contract(request)
    payload_hash = payload_digest(entries)
    partition_files: list[dict[str, Any]] = []
    for entry in entries:
        base = paths["raw_final"] if entry["layer"] == "raw" else paths["curated_final"]
        partition_files.append(
            {
                **entry,
                "workspace_relative_path": workspace_relative(base / Path(entry["relative_path"]), workspace_root),
            }
        )
    manifest = {
        "schema_version": 1,
        "dataset_id": request["dataset_id"],
        "snapshot_id": request["snapshot_id"],
        "root_path": catalog_path_string(paths["snapshot_final"]),
        "hash_algorithm": HASH_ALGORITHM,
        "payload_hash": payload_hash,
        "payload_hash_algorithm": "sha256-canonical-file-list-v1",
        "contract_id": request["contract_id"],
        "dataset_schema_version": request["dataset_schema_version"],
        "created_at": request["created_at"],
        "available_at": request["available_at"],
        "partition_segments": request["partition_segments"],
        "raw_root": workspace_relative(paths["raw_final"], workspace_root),
        "curated_root": workspace_relative(paths["curated_final"], workspace_root),
        "status": "SEALED_SNAPSHOT",
        "synthetic_fixture": True,
    }
    provenance = {
        "schema_version": 1,
        "dataset_id": request["dataset_id"],
        "snapshot_id": request["snapshot_id"],
        "mode": request["mode"],
        "source_id": request["source_id"],
        "contract_id": request["contract_id"],
        "request_contract_hash": request_contract_hash(request_contract),
        "payload_hash": payload_hash,
        "ingest_date": request["ingest_date"],
        "available_at": request["available_at"],
        "operational_run_records_root": "data/market/transactions",
    }
    quality = {
        "schema_version": 1,
        "dataset_id": request["dataset_id"],
        "snapshot_id": request["snapshot_id"],
        "overall": "PASS_FOR_INFRASTRUCTURE_TEST_ONLY",
        "catalog_quality_status": request["quality_status"],
        "purpose_ceiling": request["purpose_ceiling"],
        "quality_gate": request["quality_gate"],
        "synthetic_fixture": True,
        "checks": request.get("quality_checks", []),
        "warning": "Synthetic fixtures cannot support data-quality, research, or investment conclusions.",
    }
    partitions = {
        "schema_version": 1,
        "dataset_id": request["dataset_id"],
        "snapshot_id": request["snapshot_id"],
        "payload_hash": payload_hash,
        "files": partition_files,
    }
    content_hash = snapshot_contract_hash(request_contract, manifest, provenance, partitions, quality)
    manifest["content_hash"] = content_hash
    return {
        "request.json": request_contract,
        "manifest.json": manifest,
        "provenance.json": provenance,
        "partition-manifest.json": partitions,
        "quality/summary.json": quality,
    }, content_hash


def verify_staged_snapshot_metadata(snapshot_stage: Path, expected_hash: str) -> None:
    documents = {relative: load_json(snapshot_stage / relative) for relative in AUTHORITY_RELATIVE_PATHS}
    actual = snapshot_contract_hash(
        documents["request.json"],
        documents["manifest.json"],
        documents["provenance.json"],
        documents["partition-manifest.json"],
        documents["quality/summary.json"],
    )
    if actual != expected_hash or documents["manifest.json"].get("content_hash") != expected_hash:
        raise PublishError("staged snapshot contract hash mismatch")


def verify_staged_snapshot_tree(snapshot_stage: Path, expected_hash: str) -> None:
    files = ensure_regular_tree(snapshot_stage, "snapshot_stage")
    actual_paths = {
        path.relative_to(snapshot_stage).as_posix()
        for path in files
    }
    if actual_paths != set(AUTHORITY_RELATIVE_PATHS):
        raise PublishError("staged snapshot contains an unexpected authority file set")
    verify_staged_snapshot_metadata(snapshot_stage, expected_hash)


def validate_prepared_for_workspace(
    workspace_root: Path,
    prepared: dict[str, Any],
    *,
    expected_run_id: str | None = None,
) -> dict[str, Any]:
    """Re-derive every mutable path and identity before any commit-side write."""

    if prepared.get("kind") != "PREPARED_SNAPSHOT":
        raise PublishError("prepared plan kind is invalid")
    if prepared.get("plan_hash") != plan_hash(prepared):
        raise PublishError("prepared plan seal mismatch")
    stage_raw = prepared.get("stage_path")
    if not isinstance(stage_raw, str):
        raise PublishError("prepared.stage_path must be a path string")
    stage_dir = assert_path_under_workspace(
        Path(stage_raw.replace("/", os.sep)),
        workspace_root,
        "prepared.stage_path",
    )
    request_path = assert_path_under_workspace(
        stage_dir / "operational-request.json",
        workspace_root,
        "prepared.operational_request",
    )
    if not request_path.is_file() or is_reparse_point(request_path):
        raise PublishError("prepared operational request is missing or not a regular file")
    request = validate_request(load_json(request_path))
    if expected_run_id is not None and request["run_id"] != expected_run_id:
        raise PublishError("prepared operational request run_id mismatch")
    operational_hash = sha256_bytes(canonical_bytes(request))
    expected_paths = build_paths(workspace_root, request)
    expected_values = {
        "stage_path": expected_paths["stage_dir"],
        "raw_final": expected_paths["raw_final"],
        "curated_final": expected_paths["curated_final"],
        "snapshot_final": expected_paths["snapshot_final"],
    }
    for field, expected_path in expected_values.items():
        value = prepared.get(field)
        if not isinstance(value, str):
            raise PublishError(f"prepared.{field} must be a path string")
        actual_path = assert_path_under_workspace(
            Path(value.replace("/", os.sep)),
            workspace_root,
            f"prepared.{field}",
        )
        if actual_path.resolve(strict=False) != expected_path.resolve(strict=False):
            raise PublishError(f"prepared.{field} differs from the request-derived path")
    for field in ("run_id", "dataset_id", "snapshot_id"):
        if prepared.get(field) != request[field]:
            raise PublishError(f"prepared.{field} differs from the operational request")
    if prepared.get("operational_request_hash") != operational_hash:
        raise PublishError("prepared operational request hash mismatch")
    if prepared.get("request_contract_hash") != request_contract_hash(
        immutable_request_contract(request)
    ):
        raise PublishError("prepared stable request contract hash mismatch")
    entries = prepared.get("entries")
    if not isinstance(entries, list) or not entries:
        raise PublishError("prepared plan has no payload inventory")
    if prepared.get("payload_hash") != payload_digest(entries):
        raise PublishError("prepared payload inventory hash mismatch")
    content_hash = prepared.get("content_hash")
    if not isinstance(content_hash, str) or not SHA256_PATTERN.fullmatch(content_hash):
        raise PublishError("prepared content_hash is invalid")
    expected_item = build_catalog_item(
        request,
        expected_paths["snapshot_final"],
        content_hash,
    )
    if prepared.get("catalog_item") != expected_item:
        raise PublishError("prepared catalog item differs from the request-derived item")
    owner_path = stage_dir / "owner.json"
    if not owner_path.is_file() or is_reparse_point(owner_path):
        raise PublishError("prepared staging owner is missing or not a regular file")
    owner = load_json(owner_path)
    if (
        owner.get("publisher_id") != PUBLISHER_ID
        or owner.get("run_id") != request["run_id"]
    ):
        raise PublishError("prepared staging ownership mismatch")
    lock_binding_from_prepared(prepared)
    return request


def prepare_candidate(
    *,
    workspace_root: Path,
    request: dict[str, Any],
    raw_dir: Path,
    curated_dir: Path,
) -> dict[str, Any]:
    paths = build_paths(workspace_root, request)
    stage_dir = paths["stage_dir"]
    staging_root = paths["market_root"] / ".staging"
    operational_hash = sha256_bytes(canonical_bytes(request))
    if stage_dir.exists():
        owner = load_json(stage_dir / "owner.json")
        if owner.get("publisher_id") != PUBLISHER_ID or owner.get("run_id") != request["run_id"]:
            raise PublishError(f"staging ownership mismatch: {stage_dir}")
        plan = load_verified_plan(stage_dir / "prepared-plan.json", "PREPARED_SNAPSHOT")
        if plan.get("operational_request_hash") != operational_hash:
            raise PublishError("existing staging belongs to a different operational request")
        validate_prepared_for_workspace(
            workspace_root,
            plan,
            expected_run_id=request["run_id"],
        )
        entries = plan.get("entries")
        if not isinstance(entries, list) or not entries:
            raise PublishError("prepared plan has no payload inventory")
        for layer, final_key in (("raw", "raw_final"), ("curated", "curated_final")):
            staged = stage_dir / layer
            final = Path(str(plan[final_key]).replace("/", os.sep))
            if staged.exists():
                verify_tree(staged, entries, layer)
            elif final.exists():
                verify_tree(final, entries, layer)
            else:
                raise PublishError(f"prepared candidate has neither staged nor final {layer} payload")
        staged_snapshot = stage_dir / "snapshot"
        if staged_snapshot.exists():
            verify_staged_snapshot_tree(staged_snapshot, plan["content_hash"])
        elif not Path(str(plan["snapshot_final"]).replace("/", os.sep)).exists():
            raise PublishError("prepared candidate has neither staged nor final snapshot")
        return plan

    source_entries = collect_payload_entries(raw_dir, curated_dir)
    temporary = stage_dir.with_name(f"{stage_dir.name}.{uuid.uuid4().hex}.tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    if temporary.exists():
        raise PublishError(f"private staging path already exists: {temporary}")
    try:
        temporary.mkdir()
        write_json(
            temporary / "owner.json",
            {
                "schema_version": 1,
                "publisher_id": PUBLISHER_ID,
                "run_id": request["run_id"],
                "created_at": utc_now(),
            },
        )
        copy_regular_tree(raw_dir, temporary / "raw", source_entries, "raw")
        copy_regular_tree(curated_dir, temporary / "curated", source_entries, "curated")
        staged_entries = collect_payload_entries(temporary / "raw", temporary / "curated")
        source_entries_after = collect_payload_entries(raw_dir, curated_dir)
        if staged_entries != source_entries or source_entries_after != source_entries:
            raise PublishError("input changed during staging; candidate was not sealed")
        documents, content_hash = build_snapshot_documents(request, paths, staged_entries, workspace_root)
        snapshot_stage = temporary / "snapshot"
        snapshot_stage.mkdir()
        for relative, document in documents.items():
            write_json(snapshot_stage / relative, document)
        verify_staged_snapshot_tree(snapshot_stage, content_hash)
        catalog_item = build_catalog_item(request, paths["snapshot_final"], content_hash)
        plan: dict[str, Any] = {
            "schema_version": 1,
            "kind": "PREPARED_SNAPSHOT",
            "publisher_id": PUBLISHER_ID,
            "run_id": request["run_id"],
            "dataset_id": request["dataset_id"],
            "snapshot_id": request["snapshot_id"],
            "operational_request_hash": operational_hash,
            "request_contract_hash": request_contract_hash(immutable_request_contract(request)),
            "payload_hash": payload_digest(staged_entries),
            "content_hash": content_hash,
            "raw_final": catalog_path_string(paths["raw_final"]),
            "curated_final": catalog_path_string(paths["curated_final"]),
            "snapshot_final": catalog_path_string(paths["snapshot_final"]),
            "stage_path": catalog_path_string(stage_dir),
            "entries": staged_entries,
            "catalog_item": catalog_item,
        }
        plan["plan_hash"] = plan_hash(plan)
        write_json(temporary / "operational-request.json", request)
        write_json(temporary / "prepared-plan.json", plan)
        os.replace(temporary, stage_dir)
    finally:
        if temporary.exists():
            owner_path = temporary / "owner.json"
            if owner_path.is_file():
                owner = load_json(owner_path)
                if owner.get("publisher_id") == PUBLISHER_ID and owner.get("run_id") == request["run_id"]:
                    shutil.rmtree(temporary)
    validate_prepared_for_workspace(
        workspace_root,
        plan,
        expected_run_id=request["run_id"],
    )
    write_transaction(
        paths["market_root"],
        request["run_id"],
        "STAGED",
        **prepared_transaction_fields(plan),
        content_hash=plan["content_hash"],
    )
    return plan


def atomic_quarantine_inputs(
    *,
    market_root: Path,
    request: dict[str, Any],
    raw_dir: Path,
    curated_dir: Path,
    entries: list[dict[str, Any]],
    reason: str,
) -> Path:
    destination = market_root / "quarantine" / request["dataset_id"] / request["run_id"]
    payload_hash = payload_digest(entries)
    if destination.exists():
        failure = load_json(destination / "failure.json")
        if failure.get("reason") == reason and failure.get("payload_hash") == payload_hash:
            return destination
        raise PublishError(f"quarantine identity conflict: {destination}")
    temporary = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    try:
        temporary.mkdir()
        copy_regular_tree(raw_dir, temporary / "raw", entries, "raw")
        copy_regular_tree(curated_dir, temporary / "curated", entries, "curated")
        write_json(temporary / "request.json", request)
        write_json(
            temporary / "failure.json",
            {
                "schema_version": 1,
                "publisher_id": PUBLISHER_ID,
                "dataset_id": request["dataset_id"],
                "snapshot_id": request["snapshot_id"],
                "run_id": request["run_id"],
                "reason": reason,
                "payload_hash": payload_hash,
                "status": "QUARANTINED",
            },
        )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination


def quarantine_stage(stage_dir: Path, market_root: Path, plan: dict[str, Any], reason: str) -> Path:
    destination = market_root / "quarantine" / plan["dataset_id"] / plan["run_id"]
    if destination.exists():
        failure = load_json(destination / "failure.json")
        if failure.get("reason") == reason and failure.get("content_hash") == plan["content_hash"]:
            return destination
        raise PublishError(f"quarantine identity conflict: {destination}")
    write_json(
        stage_dir / "failure.json",
        {
            "schema_version": 1,
            "publisher_id": PUBLISHER_ID,
            "dataset_id": plan["dataset_id"],
            "snapshot_id": plan["snapshot_id"],
            "run_id": plan["run_id"],
            "reason": reason,
            "content_hash": plan["content_hash"],
            "status": "QUARANTINED",
        },
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(stage_dir, destination)
    return destination


def create_or_load_commit_plan(
    stage_dir: Path,
    prepared: dict[str, Any],
    catalog: dict[str, Any],
    catalog_hash_before: str,
) -> dict[str, Any]:
    path = stage_dir / "commit-plan.json"
    if path.exists():
        plan = load_verified_plan(path, "CATALOG_COMMIT")
        if plan.get("prepared_plan_hash") != prepared.get("plan_hash"):
            raise PublishError("commit plan does not bind the prepared plan")
        return plan
    candidate = copy.deepcopy(catalog)
    candidate["datasets"].append(copy.deepcopy(prepared["catalog_item"]))
    candidate["updated_at"] = utc_now()
    candidate_hash = sha256_bytes(
        json.dumps(
            candidate,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    plan = {
        "schema_version": 1,
        "kind": "CATALOG_COMMIT",
        "publisher_id": PUBLISHER_ID,
        "run_id": prepared["run_id"],
        "dataset_id": prepared["dataset_id"],
        "snapshot_id": prepared["snapshot_id"],
        "prepared_plan_hash": prepared["plan_hash"],
        "base_catalog_hash": catalog_hash_before,
        "candidate_catalog_hash": candidate_hash,
        "candidate_catalog": candidate,
    }
    plan["plan_hash"] = plan_hash(plan)
    write_json(path, plan)
    return plan


def assert_same_volume(catalog_path: Path, paths: list[Path]) -> None:
    catalog_drive = catalog_path.resolve().drive.casefold()
    catalog_device = catalog_path.parent.stat().st_dev
    for path in paths:
        existing = path
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        if existing.resolve().drive.casefold() != catalog_drive or existing.stat().st_dev != catalog_device:
            raise PublishError(f"atomic publication target is not on the catalog volume: {path}")


def atomic_replace_catalog(
    *,
    catalog_path: Path,
    candidate: dict[str, Any],
    expected_before_hash: str,
    expected_after_hash: str,
    failpoint: str | None,
) -> None:
    validate_catalog_candidate(candidate)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=catalog_path.parent,
            prefix=f".{catalog_path.name}.candidate.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(
                candidate,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if sha256_file(temp_path) != expected_after_hash:
            raise PublishError("candidate catalog hash differs from sealed commit plan")
        if failpoint == "before_catalog_replace":
            raise InjectedFailure("injected failure before catalog replace")
        if sha256_file(catalog_path) != expected_before_hash:
            raise PublishError("catalog changed after commit plan sealing; refusing lost update")
        os.replace(temp_path, catalog_path)
        temp_path = None
        if failpoint == "hard_crash_after_catalog_replace":
            os._exit(92)
        if failpoint == "after_catalog_replace":
            raise InjectedFailure("injected failure after catalog replace")
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def verify_final_snapshot(
    workspace_root: Path,
    catalog: dict[str, Any],
    catalog_item: dict[str, Any],
) -> None:
    snapshot_path = Path(str(catalog_item["physical_path"]).replace("/", os.sep))
    errors = verify_snapshot_contract(
        catalog=catalog,
        item=catalog_item,
        snapshot_path=snapshot_path,
        deep_payload=True,
    )
    if errors:
        raise PublishError("final snapshot verification failed: " + " | ".join(errors))
    assert_path_under_workspace(snapshot_path, workspace_root, "snapshot_path")


def commit_prepared(
    *,
    workspace_root: Path,
    catalog_path: Path,
    prepared: dict[str, Any],
    failpoint: str | None,
) -> PublishResult:
    validate_prepared_for_workspace(
        workspace_root,
        prepared,
        expected_run_id=prepared.get("run_id"),
    )
    market_root = workspace_root / "data" / "market"
    stage_dir = Path(str(prepared["stage_path"]).replace("/", os.sep))
    staging_root = market_root / ".staging"
    run_id = prepared["run_id"]
    catalog = load_json(catalog_path)
    validate_catalog_for_workspace(catalog, workspace_root)
    catalog_hash_before = sha256_file(catalog_path)
    existing = find_catalog_item(catalog, prepared["dataset_id"], prepared["snapshot_id"])
    if existing is not None:
        if existing.get("content_hash") != prepared["content_hash"]:
            quarantine = quarantine_stage(stage_dir, market_root, prepared, "DATASET_SNAPSHOT_IDENTITY_CONFLICT")
            write_transaction(
                market_root,
                run_id,
                "QUARANTINED",
                **prepared_transaction_fields(prepared),
                reason="DATASET_SNAPSHOT_IDENTITY_CONFLICT",
                quarantine_path=catalog_path_string(quarantine),
            )
            return PublishResult(
                "QUARANTINED",
                prepared["dataset_id"],
                prepared["snapshot_id"],
                run_id,
                prepared["content_hash"],
                None,
                catalog_path_string(quarantine),
                catalog_hash_before,
                sha256_file(catalog_path),
            )
        verify_final_snapshot(workspace_root, catalog, existing)
        cleanup_error = (
            try_remove_owned_stage(stage_dir, staging_root, run_id)
            if stage_dir.exists()
            else None
        )
        result_status = "NO_CHANGE_CLEANUP_PENDING" if cleanup_error else "NO_CHANGE"
        write_transaction(
            market_root,
            run_id,
            result_status,
            **prepared_transaction_fields(prepared),
            content_hash=prepared["content_hash"],
            catalog_hash_before=catalog_hash_before,
            catalog_hash_after=catalog_hash_before,
            **({"cleanup_error": cleanup_error} if cleanup_error else {}),
        )
        return PublishResult(
            result_status,
            prepared["dataset_id"],
            prepared["snapshot_id"],
            run_id,
            prepared["content_hash"],
            existing["physical_path"],
            None,
            catalog_hash_before,
            catalog_hash_before,
        )

    commit_plan = create_or_load_commit_plan(stage_dir, prepared, catalog, catalog_hash_before)
    current_hash = sha256_file(catalog_path)
    if current_hash != commit_plan["base_catalog_hash"]:
        if current_hash == commit_plan["candidate_catalog_hash"]:
            committed = find_catalog_item(
                load_json(catalog_path), prepared["dataset_id"], prepared["snapshot_id"]
            )
            if committed is None:
                raise PublishError("candidate catalog hash is present but snapshot identity is missing")
            verify_final_snapshot(workspace_root, load_json(catalog_path), committed)
            cleanup_error = (
                try_remove_owned_stage(stage_dir, staging_root, run_id)
                if stage_dir.exists()
                else None
            )
            result_status = "NO_CHANGE_CLEANUP_PENDING" if cleanup_error else "NO_CHANGE"
            write_transaction(
                market_root,
                run_id,
                result_status,
                **prepared_transaction_fields(prepared),
                content_hash=prepared["content_hash"],
                snapshot_path=committed["physical_path"],
                catalog_hash_before=commit_plan["base_catalog_hash"],
                catalog_hash_after=current_hash,
                commit_plan_hash=commit_plan["plan_hash"],
                **({"cleanup_error": cleanup_error} if cleanup_error else {}),
            )
            return PublishResult(
                result_status,
                prepared["dataset_id"],
                prepared["snapshot_id"],
                run_id,
                prepared["content_hash"],
                committed["physical_path"],
                None,
                commit_plan["base_catalog_hash"],
                current_hash,
            )
        raise PublishError("catalog base changed after commit plan creation; manual rebase is required")

    entries = prepared["entries"]
    raw_final = Path(str(prepared["raw_final"]).replace("/", os.sep))
    curated_final = Path(str(prepared["curated_final"]).replace("/", os.sep))
    snapshot_final = Path(str(prepared["snapshot_final"]).replace("/", os.sep))
    for label, path in (
        ("prepared.stage_path", stage_dir),
        ("prepared.raw_final", raw_final),
        ("prepared.curated_final", curated_final),
        ("prepared.snapshot_final", snapshot_final),
    ):
        assert_path_under_workspace(path, workspace_root, label)
    assert_same_volume(catalog_path, [stage_dir, raw_final, curated_final, snapshot_final])
    for layer, final_payload in (("raw", raw_final), ("curated", curated_final)):
        staged_payload = stage_dir / layer
        if final_payload.exists():
            verify_tree(final_payload, entries, layer)
            if staged_payload.exists():
                verify_tree(staged_payload, entries, layer)
                shutil.rmtree(staged_payload)
        else:
            if not staged_payload.exists():
                raise PublishError(f"missing both staged and final {layer} payload")
            verify_tree(staged_payload, entries, layer)
            assert_path_under_workspace(final_payload, workspace_root, f"prepared.{layer}_final")
            final_payload.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_payload, final_payload)
            verify_tree(final_payload, entries, layer)

    snapshot_stage = stage_dir / "snapshot"
    if snapshot_final.exists():
        verify_final_snapshot(workspace_root, commit_plan["candidate_catalog"], prepared["catalog_item"])
        if snapshot_stage.exists():
            verify_staged_snapshot_tree(snapshot_stage, prepared["content_hash"])
            shutil.rmtree(snapshot_stage)
    else:
        if not snapshot_stage.exists():
            raise PublishError("missing both staged and final snapshot contract")
        verify_staged_snapshot_tree(snapshot_stage, prepared["content_hash"])
        assert_path_under_workspace(
            snapshot_final,
            workspace_root,
            "prepared.snapshot_final",
        )
        snapshot_final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(snapshot_stage, snapshot_final)
        verify_final_snapshot(workspace_root, commit_plan["candidate_catalog"], prepared["catalog_item"])

    write_transaction(
        market_root,
        run_id,
        "SNAPSHOT_FINALIZED_NOT_YET_CATALOG_VISIBLE",
        **prepared_transaction_fields(prepared),
        content_hash=prepared["content_hash"],
        snapshot_path=catalog_path_string(snapshot_final),
        catalog_hash_before=commit_plan["base_catalog_hash"],
        commit_plan_hash=commit_plan["plan_hash"],
    )
    if failpoint == "hard_crash_after_snapshot_finalize":
        os._exit(91)
    if failpoint == "after_snapshot_finalize":
        raise InjectedFailure("injected failure after snapshot finalize")

    atomic_replace_catalog(
        catalog_path=catalog_path,
        candidate=commit_plan["candidate_catalog"],
        expected_before_hash=commit_plan["base_catalog_hash"],
        expected_after_hash=commit_plan["candidate_catalog_hash"],
        failpoint=failpoint,
    )
    catalog_hash_after = sha256_file(catalog_path)
    if catalog_hash_after != commit_plan["candidate_catalog_hash"]:
        raise PublishError("catalog commit hash mismatch after replace")
    write_transaction(
        market_root,
        run_id,
        "CATALOG_COMMITTED",
        **prepared_transaction_fields(prepared),
        content_hash=prepared["content_hash"],
        snapshot_path=catalog_path_string(snapshot_final),
        catalog_hash_before=commit_plan["base_catalog_hash"],
        catalog_hash_after=catalog_hash_after,
        commit_plan_hash=commit_plan["plan_hash"],
    )
    cleanup_error = (
        try_remove_owned_stage(stage_dir, staging_root, run_id)
        if stage_dir.exists()
        else None
    )
    if cleanup_error:
        write_transaction(
            market_root,
            run_id,
            "PUBLISHED_CLEANUP_PENDING",
            **prepared_transaction_fields(prepared),
            content_hash=prepared["content_hash"],
            snapshot_path=catalog_path_string(snapshot_final),
            catalog_hash_before=commit_plan["base_catalog_hash"],
            catalog_hash_after=catalog_hash_after,
            commit_plan_hash=commit_plan["plan_hash"],
            cleanup_error=cleanup_error,
        )
    return PublishResult(
        "PUBLISHED_CLEANUP_PENDING" if cleanup_error else "PUBLISHED",
        prepared["dataset_id"],
        prepared["snapshot_id"],
        run_id,
        prepared["content_hash"],
        catalog_path_string(snapshot_final),
        None,
        commit_plan["base_catalog_hash"],
        catalog_hash_after,
    )


@contextmanager
def recovery_catalog_lock(
    *,
    workspace_root: Path,
    catalog_path: Path,
    run_id: str,
    confirm_lock_sha256: str | None,
    expected_operational_request_hash: str | None,
) -> Iterator[dict[str, Any]]:
    market_root = workspace_root / "data" / "market"
    lock_path = catalog_path.with_name(f".{catalog_path.name}.lock")
    with catalog_transition_guard(catalog_path):
        if lock_path.exists():
            if confirm_lock_sha256 is None:
                raise PublishError("recovery requires the exact stale lock SHA-256")
            lock, payload = read_confirmed_stale_lock(
                lock_path,
                run_id,
                confirm_lock_sha256,
            )
            prepared = load_prepared_from_record(
                workspace_root=workspace_root,
                record=lock,
                run_id=run_id,
                label="lock",
            )
            if (
                expected_operational_request_hash is not None
                and prepared["operational_request_hash"]
                != expected_operational_request_hash
            ):
                raise PublishError(
                    "stale lock belongs to a different operational request"
                )
            archive_confirmed_stale_lock(
                lock_path,
                market_root,
                lock,
                payload,
            )
        else:
            if confirm_lock_sha256 is not None:
                raise PublishError("no catalog lock exists for the supplied recovery hash")
            transaction = load_json(transaction_path(market_root, run_id))
            if (
                transaction.get("schema_version") != 1
                or transaction.get("publisher_id") != PUBLISHER_ID
                or transaction.get("run_id") != run_id
            ):
                raise PublishError("transaction recovery contract is invalid")
            if transaction.get("state") not in {
                "STAGED",
                "SNAPSHOT_FINALIZED_NOT_YET_CATALOG_VISIBLE",
                "PUBLISHED_CLEANUP_PENDING",
                "NO_CHANGE_CLEANUP_PENDING",
            }:
                raise PublishError(
                    f"transaction state is not recoverable: {transaction.get('state')!r}"
                )
            prepared = load_prepared_from_record(
                workspace_root=workspace_root,
                record=transaction,
                run_id=run_id,
                label="transaction",
            )
            if (
                expected_operational_request_hash is not None
                and prepared["operational_request_hash"]
                != expected_operational_request_hash
            ):
                raise PublishError(
                    "transaction belongs to a different operational request"
                )
        lock_id = create_catalog_lock_file(catalog_path, prepared)
    try:
        yield prepared
    finally:
        remove_owned_catalog_lock(catalog_path, lock_id)


def publish_snapshot(
    *,
    workspace_root: Path,
    catalog_path: Path,
    request_path: Path,
    raw_dir: Path,
    curated_dir: Path,
    failpoint: str | None = None,
    recover_stale_lock_sha256: str | None = None,
) -> PublishResult:
    workspace_root = Path(os.path.abspath(workspace_root))
    catalog_path = Path(os.path.abspath(catalog_path))
    request_path = assert_path_under_workspace(request_path, workspace_root, "request_path")
    raw_dir = assert_path_under_workspace(raw_dir, workspace_root, "raw_dir")
    curated_dir = assert_path_under_workspace(curated_dir, workspace_root, "curated_dir")
    if failpoint is not None and failpoint not in ALLOWED_FAILPOINTS:
        raise PublishError(f"unknown failpoint: {failpoint}")
    assert_isolated_synthetic_workspace(workspace_root, catalog_path)
    request = validate_request(load_json(request_path))
    catalog = load_json(catalog_path)
    validate_catalog_for_workspace(catalog, workspace_root)
    paths = build_paths(workspace_root, request)
    operational_request_hash = sha256_bytes(canonical_bytes(request))
    lock_path = catalog_path.with_name(f".{catalog_path.name}.lock")
    if lock_path.exists() or recover_stale_lock_sha256 is not None:
        if recover_stale_lock_sha256 is None:
            raise PublishError(f"catalog writer lock already exists: {lock_path}")
        return recover_snapshot(
            workspace_root=workspace_root,
            catalog_path=catalog_path,
            run_id=request["run_id"],
            confirm_lock_sha256=recover_stale_lock_sha256,
            expected_operational_request_hash=operational_request_hash,
        )

    if request["quality_gate"] != "PASS":
        entries = collect_payload_entries(raw_dir, curated_dir)
        quarantine = atomic_quarantine_inputs(
            market_root=paths["market_root"],
            request=request,
            raw_dir=raw_dir,
            curated_dir=curated_dir,
            entries=entries,
            reason="QUALITY_GATE_FAILED",
        )
        before = sha256_file(catalog_path)
        write_transaction(
            paths["market_root"],
            request["run_id"],
            "QUARANTINED",
            dataset_id=request["dataset_id"],
            snapshot_id=request["snapshot_id"],
            operational_request_hash=operational_request_hash,
            stage_path=catalog_path_string(paths["stage_dir"]),
            prepared_plan_hash=None,
            reason="QUALITY_GATE_FAILED",
            quarantine_path=catalog_path_string(quarantine),
        )
        return PublishResult(
            "QUARANTINED",
            request["dataset_id"],
            request["snapshot_id"],
            request["run_id"],
            None,
            None,
            catalog_path_string(quarantine),
            before,
            sha256_file(catalog_path),
        )

    prepared = prepare_candidate(
        workspace_root=workspace_root,
        request=request,
        raw_dir=raw_dir,
        curated_dir=curated_dir,
    )
    expected_hash = request.get("expected_payload_hash")
    if expected_hash is not None and expected_hash != prepared["payload_hash"]:
        quarantine = quarantine_stage(
            paths["stage_dir"], paths["market_root"], prepared, "EXPECTED_PAYLOAD_HASH_MISMATCH"
        )
        before = sha256_file(catalog_path)
        write_transaction(
            paths["market_root"],
            request["run_id"],
            "QUARANTINED",
            **prepared_transaction_fields(prepared),
            reason="EXPECTED_PAYLOAD_HASH_MISMATCH",
            quarantine_path=catalog_path_string(quarantine),
        )
        return PublishResult(
            "QUARANTINED",
            request["dataset_id"],
            request["snapshot_id"],
            request["run_id"],
            prepared["content_hash"],
            None,
            catalog_path_string(quarantine),
            before,
            sha256_file(catalog_path),
        )

    with catalog_lock(catalog_path, prepared):
        return commit_prepared(
            workspace_root=workspace_root,
            catalog_path=catalog_path,
            prepared=prepared,
            failpoint=failpoint,
        )


def recover_snapshot(
    *,
    workspace_root: Path,
    catalog_path: Path,
    run_id: str,
    confirm_lock_sha256: str | None,
    expected_operational_request_hash: str | None = None,
) -> PublishResult:
    workspace_root = Path(os.path.abspath(workspace_root))
    catalog_path = Path(os.path.abspath(catalog_path))
    validate_component(run_id, "run_id")
    assert_isolated_synthetic_workspace(workspace_root, catalog_path)
    with recovery_catalog_lock(
        workspace_root=workspace_root,
        catalog_path=catalog_path,
        run_id=run_id,
        confirm_lock_sha256=confirm_lock_sha256,
        expected_operational_request_hash=expected_operational_request_hash,
    ) as prepared:
        return commit_prepared(
            workspace_root=workspace_root,
            catalog_path=catalog_path,
            prepared=prepared,
            failpoint=None,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="L1-D12A isolated synthetic atomic publisher")
    subparsers = parser.add_subparsers(dest="command", required=True)
    publish = subparsers.add_parser("publish", help="prepare and commit one synthetic fixture")
    publish.add_argument("--workspace-root", type=Path, required=True)
    publish.add_argument("--catalog", type=Path, required=True)
    publish.add_argument("--request", type=Path, required=True)
    publish.add_argument("--raw-dir", type=Path, required=True)
    publish.add_argument("--curated-dir", type=Path, required=True)
    publish.add_argument("--failpoint", choices=sorted(ALLOWED_FAILPOINTS))
    publish.add_argument("--recover-stale-lock-sha256")
    recover = subparsers.add_parser("recover", help="resume a sealed run without source inputs")
    recover.add_argument("--workspace-root", type=Path, required=True)
    recover.add_argument("--catalog", type=Path, required=True)
    recover.add_argument("--run-id", required=True)
    recover.add_argument("--confirm-lock-sha256")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "publish":
            result = publish_snapshot(
                workspace_root=args.workspace_root,
                catalog_path=args.catalog,
                request_path=args.request,
                raw_dir=args.raw_dir,
                curated_dir=args.curated_dir,
                failpoint=args.failpoint,
                recover_stale_lock_sha256=args.recover_stale_lock_sha256,
            )
        else:
            result = recover_snapshot(
                workspace_root=args.workspace_root,
                catalog_path=args.catalog,
                run_id=args.run_id,
                confirm_lock_sha256=args.confirm_lock_sha256,
            )
    except InjectedFailure as exc:
        print(f"D12A INJECTED FAILURE: {exc}", file=sys.stderr)
        return 3
    except PublishError as exc:
        print(f"D12A PUBLISH REJECTED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
