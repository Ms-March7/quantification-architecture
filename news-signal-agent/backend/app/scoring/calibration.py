from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


COMPONENTS = (
    "direction_confidence",
    "relevance",
    "source_credibility",
    "impact",
    "freshness",
    "corroboration",
    "market_validation",
)


def calibrate_scoring_weights(project_root: Path, min_samples: int = 20) -> dict[str, Any]:
    rows = _history_rows(project_root)
    samples = [_sample_from_row(row) for row in rows]
    samples = [sample for sample in samples if sample]
    if len(samples) < min_samples:
        return {
            "status": "not_enough_samples",
            "sample_count": len(samples),
            "min_samples": min_samples,
            "suggested_weights": None,
        }
    separations: dict[str, float] = {}
    for component in COMPONENTS:
        good = [sample["score"][component] for sample in samples if sample["aligned"]]
        bad = [sample["score"][component] for sample in samples if not sample["aligned"]]
        if not good or not bad:
            separations[component] = 0
            continue
        separations[component] = max(0, sum(good) / len(good) - sum(bad) / len(bad))
    total = sum(separations.values()) or 1
    suggested = {key: round(value / total, 4) for key, value in separations.items()}
    return {
        "status": "ok",
        "sample_count": len(samples),
        "accuracy_proxy": round(sum(1 for sample in samples if sample["aligned"]) / len(samples), 4),
        "component_separation": separations,
        "suggested_weights": suggested,
        "note": "基于历史榜单与T+5异常收益方向一致性生成，仅供校准参考。",
    }


def _history_rows(project_root: Path) -> list[sqlite3.Row]:
    db_path = project_root / "data" / "news_signal_agent.sqlite3"
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            return list(
                conn.execute(
                    """
                    SELECT rank_type, final_score, score_json, metadata_json
                    FROM rank_history
                    WHERE score_json IS NOT NULL AND metadata_json IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT 1000
                    """
                )
            )
        except sqlite3.OperationalError:
            return []


def _sample_from_row(row: sqlite3.Row) -> dict[str, Any] | None:
    try:
        score = json.loads(row["score_json"] or "{}")
        metadata = json.loads(row["metadata_json"] or "{}")
    except json.JSONDecodeError:
        return None
    backtest = metadata.get("backtest") or {}
    abnormal = ((backtest.get("windows") or {}).get("T+5") or {}).get("abnormal_return")
    if abnormal is None:
        return None
    sign = 1 if float(row["final_score"]) >= 0 else -1
    aligned = sign * float(abnormal) > 0
    return {
        "aligned": aligned,
        "abnormal_return": abnormal,
        "score": {component: float(score.get(component) or 0) for component in COMPONENTS},
    }
