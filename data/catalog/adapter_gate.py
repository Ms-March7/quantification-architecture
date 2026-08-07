#!/usr/bin/env python3
"""Fail-closed sample gate shared by future qdata source adapters."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date
from typing import Any


class AdapterGateError(RuntimeError):
    """Raised when a gate contract or sample envelope is malformed."""


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AdapterGateError(f"{label} must be an object")
    return value


def require_string_array(
    value: Any,
    label: str,
    *,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AdapterGateError(f"{label} must be an array of strings")
    if not allow_empty and not value:
        raise AdapterGateError(f"{label} must not be empty")
    if len(value) != len(set(value)):
        raise AdapterGateError(f"{label} must not contain duplicates")
    return value


def is_null(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, float):
        return not math.isfinite(value)
    return False


def is_finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def canonical_row_sha256(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AdapterGateError("raw row is not canonical JSON") from exc
    return hashlib.sha256(payload).hexdigest()


def matches_field_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str) and bool(value.strip())
    if expected == "date":
        if (
            not isinstance(value, str)
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None
        ):
            return False
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
        return True
    if expected == "number":
        return is_finite_number(value)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    raise AdapterGateError(f"unsupported normalized field type: {expected}")


def validate_contract(contract: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "contract_id",
        "purpose_ceiling",
        "source_admission",
        "expected_raw_field_count",
        "required_normalized_fields",
        "primary_key_fields",
        "critical_non_null_fields",
        "field_types",
        "numeric_fields",
        "non_negative_fields",
        "positive_fields",
        "expected_units",
        "ohlc_fields",
        "row_mapping_cardinality",
    }
    missing = sorted(required - set(contract))
    if missing:
        raise AdapterGateError(f"contract is missing fields: {', '.join(missing)}")
    allowed = required | {"status", "observed_at"}
    unsupported = sorted(set(contract) - allowed)
    if unsupported:
        raise AdapterGateError(
            f"contract contains unsupported fields: {', '.join(unsupported)}"
        )
    if contract["schema_version"] != 1:
        raise AdapterGateError("contract.schema_version must be 1")
    if not isinstance(contract["contract_id"], str) or not contract["contract_id"]:
        raise AdapterGateError("contract.contract_id must be a non-empty string")
    if contract["purpose_ceiling"] != "SMOKE_ONLY":
        raise AdapterGateError("adapter gate contract must remain SMOKE_ONLY")
    if contract["source_admission"] is not False:
        raise AdapterGateError("adapter gate contract must not grant source admission")
    if contract["row_mapping_cardinality"] != "ONE_TO_ONE":
        raise AdapterGateError("adapter gate requires ONE_TO_ONE row mapping")
    width = contract["expected_raw_field_count"]
    if not isinstance(width, int) or isinstance(width, bool) or width <= 0:
        raise AdapterGateError(
            "contract.expected_raw_field_count must be a positive integer"
        )
    for field in (
        "required_normalized_fields",
        "primary_key_fields",
        "critical_non_null_fields",
        "numeric_fields",
        "non_negative_fields",
        "positive_fields",
    ):
        require_string_array(
            contract[field],
            f"contract.{field}",
            allow_empty=field in {"non_negative_fields", "positive_fields"},
        )
    required_fields = set(contract["required_normalized_fields"])
    field_types = require_object(contract["field_types"], "contract.field_types")
    supported_types = {"string", "date", "number", "integer"}
    if set(field_types) != required_fields or not all(
        expected_type in supported_types
        for expected_type in field_types.values()
    ):
        raise AdapterGateError(
            "contract.field_types must cover every required field with a supported scalar type"
        )
    expected_units = require_object(
        contract["expected_units"],
        "contract.expected_units",
    )
    if not expected_units or not all(
        isinstance(key, str)
        and key
        and isinstance(unit, str)
        and unit
        for key, unit in expected_units.items()
    ):
        raise AdapterGateError(
            "contract.expected_units must map fields to non-empty unit strings"
        )
    ohlc_fields = require_object(contract["ohlc_fields"], "contract.ohlc_fields")
    if set(ohlc_fields) != {"open", "high", "low", "close"} or not all(
        isinstance(value, str) and value
        for value in ohlc_fields.values()
    ) or len(set(ohlc_fields.values())) != 4:
        raise AdapterGateError("contract.ohlc_fields must map open/high/low/close")
    referenced = (
        set(contract["primary_key_fields"])
        | set(contract["critical_non_null_fields"])
        | set(contract["numeric_fields"])
        | set(contract["non_negative_fields"])
        | set(contract["positive_fields"])
        | set(expected_units)
        | set(ohlc_fields.values())
    )
    outside = sorted(referenced - required_fields)
    if outside:
        raise AdapterGateError(
            "contract references fields outside required_normalized_fields: "
            + ", ".join(outside)
        )
    primary_keys = set(contract["primary_key_fields"])
    if not primary_keys <= set(contract["critical_non_null_fields"]):
        raise AdapterGateError(
            "contract.primary_key_fields must be critical non-null fields"
        )
    numeric_fields = set(contract["numeric_fields"])
    if any(
        field_types[field] not in {"number", "integer"}
        for field in numeric_fields
    ):
        raise AdapterGateError(
            "contract.numeric_fields must use number/integer field types"
        )
    if not set(contract["positive_fields"]) <= numeric_fields:
        raise AdapterGateError("contract.positive_fields must be numeric fields")
    if not set(contract["non_negative_fields"]) <= numeric_fields:
        raise AdapterGateError("contract.non_negative_fields must be numeric fields")
    ohlc_value_fields = set(ohlc_fields.values())
    if not ohlc_value_fields <= numeric_fields:
        raise AdapterGateError("contract OHLC fields must be numeric fields")
    if not ohlc_value_fields <= set(contract["positive_fields"]):
        raise AdapterGateError("contract OHLC fields must be positive fields")
    if set(expected_units) != numeric_fields:
        raise AdapterGateError(
            "contract.expected_units must cover numeric_fields exactly"
        )
    return contract


def validate_adapter_sample(
    contract: dict[str, Any],
    sample: dict[str, Any],
) -> dict[str, Any]:
    contract = validate_contract(require_object(contract, "contract"))
    sample = require_object(sample, "sample")
    sample_fields = {
        "schema_version",
        "contract_id",
        "declared_units",
        "raw_rows",
        "normalized_rows",
        "row_lineage",
    }
    missing_sample_fields = sorted(sample_fields - set(sample))
    unsupported_sample_fields = sorted(set(sample) - sample_fields)
    if missing_sample_fields or unsupported_sample_fields:
        raise AdapterGateError(
            "sample field contract mismatch; "
            f"missing={missing_sample_fields}, unsupported={unsupported_sample_fields}"
        )
    if sample["schema_version"] != 1:
        raise AdapterGateError("sample.schema_version must be 1")
    if sample["contract_id"] != contract["contract_id"]:
        raise AdapterGateError("sample.contract_id does not match the gate contract")
    raw_rows = sample["raw_rows"]
    normalized_rows = sample["normalized_rows"]
    row_lineage = sample["row_lineage"]
    declared_units = require_object(
        sample["declared_units"],
        "sample.declared_units",
    )
    if not isinstance(raw_rows, list) or not raw_rows:
        raise AdapterGateError("sample.raw_rows must be a non-empty array")
    if not isinstance(normalized_rows, list) or not normalized_rows:
        raise AdapterGateError("sample.normalized_rows must be a non-empty array")
    if not isinstance(row_lineage, list) or not row_lineage:
        raise AdapterGateError("sample.row_lineage must be a non-empty array")

    bad_raw_width_rows: list[int] = []
    observed_widths: list[int | None] = []
    expected_width = contract["expected_raw_field_count"]
    for index, row in enumerate(raw_rows):
        width = len(row) if isinstance(row, list) else None
        observed_widths.append(width)
        if width != expected_width:
            bad_raw_width_rows.append(index)

    row_count_preserved = len(raw_rows) == len(normalized_rows)
    lineage_errors: list[dict[str, Any]] = []
    seen_raw_indexes: set[int] = set()
    seen_normalized_indexes: set[int] = set()
    lineage_fields = {"raw_index", "normalized_index", "raw_row_sha256"}
    for lineage_index, lineage in enumerate(row_lineage):
        if not isinstance(lineage, dict) or set(lineage) != lineage_fields:
            lineage_errors.append(
                {"lineage": lineage_index, "reason": "invalid_lineage_shape"}
            )
            continue
        raw_index = lineage["raw_index"]
        normalized_index = lineage["normalized_index"]
        raw_hash = lineage["raw_row_sha256"]
        if (
            not isinstance(raw_index, int)
            or isinstance(raw_index, bool)
            or not isinstance(normalized_index, int)
            or isinstance(normalized_index, bool)
            or not isinstance(raw_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", raw_hash) is None
        ):
            lineage_errors.append(
                {"lineage": lineage_index, "reason": "invalid_lineage_value"}
            )
            continue
        if not 0 <= raw_index < len(raw_rows) or not 0 <= normalized_index < len(
            normalized_rows
        ):
            lineage_errors.append(
                {"lineage": lineage_index, "reason": "lineage_index_out_of_range"}
            )
            continue
        if raw_index in seen_raw_indexes or normalized_index in seen_normalized_indexes:
            lineage_errors.append(
                {"lineage": lineage_index, "reason": "duplicate_lineage_index"}
            )
            continue
        seen_raw_indexes.add(raw_index)
        seen_normalized_indexes.add(normalized_index)
        if canonical_row_sha256(raw_rows[raw_index]) != raw_hash:
            lineage_errors.append(
                {"lineage": lineage_index, "reason": "raw_row_hash_mismatch"}
            )
    if (
        len(row_lineage) != len(raw_rows)
        or seen_raw_indexes != set(range(len(raw_rows)))
        or seen_normalized_indexes != set(range(len(normalized_rows)))
    ):
        lineage_errors.append({"reason": "lineage_is_not_one_to_one_and_complete"})

    unit_mismatches: list[dict[str, Any]] = []
    expected_units = contract["expected_units"]
    for field, expected in expected_units.items():
        actual = declared_units.get(field)
        if actual != expected:
            unit_mismatches.append(
                {"field": field, "expected": expected, "actual": actual}
            )
    for field in sorted(set(declared_units) - set(expected_units)):
        unit_mismatches.append(
            {"field": field, "expected": None, "actual": declared_units[field]}
        )

    required_fields = contract["required_normalized_fields"]
    required_field_set = set(required_fields)
    critical_fields = contract["critical_non_null_fields"]
    field_types = contract["field_types"]
    numeric_fields = contract["numeric_fields"]
    non_negative_fields = contract["non_negative_fields"]
    positive_fields = contract["positive_fields"]
    key_fields = contract["primary_key_fields"]
    ohlc = contract["ohlc_fields"]
    missing_field_rows: list[dict[str, Any]] = []
    unexpected_field_rows: list[dict[str, Any]] = []
    critical_null_rows: list[dict[str, Any]] = []
    invalid_type_rows: list[dict[str, Any]] = []
    invalid_numeric_rows: list[dict[str, Any]] = []
    negative_value_rows: list[dict[str, Any]] = []
    non_positive_value_rows: list[dict[str, Any]] = []
    ohlc_invalid_rows: list[int] = []
    duplicate_key_rows: list[dict[str, Any]] = []
    seen_keys: dict[tuple[Any, ...], int] = {}

    for index, normalized_row in enumerate(normalized_rows):
        if not isinstance(normalized_row, dict):
            missing_field_rows.append({"row": index, "fields": required_fields})
            continue
        missing_fields = [
            field
            for field in required_fields
            if field not in normalized_row
        ]
        if missing_fields:
            missing_field_rows.append({"row": index, "fields": missing_fields})
        unexpected_fields = sorted(set(normalized_row) - required_field_set)
        if unexpected_fields:
            unexpected_field_rows.append(
                {"row": index, "fields": unexpected_fields}
            )
        null_fields = [
            field
            for field in critical_fields
            if field not in normalized_row or is_null(normalized_row.get(field))
        ]
        if null_fields:
            critical_null_rows.append({"row": index, "fields": null_fields})
        invalid_types = [
            field
            for field, expected_type in field_types.items()
            if field in normalized_row
            and not matches_field_type(normalized_row[field], expected_type)
        ]
        if invalid_types:
            invalid_type_rows.append({"row": index, "fields": invalid_types})
        invalid_numeric = [
            field
            for field in numeric_fields
            if field in normalized_row
            and not is_finite_number(normalized_row[field])
        ]
        if invalid_numeric:
            invalid_numeric_rows.append({"row": index, "fields": invalid_numeric})
        negative_fields = [
            field
            for field in non_negative_fields
            if field in normalized_row
            and is_finite_number(normalized_row[field])
            and float(normalized_row[field]) < 0
        ]
        if negative_fields:
            negative_value_rows.append({"row": index, "fields": negative_fields})
        non_positive_fields = [
            field
            for field in positive_fields
            if field in normalized_row
            and is_finite_number(normalized_row[field])
            and float(normalized_row[field]) <= 0
        ]
        if non_positive_fields:
            non_positive_value_rows.append(
                {"row": index, "fields": non_positive_fields}
            )
        if all(
            field in normalized_row and is_finite_number(normalized_row[field])
            for field in ohlc.values()
        ):
            open_value = float(normalized_row[ohlc["open"]])
            high_value = float(normalized_row[ohlc["high"]])
            low_value = float(normalized_row[ohlc["low"]])
            close_value = float(normalized_row[ohlc["close"]])
            if (
                low_value > high_value
                or high_value < max(open_value, close_value)
                or low_value > min(open_value, close_value)
            ):
                ohlc_invalid_rows.append(index)
        if all(
            field in normalized_row
            and not is_null(normalized_row[field])
            and matches_field_type(normalized_row[field], field_types[field])
            for field in key_fields
        ):
            key = tuple(normalized_row[field] for field in key_fields)
            if key in seen_keys:
                duplicate_key_rows.append(
                    {
                        "row": index,
                        "first_row": seen_keys[key],
                        "key": list(key),
                    }
                )
            else:
                seen_keys[key] = index

    checks = {
        "raw_width": len(bad_raw_width_rows) == 0,
        "row_count_preserved": row_count_preserved,
        "row_lineage": len(lineage_errors) == 0,
        "declared_units": len(unit_mismatches) == 0,
        "required_fields": len(missing_field_rows) == 0,
        "unexpected_fields": len(unexpected_field_rows) == 0,
        "critical_non_null": len(critical_null_rows) == 0,
        "normalized_field_types": len(invalid_type_rows) == 0,
        "numeric_finite": len(invalid_numeric_rows) == 0,
        "non_negative": len(negative_value_rows) == 0,
        "positive": len(non_positive_value_rows) == 0,
        "primary_key_unique": len(duplicate_key_rows) == 0,
        "ohlc_consistent": len(ohlc_invalid_rows) == 0,
    }
    errors = [name for name, passed in checks.items() if not passed]
    return {
        "schema_version": 1,
        "contract_id": contract["contract_id"],
        "status": "PASS" if not errors else "FAIL",
        "decision": (
            "SMOKE_PASS__NO_SOURCE_ADMISSION"
            if not errors
            else "SMOKE_FAIL__SOURCE_ADMISSION_BLOCKED"
        ),
        "purpose_ceiling": "SMOKE_ONLY",
        "source_admission": False,
        "checks": checks,
        "metrics": {
            "raw_rows": len(raw_rows),
            "normalized_rows": len(normalized_rows),
            "observed_raw_widths": sorted(
                {width for width in observed_widths if width is not None}
            ),
            "bad_raw_width_rows": bad_raw_width_rows,
            "lineage_errors": lineage_errors,
            "unit_mismatches": unit_mismatches,
            "missing_field_rows": missing_field_rows,
            "unexpected_field_rows": unexpected_field_rows,
            "critical_null_rows": critical_null_rows,
            "invalid_type_rows": invalid_type_rows,
            "invalid_numeric_rows": invalid_numeric_rows,
            "negative_value_rows": negative_value_rows,
            "non_positive_value_rows": non_positive_value_rows,
            "duplicate_key_rows": duplicate_key_rows,
            "ohlc_invalid_rows": ohlc_invalid_rows,
        },
        "errors": errors,
        "limitations": [
            "declared unit equality does not independently prove source semantics",
            "row lineage binds sample records but does not prove transformation semantics",
            "sample PASS does not grant source admission or research quality",
        ],
    }
