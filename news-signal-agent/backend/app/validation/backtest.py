from __future__ import annotations

from datetime import datetime
from typing import Any

from app.collectors.quotes import fetch_daily_bars
from app.models import SignalItem, WatchSymbol


WINDOWS = (1, 3, 5, 10, 20)


def validate_signals_with_event_study(
    signals: list[SignalItem],
    watchlist: list[WatchSymbol],
    benchmark_symbol: str | None = None,
    timeout_seconds: float = 8,
) -> dict[str, dict[str, Any]]:
    symbols = {item.symbol: item for item in watchlist}
    cache: dict[str, list[dict[str, Any]]] = {}
    benchmark_bars = _benchmark_bars(watchlist, benchmark_symbol, timeout_seconds)
    result: dict[str, dict[str, Any]] = {}
    for signal in signals:
        symbol = symbols.get(signal.symbol)
        if not symbol:
            continue
        if signal.symbol not in cache:
            try:
                cache[signal.symbol] = fetch_daily_bars(symbol, days=160, timeout_seconds=timeout_seconds)
            except Exception:
                cache[signal.symbol] = []
        result[signal.id] = _event_study(signal, cache[signal.symbol], benchmark_bars)
    return result


def _event_study(
    signal: SignalItem,
    bars: list[dict[str, Any]],
    benchmark_bars: list[dict[str, Any]],
) -> dict[str, Any]:
    if len(bars) < 25:
        return {"status": "not_enough_market_history", "windows": {}}
    event_date = _event_date(signal)
    index = _locate_event_index(bars, event_date)
    if index is None:
        index = len(bars) - 1
    start_close = bars[index].get("close")
    if not start_close:
        return {"status": "missing_event_close", "windows": {}}
    windows: dict[str, Any] = {}
    for window in WINDOWS:
        target_index = min(index + window, len(bars) - 1)
        close = bars[target_index].get("close")
        if close:
            raw_return = (float(close) / float(start_close) - 1) * 100
            benchmark_return = _benchmark_return(benchmark_bars, bars[index].get("date"), window)
            abnormal_return = raw_return - benchmark_return if benchmark_return is not None else None
            windows[f"T+{window}"] = {
                "raw_return": round(raw_return, 4),
                "benchmark_return": round(benchmark_return, 4) if benchmark_return is not None else None,
                "abnormal_return": round(abnormal_return, 4) if abnormal_return is not None else None,
                "car": round(abnormal_return, 4) if abnormal_return is not None else None,
            }
    return {
        "status": "ok",
        "event_date": bars[index].get("date"),
        "windows": windows,
        "method": "event_study_abnormal_return_v1",
    }


def _event_date(signal: SignalItem) -> str | None:
    value = signal.metadata.get("event_last_seen_at") or signal.metadata.get("published_at")
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _locate_event_index(bars: list[dict[str, Any]], event_date: str | None) -> int | None:
    if not event_date:
        return None
    for index, bar in enumerate(bars):
        if str(bar.get("date")) >= event_date:
            return index
    return None


def _benchmark_bars(
    watchlist: list[WatchSymbol],
    benchmark_symbol: str | None,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    if not benchmark_symbol:
        return []
    symbol = next((item for item in watchlist if item.symbol == benchmark_symbol), None)
    if symbol is None:
        market = "SH" if benchmark_symbol.startswith(("5", "6", "9")) else "SZ"
        symbol = WatchSymbol(symbol=benchmark_symbol, market=market)
    try:
        return fetch_daily_bars(symbol, days=180, timeout_seconds=timeout_seconds)
    except Exception:
        return []


def _benchmark_return(
    bars: list[dict[str, Any]],
    event_date: str | None,
    window: int,
) -> float | None:
    if len(bars) < window + 2:
        return None
    index = _locate_event_index(bars, str(event_date) if event_date else None)
    if index is None:
        return None
    start_close = bars[index].get("close")
    target_close = bars[min(index + window, len(bars) - 1)].get("close")
    if not start_close or not target_close:
        return None
    return (float(target_close) / float(start_close) - 1) * 100
