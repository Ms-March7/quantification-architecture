#!/usr/bin/env python3
"""Normalize one explicit hash-sealed local daily CSV below ``tmp/``.

The v1 adapter is deliberately narrow.  It accepts only a request file below
``tmp/source-normalization-adapter/requests/`` and one CSV already bound by a
``local_raw_seal_v1`` manifest.  It never discovers inputs, performs network
access, writes the formal catalog/market store, or grants research use.
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
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ID = "source_normalization_adapter_v1"
ENGINE_ID = "source_normalization_adapter/v1"
PURPOSE_CEILING = "INSPECTION_ONLY"
CONTRACT_PATH = (
    ROOT
    / "data"
    / "catalog"
    / "contracts"
    / "source_normalization_adapter_v1"
    / "contract.json"
)
INPUT_ROOT = ROOT / "tmp" / "local-raw-seal"
OUTPUT_ROOT = ROOT / "tmp" / "source-normalization-adapter"
REQUEST_ROOT = OUTPUT_ROOT / "requests"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SOURCE_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
DATASET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PERMANENT_ID_RE = re.compile(r"^[A-Z]{2}\.[A-Z0-9]{4}\.[A-Z0-9._-]{1,32}$")
DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")

CANONICAL_INPUT_FIELDS = [
    "source_security_id",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "previous_close",
    "volume",
    "amount",
]
CANONICAL_OUTPUT_FIELDS = [
    "permanent_security_id",
    "source_security_id",
    "trade_date",
    "open_cny_per_share",
    "high_cny_per_share",
    "low_cny_per_share",
    "close_cny_per_share",
    "previous_close_cny_per_share",
    "volume_shares",
    "amount_cny",
    "timezone",
    "available_at",
    "source_id",
    "dataset_id",
    "source_file_sha256",
    "raw_row_number",
    "raw_row_sha256",
]
REQUIRED_UNITS = {
    "open": "CNY_PER_SHARE",
    "high": "CNY_PER_SHARE",
    "low": "CNY_PER_SHARE",
    "close": "CNY_PER_SHARE",
    "previous_close": "CNY_PER_SHARE",
    "volume": "SHARE",
    "amount": "CNY",
}
REQUEST_FIELDS = {
    "schema_version",
    "contract_id",
    "run_id",
    "seal_run_id",
    "sealed_relative_path",
    "sealed_sha256",
    "source_id",
    "dataset_id",
    "trade_date",
    "timezone",
    "available_at",
    "field_map",
    "unit_map",
    "security_id_map",
    "expected_permanent_security_ids",
}


class NormalizationError(RuntimeError):
    """Fail-closed request, seal, schema, identity, PIT, or value error."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_constant(value: str) -> None:
    raise NormalizationError(f"non-standard JSON constant is forbidden: {value}")


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NormalizationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    payload = path.read_bytes()
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NormalizationError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise NormalizationError(f"{label} must be a JSON object")
    return value, payload


def _is_reparse_or_symlink(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _assert_plain_chain(base: Path, target: Path) -> None:
    base_abs = Path(os.path.abspath(base))
    target_abs = Path(os.path.abspath(target))
    try:
        relative = target_abs.relative_to(base_abs)
    except ValueError as exc:
        raise NormalizationError("path escapes its allowed root") from exc
    current = base_abs
    if current.exists() and _is_reparse_or_symlink(current):
        raise NormalizationError(f"reparse/symlink component is forbidden: {current}")
    for part in relative.parts:
        current = current / part
        if current.exists() and _is_reparse_or_symlink(current):
            raise NormalizationError(f"reparse/symlink component is forbidden: {current}")


def validate_request_path(path: Path) -> Path:
    lexical = Path(os.path.abspath(path))
    root = Path(os.path.abspath(REQUEST_ROOT))
    if lexical.parent != root or lexical.suffix.lower() != ".json":
        raise NormalizationError(
            "request must be a direct JSON file below "
            "tmp/source-normalization-adapter/requests"
        )
    _assert_plain_chain(root, lexical)
    if not lexical.exists() or not lexical.is_file():
        raise NormalizationError("request is not an ordinary file")
    return lexical


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise NormalizationError(f"{field} must be a non-empty string")
    return value


def _validate_iso_date(value: Any, field: str) -> str:
    raw = _require_string(value, field)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise NormalizationError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != raw:
        raise NormalizationError(f"{field} must be canonical YYYY-MM-DD")
    return raw


def _validate_available_at(value: Any, trade_date: str) -> str:
    raw = _require_string(value, "available_at")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise NormalizationError("available_at must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(hours=8):
        raise NormalizationError("available_at must have the Asia/Shanghai +08:00 offset")
    if parsed.date() < date.fromisoformat(trade_date):
        raise NormalizationError("available_at cannot precede trade_date")
    if parsed.isoformat(timespec="seconds") != raw:
        raise NormalizationError(
            "available_at must be canonical seconds precision with +08:00"
        )
    return raw


def _validate_mapping(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise NormalizationError(f"{field} must be an object")
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise NormalizationError(f"{field} keys and values must be strings")
    return dict(value)


def _load_contract() -> tuple[dict[str, Any], str]:
    contract, payload = load_json_object(CONTRACT_PATH, "contract")
    if contract.get("contract_id") != CONTRACT_ID:
        raise NormalizationError("contract identity mismatch")
    if contract.get("engine_id") != ENGINE_ID:
        raise NormalizationError("contract engine mismatch")
    if contract.get("purpose_ceiling") != PURPOSE_CEILING:
        raise NormalizationError("contract purpose ceiling mismatch")
    if contract.get("formal_effect") != "NONE":
        raise NormalizationError("contract must have no formal effect")
    if contract.get("canonical_input_fields") != CANONICAL_INPUT_FIELDS:
        raise NormalizationError("contract input fields drifted")
    if contract.get("canonical_output_fields") != CANONICAL_OUTPUT_FIELDS:
        raise NormalizationError("contract output fields drifted")
    if contract.get("required_unit_map") != REQUIRED_UNITS:
        raise NormalizationError("contract units drifted")
    return contract, sha256_bytes(payload)


def validate_request(request: dict[str, Any]) -> dict[str, Any]:
    if set(request) != REQUEST_FIELDS:
        missing = sorted(REQUEST_FIELDS - set(request))
        extra = sorted(set(request) - REQUEST_FIELDS)
        raise NormalizationError(f"request field mismatch; missing={missing}, extra={extra}")
    if request.get("schema_version") != 1 or request.get("contract_id") != CONTRACT_ID:
        raise NormalizationError("request schema or contract identity mismatch")
    run_id = _require_string(request["run_id"], "run_id")
    seal_run_id = _require_string(request["seal_run_id"], "seal_run_id")
    if not RUN_ID_RE.fullmatch(run_id) or not RUN_ID_RE.fullmatch(seal_run_id):
        raise NormalizationError("run_id or seal_run_id has an invalid format")
    sealed_relative_path = _require_string(
        request["sealed_relative_path"], "sealed_relative_path"
    )
    relative = Path(sealed_relative_path)
    if (
        relative.is_absolute()
        or relative.drive
        or ".." in relative.parts
        or len(relative.parts) != 2
        or relative.parts[0] != "sealed"
    ):
        raise NormalizationError("sealed_relative_path must be sealed/<file>")
    sealed_sha256 = _require_string(request["sealed_sha256"], "sealed_sha256")
    if not SHA256_RE.fullmatch(sealed_sha256):
        raise NormalizationError("sealed_sha256 must be lowercase SHA-256")
    source_id = _require_string(request["source_id"], "source_id")
    dataset_id = _require_string(request["dataset_id"], "dataset_id")
    if not SOURCE_ID_RE.fullmatch(source_id):
        raise NormalizationError("source_id has an invalid format")
    if not DATASET_ID_RE.fullmatch(dataset_id):
        raise NormalizationError("dataset_id has an invalid format")
    trade_date = _validate_iso_date(request["trade_date"], "trade_date")
    if request["timezone"] != "Asia/Shanghai":
        raise NormalizationError("timezone must be Asia/Shanghai")
    available_at = _validate_available_at(request["available_at"], trade_date)

    field_map = _validate_mapping(request["field_map"], "field_map")
    if set(field_map) != set(CANONICAL_INPUT_FIELDS):
        raise NormalizationError("field_map must contain the exact canonical input keys")
    source_columns = [field_map[field] for field in CANONICAL_INPUT_FIELDS]
    if any(not item or item.strip() != item for item in source_columns):
        raise NormalizationError("field_map source columns must be nonblank and unpadded")
    if len(source_columns) != len(set(source_columns)):
        raise NormalizationError("field_map must be one-to-one")

    unit_map = _validate_mapping(request["unit_map"], "unit_map")
    if unit_map != REQUIRED_UNITS:
        raise NormalizationError("unit_map must exactly match the v1 units")

    security_id_map = _validate_mapping(request["security_id_map"], "security_id_map")
    if not security_id_map:
        raise NormalizationError("security_id_map cannot be empty")
    if any(not key or key.strip() != key for key in security_id_map):
        raise NormalizationError("source security IDs must be nonblank and unpadded")
    permanent_ids = list(security_id_map.values())
    if any(PERMANENT_ID_RE.fullmatch(value) is None for value in permanent_ids):
        raise NormalizationError("security_id_map contains an invalid permanent ID")
    if len(permanent_ids) != len(set(permanent_ids)):
        raise NormalizationError("security_id_map must be one-to-one")

    expected_raw = request["expected_permanent_security_ids"]
    if not isinstance(expected_raw, list) or not expected_raw:
        raise NormalizationError("expected_permanent_security_ids must be a nonempty array")
    if not all(isinstance(value, str) for value in expected_raw):
        raise NormalizationError("expected permanent IDs must be strings")
    expected = list(expected_raw)
    if len(expected) != len(set(expected)):
        raise NormalizationError("expected permanent IDs must be unique")
    if any(PERMANENT_ID_RE.fullmatch(value) is None for value in expected):
        raise NormalizationError("expected permanent IDs contain an invalid ID")
    if set(expected) != set(permanent_ids):
        raise NormalizationError(
            "security_id_map values must exactly equal expected permanent IDs"
        )

    return {
        **request,
        "run_id": run_id,
        "seal_run_id": seal_run_id,
        "sealed_relative_path": relative.as_posix(),
        "source_id": source_id,
        "dataset_id": dataset_id,
        "trade_date": trade_date,
        "available_at": available_at,
        "field_map": field_map,
        "unit_map": unit_map,
        "security_id_map": security_id_map,
        "expected_permanent_security_ids": sorted(expected),
    }


def _load_and_verify_seal(request: dict[str, Any]) -> dict[str, Any]:
    seal_root = Path(os.path.abspath(INPUT_ROOT / request["seal_run_id"]))
    if seal_root.parent != Path(os.path.abspath(INPUT_ROOT)):
        raise NormalizationError("seal root escapes tmp/local-raw-seal")
    _assert_plain_chain(INPUT_ROOT, seal_root)
    if not seal_root.exists() or not seal_root.is_dir():
        raise NormalizationError("seal run directory is missing")

    manifest_path = seal_root / "SEAL_MANIFEST.json"
    digest_path = seal_root / "SEAL_MANIFEST.sha256"
    for path in (manifest_path, digest_path):
        _assert_plain_chain(seal_root, path)
        if not path.exists() or not path.is_file():
            raise NormalizationError(f"required seal evidence is missing: {path.name}")
    manifest, manifest_payload = load_json_object(manifest_path, "seal manifest")
    manifest_sha256 = sha256_bytes(manifest_payload)
    try:
        digest_text = digest_path.read_text(encoding="ascii", errors="strict")
    except (UnicodeDecodeError, OSError) as exc:
        raise NormalizationError("seal manifest digest is unreadable") from exc
    if digest_text != f"{manifest_sha256}  SEAL_MANIFEST.json\n":
        raise NormalizationError("seal manifest digest mismatch")
    if manifest.get("contract_id") != "local_raw_seal_v1":
        raise NormalizationError("input is not bound by local_raw_seal_v1")
    if manifest.get("seal_run_id") != request["seal_run_id"]:
        raise NormalizationError("seal_run_id does not match seal manifest")
    for flag in (
        "formal_source_admission",
        "formal_catalog_written",
        "quality_promotion",
        "production_raw_store_written",
        "research_ready",
    ):
        if manifest.get(flag) is not False:
            raise NormalizationError(f"seal manifest boundary drift: {flag}")
    entries = manifest.get("inputs")
    if not isinstance(entries, list) or not entries:
        raise NormalizationError("seal manifest inputs must be a nonempty array")
    matches = [
        item
        for item in entries
        if isinstance(item, dict)
        and item.get("sealed_relative_path") == request["sealed_relative_path"]
    ]
    if len(matches) != 1:
        raise NormalizationError("sealed file is missing or duplicated in seal manifest")
    entry = matches[0]
    if entry.get("source_id") != request["source_id"]:
        raise NormalizationError("request source_id does not match seal manifest")
    if entry.get("sealed_sha256") != request["sealed_sha256"]:
        raise NormalizationError("request SHA-256 does not match seal manifest")
    if not isinstance(entry.get("sealed_bytes"), int) or entry["sealed_bytes"] < 0:
        raise NormalizationError("seal manifest has an invalid byte count")

    source_file = Path(os.path.abspath(seal_root / request["sealed_relative_path"]))
    _assert_plain_chain(seal_root, source_file)
    if not source_file.exists() or not source_file.is_file():
        raise NormalizationError("sealed source file is missing")
    if source_file.stat().st_size != entry["sealed_bytes"]:
        raise NormalizationError("sealed source byte-count mismatch")
    actual_sha256 = sha256_file(source_file)
    if actual_sha256 != request["sealed_sha256"]:
        raise NormalizationError("sealed source SHA-256 mismatch")
    return {
        "seal_root": seal_root,
        "seal_manifest_path": manifest_path,
        "seal_manifest_sha256": manifest_sha256,
        "source_file": source_file,
        "source_file_sha256": actual_sha256,
        "source_file_bytes": source_file.stat().st_size,
    }


def _parse_csv_line(line: bytes, label: str) -> list[str]:
    try:
        text = line.decode("utf-8", errors="strict")
        rows = list(csv.reader([text], strict=True))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise NormalizationError(f"{label} is not a valid single-line UTF-8 CSV record") from exc
    if len(rows) != 1:
        raise NormalizationError(f"{label} must contain exactly one CSV record")
    return rows[0]


def _parse_decimal(raw: str, field: str, row_number: int, *, positive: bool) -> Decimal:
    if raw.strip() != raw or DECIMAL_RE.fullmatch(raw) is None:
        raise NormalizationError(
            f"row {row_number} {field} must be an unsigned canonical decimal"
        )
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise NormalizationError(f"row {row_number} {field} is invalid") from exc
    if not value.is_finite() or value < 0:
        raise NormalizationError(f"row {row_number} {field} is negative or non-finite")
    if positive and value <= 0:
        raise NormalizationError(f"row {row_number} {field} must be positive")
    return value


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def normalize_csv(
    request: dict[str, Any], seal: dict[str, Any]
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    payload = seal["source_file"].read_bytes()
    if not payload:
        raise NormalizationError("sealed CSV is empty")
    if payload.startswith(b"\xef\xbb\xbf") or b"\x00" in payload or b"\r" in payload:
        raise NormalizationError("CSV must be BOM-free, NUL-free, canonical UTF-8 LF")
    if not payload.endswith(b"\n"):
        raise NormalizationError("CSV must end with LF")
    physical_lines = payload.split(b"\n")[:-1]
    if len(physical_lines) < 2 or any(not line for line in physical_lines):
        raise NormalizationError("CSV needs a header, rows, and no blank physical lines")

    expected_header = [request["field_map"][field] for field in CANONICAL_INPUT_FIELDS]
    header = _parse_csv_line(physical_lines[0], "CSV header")
    if header != expected_header:
        raise NormalizationError(
            f"CSV header mismatch; expected={expected_header}, actual={header}"
        )

    field_map_sha256 = sha256_bytes(canonical_json(request["field_map"]).encode("utf-8"))
    unit_map_sha256 = sha256_bytes(canonical_json(request["unit_map"]).encode("utf-8"))
    normalized: list[dict[str, str]] = []
    lineage: list[dict[str, Any]] = []
    seen_source: set[str] = set()
    seen_permanent: set[str] = set()
    security_map: dict[str, str] = request["security_id_map"]

    for row_number, raw_line in enumerate(physical_lines[1:], start=2):
        values = _parse_csv_line(raw_line, f"CSV row {row_number}")
        if len(values) != len(expected_header):
            raise NormalizationError(f"row {row_number} has CSV width drift")
        raw = dict(zip(CANONICAL_INPUT_FIELDS, values, strict=True))
        for field, value in raw.items():
            if not value or value.strip() != value:
                raise NormalizationError(f"row {row_number} {field} is blank or padded")

        source_security_id = raw["source_security_id"]
        if source_security_id not in security_map:
            raise NormalizationError(
                f"row {row_number} contains unknown or extra security: {source_security_id}"
            )
        permanent_id = security_map[source_security_id]
        if source_security_id in seen_source or permanent_id in seen_permanent:
            raise NormalizationError(f"row {row_number} duplicates a security/key")
        seen_source.add(source_security_id)
        seen_permanent.add(permanent_id)
        row_trade_date = _validate_iso_date(raw["trade_date"], f"row {row_number} trade_date")
        if row_trade_date != request["trade_date"]:
            raise NormalizationError(f"row {row_number} trade_date mismatch")

        numbers = {
            field: _parse_decimal(
                raw[field], field, row_number, positive=field not in {"volume", "amount"}
            )
            for field in ("open", "high", "low", "close", "previous_close", "volume", "amount")
        }
        if numbers["high"] < max(numbers["open"], numbers["low"], numbers["close"]):
            raise NormalizationError(f"row {row_number} high violates OHLC")
        if numbers["low"] > min(numbers["open"], numbers["high"], numbers["close"]):
            raise NormalizationError(f"row {row_number} low violates OHLC")

        raw_row_sha256 = sha256_bytes(raw_line)
        output_row = {
            "permanent_security_id": permanent_id,
            "source_security_id": source_security_id,
            "trade_date": row_trade_date,
            "open_cny_per_share": _decimal_text(numbers["open"]),
            "high_cny_per_share": _decimal_text(numbers["high"]),
            "low_cny_per_share": _decimal_text(numbers["low"]),
            "close_cny_per_share": _decimal_text(numbers["close"]),
            "previous_close_cny_per_share": _decimal_text(numbers["previous_close"]),
            "volume_shares": _decimal_text(numbers["volume"]),
            "amount_cny": _decimal_text(numbers["amount"]),
            "timezone": request["timezone"],
            "available_at": request["available_at"],
            "source_id": request["source_id"],
            "dataset_id": request["dataset_id"],
            "source_file_sha256": seal["source_file_sha256"],
            "raw_row_number": str(row_number),
            "raw_row_sha256": raw_row_sha256,
        }
        lineage_record: dict[str, Any] = {
            "schema_version": 1,
            "permanent_security_id": permanent_id,
            "source_security_id": source_security_id,
            "trade_date": row_trade_date,
            "timezone": request["timezone"],
            "available_at": request["available_at"],
            "source_id": request["source_id"],
            "dataset_id": request["dataset_id"],
            "source_file_identifier": (
                f"{request['source_id']}:{seal['source_file_sha256']}"
            ),
            "source_file_absolute_path": str(seal["source_file"]),
            "source_file_relative_path": seal["source_file"].relative_to(ROOT).as_posix(),
            "source_file_sha256": seal["source_file_sha256"],
            "source_file_bytes": seal["source_file_bytes"],
            "seal_run_id": request["seal_run_id"],
            "seal_manifest_sha256": seal["seal_manifest_sha256"],
            "sealed_relative_path": request["sealed_relative_path"],
            "raw_row_number": row_number,
            "raw_row_sha256": raw_row_sha256,
            "field_map": request["field_map"],
            "field_map_sha256": field_map_sha256,
            "unit_map": request["unit_map"],
            "unit_map_sha256": unit_map_sha256,
        }
        lineage_record["lineage_id"] = sha256_bytes(
            canonical_json(lineage_record).encode("utf-8")
        )
        normalized.append(output_row)
        lineage.append(lineage_record)

    missing = sorted(set(request["expected_permanent_security_ids"]) - seen_permanent)
    extra = sorted(seen_permanent - set(request["expected_permanent_security_ids"]))
    if missing or extra:
        raise NormalizationError(
            f"security coverage mismatch; missing={missing}, extra={extra}"
        )
    paired = sorted(zip(normalized, lineage, strict=True), key=lambda item: item[0]["permanent_security_id"])
    return [item[0] for item in paired], [item[1] for item in paired]


def _boundary(status: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "engine_id": ENGINE_ID,
        "contract_id": CONTRACT_ID,
        "status": status,
        "purpose_ceiling": PURPOSE_CEILING,
        "network_used": False,
        "input_discovery_used": False,
        "formal_source_admission": False,
        "formal_catalog_written": False,
        "quality_promotion": False,
        "production_raw_store_written": False,
        "snapshot_published": False,
        "scheduler_created": False,
        "research_ready": False,
        "formal_effect": "NONE",
    }


def _output_path(run_id: str) -> Path:
    output = Path(os.path.abspath(OUTPUT_ROOT / run_id))
    if output.parent != Path(os.path.abspath(OUTPUT_ROOT)) or output.name != run_id:
        raise NormalizationError("output path is outside the owned root")
    _assert_plain_chain(OUTPUT_ROOT, output.parent)
    if output.exists():
        raise NormalizationError("output already exists; normalization runs never overwrite")
    return output


def prepare(request_path: Path) -> dict[str, Any]:
    request_file = validate_request_path(request_path)
    request, request_payload = load_json_object(request_file, "request")
    validated = validate_request(request)
    contract, contract_sha256 = _load_contract()
    seal = _load_and_verify_seal(validated)
    normalized, lineage = normalize_csv(validated, seal)
    return {
        "request": validated,
        "request_path": request_file,
        "request_sha256": sha256_bytes(request_payload),
        "contract": contract,
        "contract_sha256": contract_sha256,
        "seal": seal,
        "normalized": normalized,
        "lineage": lineage,
        "output": _output_path(validated["run_id"]),
    }


def _normalized_csv_bytes(rows: list[dict[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CANONICAL_OUTPUT_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _cleanup_owned_stage(stage: Path) -> None:
    lexical = Path(os.path.abspath(stage))
    root = Path(os.path.abspath(OUTPUT_ROOT))
    if lexical.parent != root or not lexical.name.startswith("."):
        raise NormalizationError("refusing to clean outside owned staging")
    if lexical.exists():
        shutil.rmtree(lexical)


def execute(prepared: dict[str, Any]) -> dict[str, Any]:
    output: Path = prepared["output"]
    if output.exists():
        raise NormalizationError("output already exists; normalization runs never overwrite")
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    _assert_plain_chain(OUTPUT_ROOT, OUTPUT_ROOT)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{prepared['request']['run_id']}.staging-", dir=OUTPUT_ROOT
        )
    )
    try:
        normalized_payload = _normalized_csv_bytes(prepared["normalized"])
        lineage_payload = (
            "".join(canonical_json(row) + "\n" for row in prepared["lineage"])
        ).encode("utf-8")
        results = {
            **_boundary("PASS_SOURCE_NORMALIZATION_INSPECTION_ONLY"),
            "run_id": prepared["request"]["run_id"],
            "source_id": prepared["request"]["source_id"],
            "dataset_id": prepared["request"]["dataset_id"],
            "trade_date": prepared["request"]["trade_date"],
            "timezone": prepared["request"]["timezone"],
            "available_at": prepared["request"]["available_at"],
            "row_count": len(prepared["normalized"]),
            "security_count": len(prepared["normalized"]),
            "source_file_absolute_path": str(prepared["seal"]["source_file"]),
            "source_file_relative_path": prepared["seal"]["source_file"]
            .relative_to(ROOT)
            .as_posix(),
            "source_file_sha256": prepared["seal"]["source_file_sha256"],
            "source_file_bytes": prepared["seal"]["source_file_bytes"],
            "seal_manifest_sha256": prepared["seal"]["seal_manifest_sha256"],
            "request_relative_path": prepared["request_path"].relative_to(ROOT).as_posix(),
            "request_sha256": prepared["request_sha256"],
            "contract_relative_path": CONTRACT_PATH.relative_to(ROOT).as_posix(),
            "contract_sha256": prepared["contract_sha256"],
            "field_map": prepared["request"]["field_map"],
            "unit_map": prepared["request"]["unit_map"],
            "write_scope": "TMP_ONLY",
        }
        results_payload = (canonical_json(results) + "\n").encode("utf-8")
        payloads = {
            "normalized.csv": normalized_payload,
            "row_lineage.jsonl": lineage_payload,
            "RESULTS.json": results_payload,
        }
        for relative, payload in payloads.items():
            (stage / relative).write_bytes(payload)
        manifest = {
            **_boundary("PASS_SOURCE_NORMALIZATION_INSPECTION_ONLY"),
            "run_id": prepared["request"]["run_id"],
            "files": [
                {
                    "path": relative,
                    "bytes": len(payload),
                    "sha256": sha256_bytes(payload),
                }
                for relative, payload in sorted(payloads.items())
            ],
        }
        manifest_payload = (canonical_json(manifest) + "\n").encode("utf-8")
        (stage / "MANIFEST.json").write_bytes(manifest_payload)
        for relative, payload in payloads.items():
            target = stage / relative
            if target.stat().st_size != len(payload) or sha256_file(target) != sha256_bytes(payload):
                raise NormalizationError(f"output verification failed: {relative}")
        if output.exists():
            raise NormalizationError("output appeared during publish; refusing overwrite")
        os.rename(stage, output)
        return {
            **results,
            "output_relative_path": output.relative_to(ROOT).as_posix(),
            "manifest_sha256": sha256_bytes(manifest_payload),
        }
    except Exception:
        _cleanup_owned_stage(stage)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        prepared = prepare(args.request)
        if not args.execute:
            result = {
                **_boundary("PLAN_SOURCE_NORMALIZATION_INSPECTION_ONLY"),
                "run_id": prepared["request"]["run_id"],
                "source_id": prepared["request"]["source_id"],
                "dataset_id": prepared["request"]["dataset_id"],
                "trade_date": prepared["request"]["trade_date"],
                "row_count": len(prepared["normalized"]),
                "source_file_sha256": prepared["seal"]["source_file_sha256"],
                "output_relative_path": prepared["output"].relative_to(ROOT).as_posix(),
                "write_performed": False,
            }
        else:
            result = execute(prepared)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    **_boundary("FAIL_CLOSED"),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
