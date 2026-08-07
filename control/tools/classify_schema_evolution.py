#!/usr/bin/env python3
"""Classify an inspection-only schema transition and emit a deterministic receipt.

This tool is deliberately narrower than a migration engine.  It has no network,
catalog writer, quality-promotion path, scheduler, data rewriter or formal
publisher.  It only compares two strict schema documents and fails closed when
the transition is unknown or breaking.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "data" / "catalog" / "contracts" / "schema_evolution_v1"
SCHEMA_CONTRACT_PATH = CONTRACT_ROOT / "schema.schema.json"

CLASSIFIER_ID = "classify_schema_evolution.py/schema-evolution-v1"
PURPOSE_CEILING = "INSPECTION_ONLY"
ACCEPT_DECISION = "ACCEPT_INSPECTION_ONLY"
REJECT_DECISION = "REJECT_FAIL_CLOSED"

CLASS_NO_CHANGE = "NO_CHANGE"
CLASS_COMPATIBLE_ADD = "COMPATIBLE_FIELD_ADDITION"
CLASS_BREAKING = "BREAKING_CHANGE"
CLASS_UNKNOWN = "UNKNOWN_FAIL_CLOSED"

COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}$")
FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){0,2}(?:[-+][A-Za-z0-9.-]+)?$")
SUPPORTED_LOGICAL_TYPES = {
    "BIGINT",
    "BOOLEAN",
    "DATE",
    "DOUBLE",
    "TIMESTAMP_TZ",
    "VARCHAR",
}
TOP_LEVEL_FIELDS = {
    "schema_contract_version",
    "dataset_id",
    "schema_version",
    "dataset_coverage_start",
    "primary_key",
    "fields",
}
FIELD_FIELDS = {
    "name",
    "logical_type",
    "nullable",
    "field_coverage_start",
}


def canonical_bytes(value: Any) -> bytes:
    """Return the canonical JSON representation used by all receipt hashes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def pretty_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def valid_iso_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value


def numeric_version(value: str) -> tuple[int, int, int]:
    numeric_prefix = value.split("-", 1)[0].split("+", 1)[0]
    parts = [int(part) for part in numeric_prefix.split(".")]
    return tuple((parts + [0, 0])[:3])  # type: ignore[return-value]


def _schema_input_identity(document: Any, raw_sha256: str | None) -> dict[str, Any]:
    try:
        canonical_sha256 = sha256_bytes(canonical_bytes(document))
    except (TypeError, ValueError):
        canonical_sha256 = None
    return {
        "raw_sha256": raw_sha256,
        "canonical_sha256": canonical_sha256,
    }


def validate_schema(document: Any, label: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Strictly validate and normalize one schema contract document."""

    errors: list[str] = []
    if not isinstance(document, dict):
        return None, [f"{label}: schema must be a JSON object"]

    missing = sorted(TOP_LEVEL_FIELDS - set(document))
    unsupported = sorted(set(document) - TOP_LEVEL_FIELDS)
    if missing:
        errors.append(f"{label}: missing top-level fields {missing}")
    if unsupported:
        errors.append(f"{label}: unsupported top-level fields {unsupported}")
    if errors:
        return None, errors

    if document["schema_contract_version"] != 1:
        errors.append(f"{label}: schema_contract_version must equal 1")

    dataset_id = document["dataset_id"]
    if not isinstance(dataset_id, str) or COMPONENT_RE.fullmatch(dataset_id) is None:
        errors.append(f"{label}: dataset_id is not a safe component")

    schema_version = document["schema_version"]
    if (
        not isinstance(schema_version, str)
        or VERSION_RE.fullmatch(schema_version) is None
    ):
        errors.append(f"{label}: schema_version is invalid")

    dataset_start = document["dataset_coverage_start"]
    if not valid_iso_date(dataset_start):
        errors.append(f"{label}: dataset_coverage_start must be YYYY-MM-DD")

    primary_key = document["primary_key"]
    if (
        not isinstance(primary_key, list)
        or not primary_key
        or any(
            not isinstance(item, str) or FIELD_RE.fullmatch(item) is None
            for item in primary_key
        )
        or len(set(primary_key)) != len(primary_key)
    ):
        errors.append(f"{label}: primary_key must be a unique non-empty field array")

    raw_fields = document["fields"]
    if not isinstance(raw_fields, list) or not raw_fields:
        errors.append(f"{label}: fields must be a non-empty array")
        return None, errors

    normalized_fields: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for index, field in enumerate(raw_fields):
        field_label = f"{label}.fields[{index}]"
        if not isinstance(field, dict):
            errors.append(f"{field_label}: field must be an object")
            continue
        missing_field_keys = sorted(FIELD_FIELDS - set(field))
        unsupported_field_keys = sorted(set(field) - FIELD_FIELDS)
        if missing_field_keys:
            errors.append(f"{field_label}: missing fields {missing_field_keys}")
        if unsupported_field_keys:
            errors.append(
                f"{field_label}: unsupported fields {unsupported_field_keys}"
            )
        if missing_field_keys or unsupported_field_keys:
            continue

        name = field["name"]
        if not isinstance(name, str) or FIELD_RE.fullmatch(name) is None:
            errors.append(f"{field_label}: name is invalid")
        elif name in seen_names:
            errors.append(f"{field_label}: duplicate name {name}")
        else:
            seen_names.add(name)

        logical_type = field["logical_type"]
        if (
            not isinstance(logical_type, str)
            or logical_type not in SUPPORTED_LOGICAL_TYPES
        ):
            errors.append(
                f"{field_label}: unsupported logical_type {logical_type!r}"
            )

        nullable = field["nullable"]
        if not isinstance(nullable, bool):
            errors.append(f"{field_label}: nullable must be boolean")

        coverage_start = field["field_coverage_start"]
        if not valid_iso_date(coverage_start):
            errors.append(
                f"{field_label}: field_coverage_start must be YYYY-MM-DD"
            )
        elif valid_iso_date(dataset_start) and coverage_start < dataset_start:
            errors.append(
                f"{field_label}: field_coverage_start precedes dataset coverage"
            )

        if not any(message.startswith(field_label) for message in errors):
            normalized_fields.append(
                {
                    "name": name,
                    "logical_type": logical_type,
                    "nullable": nullable,
                    "field_coverage_start": coverage_start,
                }
            )

    if errors:
        return None, errors

    fields_by_name = {field["name"]: field for field in normalized_fields}
    assert isinstance(primary_key, list)
    for key_name in primary_key:
        field = fields_by_name.get(key_name)
        if field is None:
            errors.append(f"{label}: primary key field {key_name!r} is absent")
            continue
        if field["nullable"]:
            errors.append(f"{label}: primary key field {key_name!r} is nullable")
        if field["field_coverage_start"] != dataset_start:
            errors.append(
                f"{label}: primary key field {key_name!r} must cover dataset start"
            )

    if errors:
        return None, errors

    return (
        {
            "schema_contract_version": 1,
            "dataset_id": dataset_id,
            "schema_version": schema_version,
            "dataset_coverage_start": dataset_start,
            "primary_key": list(primary_key),
            "fields": sorted(normalized_fields, key=lambda item: item["name"]),
        },
        [],
    )


def _empty_changes() -> dict[str, Any]:
    return {
        "added_fields": [],
        "removed_fields": [],
        "type_changes": [],
        "nullable_changes": [],
        "field_coverage_start_changes": [],
        "primary_key_change": None,
        "dataset_coverage_start_change": None,
        "schema_version_change": None,
    }


def _finalize_receipt(core: dict[str, Any]) -> dict[str, Any]:
    receipt = dict(core)
    receipt["receipt_fingerprint"] = sha256_bytes(canonical_bytes(core))
    return receipt


def _base_receipt(
    *,
    old_document: Any,
    new_document: Any,
    old_raw_sha256: str | None,
    new_raw_sha256: str | None,
) -> dict[str, Any]:
    return {
        "receipt_schema_version": 1,
        "classifier_id": CLASSIFIER_ID,
        "purpose_ceiling": PURPOSE_CEILING,
        "inputs": {
            "old": _schema_input_identity(old_document, old_raw_sha256),
            "new": _schema_input_identity(new_document, new_raw_sha256),
        },
        "boundaries": {
            "network_used": False,
            "source_admission": False,
            "formal_catalog_written": False,
            "quality_promotion": False,
            "snapshot_published": False,
            "scheduler_created": False,
            "research_ready": False,
        },
    }


def unknown_receipt(
    *,
    old_document: Any,
    new_document: Any,
    errors: list[str],
    old_raw_sha256: str | None = None,
    new_raw_sha256: str | None = None,
) -> dict[str, Any]:
    core = _base_receipt(
        old_document=old_document,
        new_document=new_document,
        old_raw_sha256=old_raw_sha256,
        new_raw_sha256=new_raw_sha256,
    )
    core.update(
        {
            "classification": CLASS_UNKNOWN,
            "decision": REJECT_DECISION,
            "validation_errors": sorted(set(errors)),
            "old_schema_version": None,
            "new_schema_version": None,
            "changes": None,
        }
    )
    return _finalize_receipt(core)


def classify_schema_evolution(
    old_document: Any,
    new_document: Any,
    *,
    old_raw_sha256: str | None = None,
    new_raw_sha256: str | None = None,
) -> dict[str, Any]:
    """Classify a transition. Unknown and breaking transitions fail closed."""

    old_schema, old_errors = validate_schema(old_document, "old")
    new_schema, new_errors = validate_schema(new_document, "new")
    if old_errors or new_errors:
        return unknown_receipt(
            old_document=old_document,
            new_document=new_document,
            errors=old_errors + new_errors,
            old_raw_sha256=old_raw_sha256,
            new_raw_sha256=new_raw_sha256,
        )

    assert old_schema is not None
    assert new_schema is not None
    if old_schema["dataset_id"] != new_schema["dataset_id"]:
        return unknown_receipt(
            old_document=old_document,
            new_document=new_document,
            errors=["dataset_id mismatch; cross-dataset comparison is forbidden"],
            old_raw_sha256=old_raw_sha256,
            new_raw_sha256=new_raw_sha256,
        )

    changes = _empty_changes()
    old_numeric_version = numeric_version(old_schema["schema_version"])
    new_numeric_version = numeric_version(new_schema["schema_version"])
    if old_schema["schema_version"] != new_schema["schema_version"]:
        if new_numeric_version > old_numeric_version:
            version_order = "ADVANCE"
        elif new_numeric_version < old_numeric_version:
            version_order = "REGRESSION"
        else:
            version_order = "LABEL_ONLY"
        changes["schema_version_change"] = {
            "old": old_schema["schema_version"],
            "new": new_schema["schema_version"],
            "numeric_order": version_order,
        }
    if old_schema["dataset_coverage_start"] != new_schema["dataset_coverage_start"]:
        changes["dataset_coverage_start_change"] = {
            "old": old_schema["dataset_coverage_start"],
            "new": new_schema["dataset_coverage_start"],
        }
    if old_schema["primary_key"] != new_schema["primary_key"]:
        changes["primary_key_change"] = {
            "old": old_schema["primary_key"],
            "new": new_schema["primary_key"],
        }

    old_fields = {field["name"]: field for field in old_schema["fields"]}
    new_fields = {field["name"]: field for field in new_schema["fields"]}
    added_names = sorted(set(new_fields) - set(old_fields))
    removed_names = sorted(set(old_fields) - set(new_fields))
    changes["added_fields"] = [new_fields[name] for name in added_names]
    changes["removed_fields"] = removed_names

    for name in sorted(set(old_fields) & set(new_fields)):
        old_field = old_fields[name]
        new_field = new_fields[name]
        if old_field["logical_type"] != new_field["logical_type"]:
            changes["type_changes"].append(
                {
                    "field": name,
                    "old": old_field["logical_type"],
                    "new": new_field["logical_type"],
                }
            )
        if old_field["nullable"] != new_field["nullable"]:
            changes["nullable_changes"].append(
                {
                    "field": name,
                    "old": old_field["nullable"],
                    "new": new_field["nullable"],
                }
            )
        if (
            old_field["field_coverage_start"]
            != new_field["field_coverage_start"]
        ):
            changes["field_coverage_start_changes"].append(
                {
                    "field": name,
                    "old": old_field["field_coverage_start"],
                    "new": new_field["field_coverage_start"],
                }
            )

    non_nullable_additions = [
        field["name"] for field in changes["added_fields"] if not field["nullable"]
    ]
    semantic_change = any(
        (
            changes["added_fields"],
            changes["removed_fields"],
            changes["type_changes"],
            changes["nullable_changes"],
            changes["field_coverage_start_changes"],
            changes["primary_key_change"],
            changes["dataset_coverage_start_change"],
        )
    )
    version_regression = new_numeric_version < old_numeric_version
    version_not_advanced_for_semantic_change = (
        semantic_change and new_numeric_version <= old_numeric_version
    )
    breaking = any(
        (
            changes["removed_fields"],
            changes["type_changes"],
            changes["nullable_changes"],
            changes["field_coverage_start_changes"],
            changes["primary_key_change"],
            changes["dataset_coverage_start_change"],
            non_nullable_additions,
            version_regression,
            version_not_advanced_for_semantic_change,
        )
    )

    if breaking:
        classification = CLASS_BREAKING
        decision = REJECT_DECISION
    elif changes["added_fields"]:
        classification = CLASS_COMPATIBLE_ADD
        decision = ACCEPT_DECISION
    else:
        classification = CLASS_NO_CHANGE
        decision = ACCEPT_DECISION

    core = _base_receipt(
        old_document=old_document,
        new_document=new_document,
        old_raw_sha256=old_raw_sha256,
        new_raw_sha256=new_raw_sha256,
    )
    core.update(
        {
            "classification": classification,
            "decision": decision,
            "validation_errors": [],
            "dataset_id": old_schema["dataset_id"],
            "old_schema_version": old_schema["schema_version"],
            "new_schema_version": new_schema["schema_version"],
            "changes": changes,
            "breaking_reasons": {
                "non_nullable_added_fields": non_nullable_additions,
                "schema_version_regression": version_regression,
                "schema_version_not_advanced_for_semantic_change": (
                    version_not_advanced_for_semantic_change
                ),
            },
        }
    )
    return _finalize_receipt(core)


def _read_json_input(path: Path, label: str) -> tuple[Any, bytes, list[str]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, b"", [f"{label}: cannot read input: {exc}"]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, raw, [f"{label}: input is not UTF-8: {exc}"]
    try:
        return json.loads(text), raw, []
    except json.JSONDecodeError as exc:
        return None, raw, [
            f"{label}: invalid JSON at line {exc.lineno} column {exc.colno}"
        ]


def classify_files(old_path: Path, new_path: Path) -> dict[str, Any]:
    old_document, old_raw, old_errors = _read_json_input(old_path, "old")
    new_document, new_raw, new_errors = _read_json_input(new_path, "new")
    old_hash = sha256_bytes(old_raw) if old_raw else None
    new_hash = sha256_bytes(new_raw) if new_raw else None
    if old_errors or new_errors:
        return unknown_receipt(
            old_document=old_document,
            new_document=new_document,
            errors=old_errors + new_errors,
            old_raw_sha256=old_hash,
            new_raw_sha256=new_hash,
        )
    return classify_schema_evolution(
        old_document,
        new_document,
        old_raw_sha256=old_hash,
        new_raw_sha256=new_hash,
    )


def validate_output_path(path: Path) -> Path:
    candidate = path if path.is_absolute() else ROOT / path
    candidate = candidate.resolve(strict=False)
    tmp_root = (ROOT / "tmp" / "schema-evolution").resolve()
    report_root = (ROOT / "reports" / "data-governance").resolve()

    allowed = False
    try:
        candidate.relative_to(tmp_root)
        allowed = True
    except ValueError:
        pass
    if not allowed:
        try:
            report_relative = candidate.relative_to(report_root)
            first_component = report_relative.parts[0]
            allowed = first_component.startswith("schema-evolution-classifier-")
        except (ValueError, IndexError):
            pass
    if not allowed:
        raise ValueError(
            "output must be under tmp/schema-evolution or a new "
            "reports/data-governance/schema-evolution-classifier-* directory"
        )
    if candidate.exists():
        raise FileExistsError(f"refusing to overwrite existing receipt: {candidate}")
    return candidate


def write_new_receipt(path: Path, receipt: dict[str, Any]) -> None:
    target = validate_output_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:
        handle.write(pretty_bytes(receipt))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Classify two strict schema contracts. Unknown and breaking changes "
            "return exit code 2 and a fail-closed JSON receipt."
        )
    )
    parser.add_argument("--old", required=True, type=Path)
    parser.add_argument("--new", required=True, type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Optional new receipt path below tmp/schema-evolution or a new "
            "schema-evolution-classifier report directory. Existing files are "
            "never overwritten."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    receipt = classify_files(args.old, args.new)
    if args.output is None:
        sys.stdout.buffer.write(pretty_bytes(receipt))
    else:
        try:
            write_new_receipt(args.output, receipt)
        except (OSError, ValueError) as exc:
            print(f"receipt write rejected: {exc}", file=sys.stderr)
            return 3
    return 0 if receipt["decision"] == ACCEPT_DECISION else 2


if __name__ == "__main__":
    raise SystemExit(main())
