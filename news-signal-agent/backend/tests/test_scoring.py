from app.models import Direction, ScoreBreakdown
from app.scoring.explainable import final_score


def test_bearish_score_keeps_negative_direction() -> None:
    breakdown = ScoreBreakdown(
        direction_confidence=90,
        relevance=100,
        source_credibility=80,
        impact=80,
        freshness=90,
        corroboration=50,
        market_validation=80,
        uncertainty_penalty=10,
    )
    assert final_score(Direction.bearish, breakdown) < 0


def test_uncertainty_penalty_reduces_absolute_score() -> None:
    base = ScoreBreakdown(
        direction_confidence=90,
        relevance=100,
        source_credibility=80,
        impact=80,
        freshness=90,
        corroboration=50,
        market_validation=80,
        uncertainty_penalty=0,
    )
    penalized = base.model_copy(update={"uncertainty_penalty": 20})
    assert final_score(Direction.bullish, penalized) < final_score(Direction.bullish, base)
    assert abs(final_score(Direction.bearish, penalized)) < abs(final_score(Direction.bearish, base))
