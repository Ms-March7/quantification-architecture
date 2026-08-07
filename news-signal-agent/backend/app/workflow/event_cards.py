from __future__ import annotations

from typing import Any, Callable

from app.llm.client import llm_json
from app.models import EventItem, NewsItem, WatchSymbol


def build_event_cards(
    events: list[EventItem],
    news_by_id: dict[str, NewsItem],
    watchlist: list[WatchSymbol],
    llm_config: dict[str, Any],
    progress_callback: Callable[[int, int, EventItem], None] | None = None,
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    watch_terms = _watch_terms(watchlist)
    total = len(events)
    for index, event in enumerate(events, start=1):
        if progress_callback:
            progress_callback(index, total, event)
        rows = [news_by_id[news_id] for news_id in event.news_ids if news_id in news_by_id]
        card = _llm_card(event, rows, watch_terms, llm_config) or _fallback_card(event, rows, watch_terms)
        event.title = card["title"]
        event.summary = card["summary"]
        event.metadata["event_card"] = card
        cards.append(card)
        if progress_callback:
            progress_callback(index + 1, total, event)
    return cards


def _llm_card(
    event: EventItem,
    rows: list[NewsItem],
    watch_terms: list[str],
    llm_config: dict[str, Any],
) -> dict[str, Any] | None:
    if not rows:
        return None
    evidence_text = "\n".join(
        f"- {item.title} | {item.summary or ''} | 来源:{item.source}" for item in rows[:8]
    )
    system_prompt = (
        "你是A股事件摘要助手。只输出JSON，不要输出投资建议。"
        "标题必须短，摘要必须简洁，保留不确定性。"
    )
    user_prompt = f"""
请把以下多条新闻聚合成一张事件卡。

关注标的/主题：{", ".join(watch_terms[:80])}
原始事件类型：{event.event_type.value}
新闻：
{evidence_text}

输出JSON字段：
title: 18字以内中文标题
summary: 80字以内中文摘要
impact_objects: 最多6个可能受影响的标的/主题
uncertainty: 30字以内不确定性
"""
    payload = llm_json(system_prompt, user_prompt, llm_config)
    if not payload:
        return None
    title = _short(str(payload.get("title") or event.title), 32)
    summary = _short(str(payload.get("summary") or event.summary or event.title), 120)
    impact_objects = payload.get("impact_objects") if isinstance(payload.get("impact_objects"), list) else []
    return {
        "event_id": event.id,
        "title": title,
        "summary": summary,
        "impact_objects": [str(item) for item in impact_objects[:6]],
        "uncertainty": _short(str(payload.get("uncertainty") or ""), 60),
        "evidence_count": len(rows),
        "evidence_links": _evidence_links(rows),
        "generator": "llm",
        "llm_meta": payload.get("_llm_meta") or {},
    }


def _fallback_card(
    event: EventItem,
    rows: list[NewsItem],
    watch_terms: list[str],
) -> dict[str, Any]:
    terms = [term for term in event.metadata.get("cluster_terms", []) if term]
    if not terms:
        terms = _matched_terms(" ".join(item.title for item in rows), watch_terms)
    title = event.title if len(rows) == 1 else f"{'、'.join(terms[:2]) or event.event_type.value}相关事件"
    fragments = []
    for item in rows[:3]:
        candidate = item.summary or item.title
        if candidate and candidate not in fragments:
            fragments.append(candidate)
    summary = "；".join(fragments) or event.summary or title
    return {
        "event_id": event.id,
        "title": _short(title, 32),
        "summary": _short(summary, 120),
        "impact_objects": terms[:6],
        "uncertainty": "未启用大模型，使用规则摘要",
        "evidence_count": len(rows),
        "evidence_links": _evidence_links(rows),
        "generator": "fallback",
    }


def _watch_terms(watchlist: list[WatchSymbol]) -> list[str]:
    terms: list[str] = []
    for item in watchlist:
        for term in [item.symbol, item.name or "", *item.tags]:
            if term and term not in terms:
                terms.append(term)
    return terms


def _matched_terms(text: str, watch_terms: list[str]) -> list[str]:
    return [term for term in watch_terms if term in text][:6]


def _evidence_links(rows: list[NewsItem]) -> list[dict[str, Any]]:
    return [
        {
            "title": item.title,
            "source": item.source,
            "url": item.url,
            "published_at": item.published_at.isoformat() if item.published_at else None,
        }
        for item in rows[:10]
    ]


def _short(value: str, limit: int) -> str:
    clean = " ".join(value.split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip("，。；、 ") + "…"
