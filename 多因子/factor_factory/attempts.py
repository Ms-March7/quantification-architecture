"""Project-local, tamper-evident registry for validation-consuming attempts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ATTEMPT_STATES = {
    "PROTOCOL_REGISTERED",
    "VALIDATION_CONSUMED",
    "ABORTED_TECHNICAL",
    "NO_FACTOR",
    "FROZEN_AWAITING_HOLDOUT",
    "HOLDOUT_OPENED",
    "RESULT_COMMITTED",
}

ALLOWED_TRANSITIONS = {
    "PROTOCOL_REGISTERED": {"VALIDATION_CONSUMED"},
    "VALIDATION_CONSUMED": {"ABORTED_TECHNICAL", "NO_FACTOR", "FROZEN_AWAITING_HOLDOUT"},
    "ABORTED_TECHNICAL": set(),
    "FROZEN_AWAITING_HOLDOUT": {"HOLDOUT_OPENED"},
    "HOLDOUT_OPENED": {"RESULT_COMMITTED"},
    "NO_FACTOR": set(),
    "RESULT_COMMITTED": set(),
}


def canonical_attempt_path() -> Path:
    return Path(__file__).resolve().parents[1] / ".state" / "factor-factory-attempts.sqlite3"


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class AttemptRecord:
    protocol_id: str
    protocol_hash: str
    state: str
    recovered: bool
    payload: dict[str, Any] | None = None
    state_evidence: dict[str, Any] | None = None


class ResearchAttemptGovernance:
    """Append-audited state machine preventing invisible validation retries.

    The SQLite rows, audit chain, and an external anchor must agree before every
    read or mutation.  A small fsynced pending journal makes the only accepted
    anchor mismatch the narrow crash window between SQLite commit and anchor
    replacement.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else canonical_attempt_path()
        self.anchor_path = self.path.with_suffix(self.path.suffix + ".anchor")
        self.anchor_pending_path = self.path.with_suffix(self.path.suffix + ".anchor.pending")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS attempts(
                    protocol_id TEXT PRIMARY KEY,
                    protocol_hash TEXT NOT NULL UNIQUE,
                    validation_start TEXT NOT NULL,
                    validation_end TEXT NOT NULL,
                    holdout_start TEXT NOT NULL,
                    holdout_end TEXT NOT NULL,
                    run_directory TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'PROTOCOL_REGISTERED','VALIDATION_CONSUMED','NO_FACTOR',
                        'ABORTED_TECHNICAL','FROZEN_AWAITING_HOLDOUT',
                        'HOLDOUT_OPENED','RESULT_COMMITTED'
                    )),
                    state_evidence_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS attempt_interval_lookup
                    ON attempts(validation_start, holdout_end);
                CREATE TABLE IF NOT EXISTS attempt_audit(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    previous_hash TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE
                );
                """
            )
            schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='attempts'"
            ).fetchone()
            if schema is not None and "ABORTED_TECHNICAL" not in str(schema[0]):
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    ALTER TABLE attempts RENAME TO attempts_legacy;
                    DROP INDEX IF EXISTS attempt_interval_lookup;
                    CREATE TABLE attempts(
                        protocol_id TEXT PRIMARY KEY,
                        protocol_hash TEXT NOT NULL UNIQUE,
                        validation_start TEXT NOT NULL,
                        validation_end TEXT NOT NULL,
                        holdout_start TEXT NOT NULL,
                        holdout_end TEXT NOT NULL,
                        run_directory TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN (
                            'PROTOCOL_REGISTERED','VALIDATION_CONSUMED','NO_FACTOR',
                            'ABORTED_TECHNICAL','FROZEN_AWAITING_HOLDOUT',
                            'HOLDOUT_OPENED','RESULT_COMMITTED'
                        )),
                        state_evidence_json TEXT NOT NULL,
                        created_at_utc TEXT NOT NULL,
                        updated_at_utc TEXT NOT NULL
                    );
                    INSERT INTO attempts SELECT * FROM attempts_legacy;
                    DROP TABLE attempts_legacy;
                    CREATE INDEX attempt_interval_lookup
                        ON attempts(validation_start, holdout_end);
                    COMMIT;
                    """
                )
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @staticmethod
    def _validated_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(payload)
        required = (
            "protocol_id", "validation_start", "validation_end",
            "holdout_start", "holdout_end", "run_directory",
        )
        missing = [name for name in required if not value.get(name)]
        if missing:
            raise ValueError(f"incomplete research attempt protocol: {missing}")
        validation_start = date.fromisoformat(str(value["validation_start"]))
        validation_end = date.fromisoformat(str(value["validation_end"]))
        holdout_start = date.fromisoformat(str(value["holdout_start"]))
        holdout_end = date.fromisoformat(str(value["holdout_end"]))
        if not validation_start <= validation_end < holdout_start <= holdout_end:
            raise ValueError("attempt dates must be ordered validation then holdout")
        value["protocol_id"] = str(value["protocol_id"])
        value["run_directory"] = str(Path(value["run_directory"]).resolve())
        return value

    @staticmethod
    def _validated_evidence(evidence: Mapping[str, Any] | None) -> dict[str, Any]:
        if evidence is None:
            return {}
        value = dict(evidence)
        _canonical(value)
        return value

    @staticmethod
    def _decode_object(payload: str, label: str) -> dict[str, Any]:
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"research attempt {label} is invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"research attempt {label} is not an object")
        return value

    @staticmethod
    def _table_digest(connection: sqlite3.Connection) -> str:
        rows = connection.execute("SELECT * FROM attempts ORDER BY protocol_id").fetchall()
        return hashlib.sha256(
            _canonical({"rows": [dict(row) for row in rows]}).encode("utf-8")
        ).hexdigest()

    def _anchor_value(self) -> str:
        if not self.anchor_path.exists():
            return "0" * 64
        return self.anchor_path.read_text(encoding="ascii").strip()

    def _write_anchor(self, event_hash: str) -> None:
        temporary = self.anchor_path.with_suffix(self.anchor_path.suffix + ".tmp")
        with temporary.open("w", encoding="ascii") as handle:
            handle.write(event_hash + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.anchor_path)

    def _reconcile_anchor(self, database_event_hash: str) -> None:
        anchor = self._anchor_value()
        if self.anchor_pending_path.exists():
            try:
                pending = json.loads(self.anchor_pending_path.read_text(encoding="utf-8"))
                previous = str(pending["previous_hash"])
                next_hash = str(pending["next_hash"])
            except (OSError, KeyError, json.JSONDecodeError) as exc:
                raise RuntimeError("research attempt anchor journal is invalid") from exc
            if anchor == next_hash and database_event_hash == next_hash:
                self.anchor_pending_path.unlink(missing_ok=True)
            elif anchor != previous:
                raise RuntimeError("research attempt anchor journal does not own current anchor")
            elif database_event_hash == next_hash:
                self._write_anchor(next_hash)
                self.anchor_pending_path.unlink(missing_ok=True)
                anchor = next_hash
            elif database_event_hash == previous:
                self.anchor_pending_path.unlink(missing_ok=True)
            else:
                raise RuntimeError("research attempt anchor journal cannot reconcile database")
        if database_event_hash == "0" * 64:
            if self.anchor_path.exists():
                raise RuntimeError("research attempt anchor exists without audit history")
        elif anchor != database_event_hash:
            raise RuntimeError("research attempt external anchor mismatch")

    def _verify_audit_chain(self, connection: sqlite3.Connection) -> str:
        previous = "0" * 64
        last_event: dict[str, Any] | None = None
        rows = connection.execute(
            "SELECT previous_hash,event_json,event_hash FROM attempt_audit ORDER BY sequence"
        ).fetchall()
        for row in rows:
            expected = hashlib.sha256((previous + row["event_json"]).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous or row["event_hash"] != expected:
                raise RuntimeError("research attempt audit chain is invalid")
            previous = expected
            last_event = self._decode_object(row["event_json"], "audit event")
        table_digest = self._table_digest(connection)
        self._reconcile_anchor(previous)
        if last_event is None:
            count = int(connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])
            if count or self.anchor_path.exists():
                raise RuntimeError("research attempt state has no matching audit history")
        else:
            if last_event.get("table_digest") != table_digest:
                raise RuntimeError("research attempt table differs from its audit checkpoint")
            if not self.anchor_path.is_file():
                raise RuntimeError("research attempt external anchor is missing")
            if self.anchor_path.read_text(encoding="ascii").strip() != previous:
                raise RuntimeError("research attempt external anchor mismatch")
        return previous

    def _prepare_anchor(self, event_hash: str) -> None:
        payload = {
            "schema_version": 1,
            "previous_hash": self._anchor_value(),
            "next_hash": event_hash,
        }
        temporary = self.anchor_pending_path.with_suffix(self.anchor_pending_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.anchor_pending_path)

    def _commit_and_finalize_anchor(
        self, connection: sqlite3.Connection, event_hash: str
    ) -> None:
        self._prepare_anchor(event_hash)
        connection.commit()
        self._write_anchor(event_hash)
        self.anchor_pending_path.unlink(missing_ok=True)

    def _append_audit(
        self, connection: sqlite3.Connection, event: Mapping[str, Any]
    ) -> str:
        row = connection.execute(
            "SELECT event_hash FROM attempt_audit ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous = str(row["event_hash"]) if row is not None else "0" * 64
        event_json = _canonical({**event, "table_digest": self._table_digest(connection)})
        event_hash = hashlib.sha256((previous + event_json).encode("utf-8")).hexdigest()
        connection.execute(
            "INSERT INTO attempt_audit(previous_hash,event_json,event_hash) VALUES(?,?,?)",
            (previous, event_json, event_hash),
        )
        return event_hash

    def _record(self, row: sqlite3.Row, recovered: bool) -> AttemptRecord:
        payload = self._decode_object(str(row["payload_json"]), "payload")
        canonical_payload = _canonical(payload)
        expected_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        if canonical_payload != row["payload_json"] or expected_hash != row["protocol_hash"]:
            raise RuntimeError("research attempt payload/hash mismatch")
        if payload.get("protocol_id") != row["protocol_id"]:
            raise RuntimeError("research attempt payload protocol_id mismatch")
        if str(Path(str(payload.get("run_directory"))).resolve()) != row["run_directory"]:
            raise RuntimeError("research attempt payload run_directory mismatch")
        evidence = self._decode_object(str(row["state_evidence_json"]), "state evidence")
        if row["state"] not in ATTEMPT_STATES:
            raise RuntimeError(f"invalid research attempt state: {row['state']}")
        return AttemptRecord(
            str(row["protocol_id"]), str(row["protocol_hash"]), str(row["state"]),
            recovered, payload, evidence,
        )

    def register(self, payload: Mapping[str, Any]) -> AttemptRecord:
        value = self._validated_payload(payload)
        payload_json = _canonical(value)
        protocol_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        evidence = {"PROTOCOL_REGISTERED": {"protocol_hash": protocol_hash}}
        evidence_json = _canonical(evidence)
        now = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_audit_chain(connection)
            existing = connection.execute(
                "SELECT * FROM attempts WHERE protocol_id=?", (value["protocol_id"],)
            ).fetchone()
            if existing is not None:
                record = self._record(existing, True)
                if (
                    record.protocol_hash != protocol_hash
                    or existing["payload_json"] != payload_json
                    or existing["run_directory"] != value["run_directory"]
                ):
                    raise RuntimeError("protocol_id already belongs to a different immutable attempt")
                connection.commit()
                return record
            overlaps = connection.execute(
                """
                SELECT * FROM attempts
                WHERE validation_start <= ? AND holdout_end >= ?
                ORDER BY created_at_utc
                """,
                (str(value["holdout_end"]), str(value["validation_start"])),
            ).fetchall()
            if overlaps:
                terminal_predecessors = {"NO_FACTOR", "ABORTED_TECHNICAL"}
                active = [row for row in overlaps if row["state"] not in terminal_predecessors]
                if active:
                    overlap = active[0]
                    raise RuntimeError(
                        "research validation/holdout interval overlaps governed protocol "
                        f"{overlap['protocol_id']} in state {overlap['state']}"
                    )
                lineage = value.get("lineage")
                lineage_sha = str(value.get("lineage_manifest_sha256") or "")
                if not isinstance(lineage, dict) or lineage.get("manifest_sha256") != lineage_sha:
                    raise RuntimeError(
                        "overlapping NO_FACTOR protocol requires exact failed-run lineage"
                    )
                lineage_directory = Path(str(lineage.get("run_directory", ""))).resolve()
                predecessors = [
                    row for row in overlaps
                    if Path(str(row["run_directory"])).resolve() == lineage_directory
                ]
                if len(predecessors) != 1:
                    raise RuntimeError("failed-run lineage does not identify one governed predecessor")
                predecessor = predecessors[0]
                predecessor_payload = self._decode_object(
                    str(predecessor["payload_json"]), "predecessor payload"
                )
                try:
                    version_advanced = int(value.get("protocol_version", 0)) > int(
                        predecessor_payload.get("protocol_version", 0)
                    )
                except (TypeError, ValueError):
                    version_advanced = False
                if not version_advanced:
                    raise RuntimeError("successor protocol_version must advance after NO_FACTOR")
                if predecessor["state"] == "NO_FACTOR":
                    lineage_path = lineage_directory / "manifest.json"
                    commit_path = lineage_directory / "COMMITTED.json"
                    if not lineage_path.is_file() or not commit_path.is_file():
                        raise RuntimeError("failed-run lineage is not a committed research directory")
                    try:
                        lineage_record = json.loads(lineage_path.read_text(encoding="utf-8"))
                        commit = json.loads(commit_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        raise RuntimeError("failed-run lineage artifacts are invalid") from exc
                    ensemble = lineage_record.get("ensemble", {})
                    manifest_attempt = lineage_record.get("research_attempt", {})
                    valid_terminal = (
                        commit.get("manifest_sha256") == lineage_sha
                        and lineage_record.get("attempt_terminal_state") == "NO_FACTOR"
                        and not bool(ensemble.get("qualified"))
                        and not bool(ensemble.get("holdout_was_opened"))
                        and manifest_attempt.get("protocol_id") == predecessor["protocol_id"]
                    )
                else:
                    lineage_path = lineage_directory / "ABORTED.json"
                    if not lineage_path.is_file():
                        raise RuntimeError("technical-abort lineage artifact is missing")
                    try:
                        lineage_record = json.loads(lineage_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        raise RuntimeError("technical-abort lineage artifact is invalid") from exc
                    abort_evidence = self._decode_object(
                        str(predecessor["state_evidence_json"]), "predecessor state evidence"
                    ).get("ABORTED_TECHNICAL", {})
                    valid_terminal = (
                        lineage_record.get("state") == "ABORTED_TECHNICAL"
                        and lineage_record.get("protocol_id") == predecessor["protocol_id"]
                        and lineage_record.get("protocol_hash") == predecessor["protocol_hash"]
                        and lineage_record.get("holdout_was_opened") is False
                        and lineage_record.get("result_committed") is False
                        and abort_evidence.get("abort_artifact_sha256") == lineage_sha
                    )
                actual_manifest_sha = _file_sha256(lineage_path)
                if actual_manifest_sha != lineage_sha:
                    raise RuntimeError("failed-run lineage artifact hash differs from disk")
                if (
                    not valid_terminal
                    or bool(lineage.get("qualified"))
                    or bool(lineage.get("holdout_was_opened"))
                ):
                    raise RuntimeError("failed-run lineage is not a sealed terminal predecessor")
                evidence["PROTOCOL_REGISTERED"]["supersedes_terminal_attempt"] = {
                    "protocol_id": str(predecessor["protocol_id"]),
                    "state": str(predecessor["state"]),
                    "manifest_sha256": lineage_sha,
                }
                evidence_json = _canonical(evidence)
            connection.execute(
                """
                INSERT INTO attempts(
                    protocol_id,protocol_hash,validation_start,validation_end,
                    holdout_start,holdout_end,run_directory,payload_json,state,
                    state_evidence_json,created_at_utc,updated_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    value["protocol_id"], protocol_hash,
                    value["validation_start"], value["validation_end"],
                    value["holdout_start"], value["holdout_end"],
                    value["run_directory"], payload_json, "PROTOCOL_REGISTERED",
                    evidence_json, now, now,
                ),
            )
            event_hash = self._append_audit(connection, {
                "event": "PROTOCOL_REGISTERED",
                "protocol_id": value["protocol_id"],
                "protocol_hash": protocol_hash,
                "evidence_sha256": hashlib.sha256(evidence_json.encode("utf-8")).hexdigest(),
                "timestamp": now,
            })
            self._commit_and_finalize_anchor(connection, event_hash)
            return AttemptRecord(
                value["protocol_id"], protocol_hash, "PROTOCOL_REGISTERED", False,
                value, evidence,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def transition(
        self,
        protocol_id: str,
        protocol_hash: str,
        target_state: str,
        evidence: Mapping[str, Any] | None = None,
    ) -> AttemptRecord:
        if target_state not in ATTEMPT_STATES:
            raise ValueError(f"invalid attempt state: {target_state}")
        target_evidence = self._validated_evidence(evidence)
        connection = self._connect()
        now = datetime.now(timezone.utc).isoformat()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_audit_chain(connection)
            row = connection.execute(
                "SELECT * FROM attempts WHERE protocol_id=?", (protocol_id,)
            ).fetchone()
            if row is None or row["protocol_hash"] != protocol_hash:
                raise RuntimeError("unknown or mismatched research attempt protocol")
            current = self._record(row, True)
            evidence_ledger = dict(current.state_evidence or {})
            if row["state"] == target_state:
                if evidence_ledger.get(target_state) != target_evidence:
                    raise RuntimeError("idempotent attempt transition has different evidence")
                connection.commit()
                return current
            if target_state not in ALLOWED_TRANSITIONS.get(str(row["state"]), set()):
                raise RuntimeError(f"invalid attempt transition {row['state']} -> {target_state}")
            evidence_ledger[target_state] = target_evidence
            evidence_json = _canonical(evidence_ledger)
            connection.execute(
                """
                UPDATE attempts SET state=?,state_evidence_json=?,updated_at_utc=?
                WHERE protocol_id=?
                """,
                (target_state, evidence_json, now, protocol_id),
            )
            event_hash = self._append_audit(connection, {
                "event": target_state,
                "protocol_id": protocol_id,
                "protocol_hash": protocol_hash,
                "evidence_sha256": hashlib.sha256(
                    _canonical(target_evidence).encode("utf-8")
                ).hexdigest(),
                "timestamp": now,
            })
            self._commit_and_finalize_anchor(connection, event_hash)
            return AttemptRecord(
                protocol_id, protocol_hash, target_state, False,
                current.payload, evidence_ledger,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def require_state(
        self, protocol_id: str, protocol_hash: str, allowed_states: set[str]
    ) -> AttemptRecord:
        invalid = set(allowed_states) - ATTEMPT_STATES
        if invalid:
            raise ValueError(f"invalid allowed attempt states: {sorted(invalid)}")
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            self._verify_audit_chain(connection)
            row = connection.execute(
                "SELECT * FROM attempts WHERE protocol_id=?", (protocol_id,)
            ).fetchone()
            if row is None or row["protocol_hash"] != protocol_hash:
                raise RuntimeError("research attempt is not registered with this protocol hash")
            record = self._record(row, True)
            if record.state not in allowed_states:
                raise RuntimeError(
                    f"research attempt state is {record.state}, expected {allowed_states}"
                )
            connection.commit()
            return record
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
