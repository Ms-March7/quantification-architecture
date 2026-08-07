from __future__ import annotations

from hashlib import sha256

from app.models import Direction, EventItem, Horizon, NewsItem, ScoreBreakdown, SignalItem, WatchSymbol


def link_events_to_watchlist(
    events: list[EventItem],
    news_by_id: dict[str, NewsItem],
    watchlist: list[WatchSymbol],
) -> list[SignalItem]:
    signals: list[SignalItem] = []
    for event in events:
        if not event.news_ids:
            continue
        for symbol in watchlist:
            matches = [
                match_watch_symbol(news_by_id[news_id], symbol)
                for news_id in event.news_ids
                if news_id in news_by_id
            ]
            match = merge_matches(matches)
            if match["relevance"] <= 0:
                continue
            signals.append(
                SignalItem(
                    id=_signal_id(event.id, symbol.symbol),
                    event_id=event.id,
                    symbol=symbol.symbol,
                    name=symbol.name,
                    direction=Direction.uncertain,
                    primary_horizon=Horizon.short,
                    final_score=0,
                    brief="待分类",
                    score_breakdown=ScoreBreakdown(relevance=match["relevance"]),
                    evidence_news_ids=event.news_ids,
                    metadata={"link": match},
                )
            )
    return signals


def merge_matches(matches: list[dict]) -> dict:
    if not matches:
        return {
            "relevance": 0,
            "matched_direct": [],
            "matched_tags": [],
            "matched_terms": [],
            "link_type": "none",
            "supporting_news_count": 0,
        }
    matched_direct = _unique(term for match in matches for term in match["matched_direct"])
    matched_tags = _unique(term for match in matches for term in match["matched_tags"])
    relevance = max(match["relevance"] for match in matches)
    supporting_news_count = sum(1 for match in matches if match["relevance"] > 0)
    if matched_direct:
        link_type = "direct"
        relevance = min(100, relevance + max(supporting_news_count - 1, 0) * 4)
    elif matched_tags:
        link_type = "theme"
        relevance = min(92, relevance + max(supporting_news_count - 1, 0) * 4)
    else:
        link_type = "none"
    return {
        "relevance": relevance,
        "matched_direct": matched_direct,
        "matched_tags": matched_tags,
        "matched_terms": matched_direct + matched_tags,
        "link_type": link_type,
        "supporting_news_count": supporting_news_count,
    }


def match_watch_symbol(news: NewsItem, symbol: WatchSymbol) -> dict:
    text = _normalize_text(f"{news.title} {news.summary or ''} {news.raw_text or ''}")
    direct_terms = [symbol.symbol, symbol.name or ""]
    tag_terms = list(symbol.tags)

    matched_direct = [term for term in direct_terms if term and _contains(text, term)]
    matched_tags = [term for term in tag_terms if term and _contains(text, term)]

    if matched_direct:
        relevance = 100
    elif matched_tags:
        base = 70 if symbol.asset_type.value == "etf" else 60
        relevance = min(90, base + max(len(matched_tags) - 1, 0) * 8)
    else:
        relevance = 0

    return {
        "relevance": relevance,
        "matched_direct": matched_direct,
        "matched_tags": matched_tags,
        "matched_terms": matched_direct + matched_tags,
        "link_type": "direct" if matched_direct else "theme" if matched_tags else "none",
    }


def _contains(normalized_text: str, term: str) -> bool:
    return term.lower() in normalized_text


def _normalize_text(text: str) -> str:
    return " ".join(text.lower().split())


def _unique(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _signal_id(event_id: str, symbol: str) -> str:
    raw = f"{event_id}:{symbol}"
    return f"signal_{sha256(raw.encode('utf-8')).hexdigest()[:16]}"
