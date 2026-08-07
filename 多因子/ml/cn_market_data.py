#!/usr/bin/env python3
"""Windows-native A-share minute-bar adapter with source provenance.

Public HTTP bars are useful for observation and shadow research, but are never
marked eligible for manual order review.  QMT bars may support broker-connected
shadow validation.  This module deliberately avoids the repository's Node
dependency and stores source, units, adjustment mode, and fetch time with every
cache.
"""

from __future__ import annotations

import json
import math
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, time as wall_time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MultiFactor/1.0"
SUPPORTED_PERIODS = {"1m": 1, "5m": 5, "15m": 15, "1d": 101}
SINA_SEMAPHORE = threading.BoundedSemaphore(2)
EASTMONEY_SEMAPHORE = threading.BoundedSemaphore(2)


@dataclass(frozen=True)
class MarketBar:
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float  # shares
    amount: float  # CNY


@dataclass(frozen=True)
class SourceBars:
    symbol: str
    name: str
    source: str
    period: str
    fetched_at: float
    bars: Tuple[MarketBar, ...]
    volume_unit: str = "share"
    amount_unit: str = "CNY"
    adjustment: str = "none"


@dataclass(frozen=True)
class QualityReport:
    status: str
    manual_review_eligible: bool
    reasons: Tuple[str, ...]
    primary_source: str
    secondary_source: str
    primary_last_time: str
    secondary_last_time: str
    last_close_difference_bps: float
    last_volume_relative_difference: float
    bar_count: int


@dataclass(frozen=True)
class DataBundle:
    symbol: str
    period: str
    bars: Tuple[MarketBar, ...]
    quality: QualityReport


def market_prefix(symbol: str) -> Tuple[str, str]:
    digits = "".join(character for character in str(symbol) if character.isdigit())
    if len(digits) != 6:
        raise ValueError(f"unsupported security code: {symbol}")
    if digits.startswith(("5", "6", "9")):
        return digits, "sh"
    if digits.startswith(("0", "1", "2", "3")):
        return digits, "sz"
    raise ValueError(f"public adapter does not support this market: {symbol}")


def parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(SHANGHAI) if value.tzinfo else value.replace(tzinfo=SHANGHAI)
    if isinstance(value, (int, float)):
        epoch = float(value)
        if epoch > 10_000_000_000:
            epoch /= 1000.0
        return datetime.fromtimestamp(epoch, tz=SHANGHAI)
    text = str(value).strip()
    if text.isdigit() and len(text) in {10, 13}:
        return parse_timestamp(int(text))
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d%H%M%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, pattern)
            if pattern == "%Y-%m-%d":
                parsed = parsed.replace(hour=15)
            return parsed.replace(tzinfo=SHANGHAI)
        except ValueError:
            continue
    raise ValueError(f"unsupported bar timestamp: {value}")


def format_timestamp(value: Any) -> str:
    return parse_timestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def finite(value: Any, fallback: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if math.isfinite(result) else fallback


def json_safe(value: Any) -> Any:
    """Convert nested values into strict JSON (no NaN or Infinity)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def valid_session_endpoint(value: Any) -> bool:
    timestamp = parse_timestamp(value)
    current = timestamp.time()
    return (
        wall_time(9, 30) < current <= wall_time(11, 30)
        or wall_time(13, 0) < current <= wall_time(15, 0)
    )


def normalize_bars(values: Iterable[MarketBar]) -> Tuple[MarketBar, ...]:
    by_time: Dict[str, MarketBar] = {}
    for value in values:
        timestamp = format_timestamp(value.time)
        prices = (value.open, value.high, value.low, value.close)
        if not valid_session_endpoint(timestamp):
            continue
        if any(not math.isfinite(item) or item <= 0.0 for item in prices):
            continue
        if value.high + 1e-12 < max(value.open, value.low, value.close):
            continue
        if value.low - 1e-12 > min(value.open, value.high, value.close):
            continue
        by_time[timestamp] = MarketBar(
            time=timestamp,
            open=float(value.open),
            high=float(value.high),
            low=float(value.low),
            close=float(value.close),
            volume=max(0.0, float(value.volume)),
            amount=max(0.0, float(value.amount)),
        )
    return tuple(by_time[key] for key in sorted(by_time))


def completed_bars(
    values: Sequence[MarketBar],
    now: Optional[datetime] = None,
    grace_seconds: float = 10.0,
) -> Tuple[MarketBar, ...]:
    epoch = (now or datetime.now(tz=SHANGHAI)).timestamp() - grace_seconds
    return tuple(value for value in values if parse_timestamp(value.time).timestamp() <= epoch)


def _http_text(
    url: str,
    *,
    referer: str = "",
    timeout: float = 15.0,
    attempts: int = 3,
) -> str:
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    last_error: Optional[Exception] = None
    for attempt in range(max(1, attempts)):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.25 * (attempt + 1))
    assert last_error is not None
    raise last_error


def parse_eastmoney_payload(symbol: str, period: str, text: str, fetched_at: Optional[float] = None) -> SourceBars:
    payload = json.loads(text)
    data = payload.get("data") or {}
    rows = data.get("klines") or []
    bars: List[MarketBar] = []
    for row in rows:
        parts = str(row).split(",")
        if len(parts) < 7:
            continue
        bars.append(MarketBar(
            time=parts[0],
            open=finite(parts[1]),
            close=finite(parts[2]),
            high=finite(parts[3]),
            low=finite(parts[4]),
            volume=finite(parts[5]) * 100.0,  # Eastmoney A-share K-lines use lots.
            amount=finite(parts[6]),
        ))
    return SourceBars(
        symbol=market_prefix(symbol)[0],
        name=str(data.get("name") or market_prefix(symbol)[0]),
        source="eastmoney",
        period=period,
        fetched_at=float(fetched_at or time.time()),
        bars=normalize_bars(bars),
    )


def parse_sina_payload(symbol: str, period: str, text: str, fetched_at: Optional[float] = None) -> SourceBars:
    start = text.find("([")
    end = text.rfind("])")
    if start < 0 or end <= start:
        raise ValueError("invalid Sina JSONP response")
    values = json.loads(text[start + 1:end + 1])
    bars = [MarketBar(
        time=value.get("day", ""),
        open=finite(value.get("open")),
        high=finite(value.get("high")),
        low=finite(value.get("low")),
        close=finite(value.get("close")),
        volume=finite(value.get("volume")),  # Sina reports shares.
        amount=finite(value.get("amount")),
    ) for value in values]
    code = market_prefix(symbol)[0]
    return SourceBars(
        symbol=code,
        name=code,
        source="sina",
        period=period,
        fetched_at=float(fetched_at or time.time()),
        bars=normalize_bars(bars),
    )


def fetch_eastmoney(symbol: str, period: str = "5m", limit: int = 1023) -> SourceBars:
    if period not in SUPPORTED_PERIODS:
        raise ValueError(f"unsupported period: {period}")
    code, market = market_prefix(symbol)
    query = urllib.parse.urlencode({
        "secid": f"{'1' if market == 'sh' else '0'}.{code}",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": str(SUPPORTED_PERIODS[period]),
        "fqt": "0",
        "beg": "20240101",
        "end": "20500101",
        "lmt": str(max(20, int(limit))),
    })
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + query
    fetched_at = time.time()
    with EASTMONEY_SEMAPHORE:
        text = _http_text(url, referer="https://quote.eastmoney.com/", attempts=3)
    return parse_eastmoney_payload(code, period, text, fetched_at)


def fetch_sina(symbol: str, period: str = "5m", limit: int = 1023) -> SourceBars:
    if period not in SUPPORTED_PERIODS:
        raise ValueError(f"unsupported period: {period}")
    code, market = market_prefix(symbol)
    query = urllib.parse.urlencode({
        "symbol": f"{market}{code}",
        "scale": "240" if period == "1d" else str(SUPPORTED_PERIODS[period]),
        "ma": "no",
        "datalen": str(min(1023, max(20, int(limit)))),
    })
    url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_data=/CN_MarketDataService.getKLineData?" + query
    fetched_at = time.time()
    # Sina closes bursty parallel connections.  Two in-flight requests plus
    # bounded retry is materially more reliable than unconstrained fan-out.
    with SINA_SEMAPHORE:
        text = _http_text(url, attempts=3)
    return parse_sina_payload(code, period, text, fetched_at)


def compare_sources(primary: SourceBars, secondary: SourceBars, limit: int) -> DataBundle:
    primary_bars = primary.bars[-limit:]
    secondary_bars = secondary.bars[-limit:]
    reasons: List[str] = []
    if not primary_bars:
        reasons.append("primary_empty")
    if not secondary_bars:
        reasons.append("secondary_empty")
    primary_last = primary_bars[-1] if primary_bars else None
    secondary_last = secondary_bars[-1] if secondary_bars else None
    close_bps = float("inf")
    volume_difference = float("inf")
    if primary_last and secondary_last:
        if primary_last.time != secondary_last.time:
            reasons.append("last_timestamp_mismatch")
        close_bps = abs(primary_last.close / secondary_last.close - 1.0) * 10_000.0
        if close_bps > 8.0:
            reasons.append("last_close_divergence")
        volume_scale = max(primary_last.volume, secondary_last.volume, 1.0)
        volume_difference = abs(primary_last.volume - secondary_last.volume) / volume_scale
        if volume_difference > 0.35:
            reasons.append("last_volume_divergence")
    status = "OK_DUAL_SOURCE" if not reasons else "DATA_BLOCKED"
    quality = QualityReport(
        status=status,
        manual_review_eligible=False,
        reasons=tuple(reasons),
        primary_source=primary.source,
        secondary_source=secondary.source,
        primary_last_time=primary_last.time if primary_last else "",
        secondary_last_time=secondary_last.time if secondary_last else "",
        last_close_difference_bps=close_bps,
        last_volume_relative_difference=volume_difference,
        bar_count=len(primary_bars),
    )
    return DataBundle(primary.symbol, primary.period, tuple(primary_bars), quality)


def fetch_public_bundle(symbol: str, period: str = "5m", limit: int = 300) -> DataBundle:
    primary_error = secondary_error = None
    primary = secondary = None
    try:
        primary = fetch_eastmoney(symbol, period, limit)
    except Exception as exc:  # network errors are reported in quality, not hidden
        primary_error = type(exc).__name__ + ": " + str(exc)
    try:
        secondary = fetch_sina(symbol, period, limit)
    except Exception as exc:
        secondary_error = type(exc).__name__ + ": " + str(exc)
    if primary is not None and secondary is not None:
        return compare_sources(primary, secondary, limit)
    available = primary or secondary
    if available is None:
        quality = QualityReport(
            status="DATA_BLOCKED",
            manual_review_eligible=False,
            reasons=(f"eastmoney:{primary_error}", f"sina:{secondary_error}"),
            primary_source="",
            secondary_source="",
            primary_last_time="",
            secondary_last_time="",
            last_close_difference_bps=float("inf"),
            last_volume_relative_difference=float("inf"),
            bar_count=0,
        )
        return DataBundle(market_prefix(symbol)[0], period, (), quality)
    bars = available.bars[-limit:]
    quality = QualityReport(
        status="OBSERVATION_ONLY_SINGLE_SOURCE",
        manual_review_eligible=False,
        reasons=("single_source_only", f"missing:{primary_error or secondary_error}"),
        primary_source=available.source,
        secondary_source="",
        primary_last_time=bars[-1].time if bars else "",
        secondary_last_time="",
        last_close_difference_bps=float("inf"),
        last_volume_relative_difference=float("inf"),
        bar_count=len(bars),
    )
    return DataBundle(available.symbol, period, tuple(bars), quality)


def fetch_public_universe(
    symbols: Sequence[str],
    period: str = "5m",
    limit: int = 300,
    workers: int = 6,
) -> Mapping[str, DataBundle]:
    unique = list(dict.fromkeys(market_prefix(symbol)[0] for symbol in symbols))
    result: Dict[str, DataBundle] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(unique)))) as executor:
        futures = {
            executor.submit(fetch_public_bundle, symbol, period, limit): symbol for symbol in unique
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                result[symbol] = future.result()
            except Exception as exc:
                quality = QualityReport(
                    "DATA_BLOCKED", False, (type(exc).__name__ + ": " + str(exc),),
                    "", "", "", "", float("inf"), float("inf"), 0,
                )
                result[symbol] = DataBundle(symbol, period, (), quality)
    return result


def write_cache(bundle: DataBundle, root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{bundle.symbol}-{bundle.period}.json"
    payload = {
        "schemaVersion": 1,
        "symbol": bundle.symbol,
        "period": bundle.period,
        "cachedAt": time.time(),
        "volumeUnit": "share",
        "amountUnit": "CNY",
        "adjustment": "none",
        "quality": asdict(bundle.quality),
        "bars": [asdict(value) for value in bundle.bars],
    }
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(json_safe(payload), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(target)
    return target
