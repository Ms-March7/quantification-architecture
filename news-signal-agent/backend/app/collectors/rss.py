from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from typing import Any

import httpx

from app.models import NewsItem


def collect_rss_feeds(
    feeds: list[dict[str, Any]],
    timeout_seconds: float = 10,
) -> tuple[list[NewsItem], list[str]]:
    items: list[NewsItem] = []
    warnings: list[str] = []
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
        for feed in feeds:
            if not feed.get("enabled", True):
                continue
            try:
                response = client.get(str(feed["url"]))
                response.raise_for_status()
                items.extend(_parse_feed(response.text, feed))
            except Exception as exc:
                warnings.append(f"rss_feed:{feed.get('name')}: {type(exc).__name__}: {exc}")
    return _dedup(items), warnings


def _parse_feed(text: str, feed: dict[str, Any]) -> list[NewsItem]:
    root = ET.fromstring(text)
    if root.tag.endswith("rss"):
        rows = root.findall("./channel/item")
        return [_rss_item_to_news(row, feed) for row in rows]
    rows = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    return [_atom_entry_to_news(row, feed) for row in rows]


def _rss_item_to_news(row: ET.Element, feed: dict[str, Any]) -> NewsItem:
    title = _text(row, "title") or "未命名消息"
    link = _text(row, "link")
    summary = _text(row, "description")
    published = _parse_time(_text(row, "pubDate"))
    return _to_news(title, summary, link, published, feed)


def _atom_entry_to_news(row: ET.Element, feed: dict[str, Any]) -> NewsItem:
    ns = "{http://www.w3.org/2005/Atom}"
    title = row.findtext(f"{ns}title") or "未命名消息"
    link_node = row.find(f"{ns}link")
    link = link_node.attrib.get("href") if link_node is not None else None
    summary = row.findtext(f"{ns}summary") or row.findtext(f"{ns}content")
    published = _parse_time(row.findtext(f"{ns}published") or row.findtext(f"{ns}updated"))
    return _to_news(title, summary, link, published, feed)


def _to_news(
    title: str,
    summary: str | None,
    link: str | None,
    published_at: datetime | None,
    feed: dict[str, Any],
) -> NewsItem:
    raw_id = f"{feed.get('name')}:{title}:{link}"
    return NewsItem(
        id="rss_" + sha256(raw_id.encode("utf-8")).hexdigest()[:16],
        title=" ".join(title.split()),
        summary=" ".join(summary.split()) if summary else None,
        source=str(feed.get("name") or "RSS"),
        source_type=str(feed.get("source_type") or "normal_media"),
        url=link,
        published_at=published_at,
        raw_text=f"{title} {summary or ''}",
        metadata={"collector": "rss", "feed": feed.get("name"), "tags": feed.get("tags", [])},
    )


def _text(row: ET.Element, tag: str) -> str | None:
    node = row.find(tag)
    if node is None or node.text is None:
        return None
    return node.text.strip()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


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
