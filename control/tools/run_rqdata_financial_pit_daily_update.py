#!/usr/bin/env python3
"""Execute one bounded task from an RQData financial-PIT quarantine queue."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

import run_rqdata_b_path_daily_update as bpath
import run_rqdata_shsz_daily_update as pv


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "data/catalog/contracts/rqdata_financial_pit_daily_candidate_v1/contract.json"
CANDIDATE_PARENT = ROOT / "data/market/quarantine/rqdata-financial-pit-daily"
SMOKE_PARENT = ROOT / "data/market/quarantine/rqdata-financial-pit-daily-smoke"
STATE_PARENT = ROOT / "data/market/transactions/rqdata-financial-pit-daily"
B_PATH_CANDIDATE_PARENT = ROOT / "data/market/quarantine/rqdata-b-path-daily"
PRODUCER_ID = "run_rqdata_financial_pit_daily_update.py/v1"
DATASET_ID = "rqdata-financial-pit-daily-candidate"
SOURCE_ID = "RQDATA_CANDIDATE"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
QUARTER_RE = re.compile(r"^[12][0-9]{3}q[1-4]$")


class UpdateError(RuntimeError):
    """The financial candidate must not be committed."""


def load_contract() -> dict[str, Any]:
    contract = pv.load_json(CONTRACT_PATH)
    if contract.get("contract_id") != "rqdata_financial_pit_daily_candidate_v1":
        raise UpdateError("unexpected financial PIT worker contract")
    safety = contract.get("safety", {})
    forbidden = (
        "catalog_write", "roadmap_write", "source_registry_write",
        "quality_promotion", "formal_snapshot_write", "research_use",
        "credential_value_capture", "scheduled_task_creation",
        "full_market_historical_backfill",
    )
    if any(safety.get(name) is not False for name in forbidden):
        raise UpdateError("financial worker safety contract is not fail-closed")
    if safety.get("purpose_ceiling") != "INSPECTION_ONLY":
        raise UpdateError("financial worker purpose ceiling must be INSPECTION_ONLY")
    return contract


def canonical_sha(value: Any) -> str:
    return hashlib.sha256(pv.canonical_bytes(value)).hexdigest()


def verify_source_queue(queue_path: Path) -> tuple[dict[str, Any], str, Path]:
    queue_path = queue_path.resolve()
    if queue_path.name != "queue.json" or queue_path.parent.name != "financial":
        raise UpdateError("queue must be an upstream financial/queue.json")
    candidate_root = queue_path.parents[1]
    manifest = bpath.verify_candidate(candidate_root)
    relative = queue_path.relative_to(candidate_root).as_posix()
    references = {str(item.get("path")): item for item in manifest.get("artifacts", [])}
    reference = references.get(relative)
    if reference is None or reference.get("sha256") != pv.sha256_file(queue_path):
        raise UpdateError("financial queue is not hash-bound to the upstream candidate")
    return pv.load_json(queue_path), pv.sha256_file(queue_path), candidate_root


def validate_queue(queue: Mapping[str, Any], queue_sha256: str, contract: Mapping[str, Any]) -> None:
    if not SHA256_RE.fullmatch(queue_sha256):
        raise UpdateError("invalid queue sha256")
    queue_id = str(queue.get("queue_id", ""))
    if (
        queue.get("schema_version") != 1
        or not queue_id.startswith("rqdata-financial-pit-")
        or not re.fullmatch(r"[A-Za-z0-9._-]{1,192}", queue_id)
    ):
        raise UpdateError("unsupported financial queue")
    tasks = queue.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != queue.get("task_count"):
        raise UpdateError("financial queue task_count mismatch")
    limits = contract["limits"]
    all_ids: list[str] = []
    seen_tasks: set[str] = set()
    for task in tasks:
        task_id = str(task.get("task_id", ""))
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,192}", task_id) or task_id in seen_tasks:
            raise UpdateError("duplicate or missing task_id")
        seen_tasks.add(task_id)
        request = task.get("request")
        if not isinstance(request, dict) or request.get("endpoint") != "get_pit_financials_ex":
            raise UpdateError(f"unexpected financial endpoint in {task_id}")
        if canonical_sha(request) != task.get("request_sha256"):
            raise UpdateError(f"request hash mismatch in {task_id}")
        ids = request.get("order_book_ids")
        fields = request.get("fields")
        if not isinstance(ids, list) or not ids or len(ids) > int(limits["maximum_securities_per_task"]):
            raise UpdateError(f"invalid security batch in {task_id}")
        if len(ids) != len(set(ids)) or not all(pv.is_shsz_a_share(str(item)) for item in ids):
            raise UpdateError(f"invalid or duplicate security identity in {task_id}")
        if not isinstance(fields, list) or not fields or len(fields) > int(limits["maximum_fields"]):
            raise UpdateError(f"invalid field list in {task_id}")
        if len(fields) != len(set(fields)) or not all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", str(item)) for item in fields):
            raise UpdateError(f"unsafe or duplicate financial field in {task_id}")
        lookback = request.get("lookback_quarters")
        if not isinstance(lookback, int) or not 1 <= lookback <= int(limits["maximum_lookback_quarters"]):
            raise UpdateError(f"invalid quarter lookback in {task_id}")
        try:
            date.fromisoformat(str(request.get("as_of_date")))
        except ValueError as exc:
            raise UpdateError(f"invalid as_of_date in {task_id}") from exc
        all_ids.extend(str(item) for item in ids)
    if len(all_ids) != len(set(all_ids)) or len(all_ids) != queue.get("security_count"):
        raise UpdateError("queue does not uniquely partition its security universe")


def quarter_range(as_of: date, lookback: int) -> tuple[str, str]:
    current_index = as_of.year * 4 + (as_of.month - 1) // 3
    end_index = current_index - 1
    start_index = end_index - lookback + 1
    def label(value: int) -> str:
        return f"{value // 4}q{value % 4 + 1}"
    return label(start_index), label(end_index)


def normalize_frame(value: Any, request: Mapping[str, Any]) -> pd.DataFrame:
    fields = [str(item) for item in request["fields"]]
    columns = [
        "order_book_id", "quarter", "info_date", "if_adjusted",
        "rice_create_tm_vendor_metadata", *fields,
    ]
    if value is None:
        return pd.DataFrame(columns=columns)
    if not isinstance(value, pd.DataFrame):
        raise UpdateError(f"unsupported financial response type: {type(value).__name__}")
    frame = value.reset_index().copy()
    frame.columns = [str(item) for item in frame.columns]
    required = {"order_book_id", "quarter", "info_date", *fields}
    missing = sorted(required - set(frame.columns))
    if missing and not frame.empty:
        raise UpdateError(f"financial response fields missing: {missing}")
    if frame.empty:
        return pd.DataFrame(columns=columns)
    frame["order_book_id"] = frame["order_book_id"].astype(str)
    allowed = set(str(item) for item in request["order_book_ids"])
    if not set(frame["order_book_id"]).issubset(allowed):
        raise UpdateError("financial response escaped requested security batch")
    frame["quarter"] = frame["quarter"].astype(str).str.lower()
    if not frame["quarter"].map(lambda item: bool(QUARTER_RE.fullmatch(item))).all():
        raise UpdateError("financial response contains invalid quarter")
    parsed = pd.to_datetime(frame["info_date"], errors="coerce")
    if parsed.isna().any():
        raise UpdateError("financial response contains missing/invalid info_date")
    frame["info_date"] = parsed.dt.date
    as_of = date.fromisoformat(str(request["as_of_date"]))
    if frame["info_date"].gt(as_of).any():
        raise UpdateError("financial response contains info_date after as_of_date")
    if "if_adjusted" not in frame.columns:
        frame["if_adjusted"] = pd.NA
    if "rice_create_tm" in frame.columns:
        frame = frame.rename(columns={"rice_create_tm": "rice_create_tm_vendor_metadata"})
    elif "rice_create_tm_vendor_metadata" not in frame.columns:
        frame["rice_create_tm_vendor_metadata"] = pd.NA
    for column in columns:
        if column not in frame.columns:
            frame[column] = pd.NA
    frame = frame[columns].copy()
    for column in frame.columns:
        frame[column] = frame[column].map(json_scalar)
    return frame.sort_values(
        ["order_book_id", "quarter", "info_date", "if_adjusted"],
        kind="mergesort", na_position="last",
    ).reset_index(drop=True)


def json_scalar(value: Any) -> Any:
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not pd.notna(value):
        return None
    if isinstance(value, float) and value in (float("inf"), float("-inf")):
        raise UpdateError("financial response contains non-finite numeric value")
    return value


@dataclass
class ApiBudget:
    maximum_calls: int
    maximum_rows: int
    calls: int = 0
    rows: int = 0
    observations: list[dict[str, Any]] = field(default_factory=list)

    def call(self, name: str, function: Callable[[], Any]) -> Any:
        if self.calls >= self.maximum_calls:
            raise UpdateError(f"business API call cap reached before {name}")
        self.calls += 1
        value = function()
        rows = bpath.count_rows(value)
        if self.rows + rows > self.maximum_rows:
            raise UpdateError(f"returned row cap exceeded by {name}")
        self.rows += rows
        self.observations.append({"name": name, "rows": rows})
        return value


def next_session_map(info_dates: list[date], trading_dates: list[Any]) -> dict[date, date]:
    calendar = sorted({pv.date_or_none(item) for item in trading_dates} - {None})
    result: dict[date, date] = {}
    for info_date in sorted(set(info_dates)):
        index = bisect_right(calendar, info_date)
        if index >= len(calendar):
            raise UpdateError(f"next verified trading day unavailable after {info_date}")
        result[info_date] = calendar[index]
    return result


def build_events(
    frame: pd.DataFrame,
    fields: list[str],
    sessions: Mapping[date, date],
    observed_at: str,
) -> list[dict[str, Any]]:
    observations: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in frame.to_dict(orient="records"):
        info_date = date.fromisoformat(str(row["info_date"]))
        for metric in fields:
            key = (str(row["order_book_id"]), str(row["quarter"]), metric)
            payload = {
                "value": row.get(metric),
                "if_adjusted": row.get("if_adjusted"),
                "source_info_date": info_date.isoformat(),
            }
            observations.setdefault(key, []).append({"info_date": info_date, "payload": payload})

    events: list[dict[str, Any]] = []
    for (order_book_id, quarter, metric), rows in sorted(observations.items()):
        unique: dict[date, dict[str, Any]] = {}
        for row in rows:
            info_date = row["info_date"]
            previous = unique.get(info_date)
            if previous is not None and canonical_sha(previous) != canonical_sha(row["payload"]):
                raise UpdateError(
                    f"same-chain same-info-date conflict: {order_book_id} {quarter} {metric} {info_date}"
                )
            unique[info_date] = row["payload"]
        supersedes: str | None = None
        for revision_no, (info_date, payload) in enumerate(sorted(unique.items()), start=1):
            material = {
                "source_id": SOURCE_ID,
                "order_book_id": order_book_id,
                "quarter": quarter,
                "metric": metric,
                "info_date": info_date.isoformat(),
                "payload": payload,
            }
            digest = canonical_sha(material)
            revision_id = f"rqfp-{digest[:32]}"
            events.append({
                "schema_version": 1,
                "event_id": f"evt-{digest[:32]}",
                "dataset_id": DATASET_ID,
                "source_id": SOURCE_ID,
                "business_key": {"order_book_id": order_book_id, "metric": metric},
                "vintage": quarter,
                "revision_id": revision_id,
                "revision_no": revision_no,
                "event_type": "UPSERT",
                "available_at": f"{sessions[info_date].isoformat()}T09:30:00+08:00",
                "observed_at": observed_at,
                "supersedes_revision_id": supersedes,
                "withdraws_revision_id": None,
                "payload": payload,
            })
            supersedes = revision_id
    validate_events(events)
    return events


def validate_events(events: list[dict[str, Any]]) -> None:
    chains: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    event_ids: set[str] = set()
    for event in events:
        if event["event_id"] in event_ids:
            raise UpdateError("duplicate financial revision event_id")
        event_ids.add(event["event_id"])
        datetime.fromisoformat(event["available_at"])
        datetime.fromisoformat(event["observed_at"])
        key = (
            event["business_key"]["order_book_id"],
            event["vintage"],
            event["business_key"]["metric"],
        )
        chains.setdefault(key, []).append(event)
    for rows in chains.values():
        rows.sort(key=lambda item: item["revision_no"])
        previous: str | None = None
        for number, event in enumerate(rows, start=1):
            if event["revision_no"] != number or event["supersedes_revision_id"] != previous:
                raise UpdateError("invalid local financial revision chain")
            previous = event["revision_id"]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    payload = b"".join(pv.canonical_bytes(row) + b"\n" for row in rows)
    pv.atomic_write(path, payload)


def numeric_quota(value: Any, field: str) -> int:
    found = pv.quota_number(value, field)
    if found is None:
        raise UpdateError(f"quota {field} unavailable")
    return int(round(found))


def enforce_initial_quota(value: Any, minimum_remaining: int) -> None:
    limit = numeric_quota(value, "bytes_limit")
    used = numeric_quota(value, "bytes_used")
    if limit and limit - used < minimum_remaining:
        raise UpdateError(f"insufficient quota remaining: {limit - used} < {minimum_remaining}")


def state_path(queue_id: str) -> Path:
    return STATE_PARENT / queue_id / "state.json"


def load_state(queue: Mapping[str, Any], queue_sha: str) -> dict[str, Any]:
    path = state_path(str(queue["queue_id"]))
    if not path.exists():
        return {
            "schema_version": 1,
            "queue_id": queue["queue_id"],
            "queue_sha256": queue_sha,
            "completed": {},
            "formal_effect": "NONE",
        }
    state = pv.load_json(path)
    if state.get("queue_id") != queue["queue_id"] or state.get("queue_sha256") != queue_sha:
        raise UpdateError("resume state is bound to a different queue")
    if not isinstance(state.get("completed"), dict):
        raise UpdateError("invalid resume state")
    return state


def verify_task_candidate(path: Path, task: Mapping[str, Any], queue_sha: str) -> dict[str, Any]:
    manifest = pv.load_json(path / "manifest.json")
    if manifest.get("producer") != PRODUCER_ID or manifest.get("status") != "PASS__SEALED_FINANCIAL_PIT_TASK_CANDIDATE":
        raise UpdateError(f"untrusted financial task candidate: {path}")
    if manifest.get("task_id") != task["task_id"] or manifest.get("request_sha256") != task["request_sha256"]:
        raise UpdateError("financial task candidate request binding mismatch")
    if manifest.get("source_queue_sha256") != queue_sha:
        raise UpdateError("financial task candidate queue binding mismatch")
    for item in manifest.get("artifacts", []):
        target = (path / str(item.get("path", ""))).resolve()
        if not target.is_relative_to(path.resolve()) or not target.is_file():
            raise UpdateError("financial candidate artifact path invalid")
        if target.stat().st_size != item.get("bytes") or pv.sha256_file(target) != item.get("sha256"):
            raise UpdateError("financial candidate artifact hash mismatch")
    return manifest


def reconcile_state(queue: Mapping[str, Any], queue_sha: str, state: dict[str, Any]) -> None:
    queue_root = CANDIDATE_PARENT / str(queue["queue_id"])
    for task in queue["tasks"]:
        task_id = str(task["task_id"])
        candidate = queue_root / f"{task_id}-v1"
        completed = state["completed"].get(task_id)
        if completed is not None:
            recorded = (ROOT / str(completed.get("candidate", ""))).resolve()
            if recorded != candidate.resolve():
                raise UpdateError("resume state candidate path mismatch")
            verify_task_candidate(candidate, task, queue_sha)
        elif candidate.is_dir():
            manifest = verify_task_candidate(candidate, task, queue_sha)
            state["completed"][task_id] = {
                "candidate": candidate.resolve().relative_to(ROOT.resolve()).as_posix(),
                "manifest_sha256": pv.sha256_file(candidate / "manifest.json"),
                "rows": manifest["raw_rows"],
                "events": manifest["revision_events"],
            }


def discover_queue_path(through: date) -> Path | None:
    """Select the oldest eligible pending sealed queue, or newest completed queue.

    Discovery is deliberately bounded to direct immutable children of the
    RQData B-path quarantine root.  Every selected queue is hash-verified
    against its parent candidate before its resume state is inspected.
    """

    if not B_PATH_CANDIDATE_PARENT.is_dir():
        return None
    eligible: list[tuple[date, str, Path, bool]] = []
    for candidate in sorted(B_PATH_CANDIDATE_PARENT.iterdir(), key=lambda item: item.name):
        if not candidate.is_dir() or not re.fullmatch(r"delta-\d{8}-\d{8}-v1", candidate.name):
            continue
        attributes = getattr(candidate.lstat(), "st_file_attributes", 0)
        if candidate.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise UpdateError(f"financial queue discovery rejects reparse candidate root: {candidate}")
        queue_path = candidate / "financial/queue.json"
        if not queue_path.is_file():
            raise UpdateError(f"sealed B-path candidate has no financial queue: {candidate}")
        queue, queue_sha, _ = verify_source_queue(queue_path)
        contract = load_contract()
        validate_queue(queue, queue_sha, contract)
        as_of_dates = {
            date.fromisoformat(str(task["request"]["as_of_date"]))
            for task in queue["tasks"]
        }
        if len(as_of_dates) != 1:
            raise UpdateError(f"financial queue mixes as_of dates: {queue_path}")
        as_of = next(iter(as_of_dates))
        if as_of > through:
            continue
        state = load_state(queue, queue_sha)
        reconcile_state(queue, queue_sha, state)
        pending = len(state["completed"]) < len(queue["tasks"])
        eligible.append((as_of, candidate.name, queue_path, pending))
    pending = [item for item in eligible if item[3]]
    if pending:
        return min(pending, key=lambda item: (item[0], item[1]))[2]
    if eligible:
        return max(eligible, key=lambda item: (item[0], item[1]))[2]
    return None


def plan_discovered(through: date) -> dict[str, Any]:
    queue_path = discover_queue_path(through)
    if queue_path is None:
        return {
            "schema_version": 1,
            "status": "SKIPPED__NO_ELIGIBLE_LOCAL_FINANCIAL_QUEUE__NO_NETWORK_NO_WRITE",
            "through": through.isoformat(),
            "formal_effect": "NONE",
            "catalog_write": False,
            "research_use": False,
        }
    result = plan(queue_path)
    result["selection"] = "OLDEST_PENDING_ELIGIBLE_QUEUE__ELSE_NEWEST_COMPLETE"
    result["discovered_queue"] = queue_path.resolve().relative_to(ROOT.resolve()).as_posix()
    return result


def plan(queue_path: Path) -> dict[str, Any]:
    contract = load_contract()
    queue, queue_sha, source_root = verify_source_queue(queue_path)
    validate_queue(queue, queue_sha, contract)
    state = load_state(queue, queue_sha)
    reconcile_state(queue, queue_sha, state)
    pending = [task["task_id"] for task in queue["tasks"] if task["task_id"] not in state["completed"]]
    return {
        "schema_version": 1,
        "status": "PLAN__NO_NETWORK_NO_WRITE",
        "queue_id": queue["queue_id"],
        "source_candidate": source_root.resolve().relative_to(ROOT.resolve()).as_posix(),
        "task_count": len(queue["tasks"]),
        "completed_tasks": len(state["completed"]),
        "pending_tasks": len(pending),
        "next_task_id": pending[0] if pending else None,
        "maximum_tasks_per_run": contract["limits"]["maximum_tasks_per_run"],
        "formal_effect": "NONE",
        "catalog_write": False,
        "research_use": False,
    }


def seal_task_candidate(
    queue: Mapping[str, Any], queue_sha: str, task: Mapping[str, Any],
    frame: pd.DataFrame, events: list[dict[str, Any]], observed_at: str,
    api_observations: list[dict[str, Any]], maximum_bytes: int,
    candidate_parent: Path | None = None,
    candidate_kind: str = "QUEUE_TASK",
    quota_increment_bytes: int | None = None,
) -> Path:
    queue_root = (candidate_parent or CANDIDATE_PARENT) / str(queue["queue_id"])
    queue_root.mkdir(parents=True, exist_ok=True)
    final = queue_root / f"{task['task_id']}-v1"
    if final.exists():
        verify_task_candidate(final, task, queue_sha)
        return final
    staging = queue_root / f".{task['task_id']}.staging-{uuid.uuid4().hex}"
    staging.mkdir(parents=False, exist_ok=False)
    try:
        raw_path = staging / "raw_observations.csv"
        event_path = staging / "revision_events.jsonl"
        quality_path = staging / "quality.json"
        bpath.write_csv(raw_path, frame)
        write_jsonl(event_path, events)
        quality = {
            "schema_version": 1,
            "status": "PASS__MECHANICAL_CANDIDATE_ONLY",
            "raw_rows": len(frame),
            "revision_events": len(events),
            "unique_securities": int(frame["order_book_id"].nunique()) if not frame.empty else 0,
            "available_at_rule": "NEXT_VERIFIED_TRADING_DAY_AT_09_30_ASIA_SHANGHAI_AFTER_INFO_DATE",
            "rice_create_tm_semantics": "VENDOR_METADATA_ONLY__NOT_AVAILABLE_AT",
            "revision_chain_semantics": "LOCAL_DETERMINISTIC_CHAIN__SOURCE_COMPLETENESS_UNPROVEN",
            "api_observations": api_observations,
            "quota_increment_bytes": quota_increment_bytes,
            "formal_effect": "NONE",
        }
        pv.write_json(quality_path, quality)
        artifacts = [raw_path, event_path, quality_path]
        total = sum(path.stat().st_size for path in artifacts)
        if total > maximum_bytes:
            raise UpdateError(f"financial task candidate byte cap exceeded: {total}")
        refs = [pv.file_ref(path, staging) for path in artifacts]
        manifest = {
            "schema_version": 1,
            "producer": PRODUCER_ID,
            "contract_id": "rqdata_financial_pit_daily_candidate_v1",
            "status": "PASS__SEALED_FINANCIAL_PIT_TASK_CANDIDATE",
            "purpose_ceiling": "INSPECTION_ONLY",
            "quality_status": "NOT_ADMITTED",
            "queue_id": queue["queue_id"],
            "source_queue_sha256": queue_sha,
            "task_id": task["task_id"],
            "candidate_kind": candidate_kind,
            "request_sha256": task["request_sha256"],
            "observed_at": observed_at,
            "raw_rows": len(frame),
            "revision_events": len(events),
            "quota_increment_bytes": quota_increment_bytes,
            "artifacts": refs,
            "content_hash": canonical_sha(refs),
            "formal_available_at": "CONSERVATIVE_LOCAL_DERIVATION__NOT_SOURCE_LEGAL_TIMESTAMP",
            "complete_revision_chain": False,
            "catalog_write": False,
            "research_ready": False,
            "factor_runs": 0,
            "formal_effect": "NONE",
        }
        pv.write_json(staging / "manifest.json", manifest)
        os.replace(staging, final)
        verify_task_candidate(final, task, queue_sha)
        return final
    except Exception:
        if staging.exists():
            failed = queue_root / f"failed-{task['task_id']}-{uuid.uuid4().hex[:8]}"
            os.replace(staging, failed)
        raise


def import_client(client: Any | None) -> Any:
    if client is not None:
        return client
    if str(pv.RQ_SITE) not in sys.path:
        sys.path.insert(0, str(pv.RQ_SITE))
    import rqdatac
    rqdatac.init(enable_bjse=False)
    return rqdatac


def acquire_one_task(
    client: Any,
    task: Mapping[str, Any],
    maximum_calls: int,
    maximum_rows: int,
) -> tuple[pd.DataFrame, dict[date, date], ApiBudget]:
    request = task["request"]
    budget = ApiBudget(maximum_calls, maximum_rows)
    start_quarter, end_quarter = quarter_range(
        date.fromisoformat(request["as_of_date"]), int(request["lookback_quarters"]),
    )
    response = budget.call(
        f"get_pit_financials_ex[{task['task_id']}]",
        lambda: client.get_pit_financials_ex(
            list(request["order_book_ids"]), fields=list(request["fields"]),
            start_quarter=start_quarter, end_quarter=end_quarter,
            date=request["as_of_date"], statements="all",
        ),
    )
    frame = normalize_frame(response, request)
    sessions: dict[date, date] = {}
    if not frame.empty:
        info_dates = [date.fromisoformat(str(item)) for item in frame["info_date"]]
        calendar = budget.call(
            "get_trading_dates[financial_available_at]",
            lambda: client.get_trading_dates(
                (min(info_dates) + timedelta(days=1)).isoformat(),
                (max(info_dates) + timedelta(days=14)).isoformat(),
            ),
        )
        sessions = next_session_map(info_dates, list(calendar))
    return frame, sessions, budget


def smoke(queue_path: Path, client: Any | None = None) -> dict[str, Any]:
    contract = load_contract()
    queue, queue_sha, _ = verify_source_queue(queue_path)
    validate_queue(queue, queue_sha, contract)
    limits = contract["live_smoke_limits"]
    source_task = queue["tasks"][0]
    source_request = source_task["request"]
    request = {
        "endpoint": "get_pit_financials_ex",
        "order_book_ids": list(source_request["order_book_ids"][: int(limits["maximum_securities"])]),
        "fields": list(source_request["fields"][: int(limits["maximum_fields"])]),
        "as_of_date": source_request["as_of_date"],
        "lookback_quarters": min(
            int(source_request["lookback_quarters"]),
            int(limits["maximum_lookback_quarters"]),
        ),
    }
    request_sha = canonical_sha(request)
    task = {
        "task_id": f"smoke-{request['as_of_date'].replace('-', '')}-{request_sha[:12]}",
        "request": request,
        "request_sha256": request_sha,
    }
    final = SMOKE_PARENT / str(queue["queue_id"]) / f"{task['task_id']}-v1"
    if final.is_dir():
        manifest = verify_task_candidate(final, task, queue_sha)
        return {
            "status": "NO_CHANGE__SMOKE_CANDIDATE_EXISTS__NO_NETWORK",
            "candidate": final.resolve().relative_to(ROOT.resolve()).as_posix(),
            "raw_rows": manifest["raw_rows"],
            "revision_events": manifest["revision_events"],
            "queue_state_advanced": False,
        }
    descriptor = pv.acquire_lock()
    try:
        client = import_client(client)
        quota_before = client.user.get_quota()
        enforce_initial_quota(quota_before, int(limits["minimum_quota_remaining_before_run_bytes"]))
        frame, sessions, budget = acquire_one_task(
            client, task, int(limits["maximum_business_api_calls"]), int(limits["maximum_returned_rows"]),
        )
        quota_after = client.user.get_quota()
        quota_delta = max(0, numeric_quota(quota_after, "bytes_used") - numeric_quota(quota_before, "bytes_used"))
        if quota_delta > int(limits["maximum_measured_quota_increment_bytes"]):
            raise UpdateError(f"live smoke quota delta cap exceeded: {quota_delta}")
        observed_at = datetime.now(timezone.utc).isoformat()
        events = build_events(frame, list(request["fields"]), sessions, observed_at)
        candidate = seal_task_candidate(
            queue, queue_sha, task, frame, events, observed_at,
            list(budget.observations), int(limits["maximum_candidate_bytes"]),
            candidate_parent=SMOKE_PARENT, candidate_kind="LIVE_SMOKE__NO_QUEUE_STATE_ADVANCE",
            quota_increment_bytes=quota_delta,
        )
        return {
            "schema_version": 1,
            "status": "PASS__LIVE_SMOKE_CANDIDATE_ONLY",
            "candidate": candidate.resolve().relative_to(ROOT.resolve()).as_posix(),
            "securities": len(request["order_book_ids"]),
            "fields": len(request["fields"]),
            "lookback_quarters": request["lookback_quarters"],
            "raw_rows": len(frame),
            "revision_events": len(events),
            "business_api_calls": budget.calls,
            "quota_increment_bytes": quota_delta,
            "queue_state_advanced": False,
            "purpose_ceiling": "INSPECTION_ONLY",
            "formal_effect": "NONE",
        }
    finally:
        pv.release_lock(descriptor)


def run(queue_path: Path, client: Any | None = None, maximum_tasks: int | None = None) -> dict[str, Any]:
    contract = load_contract()
    queue, queue_sha, _ = verify_source_queue(queue_path)
    validate_queue(queue, queue_sha, contract)
    state = load_state(queue, queue_sha)
    reconcile_state(queue, queue_sha, state)
    pending = [task for task in queue["tasks"] if task["task_id"] not in state["completed"]]
    if not pending:
        return {"status": "NO_CHANGE__QUEUE_COMPLETE__NO_NETWORK", "queue_id": queue["queue_id"]}
    cap = int(contract["limits"]["maximum_tasks_per_run"])
    requested_cap = cap if maximum_tasks is None else maximum_tasks
    if not 1 <= requested_cap <= cap:
        raise UpdateError(f"maximum_tasks must be between 1 and {cap}")
    selected = pending[:requested_cap]
    descriptor = pv.acquire_lock()
    try:
        client = import_client(client)
        limits = contract["limits"]
        quota_before = client.user.get_quota()
        enforce_initial_quota(quota_before, int(limits["minimum_quota_remaining_before_run_bytes"]))
        budget = ApiBudget(
            int(limits["maximum_business_api_calls"]),
            int(limits["maximum_returned_rows"]),
        )
        acquired: list[tuple[dict[str, Any], pd.DataFrame]] = []
        info_dates: list[date] = []
        for task in selected:
            request = task["request"]
            start_quarter, end_quarter = quarter_range(
                date.fromisoformat(request["as_of_date"]), int(request["lookback_quarters"]),
            )
            response = budget.call(
                f"get_pit_financials_ex[{task['task_id']}]",
                lambda r=request, s=start_quarter, e=end_quarter: client.get_pit_financials_ex(
                    list(r["order_book_ids"]), fields=list(r["fields"]),
                    start_quarter=s, end_quarter=e, date=r["as_of_date"], statements="all",
                ),
            )
            frame = normalize_frame(response, request)
            acquired.append((task, frame))
            if not frame.empty:
                info_dates.extend(date.fromisoformat(str(item)) for item in frame["info_date"])
        sessions: dict[date, date] = {}
        if info_dates:
            calendar = budget.call(
                "get_trading_dates[financial_available_at]",
                lambda: client.get_trading_dates(
                    (min(info_dates) + timedelta(days=1)).isoformat(),
                    (max(info_dates) + timedelta(days=14)).isoformat(),
                ),
            )
            sessions = next_session_map(info_dates, list(calendar))
        quota_after = client.user.get_quota()
        quota_delta = max(0, numeric_quota(quota_after, "bytes_used") - numeric_quota(quota_before, "bytes_used"))
        if quota_delta > int(limits["maximum_measured_quota_increment_bytes"]):
            raise UpdateError(f"financial quota delta cap exceeded: {quota_delta}")
        observed_at = datetime.now(timezone.utc).isoformat()

        completed: list[dict[str, Any]] = []
        for task, frame in acquired:
            events = build_events(frame, list(task["request"]["fields"]), sessions, observed_at)
            candidate = seal_task_candidate(
                queue, queue_sha, task, frame, events, observed_at,
                list(budget.observations), int(limits["maximum_candidate_bytes_per_task"]),
                quota_increment_bytes=quota_delta,
            )
            manifest = verify_task_candidate(candidate, task, queue_sha)
            state["completed"][task["task_id"]] = {
                "candidate": candidate.resolve().relative_to(ROOT.resolve()).as_posix(),
                "manifest_sha256": pv.sha256_file(candidate / "manifest.json"),
                "rows": manifest["raw_rows"],
                "events": manifest["revision_events"],
            }
            completed.append({"task_id": task["task_id"], **state["completed"][task["task_id"]]})
        state["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
        state["completed_tasks"] = len(state["completed"])
        state["task_count"] = len(queue["tasks"])
        state["status"] = "COMPLETE" if len(state["completed"]) == len(queue["tasks"]) else "PARTIAL__RESUMABLE"
        path = state_path(str(queue["queue_id"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        pv.write_json(path, state)
        return {
            "schema_version": 1,
            "status": "PASS__BOUNDED_FINANCIAL_PIT_CANDIDATE_TASK",
            "queue_id": queue["queue_id"],
            "completed_this_run": completed,
            "completed_tasks_total": len(state["completed"]),
            "task_count": len(queue["tasks"]),
            "business_api_calls": budget.calls,
            "returned_rows": budget.rows,
            "quota_increment_bytes": quota_delta,
            "purpose_ceiling": "INSPECTION_ONLY",
            "formal_effect": "NONE",
            "catalog_write": False,
            "factor_runs": 0,
        }
    finally:
        pv.release_lock(descriptor)


def run_discovered(
    through: date,
    maximum_tasks: int | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    queue_path = discover_queue_path(through)
    if queue_path is None:
        return {
            "schema_version": 1,
            "status": "SKIPPED__NO_ELIGIBLE_LOCAL_FINANCIAL_QUEUE__NO_NETWORK_NO_WRITE",
            "through": through.isoformat(),
            "formal_effect": "NONE",
            "catalog_write": False,
            "factor_runs": 0,
        }
    result = run(queue_path, client=client, maximum_tasks=maximum_tasks)
    result["selection"] = "OLDEST_PENDING_ELIGIBLE_QUEUE__ELSE_NEWEST_COMPLETE"
    result["discovered_queue"] = queue_path.resolve().relative_to(ROOT.resolve()).as_posix()
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--smoke", action="store_true")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--queue", type=Path)
    source.add_argument("--discover-through", type=date.fromisoformat)
    parser.add_argument("--maximum-tasks", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.smoke and args.discover_through is not None:
            raise UpdateError("--smoke requires an explicit --queue")
        if args.plan and args.discover_through is not None:
            result = plan_discovered(args.discover_through)
        elif args.run and args.discover_through is not None:
            result = run_discovered(args.discover_through, maximum_tasks=args.maximum_tasks)
        elif args.plan:
            result = plan(args.queue)
        elif args.smoke:
            if args.maximum_tasks is not None:
                raise UpdateError("--maximum-tasks is not valid with --smoke")
            result = smoke(args.queue)
        else:
            result = run(args.queue, maximum_tasks=args.maximum_tasks)
    except Exception as exc:
        print(json.dumps({"status": "FAIL_CLOSED", "error": pv.safe_error(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(pv.json_safe(result), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
