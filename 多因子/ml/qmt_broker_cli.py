#!/usr/bin/env python3
"""Fail-closed QMT read-only account and shadow-intent CLI.

Live submission is intentionally not exposed.  MiniQMT must already be running
and logged in for read-only account/market-data queries.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Tuple

from broker_gateway import (
    AccountSnapshot,
    DayRiskState,
    OrderIntent,
    OrderRiskGate,
    QmtGateway,
    RiskPolicy,
    SHANGHAI,
    bare_code,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = REPO_ROOT / ".tmp" / "short-cycle" / "qmt-risk-state.json"
DEFAULT_AUDIT = REPO_ROOT / ".tmp" / "short-cycle" / "qmt-order-audit.jsonl"


def atomic_write(target: Path, value: Mapping[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def gateway_config(args: argparse.Namespace) -> Tuple[Path, str]:
    userdata = args.qmt_userdata or os.environ.get("MULTIFACTOR_QMT_USERDATA", "")
    account_id = args.account_id or os.environ.get("MULTIFACTOR_QMT_ACCOUNT_ID", "")
    if not userdata or not account_id:
        raise RuntimeError(
            "set MULTIFACTOR_QMT_USERDATA and MULTIFACTOR_QMT_ACCOUNT_ID locally, or pass the two options"
        )
    return Path(userdata), account_id


def connect_gateway(args: argparse.Namespace) -> QmtGateway:
    userdata, account_id = gateway_config(args)
    gateway = QmtGateway(
        userdata,
        account_id,
        account_type=args.account_type,
        audit_log=args.audit_log,
    )
    gateway.connect()
    return gateway


def new_state(account: AccountSnapshot, pilot_pnl: float = 0.0) -> DayRiskState:
    return DayRiskState(
        date=datetime.now(tz=SHANGHAI).date().isoformat(),
        start_equity=account.total_asset,
        current_equity=account.total_asset,
        pilot_pnl=pilot_pnl,
    )


def load_state(path: Path, account: AccountSnapshot, pilot_pnl: float | None = None) -> DayRiskState:
    today = datetime.now(tz=SHANGHAI).date().isoformat()
    if not path.exists():
        raise RuntimeError(f"QMT risk state is absent; initialize it explicitly: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("date") != today:
            raise RuntimeError(f"QMT risk state is not for {today}; initialize today's state explicitly")
        return DayRiskState(
            date=today,
            start_equity=float(value["start_equity"]),
            current_equity=account.total_asset,
            new_exposure=float(value.get("new_exposure", 0.0)),
            submitted_orders=int(value.get("submitted_orders", 0)),
            submitted_keys=tuple(value.get("submitted_keys", [])),
            pilot_pnl=float(value.get("pilot_pnl", 0.0) if pilot_pnl is None else pilot_pnl),
            kill_switch=bool(value.get("kill_switch", False)),
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(f"invalid QMT risk state: {path}") from exc


def save_state(path: Path, value: DayRiskState) -> None:
    atomic_write(path, asdict(value))


def policy(args: argparse.Namespace, state: DayRiskState) -> RiskPolicy:
    allowed = tuple(item.strip() for item in args.allowed_symbols.split(",") if item.strip())
    if not allowed:
        raise RuntimeError("allowed-symbols whitelist must be non-empty")
    whole_account_loss_stop = max(args.account_loss_floor, state.start_equity * args.account_loss_fraction)
    return RiskPolicy(
        pilot_capital=args.pilot_capital,
        max_order_value=args.max_order_value,
        max_single_position_value=args.max_single_position_value,
        max_daily_new_exposure=args.max_daily_new_exposure,
        max_orders_per_day=args.max_orders_per_day,
        max_quote_age_seconds=args.max_quote_age_seconds,
        max_signal_age_seconds=args.max_signal_age_seconds,
        max_limit_deviation_bps=args.max_limit_deviation_bps,
        daily_loss_stop=whole_account_loss_stop,
        pilot_daily_loss_stop=args.pilot_daily_loss_stop,
        allowed_symbols=allowed,
    )


def initialize_state_command(args: argparse.Namespace) -> Mapping[str, Any]:
    gateway = connect_gateway(args)
    account = gateway.get_account()
    if args.state.exists() and not args.replace:
        raise RuntimeError(f"risk state already exists; pass --replace after inspecting it: {args.state}")
    state = new_state(account, args.pilot_pnl or 0.0)
    save_state(args.state, state)
    return {"mode": "RISK_STATE_INITIALIZED_FAIL_CLOSED", "state": asdict(state)}


def snapshot_command(args: argparse.Namespace) -> Mapping[str, Any]:
    gateway = connect_gateway(args)
    account = gateway.get_account()
    return {
        "connection": "READ_ONLY",
        "account": asdict(account),
        "positions": [asdict(value) for value in gateway.get_positions()],
        "orders": list(gateway.get_orders()),
        "trades": list(gateway.get_trades()),
        "capabilities": dict(gateway.capabilities()),
    }


def preview_command(args: argparse.Namespace) -> Mapping[str, Any]:
    gateway = connect_gateway(args)
    account = gateway.get_account()
    positions = gateway.get_positions()
    quote = gateway.get_quote(args.symbol)
    state = load_state(args.state, account, args.pilot_pnl)
    now = datetime.now(tz=SHANGHAI)
    intent = OrderIntent(
        symbol=args.symbol,
        side=args.side,
        quantity=args.quantity,
        limit_price=args.limit_price,
        signal_timestamp=now.timestamp(),
        expires_at=now.timestamp() + args.expires_seconds,
        reason=args.reason,
    ).normalized()
    gate = OrderRiskGate(policy(args, state))
    decision = gate.evaluate(intent, account, positions, quote, state, now=now)
    result: dict[str, Any] = {
        "mode": "SHADOW_INTENT_ONLY",
        "account": asdict(account),
        "quote": asdict(quote),
        "intent": asdict(intent),
        "risk": asdict(decision),
        "state": asdict(state),
    }
    result["nextStep"] = "none; shadow intents cannot be consumed by a live-order command"
    return result


def kill_switch_command(args: argparse.Namespace) -> Mapping[str, Any]:
    gateway = connect_gateway(args)
    account = gateway.get_account()
    state = load_state(args.state, account, args.pilot_pnl)
    updated = DayRiskState(
        date=state.date,
        start_equity=state.start_equity,
        current_equity=account.total_asset,
        new_exposure=state.new_exposure,
        submitted_orders=state.submitted_orders,
        submitted_keys=state.submitted_keys,
        pilot_pnl=state.pilot_pnl,
        kill_switch=True,
    )
    save_state(args.state, updated)
    return {"mode": "KILL_SWITCH_ENABLED", "state": asdict(updated)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qmt-userdata", default="")
    parser.add_argument("--account-id", default="")
    parser.add_argument("--account-type", default="STOCK")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--audit-log", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--pilot-capital", type=float, default=100_000.0)
    parser.add_argument("--max-order-value", type=float, default=10_000.0)
    parser.add_argument("--max-single-position-value", type=float, default=20_000.0)
    parser.add_argument("--max-daily-new-exposure", type=float, default=30_000.0)
    parser.add_argument("--max-orders-per-day", type=int, default=3)
    parser.add_argument("--max-quote-age-seconds", type=float, default=15.0)
    parser.add_argument("--max-signal-age-seconds", type=float, default=60.0)
    parser.add_argument("--max-limit-deviation-bps", type=float, default=30.0)
    parser.add_argument("--pilot-daily-loss-stop", type=float, default=1_000.0)
    parser.add_argument("--account-loss-fraction", type=float, default=0.01)
    parser.add_argument("--account-loss-floor", type=float, default=1_000.0)
    parser.add_argument(
        "--allowed-symbols",
        default="159995,159516,603986,588170,159338,159819,588330,512050,600487,508033",
    )
    parser.add_argument("--pilot-pnl", type=float)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("snapshot")
    initialize = subparsers.add_parser("initialize-state")
    initialize.add_argument("--replace", action="store_true")
    preview = subparsers.add_parser("preview-order")
    preview.add_argument("--symbol", required=True)
    preview.add_argument("--side", choices=("BUY", "SELL"), required=True)
    preview.add_argument("--quantity", type=int, required=True)
    preview.add_argument("--limit-price", type=float, required=True)
    preview.add_argument("--reason", required=True)
    preview.add_argument("--expires-seconds", type=float, default=45.0)
    subparsers.add_parser("kill-switch")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        if args.command == "snapshot":
            result = snapshot_command(args)
        elif args.command == "initialize-state":
            result = initialize_state_command(args)
        elif args.command == "preview-order":
            result = preview_command(args)
        else:
            result = kill_switch_command(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if result.get("mode") == "LIVE_SUBMIT_BLOCKED":
            raise SystemExit(2)
    except Exception as exc:
        print(json.dumps({
            "status": "FAILED_CLOSED",
            "errorType": type(exc).__name__,
            "message": str(exc),
        }, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
