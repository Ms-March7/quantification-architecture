"""Mechanical action-sensitivity classification for raw-price expressions."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .expressions import Expression, expression_price_scale_exponent


PRICE_FIELDS = frozenset({"open", "high", "low", "close", "vwap"})
SHARE_VOLUME_FIELDS = frozenset({"volume"})
TIME_SERIES_OPERATORS = frozenset(
    {"delay", "delta", "ts_mean", "ts_std", "ts_min", "ts_max", "ts_rank", "ts_corr"}
)


@dataclass(frozen=True)
class FactorSafetyAssessment:
    tier: str
    survivor_eligible: bool
    reasons: tuple[str, ...]
    fields: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _fields(expression: Expression) -> frozenset[str]:
    if expression.op == "field":
        return frozenset({str(expression.field)})
    result: set[str] = set()
    for child in expression.children:
        result.update(_fields(child))
    return frozenset(result)


def assess_factor_safety(
    expression: Expression,
    *,
    price_convention: str = "RAW_UNADJUSTED",
    share_volume_convention: str = "RAW_SHARES",
) -> FactorSafetyAssessment:
    """Classify whether raw levels cross sessions before becoming dimensionless.

    Same-day homogeneous ratios such as ``close / open`` are action invariant.
    Applying a time-series operator to a price-scale-dependent subtree is not:
    an ex-rights jump can then masquerade as momentum or volatility.  Share
    volume across sessions is retained as a diagnostic tier because splits can
    mechanically change the share count even when traded value is stable.
    """

    if price_convention not in {"RAW_UNADJUSTED", "CAUSAL_TOTAL_RETURN_PROJECTION"}:
        raise ValueError(f"unsupported price convention: {price_convention}")
    if share_volume_convention not in {"RAW_SHARES", "ACTION_ADJUSTED_SHARES"}:
        raise ValueError(f"unsupported share-volume convention: {share_volume_convention}")
    reasons: set[str] = set()

    def visit(node: Expression) -> None:
        for child in node.children:
            visit(child)
        if node.op not in TIME_SERIES_OPERATORS:
            return
        for child in node.children:
            fields = _fields(child)
            exponent = expression_price_scale_exponent(child)
            if (
                price_convention == "RAW_UNADJUSTED"
                and fields & PRICE_FIELDS
                and exponent != 0
            ):
                reasons.add("RAW_PRICE_LEVEL_CROSSES_SESSIONS")
            if share_volume_convention == "RAW_SHARES" and fields & SHARE_VOLUME_FIELDS:
                reasons.add("SHARE_VOLUME_SPLIT_SENSITIVE_ACROSS_SESSIONS")

    visit(expression)
    fields = tuple(sorted(_fields(expression)))
    if "RAW_PRICE_LEVEL_CROSSES_SESSIONS" in reasons:
        tier = "RAW_PRICE_ACTION_SENSITIVE_DIAGNOSTIC_ONLY"
        eligible = False
    elif "SHARE_VOLUME_SPLIT_SENSITIVE_ACROSS_SESSIONS" in reasons:
        tier = "SHARE_VOLUME_SPLIT_SENSITIVE_DIAGNOSTIC_ONLY"
        eligible = False
    else:
        tier = (
            "CAUSAL_TOTAL_RETURN_PRICE_SURVIVOR_ELIGIBLE"
            if price_convention == "CAUSAL_TOTAL_RETURN_PROJECTION"
            else "ACTION_INVARIANT_SURVIVOR_ELIGIBLE"
        )
        eligible = True
    return FactorSafetyAssessment(tier, eligible, tuple(sorted(reasons)), fields)
