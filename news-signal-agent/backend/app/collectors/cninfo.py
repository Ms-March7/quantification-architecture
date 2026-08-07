from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

import httpx

from app.models import NewsItem, WatchSymbol


CNINFO_QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC_URL = "http://static.cninfo.com.cn/"
CNINFO_REFERER = "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search"
CN_TZ = timezone(timedelta(hours=8))


def collect_cninfo_announcements(
    watchlist: list[WatchSymbol],
    page_size: int = 20,
    lookback_days: int = 14,
    timeout_seconds: float = 10,
    extract_body: bool = False,
    max_body_chars: int = 4000,
) -> list[NewsItem]:
    items: list[NewsItem] = []
    headers = {"User-Agent": "Mozilla/5.0", "Referer": CNINFO_REFERER}
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
        for symbol in watchlist:
            if symbol.asset_type.value not in {"stock", "unknown"}:
                continue
            items.extend(
                _collect_symbol_announcements(
                    client=client,
                    symbol=symbol,
                    page_size=page_size,
                    lookback_days=lookback_days,
                    extract_body=extract_body,
                    max_body_chars=max_body_chars,
                )
            )
    return _dedup(items)


def _collect_symbol_announcements(
    client: httpx.Client,
    symbol: WatchSymbol,
    page_size: int,
    lookback_days: int,
    extract_body: bool,
    max_body_chars: int,
) -> list[NewsItem]:
    end = date.today()
    start = end - timedelta(days=lookback_days)
    payload = {
        "pageNum": "1",
        "pageSize": str(page_size),
        "column": "sse" if (symbol.market or "").upper() == "SH" else "szse",
        "tabName": "fulltext",
        "plate": "",
        "stock": symbol.symbol,
        "searchkey": "",
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": f"{start}~{end}",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    response = client.post(CNINFO_QUERY_URL, data=payload)
    response.raise_for_status()
    rows = (response.json().get("announcements") or [])
    result: list[NewsItem] = []
    for row in rows:
        result.append(_announcement_to_news(client, row, symbol, extract_body, max_body_chars))
    return result


def _announcement_to_news(
    client: httpx.Client,
    row: dict[str, Any],
    symbol: WatchSymbol,
    extract_body: bool,
    max_body_chars: int,
) -> NewsItem:
    title = _clean_title(str(row.get("announcementTitle") or "未命名公告"))
    adjunct_url = str(row.get("adjunctUrl") or "")
    url = CNINFO_STATIC_URL + adjunct_url if adjunct_url else None
    published_at = _announcement_time(row.get("announcementTime"))
    announcement_id = str(row.get("announcementId") or "")
    raw_id = announcement_id or f"{symbol.symbol}:{title}:{adjunct_url}"
    body_text, body_status = ("", "disabled")
    if extract_body and url:
        body_text, body_status = _extract_announcement_body(client, url, max_body_chars)
    summary = _body_summary(body_text) or title
    return NewsItem(
        id="cninfo_" + sha256(raw_id.encode("utf-8")).hexdigest()[:16],
        title=f"{symbol.symbol} {symbol.name or ''}：{title}",
        summary=summary,
        source="巨潮资讯",
        source_type="official",
        url=url,
        published_at=published_at,
        raw_text=f"{symbol.symbol} {symbol.name or ''} {title} {body_text}".strip(),
        metadata={
            "collector": "cninfo",
            "symbol": symbol.symbol,
            "announcement_id": announcement_id,
            "category": row.get("category"),
            "adjunct_url": adjunct_url,
            "body_extract_status": body_status,
            "body_chars": len(body_text),
        },
    )


def _announcement_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        seconds = float(value) / 1000
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, tz=CN_TZ)


def _clean_title(value: str) -> str:
    return (
        value.replace("<em>", "")
        .replace("</em>", "")
        .replace("&nbsp;", " ")
        .replace("  ", " ")
        .strip()
    )


def _extract_announcement_body(
    client: httpx.Client,
    url: str,
    max_body_chars: int,
) -> tuple[str, str]:
    try:
        response = client.get(url)
        response.raise_for_status()
    except Exception as exc:
        return "", f"download_failed:{type(exc).__name__}"
    content_type = response.headers.get("content-type", "").lower()
    if "pdf" in content_type or url.lower().endswith(".pdf"):
        return _extract_pdf_text(response.content, max_body_chars)
    return _extract_html_text(response.text, max_body_chars), "html_text"


def _extract_pdf_text(content: bytes, max_body_chars: int) -> tuple[str, str]:
    try:
        from pypdf import PdfReader
    except Exception:
        return "", "missing_pypdf"
    try:
        import io

        reader = PdfReader(io.BytesIO(content))
        parts: list[str] = []
        for page in reader.pages[:8]:
            parts.append(page.extract_text() or "")
            if sum(len(part) for part in parts) >= max_body_chars:
                break
    except Exception as exc:
        return "", f"pdf_extract_failed:{type(exc).__name__}"
    text = _compact_text(" ".join(parts))
    return text[:max_body_chars], "pdf_text"


def _extract_html_text(text: str, max_body_chars: int) -> str:
    import re

    cleaned = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<style[\s\S]*?</style>", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    return _compact_text(cleaned)[:max_body_chars]


def _body_summary(text: str) -> str | None:
    clean = _compact_text(text)
    if not clean:
        return None
    return clean[:180].rstrip("，。；、 ") + ("…" if len(clean) > 180 else "")


def _compact_text(text: str) -> str:
    return " ".join(text.replace("\u3000", " ").split())


def _dedup(items: list[NewsItem]) -> list[NewsItem]:
    seen: set[str] = set()
    result: list[NewsItem] = []
    for item in items:
        key = item.url or item.title
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
