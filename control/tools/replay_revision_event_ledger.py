#!/usr/bin/env python3
"""Validate and replay an inspection-only append-only revision event ledger.

The tool is deliberately read-only. It has no network client, credentials,
catalog writer, snapshot publisher, quality promotion, or scheduler. Input is
UTF-8 JSON Lines and output is JSON on stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = (
    ROOT / "data" / "catalog" / "contracts" / "revision_event_ledger_v1"
)
DEFAULT_CONTRACT = CONTRACT_ROOT / "contract.json"
PURPOSE_CEILING = "INSPECTION_ONLY"
ENGINE_ID = "replay_revision_event_ledger.py/revision-event-ledger-v1"
COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=:@+-]{0,191}$")
EVENT_TYPES = {"UPSERT", "WITHDRAWAL"}
EVENT_FIELDS = {
    "schema_version",
    "event_id",
    "dataset_id",
    "source_id",
    "business_key",
    "vintage",
    "revision_id",
    "revision_no",
    "event_type",
    "available_at",
    "observed_at",
    "supersedes_revision_id",
    "withdraws_revision_id",
    "payload",
}


class LedgerError(RuntimeError):
    """Fail-closed contract or ledger error."""

    def __init__(self, message: str, *, code: str = "LEDGER_REJECTED"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ValidatedEvent:
    raw: dict[str, Any]
    canonical: dict[str, Any]
    digest: str
    available_at: datetime
    observed_at: datetime
    visible_at: datetime
    chain_key: tuple[str, str, str, str]


@dataclass(frozen=True)
class LedgerState:
    events: tuple[ValidatedEvent, ...]
    input_line_count: int
    duplicate_event_count: int
    late_arrival_count: int
    out_of_order_available_count: int
    dataset_id: str
    content_sha256: str


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def strict_json_loads(text: str, label: str) -> Any:
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise LedgerError(f"invalid JSON in {label}: {exc}", code="INVALID_JSON") from exc


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LedgerError(f"value is not canonical JSON: {exc}") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise LedgerError(f"{label} must be a non-empty RFC3339 timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise LedgerError(f"{label} is not a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LedgerError(
            f"{label} must include an explicit timezone offset",
            code="TIMEZONE_REQUIRED",
        )
    return parsed.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_component(value: Any, label: str) -> str:
    if not isinstance(value, str) or COMPONENT_RE.fullmatch(value) is None:
        raise LedgerError(f"{label} is not a safe non-empty component")
    return value


def validate_business_key(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise LedgerError("business_key must be a non-empty object")
    normalized: dict[str, Any] = {}
    for key in sorted(value):
        item = value[key]
        if not isinstance(key, str) or not key:
            raise LedgerError("business_key names must be non-empty strings")
        if item is None or isinstance(item, (dict, list)):
            raise LedgerError(
                "business_key values must be non-null JSON scalars",
                code="BUSINESS_KEY_INVALID",
            )
        if isinstance(item, float) and not math.isfinite(item):
            raise LedgerError("business_key contains a non-finite number")
        if not isinstance(item, (str, int, float, bool)):
            raise LedgerError("business_key contains an unsupported value")
        normalized[key] = item
    return normalized


def load_contract(path: Path = DEFAULT_CONTRACT) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise LedgerError(f"cannot read contract {path}: {exc}") from exc
    contract = strict_json_loads(text, str(path))
    if not isinstance(contract, dict):
        raise LedgerError("contract must be a JSON object")
    expected = {
        "schema_version": 1,
        "contract_id": "revision-event-ledger-v1",
        "status": PURPOSE_CEILING,
        "event_schema": "event.schema.json",
    }
    for field, value in expected.items():
        if contract.get(field) != value:
            raise LedgerError(f"contract.{field} must equal {value!r}")
    limits = contract.get("limits")
    if (
        not isinstance(limits, dict)
        or set(limits) != {"max_events", "max_input_bytes"}
        or any(
            not isinstance(limits[name], int)
            or isinstance(limits[name], bool)
            or limits[name] <= 0
            for name in limits
        )
    ):
        raise LedgerError("contract limits are invalid")
    return contract


def validate_event(value: Any, line_number: int) -> ValidatedEvent:
    label = f"line {line_number}"
    if not isinstance(value, dict):
        raise LedgerError(f"{label}: event must be an object")
    missing = sorted(EVENT_FIELDS - set(value))
    extra = sorted(set(value) - EVENT_FIELDS)
    if missing or extra:
        raise LedgerError(
            f"{label}: event field mismatch; missing={missing}, extra={extra}",
            code="EVENT_SCHEMA_MISMATCH",
        )
    if value["schema_version"] != 1:
        raise LedgerError(f"{label}: schema_version must equal 1")
    event_id = safe_component(value["event_id"], f"{label}.event_id")
    dataset_id = safe_component(value["dataset_id"], f"{label}.dataset_id")
    source_id = safe_component(value["source_id"], f"{label}.source_id")
    revision_id = safe_component(value["revision_id"], f"{label}.revision_id")
    vintage = safe_component(value["vintage"], f"{label}.vintage")
    revision_no = value["revision_no"]
    if (
        not isinstance(revision_no, int)
        or isinstance(revision_no, bool)
        or revision_no < 1
    ):
        raise LedgerError(
            f"{label}.revision_no must be a positive integer",
            code="REVISION_NUMBER_INVALID",
        )
    event_type = value["event_type"]
    if event_type not in EVENT_TYPES:
        raise LedgerError(f"{label}.event_type is unsupported")
    business_key = validate_business_key(value["business_key"])
    available = parse_timestamp(value["available_at"], f"{label}.available_at")
    observed = parse_timestamp(value["observed_at"], f"{label}.observed_at")
    supersedes = value["supersedes_revision_id"]
    withdraws = value["withdraws_revision_id"]
    if supersedes is not None:
        supersedes = safe_component(supersedes, f"{label}.supersedes_revision_id")
    if withdraws is not None:
        withdraws = safe_component(withdraws, f"{label}.withdraws_revision_id")
    payload = value["payload"]
    if event_type == "UPSERT":
        if not isinstance(payload, dict) or not payload:
            raise LedgerError(
                f"{label}: UPSERT payload must be a non-empty object",
                code="UPSERT_PAYLOAD_INVALID",
            )
        if withdraws is not None:
            raise LedgerError(f"{label}: UPSERT cannot set withdraws_revision_id")
    else:
        if payload is not None:
            raise LedgerError(f"{label}: WITHDRAWAL payload must be null")
        if supersedes is not None:
            raise LedgerError(
                f"{label}: WITHDRAWAL cannot set supersedes_revision_id"
            )
        if withdraws is None:
            raise LedgerError(
                f"{label}: WITHDRAWAL requires withdraws_revision_id",
                code="WITHDRAWAL_TARGET_MISSING",
            )
    normalized = {
        **value,
        "event_id": event_id,
        "dataset_id": dataset_id,
        "source_id": source_id,
        "business_key": business_key,
        "vintage": vintage,
        "revision_id": revision_id,
        "available_at": format_timestamp(available),
        "observed_at": format_timestamp(observed),
        "supersedes_revision_id": supersedes,
        "withdraws_revision_id": withdraws,
    }
    digest = sha256_bytes(canonical_bytes(normalized))
    key_digest = sha256_bytes(canonical_bytes(business_key))
    return ValidatedEvent(
        raw=value,
        canonical=normalized,
        digest=digest,
        available_at=available,
        observed_at=observed,
        visible_at=max(available, observed),
        chain_key=(dataset_id, source_id, key_digest, vintage),
    )


def _ordinary_file(path: Path) -> None:
    if not path.is_file():
        raise LedgerError(f"event ledger is not a file: {path}")
    info = path.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(info, "st_file_attributes", 0)
    if path.is_symlink() or bool(attributes & reparse):
        raise LedgerError("symlink/reparse event ledgers are forbidden")


def read_events(path: Path, contract: dict[str, Any]) -> tuple[list[Any], int]:
    _ordinary_file(path)
    size = path.stat().st_size
    if size > contract["limits"]["max_input_bytes"]:
        raise LedgerError("event ledger exceeds max_input_bytes", code="LIMIT_EXCEEDED")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise LedgerError(f"cannot read event ledger: {exc}") from exc
    nonempty = [(number, text) for number, text in enumerate(lines, 1) if text.strip()]
    if not nonempty:
        raise LedgerError("event ledger is empty")
    if len(nonempty) > contract["limits"]["max_events"]:
        raise LedgerError("event ledger exceeds max_events", code="LIMIT_EXCEEDED")
    events = [strict_json_loads(text, f"{path}:{number}") for number, text in nonempty]
    return events, len(nonempty)


def build_ledger(
    values: Iterable[Any],
    *,
    input_line_count: int | None = None,
) -> LedgerState:
    unique_events: list[ValidatedEvent] = []
    event_ids: dict[str, str] = {}
    revision_ids: dict[str, ValidatedEvent] = {}
    chains: dict[tuple[str, str, str, str], list[ValidatedEvent]] = {}
    duplicate_count = 0
    late_count = 0
    out_of_order_count = 0
    previous_observed: datetime | None = None
    maximum_available: datetime | None = None
    dataset_id: str | None = None

    for line_number, value in enumerate(values, 1):
        event = validate_event(value, line_number)
        prior_digest = event_ids.get(event.canonical["event_id"])
        if prior_digest is not None:
            if prior_digest != event.digest:
                raise LedgerError(
                    f"event_id conflict: {event.canonical['event_id']}",
                    code="EVENT_ID_CONFLICT",
                )
            duplicate_count += 1
            continue
        if dataset_id is None:
            dataset_id = event.canonical["dataset_id"]
        elif dataset_id != event.canonical["dataset_id"]:
            raise LedgerError(
                "one ledger may contain exactly one dataset_id",
                code="MULTI_DATASET_LEDGER",
            )
        if (
            previous_observed is not None
            and event.observed_at < previous_observed
        ):
            raise LedgerError(
                "observed_at must be non-decreasing in append order",
                code="OBSERVED_AT_ORDER_INVALID",
            )
        if maximum_available is not None and event.available_at < maximum_available:
            out_of_order_count += 1
        if event.observed_at > event.available_at:
            late_count += 1
        if event.canonical["revision_id"] in revision_ids:
            raise LedgerError(
                f"revision_id conflict: {event.canonical['revision_id']}",
                code="REVISION_ID_CONFLICT",
            )

        chain = chains.setdefault(event.chain_key, [])
        expected_revision = len(chain) + 1
        if event.canonical["revision_no"] != expected_revision:
            raise LedgerError(
                f"revision_no must be contiguous and monotonic; "
                f"expected {expected_revision}",
                code="REVISION_NOT_MONOTONIC",
            )
        previous = chain[-1] if chain else None
        if previous is None:
            if event.canonical["event_type"] != "UPSERT":
                raise LedgerError(
                    "a chain must start with UPSERT",
                    code="CHAIN_START_INVALID",
                )
            if event.canonical["supersedes_revision_id"] is not None:
                raise LedgerError(
                    "revision 1 cannot supersede another revision",
                    code="SUPERSEDES_TARGET_INVALID",
                )
        else:
            if event.available_at <= previous.available_at:
                raise LedgerError(
                    "available_at must strictly increase with revision_no",
                    code="REVISION_AVAILABLE_AT_INVALID",
                )
            if event.visible_at <= previous.visible_at:
                raise LedgerError(
                    "visible_at must strictly increase with revision_no",
                    code="REVISION_VISIBLE_AT_INVALID",
                )
            if event.canonical["event_type"] == "UPSERT":
                target = event.canonical["supersedes_revision_id"]
                if target is None or target not in revision_ids:
                    raise LedgerError(
                        "supersedes_revision_id must target an existing revision",
                        code="SUPERSEDES_TARGET_MISSING",
                    )
                if target != previous.canonical["revision_id"]:
                    raise LedgerError(
                        "UPSERT must supersede the current chain head",
                        code="SUPERSEDES_TARGET_INVALID",
                    )
            else:
                target = event.canonical["withdraws_revision_id"]
                if target is None or target not in revision_ids:
                    raise LedgerError(
                        "withdraws_revision_id must target an existing revision",
                        code="WITHDRAWAL_TARGET_MISSING",
                    )
                if (
                    target != previous.canonical["revision_id"]
                    or previous.canonical["event_type"] != "UPSERT"
                ):
                    raise LedgerError(
                        "WITHDRAWAL must target the current active UPSERT",
                        code="WITHDRAWAL_TARGET_INVALID",
                    )

        event_ids[event.canonical["event_id"]] = event.digest
        revision_ids[event.canonical["revision_id"]] = event
        chain.append(event)
        unique_events.append(event)
        previous_observed = event.observed_at
        maximum_available = (
            event.available_at
            if maximum_available is None
            else max(maximum_available, event.available_at)
        )

    if not unique_events or dataset_id is None:
        raise LedgerError("event ledger has no unique events")
    content_hash = sha256_bytes(
        canonical_bytes([event.canonical for event in unique_events])
    )
    return LedgerState(
        events=tuple(unique_events),
        input_line_count=(
            input_line_count if input_line_count is not None else len(unique_events)
        ),
        duplicate_event_count=duplicate_count,
        late_arrival_count=late_count,
        out_of_order_available_count=out_of_order_count,
        dataset_id=dataset_id,
        content_sha256=content_hash,
    )


def validate_file(
    events_path: Path,
    contract_path: Path = DEFAULT_CONTRACT,
) -> LedgerState:
    contract = load_contract(contract_path)
    values, input_lines = read_events(events_path, contract)
    return build_ledger(values, input_line_count=input_lines)


def replay_ledger(state: LedgerState, decision_time: str) -> dict[str, Any]:
    cutoff = parse_timestamp(decision_time, "decision_time")
    visible = [
        event
        for event in state.events
        if event.available_at <= cutoff and event.observed_at <= cutoff
    ]
    heads: dict[tuple[str, str, str, str], ValidatedEvent] = {}
    for event in visible:
        heads[event.chain_key] = event
    active: list[dict[str, Any]] = []
    withdrawn: list[dict[str, Any]] = []
    for key in sorted(heads):
        event = heads[key]
        common = {
            "dataset_id": event.canonical["dataset_id"],
            "source_id": event.canonical["source_id"],
            "business_key": event.canonical["business_key"],
            "vintage": event.canonical["vintage"],
            "revision_id": event.canonical["revision_id"],
            "revision_no": event.canonical["revision_no"],
            "available_at": event.canonical["available_at"],
            "observed_at": event.canonical["observed_at"],
            "visible_at": format_timestamp(event.visible_at),
        }
        if event.canonical["event_type"] == "UPSERT":
            active.append({**common, "payload": event.canonical["payload"]})
        else:
            withdrawn.append(
                {
                    **common,
                    "withdraws_revision_id": event.canonical[
                        "withdraws_revision_id"
                    ],
                }
            )
    return {
        "schema_version": 1,
        "engine_id": ENGINE_ID,
        "status": "PASS_INSPECTION_ONLY",
        "purpose_ceiling": PURPOSE_CEILING,
        "source_admission": False,
        "catalog_registered": False,
        "quality_promotion": False,
        "research_ready": False,
        "dataset_id": state.dataset_id,
        "decision_time": format_timestamp(cutoff),
        "visibility_rule": (
            "available_at <= decision_time AND observed_at <= decision_time"
        ),
        "ledger": {
            "input_line_count": state.input_line_count,
            "unique_event_count": len(state.events),
            "duplicate_event_count": state.duplicate_event_count,
            "late_arrival_count": state.late_arrival_count,
            "out_of_order_available_count": state.out_of_order_available_count,
            "content_sha256": state.content_sha256,
        },
        "visible_event_count": len(visible),
        "active_record_count": len(active),
        "withdrawn_record_count": len(withdrawn),
        "active_records": active,
        "withdrawn_records": withdrawn,
    }


def validation_receipt(state: LedgerState) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "engine_id": ENGINE_ID,
        "status": "PASS_INSPECTION_ONLY",
        "purpose_ceiling": PURPOSE_CEILING,
        "source_admission": False,
        "catalog_registered": False,
        "quality_promotion": False,
        "research_ready": False,
        "dataset_id": state.dataset_id,
        "input_line_count": state.input_line_count,
        "unique_event_count": len(state.events),
        "duplicate_event_count": state.duplicate_event_count,
        "late_arrival_count": state.late_arrival_count,
        "out_of_order_available_count": state.out_of_order_available_count,
        "content_sha256": state.content_sha256,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", required=True, type=Path)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate-only", action="store_true")
    mode.add_argument("--decision-time")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        state = validate_file(args.events.resolve(), args.contract.resolve())
        result = (
            validation_receipt(state)
            if args.validate_only
            else replay_ledger(state, args.decision_time)
        )
    except LedgerError as exc:
        result = {
            "schema_version": 1,
            "engine_id": ENGINE_ID,
            "status": "FAIL_CLOSED",
            "error_code": exc.code,
            "error": str(exc),
            "purpose_ceiling": PURPOSE_CEILING,
            "source_admission": False,
            "catalog_registered": False,
            "quality_promotion": False,
            "research_ready": False,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
