#!/usr/bin/env python3
"""Inspection-only local daily orchestration state layer.

This module persists job dates, claims, leases, attempts, a contiguous
watermark and an append-only event ledger in SQLite.  It deliberately has no
network client, worker command execution, scheduler integration, formal data
writer, catalog writer or quality-promotion path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[2]
FORMAL_CATALOG_REL = Path("data/catalog/datasets.json")
OUTPUT_REL = Path("tmp/local-daily-orchestration")
ENGINE_ID = "local_daily_orchestration_state/sqlite-v1"
PURPOSE_CEILING = "INSPECTION_ONLY"
DB_SCHEMA_VERSION = "1"
REQUEST_MODE = "local_daily_orchestration_state"
COMPLETE_STATUSES = {"SUCCEEDED", "NO_CHANGE"}
OPEN_STATUSES = {"PENDING", "RETRY_PENDING"}
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class OrchestrationError(RuntimeError):
    """Fail-closed orchestration error with a stable machine code."""

    def __init__(self, message: str, *, code: str = "ORCHESTRATION_REJECTED"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Context:
    workspace_root: Path
    request_path: Path
    request: dict[str, Any]
    request_sha256: str
    catalog_path: Path
    catalog_sha256: str
    output_root: Path
    job_root: Path
    database_path: Path


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OrchestrationError(f"invalid JSON document: {path}") from exc


def is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & 0x400)


def is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def safe_component(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_COMPONENT.fullmatch(value):
        raise OrchestrationError(f"{label} must be a safe path component")
    if value.endswith((".", " ")):
        raise OrchestrationError(f"{label} has an unsafe Windows path form")
    if value.split(".", 1)[0].upper() in RESERVED_WINDOWS_NAMES:
        raise OrchestrationError(f"{label} uses a reserved Windows device name")
    return value


def parse_date(value: Any, label: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise OrchestrationError(f"{label} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise OrchestrationError(f"{label} is not a valid date") from exc


def parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not RFC3339.fullmatch(value):
        raise OrchestrationError(f"{label} must be strict RFC 3339 with timezone")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise OrchestrationError(f"{label} is not a valid date-time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OrchestrationError(f"{label} must contain an explicit timezone")
    return parsed


def normalize_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def validate_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise OrchestrationError("request must be a JSON object")
    expected = {
        "schema_version",
        "mode",
        "purpose_ceiling",
        "dataset_scope",
        "job_id",
        "dataset_id",
        "source_id",
        "timezone",
        "calendar_dates",
        "max_attempts",
        "lease_seconds",
        "authorization_note",
    }
    missing = sorted(expected - set(payload))
    extras = sorted(set(payload) - expected)
    if missing or extras:
        raise OrchestrationError(
            f"request field mismatch; missing={missing}, unsupported={extras}"
        )
    if payload["schema_version"] != 1:
        raise OrchestrationError("request.schema_version must be 1")
    if payload["mode"] != REQUEST_MODE:
        raise OrchestrationError(f"request.mode must be {REQUEST_MODE}")
    if payload["purpose_ceiling"] != PURPOSE_CEILING:
        raise OrchestrationError("purpose_ceiling must remain INSPECTION_ONLY")
    if payload["dataset_scope"] not in {
        "synthetic_fixture",
        "registered_inspection",
    }:
        raise OrchestrationError("unsupported dataset_scope")
    for field in ("job_id", "dataset_id", "source_id"):
        safe_component(payload[field], field)
    timezone_name = payload["timezone"]
    if (
        not isinstance(timezone_name, str)
        or not timezone_name
        or len(timezone_name) > 64
        or any(character in timezone_name for character in "\x00\r\n")
    ):
        raise OrchestrationError("timezone must be a bounded textual identifier")
    calendar_dates = payload["calendar_dates"]
    if (
        not isinstance(calendar_dates, list)
        or not calendar_dates
        or len(calendar_dates) > 4000
        or any(not isinstance(item, str) for item in calendar_dates)
    ):
        raise OrchestrationError("calendar_dates must contain 1..4000 dates")
    parsed_dates = [parse_date(item, "calendar_dates[]") for item in calendar_dates]
    if parsed_dates != sorted(set(parsed_dates)):
        raise OrchestrationError("calendar_dates must be strictly sorted and unique")
    if (
        isinstance(payload["max_attempts"], bool)
        or not isinstance(payload["max_attempts"], int)
        or not 1 <= payload["max_attempts"] <= 10
    ):
        raise OrchestrationError("max_attempts must be an integer from 1 to 10")
    if (
        isinstance(payload["lease_seconds"], bool)
        or not isinstance(payload["lease_seconds"], int)
        or not 30 <= payload["lease_seconds"] <= 86_400
    ):
        raise OrchestrationError("lease_seconds must be from 30 to 86400")
    note = payload["authorization_note"]
    if (
        not isinstance(note, str)
        or not 1 <= len(note) <= 500
        or any(character in note for character in "\x00\r")
    ):
        raise OrchestrationError("authorization_note must be bounded text")
    if payload["dataset_scope"] == "synthetic_fixture":
        identity = f"{payload['dataset_id']} {payload['source_id']}".lower()
        if "synthetic" not in identity and "fixture" not in identity:
            raise OrchestrationError(
                "synthetic_fixture scope requires a synthetic or fixture identity"
            )
    return payload


def validate_workspace(workspace_root: Path, request_path: Path) -> Context:
    workspace_root = workspace_root.resolve()
    if not workspace_root.is_dir() or is_reparse(workspace_root):
        raise OrchestrationError("workspace root must be a regular directory")
    request_path = request_path.resolve()
    if (
        not request_path.is_file()
        or is_reparse(request_path)
        or not is_under(request_path, workspace_root)
    ):
        raise OrchestrationError("request must be a regular file inside the workspace")
    request = validate_request(read_json(request_path))
    request_sha256 = sha256_bytes(canonical_json_bytes(request))
    catalog_path = workspace_root / FORMAL_CATALOG_REL
    if not catalog_path.is_file() or is_reparse(catalog_path):
        raise OrchestrationError("formal catalog must be a regular read-only input")
    catalog = read_json(catalog_path)
    if not isinstance(catalog, dict) or not isinstance(catalog.get("datasets"), list):
        raise OrchestrationError("formal catalog shape is invalid")
    if request["dataset_scope"] == "registered_inspection":
        matches = [
            item
            for item in catalog["datasets"]
            if isinstance(item, dict)
            and item.get("dataset_id") == request["dataset_id"]
        ]
        if len(matches) != 1:
            raise OrchestrationError(
                "registered_inspection requires exactly one catalog identity"
            )
    output_root = workspace_root / OUTPUT_REL
    job_root = output_root / request["job_id"]
    database_path = job_root / "orchestrator.sqlite3"
    if not is_under(database_path, workspace_root / "tmp"):
        raise OrchestrationError("orchestration database escapes tmp")
    return Context(
        workspace_root=workspace_root,
        request_path=request_path,
        request=request,
        request_sha256=request_sha256,
        catalog_path=catalog_path,
        catalog_sha256=sha256_file(catalog_path),
        output_root=output_root,
        job_root=job_root,
        database_path=database_path,
    )


def configure_connection(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 0")


def open_writable_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=0, isolation_level=None)
    configure_connection(connection)
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def open_readonly_database(path: Path) -> sqlite3.Connection:
    if not path.is_file() or is_reparse(path):
        raise OrchestrationError("orchestration database is missing or linked")
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=0, isolation_level=None)
    configure_connection(connection)
    return connection


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            dataset_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            dataset_scope TEXT NOT NULL,
            timezone TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            request_json TEXT NOT NULL,
            max_attempts INTEGER NOT NULL,
            lease_seconds INTEGER NOT NULL,
            watermark_date TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            purpose_ceiling TEXT NOT NULL,
            formal_catalog_written INTEGER NOT NULL,
            scheduler_created INTEGER NOT NULL
        );
        CREATE TABLE schedule_dates (
            job_id TEXT NOT NULL,
            run_date TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            active_claim_token TEXT,
            claimed_at TEXT,
            lease_expires_at TEXT,
            result_sha256 TEXT,
            last_error_code TEXT,
            PRIMARY KEY (job_id, run_date),
            FOREIGN KEY (job_id) REFERENCES jobs(job_id)
        );
        CREATE TABLE attempts (
            job_id TEXT NOT NULL,
            run_date TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            claim_token TEXT NOT NULL UNIQUE,
            worker_id TEXT NOT NULL,
            claimed_at TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL,
            finished_at TEXT,
            outcome TEXT,
            result_sha256 TEXT,
            error_code TEXT,
            PRIMARY KEY (job_id, run_date, attempt_no),
            FOREIGN KEY (job_id, run_date)
                REFERENCES schedule_dates(job_id, run_date)
        );
        CREATE TABLE events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            operation TEXT NOT NULL,
            command_sha256 TEXT NOT NULL,
            event_time TEXT NOT NULL,
            run_date TEXT,
            status TEXT NOT NULL,
            receipt_json TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO meta(key, value) VALUES('db_schema_version', ?)",
        (DB_SCHEMA_VERSION,),
    )
    connection.execute(
        "INSERT INTO meta(key, value) VALUES('engine_id', ?)",
        (ENGINE_ID,),
    )


def verify_database(connection: sqlite3.Connection, context: Context) -> sqlite3.Row:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise OrchestrationError("SQLite integrity_check failed")
    meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())
    if (
        meta.get("db_schema_version") != DB_SCHEMA_VERSION
        or meta.get("engine_id") != ENGINE_ID
    ):
        raise OrchestrationError("orchestration database contract mismatch")
    job = connection.execute(
        "SELECT * FROM jobs WHERE job_id = ?", (context.request["job_id"],)
    ).fetchone()
    if job is None:
        raise OrchestrationError("job identity is missing from database")
    if (
        job["request_sha256"] != context.request_sha256
        or job["request_json"]
        != canonical_json_bytes(context.request).decode("utf-8")
        or job["dataset_id"] != context.request["dataset_id"]
        or job["source_id"] != context.request["source_id"]
    ):
        raise OrchestrationError(
            "request no longer matches sealed job",
            code="REQUEST_BINDING_MISMATCH",
        )
    if (
        job["purpose_ceiling"] != PURPOSE_CEILING
        or job["formal_catalog_written"] != 0
        or job["scheduler_created"] != 0
    ):
        raise OrchestrationError("job exceeded inspection-only boundary")
    return job


@contextmanager
def writer_transaction(context: Context) -> Iterator[sqlite3.Connection]:
    connection = open_writable_database(context.database_path)
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise OrchestrationError(
                    "another orchestration writer holds the database",
                    code="ORCHESTRATOR_WRITER_BUSY",
                ) from exc
            raise
        verify_database(connection, context)
        yield connection
        if sha256_file(context.catalog_path) != context.catalog_sha256:
            raise OrchestrationError(
                "formal catalog changed during state mutation",
                code="FORMAL_CATALOG_CHANGED",
            )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def command_sha256(operation: str, context: Context, arguments: dict[str, Any]) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "operation": operation,
                "request_sha256": context.request_sha256,
                "arguments": arguments,
            }
        )
    )


def replay_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    operation: str,
    command_hash: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT operation, command_sha256, receipt_json FROM events "
        "WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        return None
    if row["operation"] != operation or row["command_sha256"] != command_hash:
        raise OrchestrationError(
            "event_id was reused with different content",
            code="EVENT_ID_REUSE_CONFLICT",
        )
    receipt = json.loads(row["receipt_json"])
    receipt["idempotent_replay"] = True
    receipt["replay_status"] = "NO_CHANGE"
    return receipt


def base_receipt(
    context: Context,
    *,
    event_id: str,
    operation: str,
    event_time: str,
    status: str,
    run_date: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "engine_id": ENGINE_ID,
        "event_id": event_id,
        "operation": operation,
        "event_time": event_time,
        "job_id": context.request["job_id"],
        "dataset_id": context.request["dataset_id"],
        "run_date": run_date,
        "status": status,
        "purpose_ceiling": PURPOSE_CEILING,
        "network_used": False,
        "worker_command_executed": False,
        "formal_data_written": False,
        "formal_catalog_written": False,
        "quality_promotion": False,
        "scheduler_created": False,
        "request_sha256": context.request_sha256,
        "formal_catalog_sha256": context.catalog_sha256,
        "idempotent_replay": False,
    }


def record_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    operation: str,
    command_hash: str,
    event_time: str,
    run_date: str | None,
    status: str,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    cursor = connection.execute(
        "INSERT INTO events("
        "event_id, operation, command_sha256, event_time, run_date, status, "
        "receipt_json) VALUES(?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            operation,
            command_hash,
            event_time,
            run_date,
            status,
            "{}",
        ),
    )
    receipt["sequence"] = cursor.lastrowid
    receipt_json = canonical_json_bytes(receipt).decode("utf-8")
    connection.execute(
        "UPDATE events SET receipt_json = ? WHERE sequence = ?",
        (receipt_json, cursor.lastrowid),
    )
    return receipt


def initialize(context: Context, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or utc_now()
    event_time = normalize_timestamp(now)
    context.output_root.mkdir(parents=True, exist_ok=True)
    if is_reparse(context.output_root):
        raise OrchestrationError("output root must not be a reparse point")
    context.job_root.mkdir(parents=True, exist_ok=True)
    if is_reparse(context.job_root):
        raise OrchestrationError("job root must not be a reparse point")
    if context.database_path.exists():
        connection = open_readonly_database(context.database_path)
        try:
            verify_database(connection, context)
            receipt = status(context)
            receipt["status"] = "NO_CHANGE"
            receipt["operation"] = "init"
            receipt["idempotent_replay"] = True
            return receipt
        finally:
            connection.close()
    temporary = context.job_root / f".orchestrator.partial-{uuid.uuid4().hex}.sqlite3"
    connection: sqlite3.Connection | None = None
    try:
        connection = open_writable_database(temporary)
        connection.execute("BEGIN IMMEDIATE")
        create_schema(connection)
        request_json = canonical_json_bytes(context.request).decode("utf-8")
        connection.execute(
            "INSERT INTO jobs("
            "job_id, dataset_id, source_id, dataset_scope, timezone, "
            "request_sha256, request_json, max_attempts, lease_seconds, "
            "watermark_date, status, created_at, updated_at, purpose_ceiling, "
            "formal_catalog_written, scheduler_created"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'READY', ?, ?, ?, 0, 0)",
            (
                context.request["job_id"],
                context.request["dataset_id"],
                context.request["source_id"],
                context.request["dataset_scope"],
                context.request["timezone"],
                context.request_sha256,
                request_json,
                context.request["max_attempts"],
                context.request["lease_seconds"],
                event_time,
                event_time,
                PURPOSE_CEILING,
            ),
        )
        connection.executemany(
            "INSERT INTO schedule_dates(job_id, run_date, status, attempt_count) "
            "VALUES(?, ?, 'PENDING', 0)",
            [
                (context.request["job_id"], run_date)
                for run_date in context.request["calendar_dates"]
            ],
        )
        event_id = f"init-{context.request_sha256[:24]}"
        command_hash = command_sha256("init", context, {})
        receipt = base_receipt(
            context,
            event_id=event_id,
            operation="init",
            event_time=event_time,
            status="INITIALIZED",
            run_date=None,
        )
        receipt["calendar_date_count"] = len(context.request["calendar_dates"])
        record_event(
            connection,
            event_id=event_id,
            operation="init",
            command_hash=command_hash,
            event_time=event_time,
            run_date=None,
            status="INITIALIZED",
            receipt=receipt,
        )
        connection.commit()
        connection.close()
        connection = None
        os.replace(temporary, context.database_path)
        return receipt
    except Exception:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        if temporary.exists():
            temporary.unlink()
        raise


def status(context: Context) -> dict[str, Any]:
    connection = open_readonly_database(context.database_path)
    try:
        job = verify_database(connection, context)
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT run_date, status, attempt_count, active_claim_token, "
                "claimed_at, lease_expires_at, result_sha256, last_error_code "
                "FROM schedule_dates WHERE job_id = ? ORDER BY run_date",
                (context.request["job_id"],),
            )
        ]
        event_count = connection.execute("SELECT count(*) FROM events").fetchone()[0]
        return {
            "schema_version": 1,
            "engine_id": ENGINE_ID,
            "operation": "status",
            "job_id": job["job_id"],
            "dataset_id": job["dataset_id"],
            "job_status": job["status"],
            "watermark_date": job["watermark_date"],
            "event_count": event_count,
            "dates": rows,
            "purpose_ceiling": PURPOSE_CEILING,
            "network_used": False,
            "worker_command_executed": False,
            "formal_data_written": False,
            "formal_catalog_written": False,
            "quality_promotion": False,
            "scheduler_created": False,
            "formal_catalog_sha256": context.catalog_sha256,
        }
    finally:
        connection.close()


def plan(context: Context, *, through_date: date) -> dict[str, Any]:
    connection = open_readonly_database(context.database_path)
    try:
        job = verify_database(connection, context)
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT run_date, status, attempt_count, lease_expires_at "
                "FROM schedule_dates WHERE job_id = ? AND run_date <= ? "
                "ORDER BY run_date",
                (context.request["job_id"], through_date.isoformat()),
            )
        ]
        unfinished = [row for row in rows if row["status"] not in COMPLETE_STATUSES]
        replay_queue = [
            row["run_date"] for row in unfinished if row["status"] in OPEN_STATUSES
        ]
        blocked = [
            {
                "run_date": row["run_date"],
                "status": row["status"],
                "lease_expires_at": row["lease_expires_at"],
            }
            for row in unfinished
            if row["status"] not in OPEN_STATUSES
        ]
        return {
            "schema_version": 1,
            "engine_id": ENGINE_ID,
            "operation": "plan",
            "job_id": job["job_id"],
            "through_date": through_date.isoformat(),
            "watermark_date": job["watermark_date"],
            "next_unfinished_date": unfinished[0]["run_date"] if unfinished else None,
            "missed_run_replay_queue": replay_queue,
            "blocked": blocked,
            "write_performed": False,
            "purpose_ceiling": PURPOSE_CEILING,
        }
    finally:
        connection.close()


def update_job_status(
    connection: sqlite3.Connection,
    context: Context,
    *,
    now_text: str,
) -> tuple[str, str | None]:
    rows = connection.execute(
        "SELECT run_date, status FROM schedule_dates WHERE job_id = ? "
        "ORDER BY run_date",
        (context.request["job_id"],),
    ).fetchall()
    watermark: str | None = None
    for row in rows:
        if row["status"] not in COMPLETE_STATUSES:
            break
        watermark = row["run_date"]
    if rows and all(row["status"] in COMPLETE_STATUSES for row in rows):
        job_status = "COMPLETE"
    elif any(row["status"] == "FAILED_TERMINAL" for row in rows):
        job_status = "BLOCKED"
    elif any(row["status"] == "RUNNING" for row in rows):
        job_status = "RUNNING"
    else:
        job_status = "READY"
    connection.execute(
        "UPDATE jobs SET watermark_date = ?, status = ?, updated_at = ? "
        "WHERE job_id = ?",
        (watermark, job_status, now_text, context.request["job_id"]),
    )
    return job_status, watermark


def claim(
    context: Context,
    *,
    event_id: str,
    worker_id: str,
    through_date: date,
    now: datetime | None = None,
) -> dict[str, Any]:
    safe_component(event_id, "event_id")
    safe_component(worker_id, "worker_id")
    now = now or utc_now()
    now_text = normalize_timestamp(now)
    arguments = {
        "worker_id": worker_id,
        "through_date": through_date.isoformat(),
    }
    digest = command_sha256("claim", context, arguments)
    with writer_transaction(context) as connection:
        replay = replay_event(
            connection,
            event_id=event_id,
            operation="claim",
            command_hash=digest,
        )
        if replay is not None:
            return replay
        row = connection.execute(
            "SELECT * FROM schedule_dates WHERE job_id = ? AND run_date <= ? "
            "AND status NOT IN ('SUCCEEDED', 'NO_CHANGE') ORDER BY run_date LIMIT 1",
            (context.request["job_id"], through_date.isoformat()),
        ).fetchone()
        run_date = row["run_date"] if row is not None else None
        if row is None:
            receipt_status = "NO_DUE_RUN"
            receipt = base_receipt(
                context,
                event_id=event_id,
                operation="claim",
                event_time=now_text,
                status=receipt_status,
                run_date=None,
            )
        elif row["status"] == "RUNNING":
            expiry = parse_timestamp(row["lease_expires_at"], "lease_expires_at")
            receipt_status = (
                "STALE_CLAIM_REQUIRES_RECOVER"
                if expiry <= now
                else "ACTIVE_CLAIM_BLOCKS_REPLAY"
            )
            receipt = base_receipt(
                context,
                event_id=event_id,
                operation="claim",
                event_time=now_text,
                status=receipt_status,
                run_date=run_date,
            )
            receipt["lease_expires_at"] = row["lease_expires_at"]
        elif row["status"] == "FAILED_TERMINAL":
            receipt_status = "BLOCKED_RETRY_EXHAUSTED"
            receipt = base_receipt(
                context,
                event_id=event_id,
                operation="claim",
                event_time=now_text,
                status=receipt_status,
                run_date=run_date,
            )
        elif row["status"] in OPEN_STATUSES:
            attempt_no = row["attempt_count"] + 1
            if attempt_no > context.request["max_attempts"]:
                raise OrchestrationError(
                    "attempt limit exceeded",
                    code="RETRY_LIMIT_EXCEEDED",
                )
            claim_token = secrets.token_hex(16)
            lease_expires = now + timedelta(
                seconds=context.request["lease_seconds"]
            )
            lease_text = normalize_timestamp(lease_expires)
            connection.execute(
                "UPDATE schedule_dates SET status = 'RUNNING', "
                "attempt_count = ?, active_claim_token = ?, claimed_at = ?, "
                "lease_expires_at = ?, last_error_code = NULL "
                "WHERE job_id = ? AND run_date = ?",
                (
                    attempt_no,
                    claim_token,
                    now_text,
                    lease_text,
                    context.request["job_id"],
                    run_date,
                ),
            )
            connection.execute(
                "INSERT INTO attempts("
                "job_id, run_date, attempt_no, claim_token, worker_id, "
                "claimed_at, lease_expires_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    context.request["job_id"],
                    run_date,
                    attempt_no,
                    claim_token,
                    worker_id,
                    now_text,
                    lease_text,
                ),
            )
            update_job_status(connection, context, now_text=now_text)
            receipt_status = "CLAIMED"
            receipt = base_receipt(
                context,
                event_id=event_id,
                operation="claim",
                event_time=now_text,
                status=receipt_status,
                run_date=run_date,
            )
            receipt.update(
                {
                    "attempt_no": attempt_no,
                    "claim_token": claim_token,
                    "worker_id": worker_id,
                    "lease_expires_at": lease_text,
                }
            )
        else:
            raise OrchestrationError("unsupported schedule state")
        return record_event(
            connection,
            event_id=event_id,
            operation="claim",
            command_hash=digest,
            event_time=now_text,
            run_date=run_date,
            status=receipt_status,
            receipt=receipt,
        )


def require_running_claim(
    connection: sqlite3.Connection,
    context: Context,
    *,
    run_date: str,
    claim_token: str,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM schedule_dates WHERE job_id = ? AND run_date = ?",
        (context.request["job_id"], run_date),
    ).fetchone()
    if row is None:
        raise OrchestrationError("run_date is not in the frozen calendar")
    if row["status"] != "RUNNING":
        raise OrchestrationError(
            "run_date does not have an active claim",
            code="CLAIM_NOT_ACTIVE",
        )
    if not secrets.compare_digest(row["active_claim_token"], claim_token):
        raise OrchestrationError(
            "claim token mismatch",
            code="CLAIM_TOKEN_MISMATCH",
        )
    return row


def complete(
    context: Context,
    *,
    event_id: str,
    run_date: date,
    claim_token: str,
    result_sha256: str,
    outcome: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    safe_component(event_id, "event_id")
    if not SHA256_PATTERN.fullmatch(result_sha256):
        raise OrchestrationError("result_sha256 must be lowercase SHA-256")
    if outcome not in {"SUCCEEDED", "NO_CHANGE"}:
        raise OrchestrationError("outcome must be SUCCEEDED or NO_CHANGE")
    now = now or utc_now()
    now_text = normalize_timestamp(now)
    run_text = run_date.isoformat()
    arguments = {
        "run_date": run_text,
        "claim_token": claim_token,
        "result_sha256": result_sha256,
        "outcome": outcome,
    }
    digest = command_sha256("complete", context, arguments)
    with writer_transaction(context) as connection:
        replay = replay_event(
            connection,
            event_id=event_id,
            operation="complete",
            command_hash=digest,
        )
        if replay is not None:
            return replay
        row = require_running_claim(
            connection,
            context,
            run_date=run_text,
            claim_token=claim_token,
        )
        connection.execute(
            "UPDATE schedule_dates SET status = ?, active_claim_token = NULL, "
            "claimed_at = NULL, lease_expires_at = NULL, result_sha256 = ?, "
            "last_error_code = NULL WHERE job_id = ? AND run_date = ?",
            (
                outcome,
                result_sha256,
                context.request["job_id"],
                run_text,
            ),
        )
        connection.execute(
            "UPDATE attempts SET finished_at = ?, outcome = ?, result_sha256 = ? "
            "WHERE job_id = ? AND run_date = ? AND attempt_no = ?",
            (
                now_text,
                outcome,
                result_sha256,
                context.request["job_id"],
                run_text,
                row["attempt_count"],
            ),
        )
        job_status, watermark = update_job_status(
            connection, context, now_text=now_text
        )
        receipt = base_receipt(
            context,
            event_id=event_id,
            operation="complete",
            event_time=now_text,
            status=outcome,
            run_date=run_text,
        )
        receipt.update(
            {
                "result_sha256": result_sha256,
                "attempt_no": row["attempt_count"],
                "watermark_date": watermark,
                "job_status": job_status,
            }
        )
        return record_event(
            connection,
            event_id=event_id,
            operation="complete",
            command_hash=digest,
            event_time=now_text,
            run_date=run_text,
            status=outcome,
            receipt=receipt,
        )


def fail(
    context: Context,
    *,
    event_id: str,
    run_date: date,
    claim_token: str,
    error_code: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    safe_component(event_id, "event_id")
    if not ERROR_CODE.fullmatch(error_code):
        raise OrchestrationError("error_code must be a stable uppercase code")
    now = now or utc_now()
    now_text = normalize_timestamp(now)
    run_text = run_date.isoformat()
    arguments = {
        "run_date": run_text,
        "claim_token": claim_token,
        "error_code": error_code,
    }
    digest = command_sha256("fail", context, arguments)
    with writer_transaction(context) as connection:
        replay = replay_event(
            connection,
            event_id=event_id,
            operation="fail",
            command_hash=digest,
        )
        if replay is not None:
            return replay
        row = require_running_claim(
            connection,
            context,
            run_date=run_text,
            claim_token=claim_token,
        )
        next_status = (
            "FAILED_TERMINAL"
            if row["attempt_count"] >= context.request["max_attempts"]
            else "RETRY_PENDING"
        )
        connection.execute(
            "UPDATE schedule_dates SET status = ?, active_claim_token = NULL, "
            "claimed_at = NULL, lease_expires_at = NULL, last_error_code = ? "
            "WHERE job_id = ? AND run_date = ?",
            (
                next_status,
                error_code,
                context.request["job_id"],
                run_text,
            ),
        )
        connection.execute(
            "UPDATE attempts SET finished_at = ?, outcome = 'FAILED', "
            "error_code = ? WHERE job_id = ? AND run_date = ? AND attempt_no = ?",
            (
                now_text,
                error_code,
                context.request["job_id"],
                run_text,
                row["attempt_count"],
            ),
        )
        job_status, watermark = update_job_status(
            connection, context, now_text=now_text
        )
        receipt = base_receipt(
            context,
            event_id=event_id,
            operation="fail",
            event_time=now_text,
            status=next_status,
            run_date=run_text,
        )
        receipt.update(
            {
                "error_code": error_code,
                "attempt_no": row["attempt_count"],
                "watermark_date": watermark,
                "job_status": job_status,
            }
        )
        return record_event(
            connection,
            event_id=event_id,
            operation="fail",
            command_hash=digest,
            event_time=now_text,
            run_date=run_text,
            status=next_status,
            receipt=receipt,
        )


def recover(
    context: Context,
    *,
    event_id: str,
    run_date: date,
    claim_token: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    safe_component(event_id, "event_id")
    now = now or utc_now()
    now_text = normalize_timestamp(now)
    run_text = run_date.isoformat()
    arguments = {
        "run_date": run_text,
        "claim_token": claim_token,
    }
    digest = command_sha256("recover", context, arguments)
    with writer_transaction(context) as connection:
        replay = replay_event(
            connection,
            event_id=event_id,
            operation="recover",
            command_hash=digest,
        )
        if replay is not None:
            return replay
        row = require_running_claim(
            connection,
            context,
            run_date=run_text,
            claim_token=claim_token,
        )
        expiry = parse_timestamp(row["lease_expires_at"], "lease_expires_at")
        if expiry > now:
            raise OrchestrationError(
                "active lease has not expired",
                code="LEASE_NOT_EXPIRED",
            )
        next_status = (
            "FAILED_TERMINAL"
            if row["attempt_count"] >= context.request["max_attempts"]
            else "RETRY_PENDING"
        )
        connection.execute(
            "UPDATE schedule_dates SET status = ?, active_claim_token = NULL, "
            "claimed_at = NULL, lease_expires_at = NULL, "
            "last_error_code = 'LEASE_EXPIRED' "
            "WHERE job_id = ? AND run_date = ?",
            (next_status, context.request["job_id"], run_text),
        )
        connection.execute(
            "UPDATE attempts SET finished_at = ?, outcome = 'LEASE_EXPIRED', "
            "error_code = 'LEASE_EXPIRED' "
            "WHERE job_id = ? AND run_date = ? AND attempt_no = ?",
            (
                now_text,
                context.request["job_id"],
                run_text,
                row["attempt_count"],
            ),
        )
        job_status, watermark = update_job_status(
            connection, context, now_text=now_text
        )
        receipt = base_receipt(
            context,
            event_id=event_id,
            operation="recover",
            event_time=now_text,
            status=next_status,
            run_date=run_text,
        )
        receipt.update(
            {
                "recovered_attempt_no": row["attempt_count"],
                "watermark_date": watermark,
                "job_status": job_status,
                "recovery_reason": "LEASE_EXPIRED",
            }
        )
        return record_event(
            connection,
            event_id=event_id,
            operation="recover",
            command_hash=digest,
            event_time=now_text,
            run_date=run_text,
            status=next_status,
            receipt=receipt,
        )


def context_from_args(args: argparse.Namespace) -> Context:
    return validate_workspace(args.workspace_root, args.request)


def add_common(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--workspace-root", type=Path, default=ROOT)
    subparser.add_argument("--request", type=Path, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Persist inspection-only local daily orchestration state. "
            "No network, worker execution, scheduler or formal publication."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    initialize_parser = subparsers.add_parser("init")
    add_common(initialize_parser)
    initialize_parser.add_argument("--now")
    status_parser = subparsers.add_parser("status")
    add_common(status_parser)
    plan_parser = subparsers.add_parser("plan")
    add_common(plan_parser)
    plan_parser.add_argument("--through-date", required=True)
    claim_parser = subparsers.add_parser("claim")
    add_common(claim_parser)
    claim_parser.add_argument("--event-id", required=True)
    claim_parser.add_argument("--worker-id", required=True)
    claim_parser.add_argument("--through-date", required=True)
    claim_parser.add_argument("--now")
    complete_parser = subparsers.add_parser("complete")
    add_common(complete_parser)
    complete_parser.add_argument("--event-id", required=True)
    complete_parser.add_argument("--run-date", required=True)
    complete_parser.add_argument("--claim-token", required=True)
    complete_parser.add_argument("--result-sha256", required=True)
    complete_parser.add_argument(
        "--outcome", choices=["SUCCEEDED", "NO_CHANGE"], required=True
    )
    complete_parser.add_argument("--now")
    fail_parser = subparsers.add_parser("fail")
    add_common(fail_parser)
    fail_parser.add_argument("--event-id", required=True)
    fail_parser.add_argument("--run-date", required=True)
    fail_parser.add_argument("--claim-token", required=True)
    fail_parser.add_argument("--error-code", required=True)
    fail_parser.add_argument("--now")
    recover_parser = subparsers.add_parser("recover")
    add_common(recover_parser)
    recover_parser.add_argument("--event-id", required=True)
    recover_parser.add_argument("--run-date", required=True)
    recover_parser.add_argument("--claim-token", required=True)
    recover_parser.add_argument("--now")
    return parser


def optional_now(value: str | None) -> datetime | None:
    return parse_timestamp(value, "now") if value is not None else None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        context = context_from_args(args)
        if args.command == "init":
            result = initialize(context, now=optional_now(args.now))
        elif args.command == "status":
            result = status(context)
        elif args.command == "plan":
            result = plan(
                context,
                through_date=parse_date(args.through_date, "through_date"),
            )
        elif args.command == "claim":
            result = claim(
                context,
                event_id=args.event_id,
                worker_id=args.worker_id,
                through_date=parse_date(args.through_date, "through_date"),
                now=optional_now(args.now),
            )
        elif args.command == "complete":
            result = complete(
                context,
                event_id=args.event_id,
                run_date=parse_date(args.run_date, "run_date"),
                claim_token=args.claim_token,
                result_sha256=args.result_sha256,
                outcome=args.outcome,
                now=optional_now(args.now),
            )
        elif args.command == "fail":
            result = fail(
                context,
                event_id=args.event_id,
                run_date=parse_date(args.run_date, "run_date"),
                claim_token=args.claim_token,
                error_code=args.error_code,
                now=optional_now(args.now),
            )
        elif args.command == "recover":
            result = recover(
                context,
                event_id=args.event_id,
                run_date=parse_date(args.run_date, "run_date"),
                claim_token=args.claim_token,
                now=optional_now(args.now),
            )
        else:
            raise AssertionError(args.command)
        print(canonical_json_bytes(result).decode("utf-8"), end="")
        return 0
    except OrchestrationError as exc:
        print(
            canonical_json_bytes(
                {
                    "status": "REJECTED",
                    "error_code": exc.code,
                    "message": str(exc),
                }
            ).decode("utf-8"),
            end="",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
