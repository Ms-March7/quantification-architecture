"""Machine-wide, crash-recoverable governance for one-shot holdout periods."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping


HOLDOUT_STATES = {"RESERVED", "OPENED", "RESULT_COMMITTED"}


def canonical_governance_path() -> Path:
    """Return the project-local governance database path."""

    return Path(__file__).resolve().parents[1] / ".state" / "factor-factory-governance.sqlite3"


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class HoldoutReservation:
    run_id: str
    status: str
    recovered: bool
    result: dict[str, Any] | None = None


class HoldoutGovernance:
    """SQLite state machine serialized with ``BEGIN IMMEDIATE``.

    Production callers use the fixed project-local path.  Tests may inject an
    isolated database through ``FactorFactory(governance_path=...)``; the CLI
    deliberately exposes no path override.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else canonical_governance_path()
        self.anchor_path = self.path.with_suffix(self.path.suffix + ".anchor")
        self.anchor_pending_path = self.path.with_suffix(self.path.suffix + ".anchor.pending")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS holdout_runs (
                    run_id TEXT PRIMARY KEY,
                    holdout_key TEXT NOT NULL,
                    protocol_id TEXT NOT NULL,
                    protocol_hash TEXT NOT NULL,
                    market TEXT NOT NULL,
                    holdout_start TEXT NOT NULL,
                    holdout_end TEXT NOT NULL,
                    horizon INTEGER NOT NULL,
                    freeze_hash TEXT NOT NULL,
                    training_fingerprint TEXT NOT NULL,
                    family_hash TEXT NOT NULL,
                    snapshot_fingerprint TEXT NOT NULL,
                    engine_fingerprint TEXT NOT NULL,
                    run_directory TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('RESERVED','OPENED','RESULT_COMMITTED')),
                    reserved_at_utc TEXT NOT NULL,
                    opened_at_utc TEXT,
                    committed_at_utc TEXT,
                    result_json TEXT,
                    result_sha256 TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS holdout_key_unique
                    ON holdout_runs(holdout_key);
                CREATE INDEX IF NOT EXISTS holdout_interval_lookup
                    ON holdout_runs(market, holdout_start, holdout_end);
                CREATE TABLE IF NOT EXISTS governance_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    previous_hash TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE
                );
                """
            )
        finally:
            connection.close()

    @staticmethod
    def _validated_record(record: Mapping[str, Any]) -> dict[str, Any]:
        required = (
            "run_id", "holdout_key", "protocol_id", "protocol_hash", "market",
            "holdout_start", "holdout_end",
            "horizon", "freeze_hash", "training_fingerprint", "family_hash",
            "snapshot_fingerprint", "engine_fingerprint", "run_directory",
        )
        missing = [name for name in required if not record.get(name)]
        if missing:
            raise ValueError(f"incomplete holdout reservation: {missing}")
        start = date.fromisoformat(str(record["holdout_start"]))
        end = date.fromisoformat(str(record["holdout_end"]))
        if start > end:
            raise ValueError("holdout_start must not be after holdout_end")
        horizon = int(record["horizon"])
        if horizon < 1:
            raise ValueError("holdout horizon must be positive")
        result = {name: str(record[name]) for name in required if name != "horizon"}
        result["horizon"] = horizon
        result["run_directory"] = str(Path(result["run_directory"]).resolve())
        return result

    @staticmethod
    def _table_digest(connection: sqlite3.Connection) -> str:
        rows = connection.execute(
            "SELECT * FROM holdout_runs ORDER BY run_id"
        ).fetchall()
        payload = [dict(row) for row in rows]
        return hashlib.sha256(canonical_json({"rows": payload}).encode("utf-8")).hexdigest()

    def _verify_audit_chain(self, connection: sqlite3.Connection) -> str:
        previous = "0" * 64
        rows = connection.execute(
            "SELECT previous_hash,event_json,event_hash FROM governance_audit ORDER BY sequence"
        ).fetchall()
        last_event: dict[str, Any] | None = None
        for row in rows:
            expected = hashlib.sha256((previous + row["event_json"]).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous or row["event_hash"] != expected:
                raise RuntimeError("holdout governance audit chain is invalid")
            previous = expected
            last_event = json.loads(row["event_json"])
        current_table_digest = self._table_digest(connection)
        self._reconcile_anchor(previous)
        if last_event is None:
            count = int(connection.execute("SELECT COUNT(*) FROM holdout_runs").fetchone()[0])
            if count or self.anchor_path.exists():
                raise RuntimeError("holdout governance state has no matching audit history")
        else:
            if last_event.get("table_digest") != current_table_digest:
                raise RuntimeError("holdout governance table differs from its audit checkpoint")
            if not self.anchor_path.is_file():
                raise RuntimeError("holdout governance external anchor is missing")
            if self.anchor_path.read_text(encoding="ascii").strip() != previous:
                raise RuntimeError("holdout governance external anchor mismatch")
        return previous

    def _anchor_value(self) -> str:
        if not self.anchor_path.exists():
            return "0" * 64
        return self.anchor_path.read_text(encoding="ascii").strip()

    def _reconcile_anchor(self, database_event_hash: str) -> None:
        """Resolve only a journaled crash window around SQLite commit.

        The pending record is written and fsynced before the database commit.
        It therefore distinguishes a committed-database/stale-anchor crash from
        arbitrary anchor drift, which remains fail-closed.
        """

        anchor = self._anchor_value()
        if self.anchor_pending_path.exists():
            try:
                pending = json.loads(self.anchor_pending_path.read_text(encoding="utf-8"))
                previous = str(pending["previous_hash"])
                next_hash = str(pending["next_hash"])
            except (OSError, KeyError, json.JSONDecodeError) as exc:
                raise RuntimeError("holdout governance anchor journal is invalid") from exc
            if anchor == next_hash and database_event_hash == next_hash:
                self.anchor_pending_path.unlink(missing_ok=True)
            elif anchor != previous:
                raise RuntimeError("holdout governance anchor journal does not own current anchor")
            elif database_event_hash == next_hash:
                self._write_anchor(next_hash)
                self.anchor_pending_path.unlink(missing_ok=True)
                anchor = next_hash
            elif database_event_hash == previous:
                self.anchor_pending_path.unlink(missing_ok=True)
            else:
                raise RuntimeError("holdout governance anchor journal cannot reconcile database")
        if database_event_hash == "0" * 64:
            if self.anchor_path.exists():
                raise RuntimeError("holdout governance anchor exists without audit history")
        elif anchor != database_event_hash:
            raise RuntimeError("holdout governance external anchor mismatch")

    def _prepare_anchor(self, event_hash: str) -> None:
        payload = {
            "schema_version": 1,
            "previous_hash": self._anchor_value(),
            "next_hash": event_hash,
        }
        temporary = self.anchor_pending_path.with_suffix(
            self.anchor_pending_path.suffix + ".tmp"
        )
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

    def _write_anchor(self, event_hash: str) -> None:
        temporary = self.anchor_path.with_suffix(self.anchor_path.suffix + ".tmp")
        with temporary.open("w", encoding="ascii") as handle:
            handle.write(event_hash + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.anchor_path)

    def _append_audit(
        self, connection: sqlite3.Connection, event: Mapping[str, Any]
    ) -> str:
        row = connection.execute(
            "SELECT event_hash FROM governance_audit ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous = str(row["event_hash"]) if row is not None else "0" * 64
        payload = canonical_json({**event, "table_digest": self._table_digest(connection)})
        event_hash = hashlib.sha256((previous + payload).encode("utf-8")).hexdigest()
        connection.execute(
            "INSERT INTO governance_audit(previous_hash,event_json,event_hash) VALUES(?,?,?)",
            (previous, payload, event_hash),
        )
        return event_hash

    def reserve(self, record: Mapping[str, Any]) -> HoldoutReservation:
        value = self._validated_record(record)
        now = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_audit_chain(connection)
            overlaps = connection.execute(
                """
                SELECT * FROM holdout_runs
                WHERE market = ? AND holdout_start <= ? AND holdout_end >= ?
                ORDER BY reserved_at_utc LIMIT 1
                """,
                (value["market"], value["holdout_end"], value["holdout_start"]),
            ).fetchone()
            if overlaps is not None:
                same_run = all(overlaps[name] == value[name] for name in value)
                if not same_run:
                    raise RuntimeError(
                        "overlapping A-share holdout is already governed by "
                        f"run {overlaps['run_id']} with state {overlaps['status']}"
                    )
                result = None
                if overlaps["status"] == "RESULT_COMMITTED":
                    result = self._decode_result(overlaps)
                connection.commit()
                return HoldoutReservation(
                    value["run_id"], overlaps["status"], recovered=True, result=result
                )
            connection.execute(
                """
                INSERT INTO holdout_runs(
                    run_id,holdout_key,protocol_id,protocol_hash,market,
                    holdout_start,holdout_end,horizon,
                    freeze_hash,training_fingerprint,family_hash,snapshot_fingerprint,
                    engine_fingerprint,run_directory,
                    status,reserved_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    value["run_id"], value["holdout_key"],
                    value["protocol_id"], value["protocol_hash"], value["market"],
                    value["holdout_start"], value["holdout_end"], value["horizon"],
                    value["freeze_hash"], value["training_fingerprint"],
                    value["family_hash"], value["snapshot_fingerprint"],
                    value["engine_fingerprint"], value["run_directory"], "RESERVED", now,
                ),
            )
            event_hash = self._append_audit(connection, {
                "event": "HOLDOUT_RESERVED", "run_id": value["run_id"],
                "holdout_key": value["holdout_key"], "timestamp": now,
            })
            self._commit_and_finalize_anchor(connection, event_hash)
            return HoldoutReservation(value["run_id"], "RESERVED", recovered=False)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_opened(self, run_id: str, freeze_hash: str) -> HoldoutReservation:
        connection = self._connect()
        now = datetime.now(timezone.utc).isoformat()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_audit_chain(connection)
            row = self._owned_row(connection, run_id, freeze_hash)
            if row["status"] == "RESERVED":
                connection.execute(
                    "UPDATE holdout_runs SET status='OPENED',opened_at_utc=? WHERE run_id=?",
                    (now, run_id),
                )
                event_hash = self._append_audit(connection, {
                    "event": "HOLDOUT_OPENED", "run_id": run_id, "timestamp": now,
                })
                status = "OPENED"
                recovered = False
                result = None
            else:
                status = row["status"]
                recovered = True
                result = self._decode_result(row) if status == "RESULT_COMMITTED" else None
            if row["status"] == "RESERVED":
                self._commit_and_finalize_anchor(connection, event_hash)
            else:
                connection.commit()
            return HoldoutReservation(run_id, status, recovered, result)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def require_state(
        self, run_id: str, freeze_hash: str, allowed_states: set[str]
    ) -> HoldoutReservation:
        invalid = set(allowed_states) - HOLDOUT_STATES
        if invalid:
            raise ValueError(f"invalid allowed holdout states: {sorted(invalid)}")
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            self._verify_audit_chain(connection)
            row = self._owned_row(connection, run_id, freeze_hash)
            if row["status"] not in allowed_states:
                raise RuntimeError(
                    f"governed holdout state is {row['status']}, expected {allowed_states}"
                )
            result = (
                self._decode_result(row) if row["status"] == "RESULT_COMMITTED" else None
            )
            connection.commit()
            return HoldoutReservation(
                run_id, str(row["status"]), recovered=True, result=result
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def commit_result(
        self,
        run_id: str,
        freeze_hash: str,
        result: Mapping[str, Any],
    ) -> HoldoutReservation:
        payload = canonical_json(result)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_audit_chain(connection)
            row = self._owned_row(connection, run_id, freeze_hash)
            if row["status"] == "RESULT_COMMITTED":
                existing = self._decode_result(row)
                if canonical_json(existing) != payload:
                    raise RuntimeError("committed holdout result differs from recovery result")
                connection.commit()
                return HoldoutReservation(run_id, "RESULT_COMMITTED", True, existing)
            if row["status"] != "OPENED":
                raise RuntimeError(f"cannot commit holdout result from state {row['status']}")
            connection.execute(
                """
                UPDATE holdout_runs
                SET status='RESULT_COMMITTED',committed_at_utc=?,result_json=?,result_sha256=?
                WHERE run_id=?
                """,
                (now, payload, digest, run_id),
            )
            event_hash = self._append_audit(connection, {
                "event": "HOLDOUT_RESULT_COMMITTED", "run_id": run_id,
                "result_sha256": digest, "timestamp": now,
            })
            self._commit_and_finalize_anchor(connection, event_hash)
            return HoldoutReservation(run_id, "RESULT_COMMITTED", False, dict(result))
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _owned_row(
        connection: sqlite3.Connection, run_id: str, freeze_hash: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM holdout_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"unknown governed holdout run: {run_id}")
        if row["freeze_hash"] != freeze_hash:
            raise RuntimeError("holdout freeze hash mismatch")
        if row["status"] not in HOLDOUT_STATES:
            raise RuntimeError(f"invalid holdout state: {row['status']}")
        return row

    @staticmethod
    def _decode_result(row: sqlite3.Row) -> dict[str, Any]:
        payload = row["result_json"]
        digest = row["result_sha256"]
        if not payload or not digest:
            raise RuntimeError("committed holdout row has no sealed result")
        if hashlib.sha256(payload.encode("utf-8")).hexdigest() != digest:
            raise RuntimeError("committed holdout result hash mismatch")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise RuntimeError("committed holdout result is not an object")
        return value
