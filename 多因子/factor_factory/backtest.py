"""Long-only event-style A-share portfolio backtest."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any

import numpy as np

from .schema import PanelData


@dataclass(frozen=True)
class CostModel:
    commission_bps: float = 2.5
    minimum_commission: float = 5.0
    sell_stamp_tax_bps: float = 5.0
    slippage_bps: float = 5.0
    lot_size: int = 100
    minimum_trade_notional: float = 1_000.0

    def __post_init__(self) -> None:
        if isinstance(self.lot_size, bool) or not isinstance(self.lot_size, int) or self.lot_size < 1:
            raise ValueError("lot_size must be a positive integer")
        for name in (
            "commission_bps", "minimum_commission", "sell_stamp_tax_bps",
            "slippage_bps", "minimum_trade_notional",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True)
class BacktestResult:
    start_date: str
    end_date: str
    total_return: float
    annualized_return: float
    annualized_volatility: float
    sharpe: float
    max_drawdown: float
    average_turnover: float
    trade_count: int
    blocked_buy_count: int
    blocked_sell_count: int
    unaffordable_lot_count: int
    skipped_small_trade_count: int
    delisting_writeoff_count: int
    delisting_writeoff_value: float
    data_invalidation_writeoff_count: int
    data_invalidation_writeoff_value: float
    ending_cash: float
    ending_equity: float
    equity_curve: tuple[float, ...]
    daily_records: tuple[dict[str, Any], ...]

    def to_dict(self, include_curve: bool = True) -> dict[str, Any]:
        result = asdict(self)
        if not include_curve:
            result.pop("equity_curve", None)
            result.pop("daily_records", None)
        return result


def _max_drawdown(equity: np.ndarray) -> float:
    peaks = np.maximum.accumulate(equity)
    return float(np.min(equity / np.maximum(peaks, 1e-12) - 1.0))


def run_a_share_backtest(
    panel: PanelData,
    factor_values: np.ndarray,
    *,
    start_index: int,
    end_index: int,
    rebalance_every: int = 5,
    execution_phase: int | None = None,
    top_k: int = 10,
    retention_k: int | None = None,
    weighting: str = "equal",
    volatility_lookback: int = 20,
    initial_cash: float = 100_000.0,
    cost: CostModel | None = None,
    risk_on_mask: np.ndarray | None = None,
) -> BacktestResult:
    """Execute signals one session later with T+1 and tradability masks.

    The engine is intentionally conservative: no leverage, no short selling,
    integer lots, sell-side stamp tax, commission minimum, and blocked orders
    when historical buy/sell masks say the asset was not tradable.
    """

    if rebalance_every < 1:
        raise ValueError("rebalance_every must be positive")
    if execution_phase is not None and not (0 <= execution_phase < rebalance_every):
        raise ValueError("execution_phase must be in [0, rebalance_every)")
    if not (0 <= start_index < end_index < len(panel.dates)):
        raise ValueError("invalid backtest date range")
    if top_k < 1:
        raise ValueError("top_k must be positive")
    if retention_k is not None and (
        isinstance(retention_k, bool)
        or not isinstance(retention_k, int)
        or retention_k < top_k
    ):
        raise ValueError("retention_k must be an integer greater than or equal to top_k")
    if weighting not in {"equal", "inverse_volatility"}:
        raise ValueError("weighting must be 'equal' or 'inverse_volatility'")
    if (
        isinstance(volatility_lookback, bool)
        or not isinstance(volatility_lookback, int)
        or volatility_lookback < 2
    ):
        raise ValueError("volatility_lookback must be an integer >= 2")
    if initial_cash <= 0.0 or not np.isfinite(initial_cash):
        raise ValueError("initial_cash must be finite and positive")
    values = np.asarray(factor_values)
    if values.shape != panel.shape:
        raise ValueError("factor values must match panel shape")
    if risk_on_mask is None:
        risk_on = np.ones(len(panel.dates), dtype=bool)
    else:
        raw_risk_on = np.asarray(risk_on_mask)
        if raw_risk_on.dtype.kind != "b":
            raise ValueError("risk_on_mask must have boolean dtype; missing/numeric states are forbidden")
        risk_on = raw_risk_on.astype(bool, copy=False)
        if risk_on.shape != (len(panel.dates),):
            raise ValueError("risk_on_mask must have one boolean per panel date")
    model = cost or CostModel()
    if model.lot_size < 1:
        raise ValueError("lot_size must be a positive integer")
    if any(
        not np.isfinite(value) or value < 0.0
        for value in (
            model.commission_bps,
            model.minimum_commission,
            model.sell_stamp_tax_bps,
            model.slippage_bps,
            model.minimum_trade_notional,
        )
    ):
        raise ValueError("all transaction costs must be finite and non-negative")
    if panel.metadata.get("price_scale") == "Qlib normalized adjusted prices" and model.lot_size != 1:
        raise ValueError(
            "Qlib normalized prices cannot support integer-lot simulation; "
            "use lot_size=1 for a weight backtest"
        )
    opens = panel.fields["open"]
    closes = panel.fields["close"]
    commission_rate = model.commission_bps / 10_000.0
    stamp_rate = model.sell_stamp_tax_bps / 10_000.0
    slippage = model.slippage_bps / 10_000.0

    cash = float(initial_cash)
    quantity = np.zeros(len(panel.symbols), dtype=np.int64)
    entry_date = np.full(len(panel.symbols), -1, dtype=np.int64)
    equity_curve: list[float] = []
    records: list[dict[str, Any]] = []
    turnover_values: list[float] = []
    trade_count = 0
    blocked_buy = 0
    blocked_sell = 0
    unaffordable_lot_count = 0
    skipped_small_trade_count = 0
    delisting_writeoff_count = 0
    delisting_writeoff_value = 0.0
    data_invalidation_writeoff_count = 0
    data_invalidation_writeoff_value = 0.0
    liquidation_pending = False
    last_valuation = np.full(len(panel.symbols), np.nan, dtype=np.float64)
    for historical in range(0, start_index):
        valid = np.isfinite(closes[historical]) & (closes[historical] > 0.0)
        last_valuation[valid] = closes[historical, valid]

    for date in range(start_index, end_index + 1):
        blocked_sell_at_open = blocked_sell
        traded_value = 0.0
        commission_paid = 0.0
        stamp_tax_paid = 0.0
        slippage_cost = 0.0
        terminal_assets = np.flatnonzero((quantity > 0) & panel.delisted[date])
        for asset in terminal_assets:
            mark = last_valuation[asset]
            if np.isfinite(mark) and mark > 0.0:
                delisting_writeoff_value += float(quantity[asset]) * float(mark)
            quantity[asset] = 0
            entry_date[asset] = -1
            delisting_writeoff_count += 1
        invalid_assets = np.flatnonzero((quantity > 0) & panel.invalidated[date])
        for asset in invalid_assets:
            mark = last_valuation[asset]
            if np.isfinite(mark) and mark > 0.0:
                data_invalidation_writeoff_value += float(quantity[asset]) * float(mark)
            quantity[asset] = 0
            entry_date[asset] = -1
            data_invalidation_writeoff_count += 1
        if liquidation_pending and not np.any(quantity > 0):
            liquidation_pending = False
        if execution_phase is None:
            scheduled_execution = date > start_index and (date - 1 - start_index) % rebalance_every == 0
        else:
            scheduled_execution = date > start_index and (date - 1) % rebalance_every == execution_phase
        signal_risk_on = bool(risk_on[date - 1]) if date > 0 else False
        if date > start_index and not signal_risk_on and bool(np.any(quantity > 0)):
            liquidation_pending = True
        forced_risk_exit = date > start_index and liquidation_pending and bool(np.any(quantity > 0))
        is_execution_day = scheduled_execution or forced_risk_exit
        targets: set[int] = set()
        target_weights: dict[int, float] = {}
        if is_execution_day:
            signal = values[date - 1]
            # Once a risk-off liquidation starts, blocked positions remain in a
            # fail-closed liquidation state until every share is sold.  A later
            # risk-on close cannot silently cancel the pending exit; re-entry
            # waits for a future scheduled rebalance after liquidation completes.
            if scheduled_execution and signal_risk_on and not liquidation_pending:
                candidates = np.flatnonzero(
                    np.isfinite(signal) & panel.eligible[date - 1]
                )
                if len(candidates):
                    order = candidates[np.argsort(signal[candidates], kind="stable")]
                    target_count = min(top_k, len(order))
                    retention_count = min(
                        len(order), retention_k if retention_k is not None else top_k
                    )
                    retained_pool = set(
                        int(index) for index in order[-retention_count:]
                    )
                    targets = {
                        int(index)
                        for index in np.flatnonzero(quantity > 0)
                        if int(index) in retained_pool
                    }
                    for index in order[::-1]:
                        if len(targets) >= target_count:
                            break
                        targets.add(int(index))

            blocked_non_target_value = 0.0
            open_marks = np.where(
                np.isfinite(opens[date]) & (opens[date] > 0.0), opens[date], last_valuation
            )
            if np.any((quantity > 0) & ~np.isfinite(open_marks)):
                raise ValueError("held position has no valid opening valuation")
            for asset in np.flatnonzero(quantity > 0):
                if int(asset) in targets:
                    continue
                if entry_date[asset] >= date:
                    blocked_sell += 1
                    blocked_non_target_value += float(quantity[asset]) * float(open_marks[asset])
                    continue
                if not panel.sellable[date, asset]:
                    blocked_sell += 1
                    blocked_non_target_value += float(quantity[asset]) * float(open_marks[asset])
                    continue
                price = opens[date, asset] * (1.0 - slippage)
                gross = float(quantity[asset]) * price
                commission = max(model.minimum_commission, gross * commission_rate)
                tax = gross * stamp_rate
                fee = commission + tax
                if gross <= fee:
                    blocked_sell += 1
                    blocked_non_target_value += float(quantity[asset]) * float(open_marks[asset])
                    continue
                cash += gross - fee
                commission_paid += commission
                stamp_tax_paid += tax
                slippage_cost += float(quantity[asset]) * float(opens[date, asset]) - gross
                traded_value += gross
                quantity[asset] = 0
                entry_date[asset] = -1
                trade_count += 1

            open_equity = cash + float(np.sum(quantity * np.nan_to_num(open_marks, nan=0.0)))
            allocatable_equity = max(0.0, open_equity - blocked_non_target_value)
            if targets:
                ordered_targets = sorted(targets)
                if weighting == "inverse_volatility":
                    signal_date = date - 1
                    history_start = max(0, signal_date - volatility_lookback)
                    history = np.asarray(
                        closes[history_start : signal_date + 1, ordered_targets],
                        dtype=np.float64,
                    )
                    previous = history[:-1]
                    current = history[1:]
                    valid_returns = (
                        np.isfinite(previous) & (previous > 0.0)
                        & np.isfinite(current) & (current > 0.0)
                    )
                    returns = np.where(
                        valid_returns, current / np.maximum(previous, 1e-12) - 1.0, np.nan
                    )
                    valid_counts = np.sum(np.isfinite(returns), axis=0)
                    volatility = np.nanstd(returns, axis=0, ddof=1)
                    usable = (
                        (valid_counts >= max(2, int(np.ceil(volatility_lookback * 0.60))))
                        & np.isfinite(volatility)
                        & (volatility > 1e-6)
                    )
                    if np.any(usable):
                        fallback = float(np.median(volatility[usable]))
                        safe_volatility = np.where(usable, volatility, fallback)
                        raw_weights = 1.0 / np.maximum(safe_volatility, 1e-6)
                    else:
                        raw_weights = np.ones(len(ordered_targets), dtype=np.float64)
                else:
                    raw_weights = np.ones(len(ordered_targets), dtype=np.float64)
                normalized_weights = raw_weights / np.sum(raw_weights)
                target_weights = {
                    asset: float(normalized_weights[index])
                    for index, asset in enumerate(ordered_targets)
                }
            desired_values = {
                asset: allocatable_equity * target_weights.get(asset, 0.0)
                for asset in targets
            }

            # Trim target names that drifted above equal weight before funding
            # deficits.  Without this step the engine is not an equal-weight
            # top-k strategy and embeds winner-drift path dependence.
            for asset in sorted(targets):
                if quantity[asset] <= 0:
                    continue
                marked_value = float(quantity[asset]) * float(open_marks[asset])
                excess = marked_value - desired_values[asset]
                if excess <= 0.0:
                    continue
                if entry_date[asset] >= date or not panel.sellable[date, asset]:
                    blocked_sell += 1
                    continue
                price = opens[date, asset] * (1.0 - slippage)
                shares = min(
                    int(quantity[asset]),
                    int(excess // (price * model.lot_size)) * model.lot_size,
                )
                if shares <= 0:
                    continue
                gross = float(shares) * price
                commission = max(model.minimum_commission, gross * commission_rate)
                tax = gross * stamp_rate
                fee = commission + tax
                if gross < model.minimum_trade_notional or gross <= fee:
                    skipped_small_trade_count += 1
                    continue
                cash += gross - fee
                commission_paid += commission
                stamp_tax_paid += tax
                slippage_cost += float(shares) * float(opens[date, asset]) - gross
                traded_value += gross
                quantity[asset] -= shares
                if quantity[asset] == 0:
                    entry_date[asset] = -1
                trade_count += 1

            for asset in sorted(targets):
                if not panel.buyable[date, asset]:
                    blocked_buy += 1
                    continue
                price = opens[date, asset] * (1.0 + slippage)
                current_value = float(quantity[asset]) * price
                budget = min(max(0.0, desired_values[asset] - current_value), cash)
                lots = int(budget // (price * model.lot_size))
                shares = lots * model.lot_size
                while shares > 0:
                    gross = shares * price
                    fee = max(model.minimum_commission, gross * commission_rate)
                    if gross + fee <= cash + 1e-9:
                        break
                    shares -= model.lot_size
                if shares <= 0:
                    if budget > 0.0:
                        unaffordable_lot_count += 1
                    continue
                gross = shares * price
                fee = max(model.minimum_commission, gross * commission_rate)
                if gross < model.minimum_trade_notional:
                    skipped_small_trade_count += 1
                    continue
                cash -= gross + fee
                commission_paid += fee
                slippage_cost += gross - float(shares) * float(opens[date, asset])
                if cash < -1e-8:
                    raise RuntimeError("backtest cash became negative")
                cash = max(0.0, cash)
                traded_value += gross
                if quantity[asset] == 0:
                    entry_date[asset] = date
                quantity[asset] += shares
                trade_count += 1

            if liquidation_pending and not np.any(quantity > 0):
                liquidation_pending = False

        valid_close = np.isfinite(closes[date]) & (closes[date] > 0.0)
        last_valuation[valid_close] = closes[date, valid_close]
        if np.any((quantity > 0) & ~np.isfinite(last_valuation)):
            raise ValueError("held position has no valid closing valuation")
        close_equity = cash + float(np.sum(quantity * np.nan_to_num(last_valuation, nan=0.0)))
        if cash < -1e-8 or not np.isfinite(close_equity):
            raise RuntimeError("backtest produced invalid cash or equity")
        reference_equity = equity_curve[-1] if equity_curve else initial_cash
        turnover = traded_value / max(reference_equity, 1e-12)
        turnover_values.append(turnover)
        equity_curve.append(close_equity)
        records.append({
            "date": panel.dates[date],
            "equity": close_equity,
            "cash": cash,
            "turnover": turnover,
            "traded_value": traded_value,
            "commission": commission_paid,
            "stamp_tax": stamp_tax_paid,
            "slippage_cost": slippage_cost,
            "gross_exposure": (
                float(np.sum(quantity * np.nan_to_num(last_valuation, nan=0.0)))
                / max(close_equity, 1e-12)
            ),
            "positions": int(np.sum(quantity > 0)),
            "portfolio_weighting": weighting,
            "target_weights": {
                panel.symbols[asset]: weight for asset, weight in target_weights.items()
            },
            "risk_on_signal": signal_risk_on,
            "scheduled_execution": bool(scheduled_execution),
            "forced_risk_exit": bool(forced_risk_exit),
            "liquidation_pending": bool(liquidation_pending),
            "blocked_sells_today": int(blocked_sell - blocked_sell_at_open),
            "delisting_writeoffs": delisting_writeoff_count,
            "data_invalidation_writeoffs": data_invalidation_writeoff_count,
        })

    equity = np.asarray(equity_curve, dtype=np.float64)
    daily_returns = np.zeros(len(equity), dtype=np.float64)
    if len(equity) > 1:
        previous = equity[:-1]
        current = equity[1:]
        positive = previous > 0.0
        period_returns = np.zeros(len(previous), dtype=np.float64)
        period_returns[positive] = current[positive] / previous[positive] - 1.0
        daily_returns[1:] = period_returns
    total_return = float(equity[-1] / initial_cash - 1.0)
    periods = max(1, len(equity) - 1)
    annualized_return = float((equity[-1] / initial_cash) ** (252.0 / periods) - 1.0)
    volatility = float(np.std(daily_returns[1:], ddof=1) * np.sqrt(252.0)) if len(equity) > 2 else 0.0
    sharpe = float(np.mean(daily_returns[1:]) / np.std(daily_returns[1:], ddof=1) * np.sqrt(252.0)) if volatility > 1e-12 else 0.0
    return BacktestResult(
        start_date=panel.dates[start_index],
        end_date=panel.dates[end_index],
        total_return=total_return,
        annualized_return=annualized_return,
        annualized_volatility=volatility,
        sharpe=sharpe,
        max_drawdown=_max_drawdown(equity),
        average_turnover=float(np.mean(turnover_values)),
        trade_count=trade_count,
        blocked_buy_count=blocked_buy,
        blocked_sell_count=blocked_sell,
        unaffordable_lot_count=unaffordable_lot_count,
        skipped_small_trade_count=skipped_small_trade_count,
        delisting_writeoff_count=delisting_writeoff_count,
        delisting_writeoff_value=delisting_writeoff_value,
        data_invalidation_writeoff_count=data_invalidation_writeoff_count,
        data_invalidation_writeoff_value=data_invalidation_writeoff_value,
        ending_cash=cash,
        ending_equity=float(equity[-1]),
        equity_curve=tuple(float(value) for value in equity),
        daily_records=tuple(records),
    )
