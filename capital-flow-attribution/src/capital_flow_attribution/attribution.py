from __future__ import annotations

from math import isfinite, sqrt
from typing import Any, Iterable, Mapping

import networkx as nx


def partial_attribution(
    total: float,
    observed: Mapping[str, float],
    bounded_proxies: Mapping[str, tuple[float, float]],
) -> list[dict[str, Any]]:
    """Return conservative attribution intervals and keep the unidentified residual."""

    if not isfinite(total) or total < 0:
        raise ValueError("total must be a finite non-negative amount")
    observed_total = sum(observed.values())
    if any(value < 0 or not isfinite(value) for value in observed.values()) or observed_total > total:
        raise ValueError("observed amounts must be finite, non-negative and no greater than total")
    rows = [
        {
            "bucket": bucket,
            "lower": value,
            "upper": value,
            "evidence_state": "OBSERVED",
            "constraint_group": "total_conservation",
        }
        for bucket, value in observed.items()
    ]
    bounds = list(bounded_proxies.items())
    for _, (lower, upper) in bounds:
        if not (0 <= lower <= upper) or not all(isfinite(x) for x in (lower, upper)):
            raise ValueError("proxy bounds must be finite and satisfy 0 <= lower <= upper")
    lower_sum = sum(lower for _, (lower, _) in bounds)
    if lower_sum > total - observed_total:
        raise ValueError("proxy lower bounds exceed the unassigned total")
    upper_sum = 0.0
    for bucket, (lower, upper) in bounds:
        joint_upper = min(upper, total - observed_total - (lower_sum - lower))
        rows.append(
            {
                "bucket": bucket,
                "lower": lower,
                "upper": joint_upper,
                "evidence_state": "PARTIALLY_IDENTIFIED",
                "constraint_group": "total_conservation",
            }
        )
        upper_sum += joint_upper
    capacity = total - observed_total
    rows.append(
        {
            "bucket": "NOT_IDENTIFIED",
            "lower": max(0.0, capacity - upper_sum),
            "upper": capacity - lower_sum,
            "evidence_state": "NOT_IDENTIFIED",
            "constraint_group": "total_conservation",
        }
    )
    return rows


def build_migration_graph(edges: Iterable[Mapping[str, Any]]) -> nx.DiGraph:
    graph = nx.DiGraph()
    for edge in edges:
        source, target = str(edge["source"]), str(edge["target"])
        lower, upper = float(edge["lower"]), float(edge["upper"])
        if not (0 <= lower <= upper):
            raise ValueError("migration edge must satisfy 0 <= lower <= upper")
        if graph.has_edge(source, target):
            raise ValueError(f"duplicate migration edge is not allowed: {source}->{target}")
        graph.add_edge(
            source,
            target,
            lower=lower,
            upper=upper,
            evidence_state=str(edge.get("evidence_state", "PARTIALLY_IDENTIFIED")),
        )
    return graph


def switchability_bounds(
    notional: float,
    average_daily_value: float,
    horizon_days: int,
    *,
    participation_lower: float = 0.05,
    participation_upper: float = 0.20,
    daily_volatility: float = 0.02,
) -> dict[str, float]:
    if notional < 0 or average_daily_value <= 0 or horizon_days <= 0:
        raise ValueError("notional, liquidity and horizon are outside the valid domain")
    if not (0 < participation_lower <= participation_upper <= 1):
        raise ValueError("participation bounds must satisfy 0 < lower <= upper <= 1")
    low_capacity = average_daily_value * horizon_days * participation_lower
    high_capacity = average_daily_value * horizon_days * participation_upper
    low_fraction = min(1.0, low_capacity / notional) if notional else 1.0
    high_fraction = min(1.0, high_capacity / notional) if notional else 1.0
    low_participation = min(participation_lower, notional / (average_daily_value * horizon_days))
    high_participation = min(participation_upper, notional / (average_daily_value * horizon_days))
    return {
        "switchable_fraction_lower": low_fraction,
        "switchable_fraction_upper": high_fraction,
        "impact_cost_lower": daily_volatility * sqrt(low_participation),
        "impact_cost_upper": daily_volatility * sqrt(high_participation),
        "evidence_state": "PARTIALLY_IDENTIFIED",
    }
