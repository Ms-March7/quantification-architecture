from __future__ import annotations

import json
from typing import Any

import httpx

from app.models import WatchSymbol


EASTMONEY_QUOTE_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
EASTMONEY_TRENDS_URL = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
SINA_QUOTE_URL = "https://hq.sinajs.cn/list={symbols}"
SINA_INTRADAY_URL = (
    "https://quotes.sina.cn/cn/api/jsonp_v2.php/=/"
    "CN_MarketDataService.getKLineData"
)


def fetch_eastmoney_quotes(
    watchlist: list[WatchSymbol],
    timeout_seconds: float = 8,
) -> dict[str, dict[str, Any]]:
    try:
        return _fetch_eastmoney_quotes(watchlist, timeout_seconds=timeout_seconds)
    except Exception:
        return _fetch_sina_quotes(watchlist, timeout_seconds=timeout_seconds)


def fetch_intraday_trends(
    symbol: WatchSymbol,
    timeout_seconds: float = 8,
) -> dict[str, Any]:
    eastmoney_result = _fetch_eastmoney_intraday_trends(symbol, timeout_seconds=timeout_seconds)
    if eastmoney_result.get("bars"):
        return eastmoney_result

    sina_result = _fetch_sina_intraday_trends(symbol, timeout_seconds=timeout_seconds)
    if sina_result.get("bars"):
        if eastmoney_result.get("error"):
            sina_result["warning"] = f"eastmoney_failed: {eastmoney_result['error']}"
        return sina_result

    if eastmoney_result.get("error") and sina_result.get("error"):
        eastmoney_result["fallback_error"] = sina_result["error"]
    return eastmoney_result


def _fetch_eastmoney_intraday_trends(
    symbol: WatchSymbol,
    timeout_seconds: float = 8,
) -> dict[str, Any]:
    secid = _secid(symbol)
    if not secid:
        return {"symbol": symbol.symbol, "bars": [], "source": "eastmoney_trends"}
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "iscr": "0",
        "iscca": "0",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
            response = client.get(EASTMONEY_TRENDS_URL, params=params)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        return {
            "symbol": symbol.symbol,
            "name": symbol.name,
            "bars": [],
            "source": "eastmoney_trends",
            "error": f"{type(exc).__name__}: {exc}",
        }
    data = payload.get("data") or {}
    bars = [_parse_trend_row(row) for row in data.get("trends") or []]
    return {
        "symbol": symbol.symbol,
        "name": data.get("name") or symbol.name,
        "bars": [bar for bar in bars if bar],
        "source": "eastmoney_trends",
    }


def _fetch_sina_intraday_trends(
    symbol: WatchSymbol,
    timeout_seconds: float = 8,
) -> dict[str, Any]:
    sina_symbol = _sina_symbol(symbol)
    if not sina_symbol:
        return {"symbol": symbol.symbol, "name": symbol.name, "bars": [], "source": "sina_intraday"}
    params = {
        "symbol": sina_symbol,
        "scale": "1",
        "ma": "no",
        "datalen": "240",
    }
    headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
            response = client.get(SINA_INTRADAY_URL, params=params)
            response.raise_for_status()
        rows = _parse_sina_intraday_payload(response.text)
    except Exception as exc:
        return {
            "symbol": symbol.symbol,
            "name": symbol.name,
            "bars": [],
            "source": "sina_intraday",
            "error": f"{type(exc).__name__}: {exc}",
        }
    bars = _parse_sina_intraday_rows(rows)
    return {
        "symbol": symbol.symbol,
        "name": symbol.name,
        "bars": bars,
        "source": "sina_intraday_1m",
    }


def fetch_daily_bars(
    symbol: WatchSymbol,
    days: int = 120,
    timeout_seconds: float = 8,
) -> list[dict[str, Any]]:
    secid = _secid(symbol)
    if not secid:
        return []
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "beg": "0",
        "end": "20500101",
        "lmt": str(days),
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
            response = client.get(EASTMONEY_KLINE_URL, params=params)
            response.raise_for_status()
            payload = response.json()
    except Exception:
        return []
    klines = ((payload.get("data") or {}).get("klines") or [])
    return [bar for row in klines if (bar := _parse_kline_row(row))]


def _fetch_eastmoney_quotes(
    watchlist: list[WatchSymbol],
    timeout_seconds: float = 8,
) -> dict[str, dict[str, Any]]:
    secids = [_secid(item) for item in watchlist if _secid(item)]
    if not secids:
        return {}
    params = {
        "secids": ",".join(secids),
        "fields": "f1,f2,f3,f4,f5,f6,f12,f13,f14,f15,f16,f17,f18,f152",
        "fltt": "2",
        "invt": "2",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
        response = client.get(EASTMONEY_QUOTE_URL, params=params)
        response.raise_for_status()
        payload = response.json()
    rows = ((payload.get("data") or {}).get("diff") or [])
    quotes: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("f12") or "")
        if not symbol:
            continue
        quotes[symbol] = {
            "symbol": symbol,
            "name": row.get("f14"),
            "market": row.get("f13"),
            "price": _num(row.get("f2")),
            "pct_change": _num(row.get("f3")),
            "change": _num(row.get("f4")),
            "volume": _num(row.get("f5")),
            "amount": _num(row.get("f6")),
            "high": _num(row.get("f15")),
            "low": _num(row.get("f16")),
            "open": _num(row.get("f17")),
            "previous_close": _num(row.get("f18")),
            "source": "eastmoney_quote",
        }
    return quotes


def _fetch_sina_quotes(
    watchlist: list[WatchSymbol],
    timeout_seconds: float = 8,
) -> dict[str, dict[str, Any]]:
    symbols = [_sina_symbol(item) for item in watchlist if _sina_symbol(item)]
    if not symbols:
        return {}
    headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
        response = client.get(SINA_QUOTE_URL.format(symbols=",".join(symbols)))
        response.raise_for_status()
    text = response.content.decode("gbk", errors="ignore")
    quotes: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        parsed = _parse_sina_line(line)
        if parsed:
            quotes[parsed["symbol"]] = parsed
    return quotes


def _secid(item: WatchSymbol) -> str | None:
    market = (item.market or "").upper()
    if market == "SH":
        return f"1.{item.symbol}"
    if market == "SZ":
        return f"0.{item.symbol}"
    if item.symbol.startswith(("5", "6", "9")):
        return f"1.{item.symbol}"
    if item.symbol.startswith(("0", "1", "2", "3")):
        return f"0.{item.symbol}"
    return None


def _sina_symbol(item: WatchSymbol) -> str | None:
    market = (item.market or "").upper()
    if market == "SH":
        return f"sh{item.symbol}"
    if market == "SZ":
        return f"sz{item.symbol}"
    if item.symbol.startswith(("5", "6", "9")):
        return f"sh{item.symbol}"
    if item.symbol.startswith(("0", "1", "2", "3")):
        return f"sz{item.symbol}"
    return None


def _parse_sina_line(line: str) -> dict[str, Any] | None:
    if '="' not in line:
        return None
    prefix, body = line.split('="', 1)
    symbol = prefix.rsplit("_", 1)[-1]
    symbol = symbol[2:] if symbol.startswith(("sh", "sz")) else symbol
    fields = body.rstrip('";').split(",")
    if len(fields) < 10 or not fields[0]:
        return None
    open_price = _num(fields[1])
    previous_close = _num(fields[2])
    price = _num(fields[3])
    high = _num(fields[4])
    low = _num(fields[5])
    volume = _num(fields[8])
    amount = _num(fields[9])
    pct_change = None
    change = None
    if price is not None and previous_close:
        change = price - previous_close
        pct_change = change / previous_close * 100
    return {
        "symbol": symbol,
        "name": fields[0],
        "price": price,
        "pct_change": round(pct_change, 4) if pct_change is not None else None,
        "change": round(change, 4) if change is not None else None,
        "volume": volume,
        "amount": amount,
        "high": high,
        "low": low,
        "open": open_price,
        "previous_close": previous_close,
        "quote_time": f"{fields[30]} {fields[31]}" if len(fields) > 31 else None,
        "source": "sina_quote",
    }


def _parse_sina_intraday_payload(text: str) -> list[dict[str, Any]]:
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return []
    payload = json.loads(text[start : end + 1])
    return payload if isinstance(payload, list) else []


def _parse_sina_intraday_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    cumulative_amount = 0.0
    cumulative_volume = 0.0
    for row in rows:
        price = _num(row.get("close"))
        volume = _num(row.get("volume")) or 0.0
        amount = _num(row.get("amount")) or 0.0
        cumulative_volume += volume
        cumulative_amount += amount
        avg_price = None
        if cumulative_volume > 0 and cumulative_amount > 0:
            avg_price = cumulative_amount / cumulative_volume
        bars.append(
            {
                "time": row.get("day"),
                "price": price,
                "avg_price": round(avg_price, 4) if avg_price is not None else None,
                "volume": volume,
                "amount": amount,
                "open": _num(row.get("open")),
                "high": _num(row.get("high")),
                "low": _num(row.get("low")),
            }
        )
    return [bar for bar in bars if bar.get("price") is not None]


def _parse_trend_row(row: str) -> dict[str, Any] | None:
    fields = str(row).split(",")
    if len(fields) < 6:
        return None
    return {
        "time": fields[0],
        "price": _num(fields[1]),
        "avg_price": _num(fields[2]),
        "volume": _num(fields[5]),
        "amount": _num(fields[6]) if len(fields) > 6 else None,
    }


def _parse_kline_row(row: str) -> dict[str, Any] | None:
    fields = str(row).split(",")
    if len(fields) < 11:
        return None
    return {
        "date": fields[0],
        "open": _num(fields[1]),
        "close": _num(fields[2]),
        "high": _num(fields[3]),
        "low": _num(fields[4]),
        "volume": _num(fields[5]),
        "amount": _num(fields[6]),
        "amplitude": _num(fields[7]),
        "pct_change": _num(fields[8]),
        "change": _num(fields[9]),
        "turnover": _num(fields[10]),
    }


def _num(value) -> float | None:
    if value in (None, "-", ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
