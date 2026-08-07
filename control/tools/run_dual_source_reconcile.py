#!/usr/bin/env python3
"""Deterministically reconcile two explicit normalized CSV inputs.

This inspection-only tool never discovers files, performs network access,
chooses a canonical source, writes the formal catalog, or grants research use.
Both inputs are workspace-relative files below ``tmp/`` and must be bound by
their source id, SHA-256, and byte count.

Input specs use the form::

    SOURCE_ID|tmp/path/to/normalized.csv|sha256|bytes

Field rules use the form::

    FIELD|exact|0|critical
    FIELD|numeric|0.000001|noncritical

One-side-missing keys always fail closed.  Field conflicts fail closed when
the affected rule is marked ``critical``.  The evidence output retains both
values and never silently elects a winner.
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
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
ENGINE_ID = "dual_source_reconcile/v1"
CONTRACT_ID = "dual_source_reconcile_v1"
CONTRACT_PATH = (
    ROOT
    / "data"
    / "catalog"
    / "contracts"
    / "dual_source_reconcile_v1"
    / "contract.json"
)
INPUT_ROOT = ROOT / "tmp"
OUTPUT_ROOT = ROOT / "tmp" / "dual-source-reconcile"
PURPOSE_CEILING = "INSPECTION_ONLY"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SOURCE_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_KEYS = 8
MAX_FIELDS = 64


class ReconcileError(RuntimeError):
    """Fail-closed error for invalid requests, inputs, or output state."""


@dataclass(frozen=True)
class InputSpec:
    source_id: str
    relative_path: str
    expected_sha256: str
    expected_bytes: int


@dataclass(frozen=True)
class FieldRule:
    field: str
    mode: str
    tolerance: Decimal
    critical: bool


@dataclass(frozen=True)
class VerifiedInput:
    spec: InputSpec
    path: Path
    actual_sha256: str
    actual_bytes: int
    mtime_ns: int
    device: int
    inode: int


@dataclass(frozen=True)
class CsvRow:
    row_number: int
    values: dict[str, str]
    row_sha256: str


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


def _is_reparse_or_symlink(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _assert_existing_chain_is_plain(base: Path, target: Path) -> None:
    try:
        relative = target.relative_to(base)
    except ValueError as exc:
        raise ReconcileError("path escapes its allowed root") from exc
    current = base
    if current.exists() and _is_reparse_or_symlink(current):
        raise ReconcileError(f"reparse/symlink component is forbidden: {current}")
    for part in relative.parts:
        current = current / part
        if current.exists() and _is_reparse_or_symlink(current):
            raise ReconcileError(
                f"reparse/symlink component is forbidden: {current}"
            )


def _validate_run_id(value: str) -> str:
    if not RUN_ID_RE.fullmatch(value):
        raise ReconcileError("run_id has an invalid format")
    return value


def parse_input_spec(value: str) -> InputSpec:
    parts = value.split("|")
    if len(parts) != 4:
        raise ReconcileError(
            "input spec must be SOURCE_ID|relative_path|sha256|bytes"
        )
    source_id, relative_path, expected_sha256, expected_bytes_raw = parts
    if not SOURCE_ID_RE.fullmatch(source_id):
        raise ReconcileError(f"invalid source_id: {source_id!r}")
    relative = Path(relative_path)
    if (
        not relative_path
        or relative.is_absolute()
        or relative.drive
        or ".." in relative.parts
    ):
        raise ReconcileError("input path must be a safe workspace-relative path")
    if not SHA256_RE.fullmatch(expected_sha256):
        raise ReconcileError("expected SHA-256 must be 64 lowercase hex characters")
    try:
        expected_bytes = int(expected_bytes_raw)
    except ValueError as exc:
        raise ReconcileError("expected bytes must be an integer") from exc
    if expected_bytes < 0:
        raise ReconcileError("expected bytes must be nonnegative")
    return InputSpec(
        source_id=source_id,
        relative_path=relative.as_posix(),
        expected_sha256=expected_sha256,
        expected_bytes=expected_bytes,
    )


def parse_keys(values: Iterable[str]) -> list[str]:
    keys = list(values)
    if not keys:
        raise ReconcileError("at least one explicit key field is required")
    if len(keys) > MAX_KEYS:
        raise ReconcileError(f"at most {MAX_KEYS} key fields are allowed")
    if len(set(keys)) != len(keys):
        raise ReconcileError("duplicate key field")
    if any(not FIELD_RE.fullmatch(field) for field in keys):
        raise ReconcileError("key fields must be canonical ASCII identifiers")
    return keys


def parse_field_rules(values: Iterable[str], keys: Iterable[str]) -> list[FieldRule]:
    rules: list[FieldRule] = []
    fields: set[str] = set()
    key_set = set(keys)
    for value in values:
        parts = value.split("|")
        if len(parts) != 4:
            raise ReconcileError(
                "field rule must be FIELD|exact|0|critical or "
                "FIELD|numeric|TOLERANCE|critical"
            )
        field, mode, tolerance_raw, critical_raw = parts
        if not FIELD_RE.fullmatch(field):
            raise ReconcileError("compare fields must be canonical ASCII identifiers")
        if field in key_set:
            raise ReconcileError(f"compare field overlaps a key field: {field}")
        if field in fields:
            raise ReconcileError(f"duplicate compare field: {field}")
        if mode not in {"exact", "numeric"}:
            raise ReconcileError(f"unsupported comparison mode for {field}: {mode}")
        try:
            tolerance = Decimal(tolerance_raw)
        except InvalidOperation as exc:
            raise ReconcileError(f"invalid tolerance for {field}") from exc
        if not tolerance.is_finite() or tolerance < 0:
            raise ReconcileError(f"tolerance for {field} must be finite and nonnegative")
        if mode == "exact" and tolerance != 0:
            raise ReconcileError(f"exact field {field} must use zero tolerance")
        if critical_raw not in {"critical", "noncritical"}:
            raise ReconcileError(
                f"criticality for {field} must be critical or noncritical"
            )
        fields.add(field)
        rules.append(
            FieldRule(
                field=field,
                mode=mode,
                tolerance=tolerance,
                critical=critical_raw == "critical",
            )
        )
    if not rules:
        raise ReconcileError("at least one explicit compare field rule is required")
    if len(rules) > MAX_FIELDS:
        raise ReconcileError(f"at most {MAX_FIELDS} compare fields are allowed")
    return rules


def _verify_input(spec: InputSpec) -> VerifiedInput:
    allowed = Path(os.path.abspath(INPUT_ROOT))
    lexical = Path(os.path.abspath(ROOT / Path(spec.relative_path)))
    try:
        relative = lexical.relative_to(allowed)
    except ValueError as exc:
        raise ReconcileError("input must stay below tmp/") from exc
    if not relative.parts or relative.parts[0] == "dual-source-reconcile":
        raise ReconcileError("reconciliation outputs cannot be used as inputs")
    _assert_existing_chain_is_plain(allowed, lexical)
    if not lexical.exists() or not lexical.is_file():
        raise ReconcileError(f"input is not an ordinary file: {spec.relative_path}")
    info = lexical.stat()
    if info.st_size != spec.expected_bytes:
        raise ReconcileError(
            f"byte-count mismatch for {spec.source_id}: "
            f"expected {spec.expected_bytes}, got {info.st_size}"
        )
    actual_sha256 = sha256_file(lexical)
    if actual_sha256 != spec.expected_sha256:
        raise ReconcileError(
            f"SHA-256 mismatch for {spec.source_id}: "
            f"expected {spec.expected_sha256}, got {actual_sha256}"
        )
    return VerifiedInput(
        spec=spec,
        path=lexical,
        actual_sha256=actual_sha256,
        actual_bytes=info.st_size,
        mtime_ns=info.st_mtime_ns,
        device=info.st_dev,
        inode=info.st_ino,
    )


def _load_contract() -> tuple[dict[str, Any], str]:
    payload = CONTRACT_PATH.read_bytes()
    try:
        contract = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconcileError("dual-source reconciliation contract is unreadable") from exc
    if contract.get("contract_id") != CONTRACT_ID:
        raise ReconcileError("contract identity mismatch")
    if contract.get("purpose_ceiling") != PURPOSE_CEILING:
        raise ReconcileError("contract purpose ceiling mismatch")
    return contract, sha256_bytes(payload)


def _validate_output(path: Path, run_id: str) -> Path:
    lexical = Path(os.path.abspath(path))
    allowed = Path(os.path.abspath(OUTPUT_ROOT))
    if lexical.parent != allowed or lexical.name != run_id:
        raise ReconcileError(
            "output must be exactly tmp/dual-source-reconcile/<run_id>"
        )
    _assert_existing_chain_is_plain(allowed, lexical.parent)
    if lexical.exists():
        raise ReconcileError("output already exists; reconciliation runs are write-once")
    return lexical


def prepare(
    run_id: str,
    left_spec_raw: str,
    right_spec_raw: str,
    key_fields: Iterable[str],
    field_rules_raw: Iterable[str],
    output: Path,
) -> dict[str, Any]:
    run_id = _validate_run_id(run_id)
    left_spec = parse_input_spec(left_spec_raw)
    right_spec = parse_input_spec(right_spec_raw)
    if left_spec.source_id == right_spec.source_id:
        raise ReconcileError("left and right source_id must be distinct")
    if left_spec.relative_path == right_spec.relative_path:
        raise ReconcileError("left and right input paths must be distinct")
    keys = parse_keys(key_fields)
    rules = parse_field_rules(field_rules_raw, keys)
    left = _verify_input(left_spec)
    right = _verify_input(right_spec)
    contract, contract_sha256 = _load_contract()
    return {
        "run_id": run_id,
        "left": left,
        "right": right,
        "keys": keys,
        "rules": rules,
        "output": _validate_output(output, run_id),
        "contract": contract,
        "contract_sha256": contract_sha256,
    }


def _assert_unchanged(item: VerifiedInput) -> None:
    info = item.path.stat()
    if (
        info.st_size != item.actual_bytes
        or info.st_mtime_ns != item.mtime_ns
        or info.st_dev != item.device
        or info.st_ino != item.inode
        or sha256_file(item.path) != item.actual_sha256
    ):
        raise ReconcileError(f"input changed during reconciliation: {item.spec.source_id}")


def _read_csv(
    item: VerifiedInput, keys: list[str], rules: list[FieldRule]
) -> tuple[list[str], dict[tuple[str, ...], CsvRow]]:
    payload = item.path.read_bytes()
    if sha256_bytes(payload) != item.actual_sha256 or len(payload) != item.actual_bytes:
        raise ReconcileError(f"input changed before parse: {item.spec.source_id}")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ReconcileError(
            f"input must be UTF-8 CSV: {item.spec.source_id}"
        ) from exc
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ReconcileError(f"CSV is empty: {item.spec.source_id}") from exc
    if not header or any(not name for name in header):
        raise ReconcileError(f"CSV header is empty or blank: {item.spec.source_id}")
    if len(set(header)) != len(header):
        raise ReconcileError(f"CSV contains duplicate headers: {item.spec.source_id}")
    required = [*keys, *(rule.field for rule in rules)]
    missing = [field for field in required if field not in header]
    if missing:
        raise ReconcileError(
            f"CSV is missing required fields for {item.spec.source_id}: {missing}"
        )
    rows: dict[tuple[str, ...], CsvRow] = {}
    for row_number, cells in enumerate(reader, start=2):
        if len(cells) != len(header):
            raise ReconcileError(
                f"CSV row width mismatch for {item.spec.source_id} at row {row_number}"
            )
        values = dict(zip(header, cells, strict=True))
        key = tuple(values[field] for field in keys)
        if any(not value.strip() for value in key):
            raise ReconcileError(
                f"blank key for {item.spec.source_id} at row {row_number}"
            )
        if key in rows:
            raise ReconcileError(
                f"duplicate key for {item.spec.source_id}: {key!r}"
            )
        rows[key] = CsvRow(
            row_number=row_number,
            values=values,
            row_sha256=sha256_bytes(
                (canonical_json(values) + "\n").encode("utf-8")
            ),
        )
    _assert_unchanged(item)
    return header, rows


def _decimal(value: str, field: str, source_id: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ReconcileError(
            f"non-numeric value for {field} from {source_id}: {value!r}"
        ) from exc
    if not number.is_finite():
        raise ReconcileError(
            f"non-finite value for {field} from {source_id}: {value!r}"
        )
    return number


def _compare_values(
    left_value: str,
    right_value: str,
    rule: FieldRule,
    left_source: str,
    right_source: str,
) -> tuple[str, str]:
    if left_value == "" or right_value == "":
        if left_value == right_value and not rule.critical:
            return "MATCH", ""
        return "FIELD_CONFLICT", ""
    if rule.mode == "exact":
        return ("MATCH" if left_value == right_value else "FIELD_CONFLICT", "")
    left_number = _decimal(left_value, rule.field, left_source)
    right_number = _decimal(right_value, rule.field, right_source)
    delta = abs(left_number - right_number)
    status = "MATCH" if delta <= rule.tolerance else "FIELD_CONFLICT"
    return status, format(delta, "f")


def _boundary(status: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "engine_id": ENGINE_ID,
        "contract_id": CONTRACT_ID,
        "status": status,
        "purpose_ceiling": PURPOSE_CEILING,
        "network_used": False,
        "input_discovery_used": False,
        "source_inputs_modified": False,
        "canonical_source_selected": False,
        "silent_overwrite_used": False,
        "formal_source_admission": False,
        "formal_catalog_written": False,
        "quality_promotion": False,
        "formal_snapshot_written": False,
        "scheduler_created": False,
        "research_ready": False,
        "formal_effect": "NONE",
    }


def reconcile(prepared: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    left: VerifiedInput = prepared["left"]
    right: VerifiedInput = prepared["right"]
    keys: list[str] = prepared["keys"]
    rules: list[FieldRule] = prepared["rules"]
    _, left_rows = _read_csv(left, keys, rules)
    _, right_rows = _read_csv(right, keys, rules)
    evidence: list[dict[str, Any]] = []
    counts = {
        "MATCH": 0,
        "ONE_SIDE_MISSING": 0,
        "FIELD_CONFLICT": 0,
        "critical_field_conflicts": 0,
        "noncritical_only_field_conflicts": 0,
    }
    for key in sorted(set(left_rows) | set(right_rows)):
        left_row = left_rows.get(key)
        right_row = right_rows.get(key)
        record: dict[str, Any] = {field: value for field, value in zip(keys, key)}
        record.update(
            {
                "outcome": "",
                "critical_failure": False,
                "left_source_id": left.spec.source_id,
                "right_source_id": right.spec.source_id,
                "left_present": left_row is not None,
                "right_present": right_row is not None,
                "left_row_number": left_row.row_number if left_row else "",
                "right_row_number": right_row.row_number if right_row else "",
                "left_row_sha256": left_row.row_sha256 if left_row else "",
                "right_row_sha256": right_row.row_sha256 if right_row else "",
            }
        )
        if left_row is None or right_row is None:
            record["outcome"] = "ONE_SIDE_MISSING"
            record["critical_failure"] = True
            counts["ONE_SIDE_MISSING"] += 1
            for rule in rules:
                record[f"{rule.field}__left"] = (
                    left_row.values[rule.field] if left_row else ""
                )
                record[f"{rule.field}__right"] = (
                    right_row.values[rule.field] if right_row else ""
                )
                record[f"{rule.field}__status"] = "ONE_SIDE_MISSING"
                record[f"{rule.field}__delta"] = ""
                record[f"{rule.field}__tolerance"] = format(rule.tolerance, "f")
                record[f"{rule.field}__critical"] = rule.critical
            evidence.append(record)
            continue

        any_conflict = False
        critical_conflict = False
        for rule in rules:
            left_value = left_row.values[rule.field]
            right_value = right_row.values[rule.field]
            field_status, delta = _compare_values(
                left_value,
                right_value,
                rule,
                left.spec.source_id,
                right.spec.source_id,
            )
            record[f"{rule.field}__left"] = left_value
            record[f"{rule.field}__right"] = right_value
            record[f"{rule.field}__status"] = field_status
            record[f"{rule.field}__delta"] = delta
            record[f"{rule.field}__tolerance"] = format(rule.tolerance, "f")
            record[f"{rule.field}__critical"] = rule.critical
            if field_status == "FIELD_CONFLICT":
                any_conflict = True
                critical_conflict = critical_conflict or rule.critical
        if any_conflict:
            record["outcome"] = "FIELD_CONFLICT"
            record["critical_failure"] = critical_conflict
            counts["FIELD_CONFLICT"] += 1
            if critical_conflict:
                counts["critical_field_conflicts"] += 1
            else:
                counts["noncritical_only_field_conflicts"] += 1
        else:
            record["outcome"] = "MATCH"
            counts["MATCH"] += 1
        evidence.append(record)
    _assert_unchanged(left)
    _assert_unchanged(right)
    return evidence, counts


def _csv_payload(
    evidence: list[dict[str, Any]], keys: list[str], rules: list[FieldRule]
) -> bytes:
    fieldnames = [
        *keys,
        "outcome",
        "critical_failure",
        "left_source_id",
        "right_source_id",
        "left_present",
        "right_present",
        "left_row_number",
        "right_row_number",
        "left_row_sha256",
        "right_row_sha256",
    ]
    for rule in rules:
        fieldnames.extend(
            [
                f"{rule.field}__left",
                f"{rule.field}__right",
                f"{rule.field}__status",
                f"{rule.field}__delta",
                f"{rule.field}__tolerance",
                f"{rule.field}__critical",
            ]
        )
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream, fieldnames=fieldnames, lineterminator="\n", extrasaction="raise"
    )
    writer.writeheader()
    writer.writerows(evidence)
    return stream.getvalue().encode("utf-8")


def _cleanup_owned_stage(stage: Path) -> None:
    allowed = Path(os.path.abspath(OUTPUT_ROOT))
    lexical = Path(os.path.abspath(stage))
    if lexical.parent != allowed or not lexical.name.startswith("."):
        raise ReconcileError("refusing to clean a staging path outside owned root")
    if lexical.exists():
        shutil.rmtree(lexical)


def execute(prepared: dict[str, Any]) -> dict[str, Any]:
    output: Path = prepared["output"]
    if output.exists():
        raise ReconcileError("output already exists; reconciliation runs are write-once")
    evidence, counts = reconcile(prepared)
    fail_closed = bool(
        counts["ONE_SIDE_MISSING"] or counts["critical_field_conflicts"]
    )
    if fail_closed:
        status = "FAIL_CLOSED_CRITICAL_RECONCILIATION_GAP"
    elif counts["noncritical_only_field_conflicts"]:
        status = "PASS_WITH_NONCRITICAL_CONFLICTS_INSPECTION_ONLY"
    else:
        status = "PASS_RECONCILED_INSPECTION_ONLY"

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    _assert_existing_chain_is_plain(OUTPUT_ROOT, OUTPUT_ROOT)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{prepared['run_id']}.staging-", dir=OUTPUT_ROOT)
    )
    try:
        csv_payload = _csv_payload(evidence, prepared["keys"], prepared["rules"])
        csv_path = stage / "RECONCILIATION.csv"
        csv_path.write_bytes(csv_payload)
        csv_sha256 = sha256_bytes(csv_payload)
        left: VerifiedInput = prepared["left"]
        right: VerifiedInput = prepared["right"]
        manifest = {
            **_boundary(status),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "run_id": prepared["run_id"],
            "fail_closed": fail_closed,
            "contract_relative_path": CONTRACT_PATH.relative_to(ROOT).as_posix(),
            "contract_sha256": prepared["contract_sha256"],
            "input_mode": "TWO_EXPLICIT_HASH_AND_BYTE_BOUND_NORMALIZED_CSVS",
            "inputs": [
                {
                    "side": "left",
                    "source_id": left.spec.source_id,
                    "relative_path": left.spec.relative_path,
                    "sha256": left.actual_sha256,
                    "bytes": left.actual_bytes,
                },
                {
                    "side": "right",
                    "source_id": right.spec.source_id,
                    "relative_path": right.spec.relative_path,
                    "sha256": right.actual_sha256,
                    "bytes": right.actual_bytes,
                },
            ],
            "key_fields": prepared["keys"],
            "field_rules": [
                {
                    "field": rule.field,
                    "mode": rule.mode,
                    "tolerance": format(rule.tolerance, "f"),
                    "critical": rule.critical,
                }
                for rule in prepared["rules"]
            ],
            "row_count": len(evidence),
            "outcome_counts": counts,
            "evidence_csv": {
                "relative_path": "RECONCILIATION.csv",
                "sha256": csv_sha256,
                "bytes": len(csv_payload),
                "contains_both_source_values": True,
                "canonical_value_column_present": False,
            },
            "known_limitations": [
                "NORMALIZED_INPUT_SEMANTICS_ARE_NOT_PROVEN_BY_RECONCILIATION",
                "SOURCE_RIGHTS_AND_PIT_ARE_NOT_CLOSED_BY_RECONCILIATION",
                "TMP_EVIDENCE_IS_NOT_A_FORMAL_IMMUTABLE_SNAPSHOT",
                "NO_CATALOG_OR_RESEARCH_GRANT",
            ],
        }
        manifest_payload = (canonical_json(manifest) + "\n").encode("utf-8")
        manifest_path = stage / "RECONCILIATION_MANIFEST.json"
        manifest_path.write_bytes(manifest_payload)
        manifest_sha256 = sha256_bytes(manifest_payload)
        (stage / "RECONCILIATION_MANIFEST.sha256").write_text(
            f"{manifest_sha256}  RECONCILIATION_MANIFEST.json\n", encoding="ascii"
        )
        if sha256_file(csv_path) != csv_sha256:
            raise ReconcileError("reconciliation CSV hash verification failed")
        if sha256_file(manifest_path) != manifest_sha256:
            raise ReconcileError("reconciliation manifest hash verification failed")
        _assert_unchanged(left)
        _assert_unchanged(right)
        if output.exists():
            raise ReconcileError("output appeared during run; refusing overwrite")
        os.rename(stage, output)
        return {
            **_boundary(status),
            "run_id": prepared["run_id"],
            "fail_closed": fail_closed,
            "output_relative_path": output.relative_to(ROOT).as_posix(),
            "row_count": len(evidence),
            "outcome_counts": counts,
            "reconciliation_csv_sha256": csv_sha256,
            "manifest_sha256": manifest_sha256,
        }
    except Exception:
        _cleanup_owned_stage(stage)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--left-spec", required=True)
    parser.add_argument("--right-spec", required=True)
    parser.add_argument("--key", action="append", required=True)
    parser.add_argument("--field-rule", action="append", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        prepared = prepare(
            args.run_id,
            args.left_spec,
            args.right_spec,
            args.key,
            args.field_rule,
            args.output,
        )
        if not args.execute:
            plan = {
                **_boundary("PLAN_DUAL_SOURCE_RECONCILIATION"),
                "run_id": prepared["run_id"],
                "output_relative_path": prepared["output"].relative_to(ROOT).as_posix(),
                "left_source_id": prepared["left"].spec.source_id,
                "right_source_id": prepared["right"].spec.source_id,
                "key_fields": prepared["keys"],
                "field_rules": [
                    {
                        "field": rule.field,
                        "mode": rule.mode,
                        "tolerance": format(rule.tolerance, "f"),
                        "critical": rule.critical,
                    }
                    for rule in prepared["rules"]
                ],
                "write_performed": False,
            }
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        result = execute(prepared)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 3 if result["fail_closed"] else 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    **_boundary("FAIL_CLOSED_INVALID_RECONCILIATION_RUN"),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=os.sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
