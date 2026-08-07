from __future__ import annotations

import math
from typing import Any, Iterable, Mapping


def _index_unique(rows: Iterable[Mapping[str, Any]], key_fields: tuple[str, ...], label: str) -> dict[tuple[Any, ...], Mapping[str, Any]]:
    index: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    for row in rows:
        key = tuple(row.get(field) for field in key_fields)
        if key in index:
            raise ValueError(f"duplicate {label} reconciliation key: {key}")
        index[key] = row
    return index


def _equal(left: Any, right: Any, tolerance: float) -> bool:
    if left is None or right is None:
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if math.isnan(float(left)) and math.isnan(float(right)):
            return True
        return math.isclose(float(left), float(right), abs_tol=tolerance, rel_tol=0.0)
    return str(left).strip() == str(right).strip()


def reconcile_records(
    primary_rows: Iterable[Mapping[str, Any]],
    secondary_rows: Iterable[Mapping[str, Any]],
    *,
    key_fields: tuple[str, ...],
    value_fields: tuple[str, ...],
    numeric_tolerances: Mapping[str, float] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Compare two sources without averaging or silently selecting the secondary value."""

    tolerances = dict(numeric_tolerances or {})
    primary = _index_unique(primary_rows, key_fields, "primary")
    secondary = _index_unique(secondary_rows, key_fields, "secondary")
    comparisons: list[dict[str, Any]] = []
    for key in sorted(set(primary) | set(secondary), key=lambda value: tuple(str(item) for item in value)):
        left = primary.get(key)
        right = secondary.get(key)
        if left is None:
            comparison_state, quality = "ONLY_SECONDARY", "MISSING"
            conflicts: list[str] = []
        elif right is None:
            comparison_state, quality = "ONLY_PRIMARY", "MISSING"
            conflicts = []
        else:
            conflicts = [
                field
                for field in value_fields
                if not _equal(left.get(field), right.get(field), tolerances.get(field, 0.0))
            ]
            comparison_state = "CONFLICT" if conflicts else "MATCH"
            quality = "CONFLICT" if conflicts else "OK"
        comparisons.append(
            {
                **{field: value for field, value in zip(key_fields, key)},
                "comparison_state": comparison_state,
                "quality": quality,
                "conflict_fields": ",".join(conflicts),
                "primary_values": {field: left.get(field) if left else None for field in value_fields},
                "secondary_values": {field: right.get(field) if right else None for field in value_fields},
            }
        )
    counts = {
        state: sum(row["comparison_state"] == state for row in comparisons)
        for state in ("MATCH", "CONFLICT", "ONLY_PRIMARY", "ONLY_SECONDARY")
    }
    return comparisons, counts
