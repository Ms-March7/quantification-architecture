from __future__ import annotations

import os
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

import httpx

from app.models import NewsItem, WatchSymbol


def collect_web_search_news(
    watchlist: list[WatchSymbol],
    provider: str = "disabled",
    max_queries: int = 8,
    timeout_seconds: float = 10,
) -> tuple[list[NewsItem], list[str]]:
    provider = provider.lower()
    queries = _queries(watchlist)[:max_queries]
    if provider == "tavily":
        token = os.getenv("TAVILY_API_KEY")
        if not token:
            return [], ["web_search_tavily_missing_api_key"]
        return _collect_tavily(queries, token, timeout_seconds), []
    if provider == "serpapi":
        token = os.getenv("SERPAPI_API_KEY")
        if not token:
            return [], ["web_search_serpapi_missing_api_key"]
        return _collect_serpapi(queries, token, timeout_seconds), []
    if provider in {"disabled", "none", ""}:
        return [], []
    return [], [f"web_search_provider_unsupported:{provider}"]


def _queries(watchlist: list[WatchSymbol]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in watchlist:
        candidates = [item.name or item.symbol, item.symbol, *item.tags[:2]]
        for candidate in candidates:
            clean = candidate.strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            result.append(f"{clean} A股 新闻")
    return result


def _collect_tavily(queries: list[str], token: str, timeout_seconds: float) -> list[NewsItem]:
    rows: list[NewsItem] = []
    with httpx.Client(timeout=timeout_seconds) as client:
        for query in queries:
            response = client.post(
                "https://api.tavily.com/search",
                json={"api_key": token, "query": query, "topic": "news", "max_results": 5},
            )
            response.raise_for_status()
            for item in response.json().get("results") or []:
                rows.append(_search_result_to_news(item, query, "Tavily"))
    return rows


def _collect_serpapi(queries: list[str], token: str, timeout_seconds: float) -> list[NewsItem]:
    rows: list[NewsItem] = []
    with httpx.Client(timeout=timeout_seconds) as client:
        for query in queries:
            response = client.get(
                "https://serpapi.com/search.json",
                params={"engine": "google_news", "q": query, "api_key": token, "num": 5},
            )
            response.raise_for_status()
            for item in response.json().get("news_results") or []:
                rows.append(_search_result_to_news(item, query, "SerpAPI"))
    return rows


def _search_result_to_news(item: dict[str, Any], query: str, source: str) -> NewsItem:
    title = str(item.get("title") or "未命名搜索结果")
    url = item.get("url") or item.get("link")
    summary = item.get("content") or item.get("snippet") or item.get("summary")
    raw_id = f"{source}:{query}:{title}:{url}"
    return NewsItem(
        id="search_" + sha256(raw_id.encode("utf-8")).hexdigest()[:16],
        title=title,
        summary=str(summary) if summary else None,
        source=source,
        source_type="normal_media",
        url=str(url) if url else None,
        published_at=datetime.now(timezone.utc),
        raw_text=f"{query} {title} {summary or ''}",
        metadata={"collector": "web_search", "query": query},
    )
