from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import fundamental_sources as fs


BASE = datetime(2026, 4, 30, 11, tzinfo=timezone.utc)
FEED = {"url": "https://example.org/feed", "scope": "竞争动态", "dimension": "competition"}
SOURCE = {
    "id": "filing", "title": "示例经营记录", "scope": "公司口径", "dimension": "company",
    "url": "https://example.org/filing", "excerpt": "材料中的说明",
    "available_at": "2026-04-30T09:00:00+00:00",
}


def rss(text="first version", title="Memory outlook"):
    return f"""<rss><channel><item><title>{title}</title>
    <link>https://example.org/filing</link><description>{text}</description>
    <pubDate>Thu, 30 Apr 2026 09:00:00 GMT</pubDate></item></channel></rss>""".encode()


def test_archive_is_revision_aware_and_does_not_backfill(tmp_path, monkeypatch):
    clock = [BASE]
    feed_body = [rss()]
    monkeypatch.setattr(fs, "_now", lambda: clock[0])
    monkeypatch.setattr(fs, "_download", lambda url: feed_body[0])
    profile = {"feeds": [FEED]}
    first, warnings = fs.collect_materials(profile, None, True, tmp_path)
    assert not warnings
    assert first[0]["available_at"] == first[0]["observed_at"]
    assert first[0]["published_at"] < first[0]["observed_at"]

    clock[0] += timedelta(hours=1)
    same, _ = fs.collect_materials(profile, None, True, tmp_path)
    assert same == first
    feed_body[0] = rss("corrected version")
    second, _ = fs.collect_materials(profile, None, True, tmp_path)
    assert second[0]["id"] == first[0]["id"] and second[0]["excerpt"] == "corrected version"
    assert first[0]["observed_at"] < second[0]["observed_at"]

    monkeypatch.setattr(fs, "_download", lambda _: pytest.fail("historical queries must be offline"))
    past, _ = fs.collect_materials(profile, BASE - timedelta(microseconds=1), True, tmp_path)
    assert not past
    at_first, _ = fs.collect_materials(profile, BASE, False, tmp_path)
    assert at_first == first
    at_second, _ = fs.collect_materials(profile, clock[0], False, tmp_path)
    assert at_second == second


def test_unavailable_feed_falls_back_with_age_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "_now", lambda: BASE)
    monkeypatch.setattr(fs, "_download", lambda url: rss())
    profile = {"feeds": [FEED]}
    first, _ = fs.collect_materials(profile, None, True, tmp_path)
    monkeypatch.setattr(fs, "_now", lambda: BASE + timedelta(days=20))

    def unavailable(_):
        raise httpx.ReadTimeout("offline")

    monkeypatch.setattr(fs, "_download", unavailable)
    fallback, warnings = fs.collect_materials(profile, None, True, tmp_path)
    assert fallback == first
    assert any("刷新失败" in w for w in warnings)
    assert any("超过 7 天" in w for w in warnings)


def test_atom_keyword_match_alternate_link_and_missing_dates(monkeypatch):
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Shipping</title><link rel="self" href="/feed/123"/>
      <link rel="alternate" href="/articles/1"/><summary>CPU and memory</summary>
      <updated>2026-04-30T09:00:00Z</updated></entry>
      <entry><title>Oil</title><link href="/2"/><updated>2026-04-30T10:00:00Z</updated></entry>
      <entry><title>Memory missing timezone</title><link href="/3"/>
      <published>2026-04-30T08:00:00</published></entry>
      <entry><title>Memory future</title><link href="/4"/>
      <updated>2026-05-01T09:00:00Z</updated></entry></feed>"""
    monkeypatch.setattr(fs, "_now", lambda: BASE)
    monkeypatch.setattr(fs, "_download", lambda url: body)
    queried = dict(FEED, keywords=["MEMORY"])
    items, warnings = fs.collect_materials({"feeds": [queried]}, None, True)
    assert [i["url"] for i in items] == ["https://example.org/articles/1"]
    assert any("2 条" in w for w in warnings)


def test_curated_dates_dedup_and_conflicting_ids(monkeypatch):
    monkeypatch.setattr(fs, "_download", lambda url: rss())
    monkeypatch.setattr(fs, "_now", lambda: BASE)
    source = dict(SOURCE, observed_at=BASE.isoformat())
    profile = {"sources": [source], "feeds": [FEED, dict(FEED, dimension="supply")]}
    early, _ = fs.collect_materials(profile, BASE - timedelta(minutes=1), False)
    assert not early
    items, _ = fs.collect_materials(profile, None, True)
    assert len(items) == 1 and items[0]["id"] == "filing"
    with pytest.raises(ValueError):
        fs.collect_materials({"sources": [source, dict(source, url="https://example.org/other")]},
                             None, False)
    revised = dict(source, observed_at=(BASE - timedelta(minutes=1)).isoformat(), excerpt="旧观测")
    items, _ = fs.collect_materials({"sources": [source, revised]}, BASE, False)
    assert items[0]["excerpt"] == SOURCE["excerpt"]


def test_archive_is_read_only_for_offline_and_rejects_unsafe_feed(tmp_path):
    missing = tmp_path / "missing"
    items, warnings = fs.collect_materials({"feeds": [FEED]}, BASE, False, missing)
    assert items == [] and warnings and not missing.exists()
    with pytest.raises(ValueError):
        fs.collect_materials({"feeds": [dict(FEED, url="file:///example.xml")]}, BASE, False)


def test_atom_content_with_future_revision_is_not_used():
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>News</title>
      <link href="/a"/><published>2026-04-29T09:00:00Z</published>
      <updated>2026-05-01T09:00:00Z</updated></entry></feed>"""
    assert fs._parse_feed(body, FEED["url"], BASE) == ([], 1)


def test_http_size_ceiling(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def send(method, url, **kwargs):
        assert method == "GET" and kwargs["timeout"] > 0
        response = httpx.Response(200, content=b"too large", request=httpx.Request("GET", url))
        try:
            yield response
        finally:
            response.close()

    monkeypatch.setattr(fs, "MAX_FEED_BYTES", 4)
    monkeypatch.setattr(httpx, "stream", send)
    with pytest.raises(ValueError, match="超过读取上限"):
        fs._download(FEED["url"])
