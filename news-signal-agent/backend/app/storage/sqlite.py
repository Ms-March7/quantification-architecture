from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from app.models import Direction, EventItem, Horizon, NewsItem, SignalItem, UserOverride


def store_raw_news_items(
    project_root: Path,
    run_id: str,
    items: list[NewsItem],
    scoring_config: dict[str, Any] | None = None,
) -> int:
    db_path = _db_path(project_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        for item in items:
            conn.execute(
                """
                INSERT OR IGNORE INTO raw_messages (
                    id, run_id, source, source_type, title, summary, url,
                    published_at, raw_text, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    run_id,
                    item.source,
                    item.source_type,
                    item.title,
                    item.summary,
                    item.url,
                    item.published_at.isoformat() if item.published_at else None,
                    item.raw_text,
                    json.dumps(item.metadata, ensure_ascii=False),
                    now,
                ),
            )
        _prune_storage(conn, scoring_config or {})
        conn.commit()
    return len(items)


def store_event_cards(
    project_root: Path,
    run_id: str,
    events: list[EventItem],
) -> int:
    db_path = _db_path(project_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        for event in events:
            card = event.metadata.get("event_card") or {}
            if not card:
                continue
            conn.execute(
                """
                INSERT INTO event_cards (
                    event_id, run_id, title, summary, event_type, first_seen_at,
                    last_seen_at, evidence_count, card_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    run_id=excluded.run_id,
                    title=excluded.title,
                    summary=excluded.summary,
                    event_type=excluded.event_type,
                    last_seen_at=excluded.last_seen_at,
                    evidence_count=excluded.evidence_count,
                    card_json=excluded.card_json,
                    updated_at=excluded.updated_at
                """,
                (
                    event.id,
                    run_id,
                    str(card.get("title") or event.title),
                    str(card.get("summary") or event.summary or ""),
                    event.event_type.value,
                    event.first_seen_at.isoformat() if event.first_seen_at else None,
                    event.last_seen_at.isoformat() if event.last_seen_at else None,
                    len(event.news_ids),
                    json.dumps(card, ensure_ascii=False),
                    now,
                ),
            )
            count += 1
        conn.commit()
    return count


def store_rank_history(
    project_root: Path,
    run_id: str,
    bullish_top: list[SignalItem],
    bearish_top: list[SignalItem],
) -> int:
    db_path = _db_path(project_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [("bullish", item) for item in bullish_top] + [("bearish", item) for item in bearish_top]
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        for rank_index, (rank_type, signal) in enumerate(rows, start=1):
            conn.execute(
                """
                INSERT OR REPLACE INTO rank_history (
                    id, run_id, rank_type, rank_index, signal_id, event_id,
                    symbol, name, final_score, brief, created_at, score_json, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"{run_id}:{rank_type}:{rank_index}:{signal.id}",
                    run_id,
                    rank_type,
                    rank_index,
                    signal.id,
                    signal.event_id,
                    signal.symbol,
                    signal.name,
                    signal.final_score,
                    signal.brief,
                    datetime.now(timezone.utc).isoformat(),
                    signal.score_breakdown.model_dump_json(),
                    json.dumps(signal.metadata, ensure_ascii=False),
                ),
            )
        conn.commit()
    return len(rows)


def list_rank_history(project_root: Path, limit: int = 100) -> list[dict[str, Any]]:
    db_path = _db_path(project_root)
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT * FROM rank_history
            ORDER BY created_at DESC, rank_index ASC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in rows]


def list_event_cards(project_root: Path, limit: int = 100) -> list[dict[str, Any]]:
    db_path = _db_path(project_root)
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT * FROM event_cards
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        result = []
        for row in rows:
            item = dict(row)
            item["card"] = json.loads(item.pop("card_json") or "{}")
            result.append(item)
        return result


def upsert_event_store(
    project_root: Path,
    run_id: str,
    events: list[EventItem],
    news_by_id: dict[str, NewsItem],
) -> list[EventItem]:
    db_path = _db_path(project_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    updated_events: list[EventItem] = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        for event in events:
            stable_key = _event_stable_key(event)
            existing = conn.execute(
                """
                SELECT * FROM events
                WHERE stable_key = ?
                LIMIT 1
                """,
                (stable_key,),
            ).fetchone()
            persistent_id = existing["id"] if existing else _persistent_event_id(stable_key)
            previous_id = event.id
            evidence_news = [news_by_id[item_id] for item_id in event.news_ids if item_id in news_by_id]
            first_seen = _min_dt(
                [
                    _parse_dt(existing["first_seen_at"]) if existing else None,
                    event.first_seen_at,
                    *(item.published_at for item in evidence_news),
                ]
            )
            last_seen = _max_dt(
                [
                    _parse_dt(existing["last_seen_at"]) if existing else None,
                    event.last_seen_at,
                    *(item.published_at for item in evidence_news),
                    now,
                ]
            )
            previous_evidence_count = int(existing["evidence_count"] or 0) if existing else 0
            evidence_count = max(previous_evidence_count, 0) + len(
                {item.id for item in evidence_news}
                - _existing_evidence_ids(conn, persistent_id)
            )
            source_count = len(
                set(_existing_event_sources(conn, persistent_id))
                | {item.source for item in evidence_news if item.source}
            )
            status = _event_status(existing, evidence_count, source_count, now, last_seen)
            event.id = persistent_id
            event.first_seen_at = first_seen
            event.last_seen_at = last_seen
            event.metadata.update(
                {
                    "previous_event_id": previous_id,
                    "stable_key": stable_key,
                    "event_store_status": status,
                    "event_lifecycle": status,
                    "stored_evidence_count": evidence_count,
                    "stored_source_count": source_count,
                }
            )
            conn.execute(
                """
                INSERT INTO events (
                    id, stable_key, title, summary, event_type, status,
                    first_seen_at, last_seen_at, evidence_count, source_count,
                    run_id, metadata_json, active, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    summary=excluded.summary,
                    event_type=excluded.event_type,
                    status=excluded.status,
                    last_seen_at=excluded.last_seen_at,
                    evidence_count=excluded.evidence_count,
                    source_count=excluded.source_count,
                    run_id=excluded.run_id,
                    metadata_json=excluded.metadata_json,
                    active=1,
                    updated_at=excluded.updated_at
                """,
                (
                    persistent_id,
                    stable_key,
                    event.title,
                    event.summary,
                    event.event_type.value,
                    status,
                    first_seen.isoformat() if first_seen else None,
                    last_seen.isoformat() if last_seen else None,
                    evidence_count,
                    source_count,
                    run_id,
                    json.dumps(event.metadata, ensure_ascii=False),
                    now.isoformat(),
                ),
            )
            for item in evidence_news:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO event_evidence (
                        event_id, news_id, run_id, title, source, url,
                        published_at, raw_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        persistent_id,
                        item.id,
                        run_id,
                        item.title,
                        item.source,
                        item.url,
                        item.published_at.isoformat() if item.published_at else None,
                        item.model_dump_json(),
                        now.isoformat(),
                    ),
                )
            updated_events.append(event)
        conn.commit()
    return updated_events


def list_events(project_root: Path, limit: int = 100) -> list[dict[str, Any]]:
    db_path = _db_path(project_root)
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT * FROM events
            WHERE active = 1
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            item["active"] = bool(item["active"])
            result.append(item)
        return result


def load_llm_cache(project_root: Path, cache_key: str) -> dict[str, Any] | None:
    db_path = _db_path(project_root)
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT response_json, metadata_json FROM llm_cache
            WHERE cache_key = ?
            LIMIT 1
            """,
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        response = json.loads(row["response_json"] or "{}")
        metadata = json.loads(row["metadata_json"] or "{}")
        if isinstance(response, dict):
            response["_llm_meta"] = {"cache_hit": True, **metadata}
        return response if isinstance(response, dict) else None


def store_llm_cache(
    project_root: Path,
    cache_key: str,
    provider: str,
    model: str,
    prompt_version: str,
    response: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    db_path = _db_path(project_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO llm_cache (
                cache_key, provider, model, prompt_version, response_json,
                metadata_json, created_at, last_used_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cache_key,
                provider,
                model,
                prompt_version,
                json.dumps(response, ensure_ascii=False),
                json.dumps(metadata, ensure_ascii=False),
                metadata.get("created_at") or datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def store_workflow_artifact(
    project_root: Path,
    run_id: str,
    node_name: str,
    payload: dict[str, Any],
) -> None:
    db_path = _db_path(project_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_id = f"{run_id}:{node_name}"
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO workflow_artifacts (
                id, run_id, node_name, payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                run_id,
                node_name,
                json.dumps(payload, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def load_latest_workflow_artifact(
    project_root: Path,
    node_name: str,
    run_id: str | None = None,
) -> dict[str, Any] | None:
    db_path = _db_path(project_root)
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        if run_id:
            row = conn.execute(
                """
                SELECT payload_json FROM workflow_artifacts
                WHERE run_id = ? AND node_name = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (run_id, node_name),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT payload_json FROM workflow_artifacts
                WHERE node_name = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (node_name,),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["payload_json"] or "{}")


def store_retained_signals(
    project_root: Path,
    run_id: str,
    signals: list[SignalItem],
    scoring_config: dict[str, Any],
    news_by_id: dict[str, NewsItem],
    events_by_id: dict[str, EventItem],
) -> list[dict[str, Any]]:
    db_path = _db_path(project_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    threshold = float(scoring_config.get("thresholds", {}).get("retain_min_abs_score", 65))
    now = datetime.now(timezone.utc)
    retained: list[dict[str, Any]] = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        _expire_old_events(conn, now)
        for signal in signals:
            if abs(signal.final_score) < threshold:
                continue
            event = events_by_id.get(signal.event_id)
            if not event:
                continue
            retain_days = _retain_days(scoring_config, signal)
            expires_at = now + timedelta(days=retain_days)
            event_key = _event_key(signal, event)
            evidence = [
                news_by_id[news_id].model_dump(mode="json")
                for news_id in signal.evidence_news_ids
                if news_id in news_by_id
            ]
            payload = {
                "id": event_key,
                "run_id": run_id,
                "symbol": signal.symbol,
                "name": signal.name,
                "event_title": event.title,
                "event_summary": event.summary,
                "direction": signal.direction.value,
                "horizon": signal.primary_horizon.value,
                "current_score": signal.final_score,
                "retain_days": retain_days,
                "expires_at": expires_at.isoformat(),
                "evidence_count": len(evidence),
            }
            conn.execute(
                """
                INSERT INTO retained_events (
                    id, symbol, name, event_title, event_summary, direction, horizon,
                    first_seen_at, last_seen_at, expires_at, run_id, current_score,
                    max_abs_score, evidence_json, score_json, metadata_json, active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(id) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,
                    expires_at=excluded.expires_at,
                    run_id=excluded.run_id,
                    current_score=excluded.current_score,
                    max_abs_score=max(retained_events.max_abs_score, excluded.max_abs_score),
                    evidence_json=excluded.evidence_json,
                    score_json=excluded.score_json,
                    metadata_json=excluded.metadata_json,
                    active=1
                """,
                (
                    event_key,
                    signal.symbol,
                    signal.name,
                    event.title,
                    event.summary,
                    signal.direction.value,
                    signal.primary_horizon.value,
                    now.isoformat(),
                    now.isoformat(),
                    expires_at.isoformat(),
                    run_id,
                    signal.final_score,
                    abs(signal.final_score),
                    json.dumps(evidence, ensure_ascii=False),
                    signal.score_breakdown.model_dump_json(),
                    json.dumps({"warnings": signal.warnings, "event": event.model_dump(mode="json")}, ensure_ascii=False),
                ),
            )
            retained.append(payload)
        conn.commit()
        retained.extend(_rows_to_dicts(_active_events(conn, limit=50)))
    return _dedup_retained(retained)


def list_active_retained_events(project_root: Path, limit: int = 50) -> list[dict[str, Any]]:
    db_path = _db_path(project_root)
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        _expire_old_events(conn, datetime.now(timezone.utc))
        return _rows_to_dicts(_active_events(conn, limit=limit))


def create_user_override(
    project_root: Path,
    target_type: str,
    target_id: str,
    field: str,
    old_value: Any,
    new_value: Any,
    reason: str,
    apply_scope: str = "this_event_only",
) -> dict[str, Any]:
    override = UserOverride(
        id="override_" + sha256(
            f"{target_type}:{target_id}:{field}:{new_value}:{datetime.now(timezone.utc).isoformat()}".encode(
                "utf-8"
            )
        ).hexdigest()[:16],
        target_type=target_type,
        target_id=target_id,
        field=field,
        old_value=old_value,
        new_value=new_value,
        reason=reason,
        apply_scope=apply_scope,
    )
    db_path = _db_path(project_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO user_overrides (
                id, target_type, target_id, field, old_value_json, new_value_json,
                reason, apply_scope, created_at, active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                override.id,
                override.target_type,
                override.target_id,
                override.field,
                json.dumps(override.old_value, ensure_ascii=False),
                json.dumps(override.new_value, ensure_ascii=False),
                override.reason,
                override.apply_scope,
                override.created_at.replace(tzinfo=timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    return override.model_dump(mode="json")


def list_user_overrides(project_root: Path, include_inactive: bool = False) -> list[dict[str, Any]]:
    db_path = _db_path(project_root)
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        clause = "" if include_inactive else "WHERE active = 1"
        rows = conn.execute(
            f"""
            SELECT * FROM user_overrides
            {clause}
            ORDER BY created_at DESC
            LIMIT 200
            """
        )
        return [_override_row_to_dict(row) for row in rows]


def active_user_overrides(project_root: Path) -> list[dict[str, Any]]:
    return list_user_overrides(project_root, include_inactive=False)


def apply_signal_override(signal: SignalItem, override: dict[str, Any]) -> bool:
    if not _override_targets_signal(signal, override):
        return False
    field = override.get("field")
    value = override.get("new_value")
    if field == "direction":
        signal.direction = Direction(value)
        signal.score_breakdown.direction_confidence = max(signal.score_breakdown.direction_confidence, 88)
        signal.brief = f"人工修正方向: {value}; {signal.brief}"
    elif field == "primary_horizon":
        signal.primary_horizon = Horizon(value)
    elif field == "brief":
        signal.brief = str(value)
    elif field == "uncertainty_penalty":
        signal.score_breakdown.uncertainty_penalty = float(value)
    else:
        return False
    signal.metadata.setdefault("user_overrides", []).append(
        {
            "id": override.get("id"),
            "field": field,
            "new_value": value,
            "reason": override.get("reason"),
        }
    )
    if "user_override_applied" not in signal.warnings:
        signal.warnings.append("user_override_applied")
    return True


def notification_allowed(
    project_root: Path,
    notification_key: str,
    channel: str,
    symbol: str,
    cooldown_minutes: int,
    daily_limit_per_symbol: int,
) -> tuple[bool, str]:
    db_path = _db_path(project_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        cooldown_since = (now - timedelta(minutes=cooldown_minutes)).isoformat()
        recent = conn.execute(
            """
            SELECT 1 FROM notification_log
            WHERE notification_key = ? AND channel = ? AND created_at >= ?
            LIMIT 1
            """,
            (notification_key, channel, cooldown_since),
        ).fetchone()
        if recent:
            return False, "cooldown"
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        sent_today = conn.execute(
            """
            SELECT COUNT(*) AS count FROM notification_log
            WHERE symbol = ? AND channel = ? AND created_at >= ? AND status = 'sent'
            """,
            (symbol, channel, day_start),
        ).fetchone()["count"]
        if int(sent_today) >= daily_limit_per_symbol:
            return False, "daily_limit"
    return True, "allowed"


def record_notification(
    project_root: Path,
    notification_id: str,
    notification_key: str,
    channel: str,
    symbol: str,
    score: float,
    status: str,
    detail: str,
) -> None:
    db_path = _db_path(project_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO notification_log (
                id, notification_key, channel, symbol, score, status, detail, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                notification_id,
                notification_key,
                channel,
                symbol,
                score,
                status,
                detail,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_messages (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            source TEXT NOT NULL,
            source_type TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT,
            url TEXT,
            published_at TEXT,
            raw_text TEXT,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_messages_created ON raw_messages(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_messages_source ON raw_messages(source, created_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            stable_key TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            summary TEXT,
            event_type TEXT NOT NULL,
            status TEXT NOT NULL,
            first_seen_at TEXT,
            last_seen_at TEXT,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            source_count INTEGER NOT NULL DEFAULT 0,
            run_id TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_status_updated ON events(status, updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_last_seen ON events(last_seen_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS event_evidence (
            event_id TEXT NOT NULL,
            news_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            url TEXT,
            published_at TEXT,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (event_id, news_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_event_evidence_event ON event_evidence(event_id, created_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS event_cards (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            event_type TEXT NOT NULL,
            first_seen_at TEXT,
            last_seen_at TEXT,
            evidence_count INTEGER NOT NULL,
            card_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_event_cards_updated ON event_cards(updated_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rank_history (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            rank_type TEXT NOT NULL,
            rank_index INTEGER NOT NULL,
            signal_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT,
            final_score REAL NOT NULL,
            brief TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rank_history_created ON rank_history(created_at)")
    _ensure_column(conn, "rank_history", "score_json", "TEXT")
    _ensure_column(conn, "rank_history", "metadata_json", "TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_artifacts (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            node_name TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_artifacts_node ON workflow_artifacts(node_name, created_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_cache (
            cache_key TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            response_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_cache_model ON llm_cache(model, last_used_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS retained_events (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            name TEXT,
            event_title TEXT NOT NULL,
            event_summary TEXT,
            direction TEXT NOT NULL,
            horizon TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            run_id TEXT NOT NULL,
            current_score REAL NOT NULL,
            max_abs_score REAL NOT NULL,
            evidence_json TEXT NOT NULL,
            score_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_retained_active ON retained_events(active, expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_retained_symbol ON retained_events(symbol)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_overrides (
            id TEXT PRIMARY KEY,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            field TEXT NOT NULL,
            old_value_json TEXT NOT NULL,
            new_value_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            apply_scope TEXT NOT NULL,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_overrides_target ON user_overrides(target_type, target_id, active)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_log (
            id TEXT PRIMARY KEY,
            notification_key TEXT NOT NULL,
            channel TEXT NOT NULL,
            symbol TEXT NOT NULL,
            score REAL NOT NULL,
            status TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notification_key ON notification_log(notification_key, channel, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notification_symbol ON notification_log(symbol, channel, created_at)")


def _active_events(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM retained_events
            WHERE active = 1
            ORDER BY max_abs_score DESC, last_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        )
    )


def _expire_old_events(conn: sqlite3.Connection, now: datetime) -> None:
    conn.execute(
        "UPDATE retained_events SET active = 0 WHERE active = 1 AND expires_at < ?",
        (now.isoformat(),),
    )


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    result = []
    now = datetime.now(timezone.utc)
    for row in rows:
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
        item["score_breakdown"] = json.loads(item.pop("score_json") or "{}")
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        item["decayed_score"] = _decayed_score(item, now)
        item["decay_state"] = _decay_state(item, now)
        result.append(item)
    return result


def _dedup_retained(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        item_id = item["id"]
        if item_id in seen:
            continue
        seen.add(item_id)
        result.append(item)
    return result


def _retain_days(scoring_config: dict[str, Any], signal: SignalItem) -> int:
    horizon = signal.primary_horizon.value
    return int(scoring_config.get("horizons", {}).get(horizon, {}).get("retain_days", 7))


def _event_key(signal: SignalItem, event: EventItem) -> str:
    raw = f"{signal.symbol}:{event.title.strip().lower()}"
    return "retained_" + sha256(raw.encode("utf-8")).hexdigest()[:16]


def _db_path(project_root: Path) -> Path:
    return project_root / "data" / "news_signal_agent.sqlite3"


def _override_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["old_value"] = json.loads(item.pop("old_value_json") or "null")
    item["new_value"] = json.loads(item.pop("new_value_json") or "null")
    item["active"] = bool(item["active"])
    return item


def _override_targets_signal(signal: SignalItem, override: dict[str, Any]) -> bool:
    target_type = override.get("target_type")
    target_id = str(override.get("target_id") or "")
    if target_type == "signal":
        return target_id in {signal.id, signal.event_id}
    if target_type == "symbol":
        return target_id == signal.symbol
    if target_type == "event":
        return target_id == signal.event_id
    return False


def _event_stable_key(event: EventItem) -> str:
    cluster_key = str(event.metadata.get("cluster_key") or "")
    parts = cluster_key.split(":", 2)
    if len(parts) == 3 and parts[1]:
        primary = parts[1].lower()
        if primary not in _GENERIC_EVENT_TERMS:
            return f"{parts[0]}:{primary}"
        return f"{parts[0]}:{primary}:{_title_fingerprint(event.title)}"
    terms = [str(item).strip().lower() for item in event.metadata.get("cluster_terms", []) if item]
    specific_terms = [term for term in terms if term not in _GENERIC_EVENT_TERMS]
    if specific_terms:
        return f"{event.event_type.value}:{':'.join(specific_terms[:3])}"
    return f"{event.event_type.value}:title:{sha256(event.title[:48].encode('utf-8')).hexdigest()[:12]}"


_GENERIC_EVENT_TERMS = {
    "公告",
    "政策",
    "业绩",
    "回购",
    "减持",
    "美股",
    "纳指",
    "大盘",
    "a股",
}


def _title_fingerprint(title: str) -> str:
    return sha256(title[:48].encode("utf-8")).hexdigest()[:10]


def _persistent_event_id(stable_key: str) -> str:
    return "event_" + sha256(stable_key.encode("utf-8")).hexdigest()[:16]


def _existing_evidence_ids(conn: sqlite3.Connection, event_id: str) -> set[str]:
    rows = conn.execute("SELECT news_id FROM event_evidence WHERE event_id = ?", (event_id,))
    return {str(row[0]) for row in rows}


def _existing_event_sources(conn: sqlite3.Connection, event_id: str) -> set[str]:
    rows = conn.execute("SELECT DISTINCT source FROM event_evidence WHERE event_id = ?", (event_id,))
    return {str(row[0]) for row in rows if row[0]}


def _event_status(
    existing: sqlite3.Row | None,
    evidence_count: int,
    source_count: int,
    now: datetime,
    last_seen: datetime | None,
) -> str:
    if not existing:
        return "confirmed" if evidence_count >= 2 or source_count >= 2 else "new"
    age_hours = 0.0
    if last_seen:
        age_hours = max((now - last_seen).total_seconds() / 3600, 0)
    if age_hours >= 72:
        return "fading"
    if age_hours >= 24:
        return "cooling"
    if evidence_count >= 2 or source_count >= 2:
        return "confirmed"
    return "updating"


def _min_dt(values: list[datetime | None]) -> datetime | None:
    rows = [value for value in values if value is not None]
    if not rows:
        return None
    return min(rows)


def _max_dt(values: list[datetime | None]) -> datetime | None:
    rows = [value for value in values if value is not None]
    if not rows:
        return None
    return max(rows)


def _decayed_score(item: dict[str, Any], now: datetime) -> float:
    current = float(item.get("current_score") or 0)
    first_seen = _parse_dt(item.get("first_seen_at"))
    expires_at = _parse_dt(item.get("expires_at"))
    if not first_seen or not expires_at or expires_at <= first_seen:
        return round(current, 2)
    progress = min(1.0, max(0.0, (now - first_seen).total_seconds() / (expires_at - first_seen).total_seconds()))
    return round(current * (1 - progress), 2)


def _decay_state(item: dict[str, Any], now: datetime) -> str:
    expires_at = _parse_dt(item.get("expires_at"))
    if not expires_at:
        return "unknown"
    remaining = (expires_at - now).total_seconds()
    total = max(1, (expires_at - (_parse_dt(item.get("first_seen_at")) or now)).total_seconds())
    ratio = remaining / total
    if ratio <= 0:
        return "expired"
    if ratio <= 0.25:
        return "fading"
    if ratio <= 0.6:
        return "cooling"
    return "active"


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if any(row[1] == column for row in rows):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _prune_storage(conn: sqlite3.Connection, scoring_config: dict[str, Any]) -> None:
    retention = scoring_config.get("storage_retention", {})
    raw_days = int(retention.get("raw_message_days", 14))
    rank_days = int(retention.get("rank_history_days", 180))
    notification_days = int(retention.get("notification_log_days", 60))
    max_event_cards = int(retention.get("max_event_cards", 5000))
    artifact_days = int(retention.get("workflow_artifact_days", 7))
    llm_cache_days = int(retention.get("llm_cache_days", 30))
    now = datetime.now(timezone.utc)
    conn.execute(
        "DELETE FROM raw_messages WHERE created_at < ?",
        ((now - timedelta(days=raw_days)).isoformat(),),
    )
    conn.execute(
        "DELETE FROM rank_history WHERE created_at < ?",
        ((now - timedelta(days=rank_days)).isoformat(),),
    )
    conn.execute(
        "DELETE FROM notification_log WHERE created_at < ?",
        ((now - timedelta(days=notification_days)).isoformat(),),
    )
    conn.execute(
        """
        DELETE FROM event_cards
        WHERE event_id NOT IN (
            SELECT event_id FROM event_cards
            ORDER BY updated_at DESC
            LIMIT ?
        )
        """,
        (max_event_cards,),
    )
    conn.execute(
        "DELETE FROM workflow_artifacts WHERE created_at < ?",
        ((now - timedelta(days=artifact_days)).isoformat(),),
    )
    conn.execute(
        "DELETE FROM llm_cache WHERE last_used_at < ?",
        ((now - timedelta(days=llm_cache_days)).isoformat(),),
    )
