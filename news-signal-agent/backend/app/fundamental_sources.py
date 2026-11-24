"""Collect configured public evidence without backdating newly observed feed content."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import unescape
from pathlib import Path
from time import monotonic
from urllib.parse import urldefrag, urljoin, urlsplit
from xml.etree import ElementTree as ET

import httpx

from app.research_review import Source, parse_timestamp


DIMENSIONS = ("company", "supply", "demand", "competition", "policy", "market")
MAX_FEED_BYTES = 2 * 1024 * 1024
ATOM = "{http://www.w3.org/2005/Atom}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return parse_timestamp(value).astimezone(timezone.utc).isoformat(timespec="microseconds")


def _url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None:
        raise ValueError("材料地址必须是无身份信息的 HTTP(S) 链接")
    return urldefrag(value).url


def _source_date(value: str) -> datetime:
    try:
        return parse_timestamp(value)
    except ValueError:
        return parse_timestamp(parsedate_to_datetime(value))


def _text(value: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", value)).split())


def _download(url: str) -> bytes:
    deadline = monotonic() + 15
    with httpx.stream(
        "GET", url, timeout=8, follow_redirects=True,
        headers={"User-Agent": "FundamentalResearchAgent/1.0"},
    ) as response:
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_bytes():
            if monotonic() > deadline:
                raise TimeoutError("订阅读取超时")
            if len(chunk) > MAX_FEED_BYTES - len(body):
                raise ValueError("订阅内容超过读取上限")
            body.extend(chunk)
        return bytes(body)


def _field(row: ET.Element, name: str) -> str:
    field = row.find(name)
    return "".join(field.itertext()).strip() if field is not None else ""


def _parse_feed(body: bytes, feed_url: str, observed: datetime) -> tuple[list[dict], int]:
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ValueError("订阅内容含不支持的文档类型")
    root = ET.fromstring(body)
    if root.tag == "rss":
        entries = root.findall("./channel/item")
    elif root.tag == ATOM + "feed":
        entries = root.findall(ATOM + "entry")
    else:
        raise ValueError("订阅格式须为 RSS 2.0 或 Atom")
    articles: dict[str, dict] = {}
    skipped = 0
    for item in entries:
        if root.tag == "rss":
            title, link = _field(item, "title"), _field(item, "link")
            excerpt, date = _field(item, "description"), _field(item, "pubDate")
        else:
            title = _field(item, ATOM + "title")
            link = next((node.get("href", "") for node in item.findall(ATOM + "link")
                         if node.get("rel", "alternate") == "alternate"), "")
            excerpt = _field(item, ATOM + "summary") or _field(item, ATOM + "content")
            date = _field(item, ATOM + "published") or _field(item, ATOM + "updated")
        try:
            published = _source_date(date)
            url = _url(urljoin(feed_url, link))
            updated = _field(item, ATOM + "updated") if root.tag == ATOM + "feed" else ""
            title = _text(title)[:300]
            if not title or not link or published > observed or (updated and _source_date(updated) > observed):
                raise ValueError("缺少标题或链接，或发布时间晚于观测")
        except (TypeError, ValueError, OverflowError):
            skipped += 1
            continue
        key = "feed_" + sha256(url.encode()).hexdigest()[:24]
        articles.setdefault(key, {
            "id": key, "title": title, "url": url, "excerpt": _text(excerpt)[:1000] or title,
            "published_at": _stamp(published),
        })
    return list(articles.values()), skipped


def _record(db: Path, feed_url: str, rows: list[dict]) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db, timeout=10)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS versions (
            feed TEXT, article TEXT, observed TEXT, payload TEXT NOT NULL,
            PRIMARY KEY(feed, article, observed))""")
        observed = _stamp(_now())
        for row in rows:
            payload = json.dumps(row, ensure_ascii=False, sort_keys=True)
            last = conn.execute(
                "SELECT payload FROM versions WHERE feed=? AND article=? ORDER BY observed DESC LIMIT 1",
                (feed_url, row["id"]),
            ).fetchone()
            if last is None or last[0] != payload:
                conn.execute("INSERT INTO versions VALUES(?, ?, ?, ?)",
                             (feed_url, row["id"], observed, payload))


def _archived(db: Path, feed_url: str, cutoff: datetime) -> list[dict]:
    if not db.is_file():
        return []
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        records = conn.execute("""
            SELECT v.payload, v.observed FROM versions v
            JOIN (SELECT article, MAX(observed) AS observed FROM versions
                  WHERE feed=? AND observed<=? GROUP BY article) hit
            ON v.article=hit.article AND v.observed=hit.observed WHERE v.feed=?
            """, (feed_url, _stamp(cutoff), feed_url))
        return [dict(json.loads(payload), observed_at=observed, available_at=observed)
                for payload, observed in records]


def _dimension(row: dict) -> str:
    name = row.get("dimension")
    if name not in DIMENSIONS:
        raise ValueError("未知研究维度")
    return name


def _curated(profile: dict, cutoff: datetime) -> list[dict]:
    result = {}
    for raw in profile.get("sources", []):
        clean = Source.model_validate({k: raw[k] for k in Source.model_fields if k in raw})
        item = clean.model_dump(mode="json")
        item["url"] = _url(item["url"])
        item["dimension"] = _dimension(raw)
        published = parse_timestamp(raw.get("published_at", clean.available_at))
        available = max(published, clean.available_at)
        if raw.get("observed_at") is not None:
            observed = parse_timestamp(raw["observed_at"])
            item["observed_at"] = _stamp(observed)
            available = max(available, observed)
        if available <= cutoff:
            item.update(available_at=_stamp(available), published_at=_stamp(published))
            old = result.get(item["id"])
            if old and (item["url"] != old["url"] or
                        (item["available_at"] == old["available_at"] and item != old)):
                raise ValueError(f"人工材料 ID、版本时点或来源发生冲突：{item['id']}")
            if old is None or item["available_at"] > old["available_at"]:
                result[item["id"]] = item
    return list(result.values())


def collect_materials(
    profile: dict, cutoff: datetime | None, refresh: bool, cache_dir: Path | None = None,
) -> tuple[list[dict], list[str]]:
    """A dated request reads only prior observations. None includes this invocation's refresh."""
    start = _now()
    limit = parse_timestamp(cutoff) if cutoff is not None else start
    if limit > start:
        raise ValueError("研究截止时点晚于当前时间")
    curated = _curated(profile, limit)
    materials = {item["id"]: item for item in curated}
    urls = {item["url"] for item in curated}
    warnings = []
    if cutoff is not None and refresh:
        warnings.append("历史查询只读取截止时点已有的材料和缓存版本。")
    db = Path(cache_dir) / "fundamental_feeds.sqlite3" if cache_dir is not None else None
    for feed in profile.get("feeds", []):
        url = _url(feed["url"])
        scope = feed["scope"]
        dimension = _dimension(feed)
        words = feed.get("keywords", [])
        if not isinstance(words, list) or not all(isinstance(w, str) and w.strip() for w in words):
            raise ValueError("keywords 须为非空关键词组成的列表")
        size = feed.get("max_items", 20)
        if type(size) is not int or not 1 <= size <= 200:
            raise ValueError("max_items 须在 1 至 200 之间")
        days = feed.get("stale_after_days", 7)
        if type(days) is not int or days < 0:
            raise ValueError("stale_after_days 须为非负整数")
        rows = []
        if cutoff is None and refresh:
            try:
                body = _download(url)
                received = _now()
                fresh, skipped = _parse_feed(body, url, received)
                if skipped:
                    warnings.append(f"{scope}：{skipped} 条材料缺少有效日期或来源，已排除。")
                rows = [dict(row, observed_at=_stamp(received), available_at=_stamp(received))
                        for row in fresh]
            except (httpx.HTTPError, OSError, ET.ParseError, ValueError) as exc:
                warnings.append(f"{scope}：刷新失败（{type(exc).__name__}），仅使用已有材料。")
            else:
                if db:
                    try:
                        _record(db, url, fresh)
                    except (sqlite3.Error, OSError) as exc:
                        warnings.append(f"{scope}：本次观测未写入缓存（{type(exc).__name__}）。")
        as_of = limit if cutoff is not None else _now()
        if db:
            try:
                archived = _archived(db, url, as_of)
                # Prefer persisted first-seen timestamps, except when a fresh revision failed to save.
                by_id = {row["id"]: row for row in archived}
                for row in rows:
                    old = by_id.get(row["id"])
                    if old is None or any(old[k] != row[k] for k in ("title", "excerpt", "published_at")):
                        by_id[row["id"]] = row
                rows = list(by_id.values())
            except (sqlite3.Error, OSError, ValueError) as exc:
                warnings.append(f"{scope}：缓存读取失败（{type(exc).__name__}）。")
        matches = [row for row in rows if not words or any(
            word.casefold() in (row["title"] + " " + row["excerpt"]).casefold() for word in words)]
        matches.sort(key=lambda row: (row["published_at"], row["id"]), reverse=True)
        if not matches:
            warnings.append(f"{scope}：该时点没有已观测且匹配的订阅材料。")
        elif as_of - parse_timestamp(matches[0]["published_at"]) > timedelta(days=days):
            warnings.append(f"{scope}：最近匹配材料的发布时间已超过 {days} 天。")
        for row in matches[:size]:
            if row["url"] in urls:
                continue
            if row["id"] in materials:
                raise ValueError(f"相同材料 ID 指向不同地址：{row['id']}")
            materials[row["id"]] = dict(row, scope=scope, dimension=dimension)
            urls.add(row["url"])
    return list(materials.values()), warnings
