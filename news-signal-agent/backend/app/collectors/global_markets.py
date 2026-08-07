from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from urllib.parse import quote

import httpx

from app.models import NewsItem


YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"


def collect_global_market_moves(
    instruments: list[dict[str, Any]],
    source_name: str,
    threshold_pct: float = 1.0,
    timeout_seconds: float = 8,
) -> tuple[list[NewsItem], list[str]]:
    items: list[NewsItem] = []
    warnings: list[str] = []
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
        for instrument in instruments:
            if not instrument.get("enabled", True):
                continue
            try:
                move = _fetch_yahoo_move(client, str(instrument["ticker"]))
            except Exception as exc:
                warnings.append(f"{source_name}:{instrument.get('ticker')}: {type(exc).__name__}: {exc}")
                continue
            if move is None:
                continue
            if abs(float(move["pct_change"])) < threshold_pct and not instrument.get("always_emit", False):
                continue
            items.append(_move_to_news(move, instrument, source_name))
    return items, warnings


def _fetch_yahoo_move(client: httpx.Client, ticker: str) -> dict[str, Any] | None:
    url = YAHOO_CHART_URL.format(ticker=quote(ticker, safe=""))
    response = client.get(url, params={"range": "5d", "interval": "1d"})
    response.raise_for_status()
    result = ((response.json().get("chart") or {}).get("result") or [])
    if not result:
        return None
    quote_rows = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
    closes = [value for value in quote_rows.get("close") or [] if value]
    timestamps = result[0].get("timestamp") or []
    if len(closes) < 2:
        return None
    previous = float(closes[-2])
    latest = float(closes[-1])
    if previous == 0:
        return None
    pct_change = (latest / previous - 1) * 100
    ts = timestamps[-1] if timestamps else None
    return {
        "ticker": ticker,
        "previous_close": previous,
        "latest_close": latest,
        "pct_change": round(pct_change, 4),
        "timestamp": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None,
    }


def _move_to_news(move: dict[str, Any], instrument: dict[str, Any], source_name: str) -> NewsItem:
    name = str(instrument.get("name") or move["ticker"])
    direction = "上涨" if move["pct_change"] >= 0 else "下跌"
    title = f"{name}{direction}{abs(move['pct_change']):.2f}%"
    summary = (
        f"{name}最新收盘{move['latest_close']:.3f}，较前收盘{direction}"
        f"{abs(move['pct_change']):.2f}%，可能影响相关A股主题。"
    )
    raw_id = f"{source_name}:{move['ticker']}:{move.get('timestamp')}:{move['pct_change']}"
    return NewsItem(
        id="global_" + sha256(raw_id.encode("utf-8")).hexdigest()[:16],
        title=title,
        summary=summary,
        source=source_name,
        source_type="data_vendor",
        url=str(instrument.get("url") or ""),
        published_at=datetime.now(timezone.utc),
        raw_text=f"{title} {summary} {' '.join(instrument.get('tags', []))}",
        metadata={
            "collector": source_name,
            "ticker": move["ticker"],
            "name": name,
            "pct_change": move["pct_change"],
            "tags": instrument.get("tags", []),
        },
    )
