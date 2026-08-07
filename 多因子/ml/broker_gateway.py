#!/usr/bin/env python3
"""Broker boundary and fail-closed order risk gate for the multifactor project.

The model layer must never call a broker SDK directly.  It creates an
``OrderIntent``; ``OrderRiskGate`` validates that intent against a fresh broker
snapshot for shadow/manual review.  This repository exposes no order-submission
method.  QMT/MiniQMT is optional and imported only when selected.

No password, token, SMS code, or GUI automation belongs in this module.
Authentication stays in the broker's official desktop client.
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from datetime import datetime, time as wall_time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


def finite(value: Any, fallback: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if math.isfinite(result) else fallback


def first_attr(value: Any, names: Sequence[str], fallback: Any = None) -> Any:
    for name in names:
        if hasattr(value, name):
            candidate = getattr(value, name)
            if candidate is not None:
                return candidate
        if isinstance(value, Mapping) and name in value and value[name] is not None:
            return value[name]
    return fallback


def mask_account(account_id: str) -> str:
    clean = str(account_id).strip()
    if len(clean) <= 4:
        return "*" * len(clean)
    return "*" * (len(clean) - 4) + clean[-4:]


def qmt_code(symbol: str) -> str:
    clean = str(symbol).strip().upper()
    if clean.endswith((".SH", ".SZ", ".BJ")):
        return clean
    digits = "".join(character for character in clean if character.isdigit())
    if len(digits) != 6:
        raise ValueError(f"unsupported security code: {symbol}")
    if digits.startswith(("4", "8")):
        suffix = ".BJ"
    elif digits.startswith(("5", "6", "9")):
        suffix = ".SH"
    else:
        suffix = ".SZ"
    return digits + suffix


def bare_code(symbol: str) -> str:
    return qmt_code(symbol).split(".", 1)[0]


@dataclass(frozen=True)
class AccountSnapshot:
    broker: str
    account_masked: str
    timestamp: float
    total_asset: float
    cash: float
    market_value: float
    connected: bool = False


@dataclass(frozen=True)
class Position:
    symbol: str
    name: str
    total_quantity: int
    sellable_quantity: int
    average_price: float
    last_price: float
    market_value: float


@dataclass(frozen=True)
class Quote:
    symbol: str
    timestamp: float
    last_price: float
    previous_close: float
    bid_price: float = 0.0
    ask_price: float = 0.0
    upper_limit: float = 0.0
    lower_limit: float = 0.0
    suspended: bool = False


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    quantity: int
    limit_price: float
    signal_timestamp: float
    expires_at: float
    reason: str
    strategy: str = "multifactor-short-cycle"
    idempotency_key: str = ""

    def normalized(self) -> "OrderIntent":
        side = self.side.strip().upper()
        symbol = qmt_code(self.symbol)
        key = self.idempotency_key.strip()
        if not key:
            material = (
                f"{symbol}|{side}|{int(self.quantity)}|{float(self.limit_price):.4f}|"
                f"{int(self.signal_timestamp)}|{self.strategy}"
            )
            key = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
        return OrderIntent(
            symbol=symbol,
            side=side,
            quantity=int(self.quantity),
            limit_price=float(self.limit_price),
            signal_timestamp=float(self.signal_timestamp),
            expires_at=float(self.expires_at),
            reason=str(self.reason),
            strategy=str(self.strategy),
            idempotency_key=key,
        )


@dataclass(frozen=True)
class RiskPolicy:
    pilot_capital: float = 100_000.0
    max_order_value: float = 10_000.0
    max_single_position_value: float = 20_000.0
    max_daily_new_exposure: float = 30_000.0
    max_orders_per_day: int = 3
    max_quote_age_seconds: float = 15.0
    max_signal_age_seconds: float = 60.0
    max_limit_deviation_bps: float = 30.0
    daily_loss_stop: float = 1_000.0
    pilot_daily_loss_stop: float = 1_000.0
    require_board_lot_for_buy: bool = True
    allowed_symbols: Tuple[str, ...] = ()


@dataclass(frozen=True)
class DayRiskState:
    date: str
    start_equity: float
    current_equity: float
    new_exposure: float = 0.0
    submitted_orders: int = 0
    submitted_keys: Tuple[str, ...] = ()
    pilot_pnl: float = 0.0
    kill_switch: bool = False


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    status: str
    reasons: Tuple[str, ...]
    checks: Mapping[str, bool]
    order_value: float


class BrokerGateway(Protocol):
    def connect(self) -> Mapping[str, Any]: ...
    def capabilities(self) -> Mapping[str, bool]: ...
    def get_account(self) -> AccountSnapshot: ...
    def get_positions(self) -> Sequence[Position]: ...
    def get_orders(self) -> Sequence[Mapping[str, Any]]: ...
    def get_trades(self) -> Sequence[Mapping[str, Any]]: ...
    def get_quote(self, symbol: str) -> Quote: ...
    def close(self) -> None: ...


def in_continuous_session(now: datetime) -> bool:
    local = now.astimezone(SHANGHAI)
    current = local.time()
    morning = wall_time(9, 45) <= current < wall_time(11, 15)
    afternoon = wall_time(13, 15) <= current < wall_time(14, 30)
    return local.weekday() < 5 and (morning or afternoon)


def position_for(positions: Iterable[Position], symbol: str) -> Optional[Position]:
    target = qmt_code(symbol)
    for position in positions:
        if qmt_code(position.symbol) == target:
            return position
    return None


class OrderRiskGate:
    """Validate a limit-order intent using current broker truth."""

    def __init__(self, policy: RiskPolicy):
        self.policy = policy

    def evaluate(
        self,
        intent: OrderIntent,
        account: AccountSnapshot,
        positions: Sequence[Position],
        quote: Quote,
        state: DayRiskState,
        now: Optional[datetime] = None,
    ) -> RiskDecision:
        order = intent.normalized()
        moment = now or datetime.now(tz=SHANGHAI)
        epoch = moment.timestamp()
        order_value = order.quantity * order.limit_price
        existing = position_for(positions, order.symbol)
        existing_value = existing.market_value if existing else 0.0
        allowed = {qmt_code(item) for item in self.policy.allowed_symbols}
        checks: Dict[str, bool] = {
            "killSwitchClear": not state.kill_switch,
            "brokerConnected": account.connected,
            "continuousSession": in_continuous_session(moment),
            "sideValid": order.side in {"BUY", "SELL"},
            "positiveQuantity": order.quantity > 0,
            "positiveLimitPrice": order.limit_price > 0.0,
            "notExpired": epoch <= order.expires_at,
            "signalFresh": 0.0 <= epoch - order.signal_timestamp <= self.policy.max_signal_age_seconds,
            "quoteFresh": 0.0 <= epoch - quote.timestamp <= self.policy.max_quote_age_seconds,
            "symbolMatchesQuote": qmt_code(quote.symbol) == order.symbol,
            "notSuspended": not quote.suspended,
            "symbolAllowed": bool(allowed) and order.symbol in allowed,
            "uniqueIntent": order.idempotency_key not in set(state.submitted_keys),
            "dailyOrderCount": state.submitted_orders < self.policy.max_orders_per_day,
            "dailyLossClear": state.current_equity >= state.start_equity - self.policy.daily_loss_stop,
            "pilotLossClear": state.pilot_pnl > -self.policy.pilot_daily_loss_stop,
            "orderValueCap": 0.0 < order_value <= self.policy.max_order_value,
            "pilotCapitalCap": (
                order.side != "BUY"
                or state.new_exposure + order_value <= self.policy.pilot_capital
            ),
        }
        checks["buyBoardLot"] = (
            order.side != "BUY"
            or not self.policy.require_board_lot_for_buy
            or order.quantity % 100 == 0
        )
        checks["sufficientCash"] = order.side != "BUY" or order_value <= account.cash
        checks["sellableQuantity"] = (
            order.side != "SELL"
            or (existing is not None and order.quantity <= existing.sellable_quantity)
        )
        checks["singlePositionCap"] = (
            order.side != "BUY"
            or existing_value + order_value <= self.policy.max_single_position_value
        )
        checks["dailyExposureCap"] = (
            order.side != "BUY"
            or state.new_exposure + order_value <= self.policy.max_daily_new_exposure
        )
        reference_price = quote.ask_price if order.side == "BUY" else quote.bid_price
        if reference_price <= 0.0:
            reference_price = quote.last_price
        deviation_bps = (
            abs(order.limit_price / reference_price - 1.0) * 10_000.0
            if reference_price > 0.0
            else float("inf")
        )
        checks["limitNearMarket"] = deviation_bps <= self.policy.max_limit_deviation_bps
        checks["insidePriceLimits"] = (
            (quote.upper_limit <= 0.0 or order.limit_price <= quote.upper_limit)
            and (quote.lower_limit <= 0.0 or order.limit_price >= quote.lower_limit)
        )
        reasons = tuple(name for name, passed in checks.items() if not passed)
        approved = not reasons
        return RiskDecision(
            approved=approved,
            status="RISK_CHECK_PASSED" if approved else "BLOCKED",
            reasons=reasons,
            checks=checks,
            order_value=order_value,
        )

class QmtGateway:
    """Official MiniQMT/XtQuant gateway with explicit read-only default."""

    def __init__(
        self,
        userdata_path: Path,
        account_id: str,
        *,
        account_type: str = "STOCK",
        session_id: Optional[int] = None,
        audit_log: Optional[Path] = None,
    ) -> None:
        self.userdata_path = Path(userdata_path)
        self._account_id = str(account_id).strip()
        self.account_type = account_type
        self.session_id = int(session_id or time.time())
        self.audit_log = audit_log
        self._trader: Any = None
        self._account: Any = None
        self._xtconstant: Any = None
        self._xtdata: Any = None

    def capabilities(self) -> Mapping[str, bool]:
        return {
            "account": True,
            "positions": True,
            "orders": True,
            "trades": True,
            "quotes": True,
            "minuteBars": True,
            "liveOrders": False,
            "liveSubmissionDisabled": True,
            "cashTransfer": False,
            "marginTrading": False,
            "guiAutomation": False,
        }

    def connect(self) -> Mapping[str, Any]:
        if not self.userdata_path.is_dir():
            raise FileNotFoundError(f"MiniQMT userdata path not found: {self.userdata_path}")
        try:
            from xtquant import xtconstant, xtdata
            from xtquant.xttrader import XtQuantTrader
            from xtquant.xttype import StockAccount
        except ImportError as exc:
            raise RuntimeError(
                "xtquant is unavailable; install the broker-provided official package "
                "for this Python version"
            ) from exc
        self._xtconstant = xtconstant
        self._xtdata = xtdata
        self._account = StockAccount(self._account_id, self.account_type)
        self._trader = XtQuantTrader(str(self.userdata_path), self.session_id)
        self._trader.start()
        result = int(self._trader.connect())
        if result != 0:
            raise ConnectionError(f"MiniQMT connection failed with code {result}")
        subscribed = int(self._trader.subscribe(self._account))
        if subscribed != 0:
            raise ConnectionError(f"MiniQMT account subscription failed with code {subscribed}")
        return {
            "connected": True,
            "broker": "QMT",
            "account": mask_account(self._account_id),
            "sessionId": self.session_id,
            "readOnly": True,
        }

    def _require_connection(self) -> None:
        if self._trader is None or self._account is None:
            raise RuntimeError("QMT gateway is not connected")

    def get_account(self) -> AccountSnapshot:
        self._require_connection()
        value = self._trader.query_stock_asset(self._account)
        if value is None:
            raise RuntimeError("QMT asset query returned no result")
        return AccountSnapshot(
            broker="QMT",
            account_masked=mask_account(self._account_id),
            timestamp=time.time(),
            total_asset=finite(first_attr(value, ("total_asset", "m_dBalance"))),
            cash=finite(first_attr(value, ("cash", "m_dCash"))),
            market_value=finite(first_attr(value, ("market_value", "m_dMarketValue"))),
            connected=True,
        )

    def get_positions(self) -> Sequence[Position]:
        self._require_connection()
        values = self._trader.query_stock_positions(self._account) or []
        result: List[Position] = []
        for value in values:
            symbol = qmt_code(str(first_attr(value, ("stock_code", "m_strInstrumentID"), "")))
            result.append(Position(
                symbol=symbol,
                name=str(first_attr(value, ("stock_name", "instrument_name"), symbol)),
                total_quantity=int(finite(first_attr(value, ("volume", "m_nVolume")))),
                sellable_quantity=int(finite(first_attr(
                    value, ("can_use_volume", "can_use", "m_nCanUseVolume")
                ))),
                average_price=finite(first_attr(value, ("open_price", "avg_price", "m_dOpenPrice"))),
                last_price=finite(first_attr(value, ("last_price", "price", "m_dLastPrice"))),
                market_value=finite(first_attr(value, ("market_value", "m_dMarketValue"))),
            ))
        return result

    @staticmethod
    def _public_record(value: Any) -> Mapping[str, Any]:
        fields = (
            "stock_code", "order_id", "order_sysid", "order_time", "order_type",
            "order_volume", "price", "order_status", "traded_volume", "traded_price",
        )
        return {name: getattr(value, name) for name in fields if hasattr(value, name)}

    def get_orders(self) -> Sequence[Mapping[str, Any]]:
        self._require_connection()
        return [self._public_record(value) for value in (self._trader.query_stock_orders(self._account) or [])]

    def get_trades(self) -> Sequence[Mapping[str, Any]]:
        self._require_connection()
        return [self._public_record(value) for value in (self._trader.query_stock_trades(self._account) or [])]

    def get_quote(self, symbol: str) -> Quote:
        self._require_connection()
        code = qmt_code(symbol)
        values = self._xtdata.get_full_tick([code]) or {}
        value = values.get(code)
        if not value:
            raise RuntimeError(f"QMT quote query returned no result for {code}")
        ask = first_attr(value, ("askPrice", "ask_price"), []) or []
        bid = first_attr(value, ("bidPrice", "bid_price"), []) or []
        timestamp_value = finite(first_attr(value, ("time", "timetag", "timestamp")), 0.0)
        if timestamp_value <= 0.0:
            raise RuntimeError(f"QMT quote has no trustworthy timestamp for {code}")
        if timestamp_value > 10_000_000_000:
            timestamp_value /= 1000.0
        return Quote(
            symbol=code,
            timestamp=timestamp_value,
            last_price=finite(first_attr(value, ("lastPrice", "last_price"))),
            previous_close=finite(first_attr(value, ("lastClose", "preClose", "previous_close"))),
            bid_price=finite(bid[0] if bid else 0.0),
            ask_price=finite(ask[0] if ask else 0.0),
            upper_limit=finite(first_attr(value, ("upperLimit", "upStopPrice"))),
            lower_limit=finite(first_attr(value, ("lowerLimit", "downStopPrice"))),
            suspended=bool(first_attr(value, ("suspended",), False)),
        )

    def get_bars(self, symbols: Sequence[str], period: str = "5m", count: int = 300) -> Mapping[str, List[Dict[str, Any]]]:
        self._require_connection()
        if period not in {"1m", "5m", "15m"}:
            raise ValueError("QMT live bars support only 1m, 5m, or 15m here")
        codes = [qmt_code(symbol) for symbol in symbols]
        subscriptions: List[int] = []
        try:
            for code in codes:
                subscription = int(self._xtdata.subscribe_quote(code, period=period, count=0))
                if subscription < 0:
                    raise RuntimeError(f"QMT quote subscription failed for {code}")
                subscriptions.append(subscription)
            time.sleep(0.35)
            values = self._xtdata.get_market_data_ex(
                field_list=["time", "open", "high", "low", "close", "volume", "amount"],
                stock_list=codes,
                period=period,
                start_time="",
                end_time="",
                count=int(count),
                dividend_type="none",
                fill_data=False,
            ) or {}
        finally:
            for subscription in subscriptions:
                try:
                    self._xtdata.unsubscribe_quote(subscription)
                except Exception:
                    pass
        result: Dict[str, List[Dict[str, Any]]] = {}
        for code in codes:
            frame = values.get(code)
            if frame is None:
                result[bare_code(code)] = []
                continue
            records: List[Dict[str, Any]] = []
            for index, row in frame.iterrows():
                item = row.to_dict() if hasattr(row, "to_dict") else dict(row)
                item.setdefault("time", index)
                records.append(item)
            result[bare_code(code)] = records
        return result

    def close(self) -> None:
        if self._trader is not None:
            try:
                if self._account is not None and hasattr(self._trader, "unsubscribe"):
                    self._trader.unsubscribe(self._account)
            except Exception:
                pass
            try:
                if hasattr(self._trader, "stop"):
                    self._trader.stop()
            except Exception:
                pass
        self._trader = None
        self._account = None
