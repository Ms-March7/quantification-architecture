from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.models import NewsItem


EASTMONEY_COLUMN_NEWS_URL = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
CN_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class EastmoneyColumn:
    id: int
    name: str


def collect_eastmoney_column_news(
    columns: list[EastmoneyColumn],
    page_size: int = 30,
    timeout_seconds: float = 10,
) -> list[NewsItem]:
    items: list[NewsItem] = []
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
        for column in columns:
            items.extend(_fetch_column(client, column, page_size))
    return _dedup_by_id(items)


def _fetch_column(
    client: httpx.Client,
    column: EastmoneyColumn,
    page_size: int,
) -> list[NewsItem]:
    params = {
        "client": "web",
        "biz": "web_news_col",
        "column": str(column.id),
        "order": "1",
        "needInteractData": "0",
        "page_index": "1",
        "page_size": str(page_size),
        "req_trace": str(int(datetime.now().timestamp() * 1000)),
        "fields": "code,showTime,title,mediaName,summary,image,url,uniqueUrl,Np_dst",
        "types": "1,20",
    }
    response = client.get(EASTMONEY_COLUMN_NEWS_URL, params=params)
    response.raise_for_status()
    payload = response.json()
    rows = ((payload.get("data") or {}).get("list") or [])
    if not isinstance(rows, list):
        return []
    return [_row_to_news_item(row, column) for row in rows if isinstance(row, dict)]


def _row_to_news_item(row: dict[str, Any], column: EastmoneyColumn) -> NewsItem:
    title = _repair_mojibake(str(row.get("title") or "").strip())
    code = str(row.get("code") or "").strip()
    url = str(row.get("uniqueUrl") or row.get("url") or "").strip() or None
    media_name = _repair_mojibake(str(row.get("mediaName") or "东方财富").strip())
    digest_raw = f"eastmoney:{code}:{title}:{url}"
    digest = sha256(digest_raw.encode("utf-8")).hexdigest()[:16]
    return NewsItem(
        id=f"em_{digest}",
        title=title,
        summary=_clean_text(row.get("summary")),
        source=media_name,
        source_type=_source_type_for_media(media_name),
        url=url,
        published_at=parse_eastmoney_time(row.get("showTime")),
        raw_text=_clean_text(row.get("summary")),
        metadata={
            "vendor": "eastmoney",
            "vendor_code": code,
            "column_id": column.id,
            "column_name": column.name,
            "image": row.get("image"),
            "np_dst": row.get("np_dst"),
            "source_url": row.get("url"),
        },
    )


def parse_eastmoney_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=CN_TZ)
        except ValueError:
            continue
    return None


def _source_type_for_media(media_name: str) -> str:
    official_keywords = ("证监会", "交易所", "发改委", "工信部", "财政部", "央行", "新华社")
    data_vendor_keywords = ("东方财富", "Choice")
    if any(keyword in media_name for keyword in official_keywords):
        return "official"
    if any(keyword in media_name for keyword in data_vendor_keywords):
        return "data_vendor"
    return "mainstream_media"


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(_repair_mojibake(str(value)).split())
    return text or None


def _repair_mojibake(value: str) -> str:
    if not any(char in value for char in ("Ã", "ä", "å", "æ", "ç", "è", "é")):
        return value
    try:
        repaired = value.encode("latin1").decode("utf-8")
    except UnicodeError:
        return value
    return repaired if repaired else value


def _dedup_by_id(items: list[NewsItem]) -> list[NewsItem]:
    seen: set[str] = set()
    deduped: list[NewsItem] = []
    for item in items:
        if item.id in seen:
            continue
        seen.add(item.id)
        deduped.append(item)
    return deduped
