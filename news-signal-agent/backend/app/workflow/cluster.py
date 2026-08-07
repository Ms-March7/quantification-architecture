from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256

from app.classify.rules import infer_news_type
from app.models import EventItem, NewsItem, WatchSymbol


THEME_KEYWORDS = (
    "半导体",
    "芯片",
    "存储",
    "功率半导体",
    "SiC",
    "AI",
    "人工智能",
    "算力",
    "大模型",
    "机器人",
    "PCB",
    "CPO",
    "新能源",
    "有色",
    "黄金",
    "铜",
    "锂",
    "稀土",
    "A500",
    "中证A500",
    "美股",
    "纳指",
    "苹果",
    "英伟达",
    "美光",
    "政策",
    "公告",
    "业绩",
    "回购",
    "减持",
)


def cluster_news_items(
    news_items: list[NewsItem],
    watchlist: list[WatchSymbol],
    max_summary_chars: int = 150,
) -> list[EventItem]:
    raw_buckets: dict[str, list[NewsItem]] = defaultdict(list)
    for item in news_items:
        terms = matched_watch_terms(item, watchlist) or fallback_terms(item)
        event_type = infer_news_type(text_of(item)).value
        day_key = _day_key(item.published_at)
        primary = _primary_cluster_term(terms, item)
        key = f"{event_type}:{primary}:{day_key}"
        raw_buckets[key].append(item)

    buckets = _merge_similar_buckets(raw_buckets, watchlist)
    events: list[EventItem] = []
    for key, rows in buckets.items():
        rows = sorted(
            rows,
            key=lambda item: item.published_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        terms = _top_terms(rows, watchlist)
        event_id = "event_" + sha256(key.encode("utf-8")).hexdigest()[:16]
        first_seen = min((item.published_at for item in rows if item.published_at), default=None)
        last_seen = max((item.published_at for item in rows if item.published_at), default=None)
        event_type = infer_news_type(" ".join(text_of(item) for item in rows))
        events.append(
            EventItem(
                id=event_id,
                title=_event_title(rows, terms),
                summary=_event_summary(rows, max_summary_chars=max_summary_chars),
                news_ids=[item.id for item in rows],
                event_type=event_type,
                first_seen_at=first_seen,
                last_seen_at=last_seen,
                metadata={
                    "cluster_key": key,
                    "cluster_method": "keyword_date_type_semantic_rule",
                    "cluster_terms": terms,
                    "news_count": len(rows),
                    "sources": sorted({item.source for item in rows}),
                    "latest_url": rows[0].url,
                    "latest_source": rows[0].source,
                    "column_names": sorted(
                        {
                            str(item.metadata.get("column_name"))
                            for item in rows
                            if item.metadata.get("column_name")
                        }
                    ),
                },
            )
        )
    return sorted(
        events,
        key=lambda item: item.last_seen_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


def matched_watch_terms(news: NewsItem, watchlist: list[WatchSymbol]) -> list[str]:
    text = text_of(news).lower()
    terms: list[str] = []
    for symbol in watchlist:
        candidates = [symbol.symbol, symbol.name or "", *symbol.tags]
        for candidate in candidates:
            if candidate and candidate.lower() in text and candidate not in terms:
                terms.append(candidate)
    return terms


def fallback_terms(news: NewsItem) -> list[str]:
    text = text_of(news)
    return [keyword for keyword in THEME_KEYWORDS if keyword in text]


def text_of(news: NewsItem) -> str:
    return f"{news.title} {news.summary or ''} {news.raw_text or ''}"


def _primary_cluster_term(terms: list[str], item: NewsItem) -> str:
    if terms:
        return _normalize_term(terms[0])
    return _title_key(item.title)


def _top_terms(rows: list[NewsItem], watchlist: list[WatchSymbol]) -> list[str]:
    counter: Counter[str] = Counter()
    for item in rows:
        counter.update(matched_watch_terms(item, watchlist) or fallback_terms(item))
    return [term for term, _ in counter.most_common(5)]


def _event_title(rows: list[NewsItem], terms: list[str]) -> str:
    if len(rows) == 1:
        return rows[0].title
    if terms:
        return f"{'、'.join(terms[:3])}相关事件（{len(rows)}条）"
    return f"{rows[0].title} 等（{len(rows)}条）"


def _event_summary(rows: list[NewsItem], max_summary_chars: int) -> str:
    fragments: list[str] = []
    for item in rows[:3]:
        candidate = _compact(item.summary or item.title)
        if candidate and candidate not in fragments:
            fragments.append(candidate)
    summary = "；".join(fragments)
    if len(summary) <= max_summary_chars:
        return summary
    return summary[: max_summary_chars - 1].rstrip("，。；、 ") + "…"


def _compact(text: str) -> str:
    cleaned = " ".join(text.split())
    for prefix in ("【", "["):
        if cleaned.startswith(prefix):
            cleaned = cleaned.replace("【", "", 1).replace("[", "", 1)
            cleaned = cleaned.split("】", 1)[-1].split("]", 1)[-1].strip()
    return cleaned


def _day_key(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    return value.strftime("%Y%m%d")


def _normalize_term(term: str) -> str:
    return term.strip().lower()


def _title_key(title: str) -> str:
    return sha256(title[:24].encode("utf-8")).hexdigest()[:8]


def _merge_similar_buckets(
    buckets: dict[str, list[NewsItem]],
    watchlist: list[WatchSymbol],
) -> dict[str, list[NewsItem]]:
    merged: dict[str, list[NewsItem]] = {}
    for key, rows in buckets.items():
        event_type, _, day_key = _split_cluster_key(key)
        target_key = None
        for existing_key, existing_rows in merged.items():
            existing_type, _, existing_day = _split_cluster_key(existing_key)
            if existing_type != event_type or existing_day != day_key:
                continue
            if _bucket_similarity(rows, existing_rows, watchlist) >= 0.42:
                target_key = existing_key
                break
        if target_key:
            merged[target_key].extend(rows)
        else:
            merged[key] = list(rows)
    return merged


def _bucket_similarity(
    left: list[NewsItem],
    right: list[NewsItem],
    watchlist: list[WatchSymbol],
) -> float:
    left_text = " ".join(text_of(item) for item in left)
    right_text = " ".join(text_of(item) for item in right)
    left_terms = set(_terms_from_text(left_text, watchlist))
    right_terms = set(_terms_from_text(right_text, watchlist))
    term_score = _jaccard(left_terms, right_terms)
    text_score = _jaccard(_char_ngrams(left_text), _char_ngrams(right_text))
    embedding_score = _optional_embedding_similarity(left_text, right_text)
    return max(term_score, text_score, embedding_score)


def _terms_from_text(text: str, watchlist: list[WatchSymbol]) -> list[str]:
    lower = text.lower()
    terms: list[str] = []
    for symbol in watchlist:
        for candidate in [symbol.symbol, symbol.name or "", *symbol.tags]:
            if candidate and candidate.lower() in lower:
                terms.append(candidate.lower())
    terms.extend(keyword.lower() for keyword in THEME_KEYWORDS if keyword in text)
    return terms


def _char_ngrams(text: str, size: int = 3) -> set[str]:
    cleaned = "".join(ch.lower() for ch in text if not ch.isspace())
    if len(cleaned) <= size:
        return {cleaned} if cleaned else set()
    return {cleaned[index : index + size] for index in range(len(cleaned) - size + 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0
    return len(left & right) / len(left | right)


def _split_cluster_key(key: str) -> tuple[str, str, str]:
    parts = key.split(":", 2)
    if len(parts) != 3:
        return key, "", ""
    return parts[0], parts[1], parts[2]


def _optional_embedding_similarity(left_text: str, right_text: str) -> float:
    try:
        from sentence_transformers import SentenceTransformer
        from sentence_transformers.util import cos_sim
    except Exception:
        return 0
    try:
        model = _embedding_model()
        embeddings = model.encode([left_text[:1200], right_text[:1200]], normalize_embeddings=True)
        return float(cos_sim(embeddings[0], embeddings[1]).item())
    except Exception:
        return 0


def _embedding_model():
    global _CACHED_EMBEDDING_MODEL
    try:
        return _CACHED_EMBEDDING_MODEL
    except NameError:
        from sentence_transformers import SentenceTransformer

        _CACHED_EMBEDDING_MODEL = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
        return _CACHED_EMBEDDING_MODEL
