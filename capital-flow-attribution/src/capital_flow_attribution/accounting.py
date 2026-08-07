from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any


EVIDENCE_STATES = {
    "OBSERVED",
    "PROXY",
    "PARTIALLY_IDENTIFIED",
    "NOT_IDENTIFIED",
}
QUALITY_STATES = {"OK", "MISSING", "STALE", "CONFLICT", "UNLICENSED"}


def _number(value: Any) -> float | None:
    if value in (None, "", "--", "-"):
        return None
    try:
        result = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


@dataclass(frozen=True)
class AccountingResult:
    quality: str
    residual: float | None
    tolerance: float
    detail: str


def reconcile_margin(
    previous_balance: Any,
    current_balance: Any,
    financing_buy: Any,
    repayment: Any,
    *,
    absolute_tolerance: float = 1.0,
    relative_tolerance: float = 1e-8,
) -> AccountingResult:
    values = [_number(item) for item in (previous_balance, current_balance, financing_buy, repayment)]
    if any(item is None for item in values):
        return AccountingResult("MISSING", None, absolute_tolerance, "margin identity has missing inputs")
    previous, current, buy, repay = (float(item) for item in values)
    residual = current - (previous + buy - repay)
    tolerance = max(absolute_tolerance, relative_tolerance * max(abs(previous), abs(current), 1.0))
    quality = "OK" if abs(residual) <= tolerance else "CONFLICT"
    return AccountingResult(quality, residual, tolerance, "B_t = B_(t-1) + buy_t - repay_t")


def implied_margin_repayment(previous_balance: Any, current_balance: Any, financing_buy: Any) -> float | None:
    values = [_number(item) for item in (previous_balance, current_balance, financing_buy)]
    if any(item is None for item in values):
        return None
    previous, current, buy = (float(item) for item in values)
    return previous + buy - current


def decompose_etf_aum(previous_shares: Any, current_shares: Any, previous_nav: Any, current_nav: Any) -> dict[str, Any]:
    values = [_number(item) for item in (previous_shares, current_shares, previous_nav, current_nav)]
    if any(item is None for item in values):
        return {
            "quality": "MISSING",
            "evidence_state": "NOT_IDENTIFIED",
            "flow_component": None,
            "market_component": None,
            "delta_aum": None,
            "residual": None,
        }
    prev_shares, curr_shares, prev_nav, curr_nav = (float(item) for item in values)
    flow_component = curr_nav * (curr_shares - prev_shares)
    market_component = prev_shares * (curr_nav - prev_nav)
    delta_aum = curr_shares * curr_nav - prev_shares * prev_nav
    residual = delta_aum - flow_component - market_component
    tolerance = max(1e-8, 1e-10 * max(abs(delta_aum), abs(flow_component), abs(market_component), 1.0))
    return {
        "quality": "OK" if abs(residual) <= tolerance else "CONFLICT",
        "evidence_state": "OBSERVED",
        "flow_component": flow_component,
        "market_component": market_component,
        "delta_aum": delta_aum,
        "residual": residual,
    }


def validate_point_in_time(available_at: str, as_of: str) -> str:
    return "OK" if available_at <= as_of else "CONFLICT"
