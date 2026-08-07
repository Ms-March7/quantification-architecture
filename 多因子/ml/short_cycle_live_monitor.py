#!/usr/bin/env python3
"""A-share 5-minute defensive monitor with optional read-only QMT account sync.

This is the operational layer for the next session.  It consumes completed
bars, computes transparent multi-factor risk diagnostics, reconciles sellable
inventory when QMT is available, and emits a fail-closed decision card.  It
does not submit orders; this repository exposes no order-submission path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, time as wall_time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from broker_gateway import Position, QmtGateway, SHANGHAI, bare_code, in_continuous_session
from cn_market_data import (
    DataBundle,
    MarketBar,
    QualityReport,
    completed_bars,
    fetch_public_universe,
    format_timestamp,
    json_safe,
    parse_timestamp,
    write_cache,
)


MINIMUM_BARS = 60


@dataclass(frozen=True)
class FactorSnapshot:
    return_1: float
    return_3: float
    return_6: float
    return_12: float
    return_48: float
    realized_vol_12: float
    realized_vol_48: float
    downside_vol_48: float
    volume_z_20: float
    amount_z_20: float
    range_z_20: float
    vwap_gap_20: float
    intraday_return: float
    intraday_drawdown: float
    last_bar_amount: float
    relative_return_6: float = 0.0


def safe_std(values: np.ndarray) -> float:
    clean = np.asarray(values, dtype=np.float64)
    clean = clean[np.isfinite(clean)]
    if clean.size < 2:
        return 0.0
    return float(np.std(clean, ddof=1))


def zscore_last(values: np.ndarray) -> float:
    scale = safe_std(values)
    return 0.0 if scale <= 1e-12 else float((values[-1] - np.mean(values)) / scale)


def instrument_class(symbol: str) -> str:
    code = bare_code(symbol)
    if code.startswith(("508", "180")):
        return "REIT"
    if code.startswith(("5", "1")):
        return "ETF_OR_FUND"
    if code.startswith(("688", "689")):
        return "STAR_STOCK"
    if code.startswith("3"):
        return "CHINEXT_STOCK"
    return "A_SHARE_STOCK"


def compute_factors(values: Sequence[MarketBar]) -> FactorSnapshot:
    if len(values) < MINIMUM_BARS:
        raise ValueError(f"at least {MINIMUM_BARS} completed bars are required")
    bars = values[-240:]
    close = np.asarray([value.close for value in bars], dtype=np.float64)
    high = np.asarray([value.high for value in bars], dtype=np.float64)
    low = np.asarray([value.low for value in bars], dtype=np.float64)
    volume = np.asarray([value.volume for value in bars], dtype=np.float64)
    amount = np.asarray([value.amount for value in bars], dtype=np.float64)
    returns = np.zeros(len(close), dtype=np.float64)
    returns[1:] = np.log(close[1:] / close[:-1])
    true_range = (high - low) / np.maximum(close, 1e-12)

    def cumulative(count: int) -> float:
        return float(np.sum(returns[-count:]))

    latest_day = parse_timestamp(bars[-1].time).date()
    day_indexes = [
        index for index, value in enumerate(bars)
        if parse_timestamp(value.time).date() == latest_day
    ]
    day_start = day_indexes[0] if day_indexes else len(bars) - 1
    day_high = float(np.max(high[day_start:]))
    day_open = float(bars[day_start].open)
    typical = (high[-20:] + low[-20:] + close[-20:]) / 3.0
    denominator = float(np.sum(volume[-20:]))
    vwap = float(np.sum(typical * volume[-20:]) / denominator) if denominator > 0.0 else close[-1]
    downside = returns[-48:][returns[-48:] < 0.0]
    return FactorSnapshot(
        return_1=cumulative(1),
        return_3=cumulative(3),
        return_6=cumulative(6),
        return_12=cumulative(12),
        return_48=cumulative(48),
        realized_vol_12=safe_std(returns[-12:]),
        realized_vol_48=safe_std(returns[-48:]),
        downside_vol_48=safe_std(downside),
        volume_z_20=zscore_last(np.log1p(volume[-20:])),
        amount_z_20=zscore_last(np.log1p(amount[-20:])),
        range_z_20=zscore_last(true_range[-20:]),
        vwap_gap_20=float(close[-1] / max(vwap, 1e-12) - 1.0),
        intraday_return=float(close[-1] / max(day_open, 1e-12) - 1.0),
        intraday_drawdown=float(close[-1] / max(day_high, 1e-12) - 1.0),
        last_bar_amount=float(amount[-1]),
    )


def add_relative_returns(values: Mapping[str, FactorSnapshot]) -> Mapping[str, FactorSnapshot]:
    if not values:
        return values
    median = float(np.median([value.return_6 for value in values.values()]))
    return {
        symbol: FactorSnapshot(**{
            **asdict(value),
            "relative_return_6": value.return_6 - median,
        })
        for symbol, value in values.items()
    }


def risk_score(value: FactorSnapshot) -> Tuple[int, Tuple[str, ...]]:
    score = 0
    reasons: List[str] = []
    short_scale = max(value.realized_vol_12, 0.0015)
    long_scale = max(value.realized_vol_48, 0.0012)
    if value.return_3 < -1.5 * short_scale:
        score += 25
        reasons.append("short_momentum_break")
    if value.relative_return_6 < -1.5 * long_scale:
        score += 20
        reasons.append("cross_sectional_weakness")
    if value.intraday_drawdown < -2.0 * long_scale:
        score += 20
        reasons.append("intraday_drawdown")
    if value.realized_vol_12 > 1.8 * max(value.realized_vol_48, 1e-6):
        score += 15
        reasons.append("volatility_burst")
    if value.volume_z_20 > 2.0 and value.return_1 < 0.0:
        score += 20
        reasons.append("negative_volume_shock")
    if value.vwap_gap_20 < -1.5 * long_scale:
        score += 10
        reasons.append("below_vwap")
    return min(100, score), tuple(reasons)


def position_map(values: Sequence[Position]) -> Mapping[str, Position]:
    return {bare_code(value.symbol): value for value in values}


def load_manual_snapshot(path: Path) -> Tuple[Mapping[str, Any], Tuple[Position, ...]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    positions = tuple(Position(
        symbol=str(item["symbol"]),
        name=str(item.get("name") or item["symbol"]),
        total_quantity=int(item["totalQuantity"]),
        sellable_quantity=int(item["sellableQuantity"]),
        average_price=float(item.get("averagePrice", 0.0)),
        last_price=float(item.get("lastPrice", 0.0)),
        market_value=float(item.get("marketValue", 0.0)),
    ) for item in value.get("positions", []))
    as_of = parse_timestamp(value["asOf"]).timestamp()
    account = {
        "connected": False,
        "source": "MANUAL_SNAPSHOT",
        "timestamp": as_of,
        "asOf": value["asOf"],
        "totalAsset": float(value.get("totalAsset", 0.0)),
        "cash": float(value.get("cash", 0.0)),
        "warning": "manual snapshot must be refreshed after every order or fill",
    }
    return account, positions


def in_decision_window(moment: datetime) -> bool:
    local = moment.astimezone(SHANGHAI)
    current = local.time()
    return (
        wall_time(9, 45) <= current <= wall_time(11, 15)
        or wall_time(13, 15) <= current <= wall_time(14, 30)
    )


def classify_action(
    score: int,
    position: Optional[Position],
    data_status: str,
    market_stress: bool,
) -> Tuple[str, Tuple[str, ...]]:
    reasons: List[str] = []
    if data_status == "DATA_BLOCKED":
        return "DATA_BLOCKED", ("data_quality_gate",)
    if score >= 70 or (score >= 55 and market_stress):
        if position and position.sellable_quantity > 0:
            return "REDUCE_REVIEW", ("risk_threshold",)
        if position and position.total_quantity > 0:
            return "T1_LOCKED_STOP_NEW_RISK", ("risk_threshold", "no_sellable_inventory")
        return "NO_NEW_RISK", ("risk_threshold",)
    if score >= 40 or market_stress:
        reasons.append("defensive_state")
        return "DEFENSIVE_HOLD", tuple(reasons)
    return "HOLD_REVIEW", ()


def qmt_bundles(gateway: QmtGateway, symbols: Sequence[str], period: str, limit: int) -> Mapping[str, DataBundle]:
    raw = gateway.get_bars(symbols, period=period, count=limit)
    result: Dict[str, DataBundle] = {}
    for symbol in symbols:
        code = bare_code(symbol)
        bars: List[MarketBar] = []
        for row in raw.get(code, []):
            bars.append(MarketBar(
                time=format_timestamp(row.get("time")),
                open=float(row.get("open", 0.0)),
                high=float(row.get("high", 0.0)),
                low=float(row.get("low", 0.0)),
                close=float(row.get("close", 0.0)),
                volume=float(row.get("volume", 0.0)),
                amount=float(row.get("amount", 0.0)),
            ))
        bars = list(completed_bars(bars))[-limit:]
        reasons = () if len(bars) >= MINIMUM_BARS else ("insufficient_qmt_bars",)
        quality = QualityReport(
            status="OK_QMT" if not reasons else "DATA_BLOCKED",
            manual_review_eligible=not reasons,
            reasons=reasons,
            primary_source="QMT",
            secondary_source="",
            primary_last_time=bars[-1].time if bars else "",
            secondary_last_time="",
            last_close_difference_bps=0.0,
            last_volume_relative_difference=0.0,
            bar_count=len(bars),
        )
        result[code] = DataBundle(code, period, tuple(bars), quality)
    return result


def build_report(
    bundles: Mapping[str, DataBundle],
    positions: Sequence[Position] = (),
    account: Optional[Mapping[str, Any]] = None,
    source_mode: str = "public",
    now: Optional[datetime] = None,
    decision_cadence_minutes: int = 15,
) -> Mapping[str, Any]:
    moment = now or datetime.now(tz=SHANGHAI)
    factors: Dict[str, FactorSnapshot] = {}
    completed: Dict[str, Tuple[MarketBar, ...]] = {}
    for symbol, bundle in bundles.items():
        values = completed_bars(bundle.bars, moment)
        completed[symbol] = values
        if bundle.quality.status != "DATA_BLOCKED" and len(values) >= MINIMUM_BARS:
            factors[symbol] = compute_factors(values)
    factors = dict(add_relative_returns(factors))
    breadth = (
        float(np.mean([value.return_3 < 0.0 for value in factors.values()]))
        if factors else 1.0
    )
    market_stress = breadth >= 0.70
    positions_by_symbol = position_map(positions)
    cards: List[Mapping[str, Any]] = []
    for symbol in sorted(bundles):
        bundle = bundles[symbol]
        security_class = instrument_class(symbol)
        value = factors.get(symbol)
        score, score_reasons = risk_score(value) if value else (100, ("factor_unavailable",))
        position = positions_by_symbol.get(symbol)
        action, action_reasons = classify_action(
            score, position, bundle.quality.status, market_stress
        )
        if security_class == "REIT" and action != "DATA_BLOCKED":
            action = "SEPARATE_REIT_REVIEW"
            action_reasons = ("reit_requires_separate_model",)
        last = completed[symbol][-1] if completed[symbol] else None
        cards.append({
            "symbol": symbol,
            "instrumentClass": security_class,
            "lastBarTime": last.time if last else "",
            "lastPrice": last.close if last else None,
            "dataQuality": asdict(bundle.quality),
            "position": asdict(position) if position else None,
            "factors": asdict(value) if value else None,
            "riskScore": score,
            "riskReasons": list(score_reasons),
            "action": action,
            "actionReasons": list(action_reasons),
        })
    review_data_ok = bool(cards) and all(
        item["dataQuality"]["manual_review_eligible"] for item in cards
    )
    account_connected = bool(account and account.get("connected"))
    account_timestamp = float(account.get("timestamp", 0.0)) if account else 0.0
    account_fresh = account_connected and 0.0 <= moment.timestamp() - account_timestamp <= 15.0
    newest_epochs = [
        parse_timestamp(item["lastBarTime"]).timestamp()
        for item in cards if item["lastBarTime"]
    ]
    period_minutes = 5
    if bundles:
        period_text = next(iter(bundles.values())).period
        period_minutes = int(period_text[:-1]) if period_text.endswith("m") else 5
    bars_fresh = bool(newest_epochs) and all(
        0.0 <= moment.timestamp() - value <= period_minutes * 60.0 + 45.0
        for value in newest_epochs
    )
    latest_bar = parse_timestamp(cards[0]["lastBarTime"]) if cards and cards[0]["lastBarTime"] else None
    cadence_open = bool(
        latest_bar and latest_bar.minute % max(1, decision_cadence_minutes) == 0
    )
    trading_session = in_continuous_session(moment)
    decision_window = in_decision_window(moment)
    workflow_status = (
        "ALLOW_MANUAL_ORDER_REVIEW"
        if (
            source_mode == "qmt" and review_data_ok and account_fresh and bars_fresh
            and trading_session and decision_window and cadence_open
        ) else "OBSERVATION_ONLY"
    )
    if any(item["action"] == "DATA_BLOCKED" for item in cards):
        workflow_status = "DATA_BLOCKED"
    latest_times = "|".join(str(item["lastBarTime"]) for item in cards)
    decision_id = hashlib.sha256(latest_times.encode("utf-8")).hexdigest()[:16]
    dynamic_market_value = sum(
        (positions_by_symbol[item["symbol"]].total_quantity * float(item["lastPrice"]))
        for item in cards
        if item["symbol"] in positions_by_symbol and item["lastPrice"] is not None
    )
    dynamic_sellable_value = sum(
        (positions_by_symbol[item["symbol"]].sellable_quantity * float(item["lastPrice"]))
        for item in cards
        if item["symbol"] in positions_by_symbol and item["lastPrice"] is not None
    )
    return {
        "generatedAt": moment.isoformat(),
        "expiresAt": moment.timestamp() + 60.0,
        "decisionId": decision_id,
        "workflowStatus": workflow_status,
        "sourceMode": source_mode,
        "account": account,
        "portfolio": {
            "instrumentCount": len(cards),
            "negativeBreadth3Bars": breadth,
            "marketStress": market_stress,
            "totalMarketValue": dynamic_market_value,
            "sellableMarketValueEstimate": dynamic_sellable_value,
        },
        "cards": cards,
        "globalBlocks": [
            message for condition, message in (
                (source_mode != "qmt", "public_data_is_observation_only"),
                (source_mode != "qmt" and bool(positions), "manual_position_snapshot_requires_reconciliation"),
                (source_mode == "qmt" and not account_connected, "broker_account_not_connected"),
                (source_mode == "qmt" and account_connected and not account_fresh, "broker_snapshot_stale"),
                (not review_data_ok, "manual_review_data_gate_failed"),
                (source_mode == "qmt" and not bars_fresh, "completed_bar_stale"),
                (source_mode == "qmt" and not trading_session, "outside_continuous_session"),
                (source_mode == "qmt" and trading_session and not decision_window, "outside_decision_window"),
                (source_mode == "qmt" and decision_window and not cadence_open, "non_decision_bar"),
                (market_stress, "portfolio_breadth_stress"),
            ) if condition
        ],
        "notice": (
            "Signals are conditional risk reviews, not profit guarantees. "
            "The monitor never submits an order and cannot sell T+1-locked inventory."
        ),
    }


def atomic_json_write(target: Path, value: Mapping[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(json_safe(value), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(target)


def parse_symbols(value: str) -> List[str]:
    return list(dict.fromkeys(
        bare_code(item.strip()) for item in value.replace(";", ",").split(",") if item.strip()
    ))


def one_cycle(args: argparse.Namespace) -> Mapping[str, Any]:
    gateway: Optional[QmtGateway] = None
    positions: Sequence[Position] = ()
    account_value: Optional[Mapping[str, Any]] = None
    symbols = parse_symbols(args.symbols) if args.symbols else []
    try:
        if args.broker == "qmt":
            userdata = args.qmt_userdata or os.environ.get("MULTIFACTOR_QMT_USERDATA", "")
            account_id = args.account_id or os.environ.get("MULTIFACTOR_QMT_ACCOUNT_ID", "")
            if not userdata or not account_id:
                raise RuntimeError("QMT mode requires local userdata path and account id")
            gateway = QmtGateway(Path(userdata), account_id, account_type=args.account_type)
            connection = gateway.connect()
            account_snapshot = gateway.get_account()
            positions = gateway.get_positions()
            if not symbols:
                symbols = [bare_code(value.symbol) for value in positions if value.total_quantity > 0]
            account_value = {**asdict(account_snapshot), "connection": connection}
            bundles = qmt_bundles(gateway, symbols, args.period, args.limit)
        else:
            if not symbols:
                raise RuntimeError("public mode requires --symbols")
            if args.position_file and args.position_file.exists():
                account_value, positions = load_manual_snapshot(args.position_file)
            bundles = fetch_public_universe(symbols, args.period, args.limit, args.workers)
        report = build_report(
            bundles,
            positions=positions,
            account=account_value,
            source_mode=args.broker,
        )
        if args.cache_dir:
            for bundle in bundles.values():
                write_cache(bundle, args.cache_dir)
        if args.output:
            atomic_json_write(args.output, report)
        return report
    finally:
        if gateway is not None:
            gateway.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", choices=("public", "qmt"), default="public")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--period", choices=("1m", "5m", "15m"), default="5m")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--qmt-userdata", default="")
    parser.add_argument("--account-id", default="")
    parser.add_argument("--account-type", default="STOCK")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--position-file", type=Path)
    parser.add_argument("--watch-seconds", type=float, default=0.0)
    parser.add_argument("--max-cycles", type=int, default=0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cycle = 0
    while True:
        report = one_cycle(args)
        print(json.dumps(json_safe({
            "generatedAt": report["generatedAt"],
            "decisionId": report["decisionId"],
            "workflowStatus": report["workflowStatus"],
            "globalBlocks": report["globalBlocks"],
            "actions": {item["symbol"]: item["action"] for item in report["cards"]},
            "output": str(args.output) if args.output else None,
        }), ensure_ascii=False, allow_nan=False))
        cycle += 1
        if args.watch_seconds <= 0.0 or (args.max_cycles and cycle >= args.max_cycles):
            break
        time.sleep(max(5.0, args.watch_seconds))


if __name__ == "__main__":
    main()
