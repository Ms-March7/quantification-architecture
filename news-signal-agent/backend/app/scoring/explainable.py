from __future__ import annotations

from app.models import Direction, ScoreBreakdown


DEFAULT_WEIGHTS = {
    "relevance": 0.25,
    "source_credibility": 0.15,
    "impact": 0.20,
    "freshness": 0.10,
    "corroboration": 0.10,
    "market_validation": 0.15,
    "direction_confidence": 0.05,
}


def final_score(direction: Direction, breakdown: ScoreBreakdown, weights: dict[str, float] | None = None) -> float:
    if direction in {Direction.neutral, Direction.uncertain}:
        return 0
    sign = 1 if direction == Direction.bullish else -1
    weighted = breakdown.weighted_score(weights or DEFAULT_WEIGHTS)
    adjusted = max(weighted - breakdown.uncertainty_penalty, 0)
    return sign * adjusted


def bar(value: float, width: int = 10) -> str:
    clamped = max(0, min(100, round(value)))
    filled = round(clamped / 100 * width)
    return "#" * filled + "-" * (width - filled)
