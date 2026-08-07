from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models import Direction, Horizon, NewsItem, NewsType, SignalItem


BULLISH_KEYWORDS: dict[str, int] = {
    "利好": 4,
    "超预期": 4,
    "大涨": 3,
    "涨超": 3,
    "涨停": 3,
    "强势": 2,
    "上调": 2,
    "景气": 2,
    "订单": 2,
    "扩产": 2,
    "涨价": 2,
    "短缺": 2,
    "突破": 2,
    "创新高": 2,
    "自主可控": 2,
    "国产替代": 2,
    "中标": 2,
    "回购": 2,
    "增持": 2,
    "签约": 2,
    "规划": 1,
    "印发": 1,
}

BEARISH_KEYWORDS: dict[str, int] = {
    "利空": 4,
    "不及预期": 4,
    "大跌": 3,
    "暴跌": 3,
    "跌停": 3,
    "下跌": 2,
    "跌近": 2,
    "跌超": 2,
    "回调": 2,
    "承压": 2,
    "走弱": 2,
    "减持": 2,
    "亏损": 2,
    "处罚": 2,
    "问询": 2,
    "终止": 2,
    "过剩": 2,
    "价格战": 2,
    "高位分化": 2,
    "业绩预降": 2,
    "解禁": 1,
}

LONG_HORIZON_KEYWORDS = (
    "十五五",
    "2030",
    "长期",
    "规划",
    "自主可控",
    "国产替代",
    "产业趋势",
)
MID_HORIZON_KEYWORDS = ("订单", "扩产", "业绩", "涨价", "短缺", "景气", "中标", "签约")
SHORT_HORIZON_KEYWORDS = (
    "今日",
    "盘中",
    "收盘",
    "涨停",
    "跌停",
    "大涨",
    "大跌",
    "回调",
    "走弱",
    "强势",
)


def classify_signal_with_rules(
    signal: SignalItem,
    news: NewsItem,
    source_scores: dict[str, Any] | None = None,
) -> None:
    text = f"{news.title} {news.summary or ''} {news.raw_text or ''}"
    bullish_hits = _keyword_hits(text, BULLISH_KEYWORDS)
    bearish_hits = _keyword_hits(text, BEARISH_KEYWORDS)
    bullish_score = sum(weight for _, weight in bullish_hits)
    bearish_score = sum(weight for _, weight in bearish_hits)

    if bullish_score > bearish_score:
        direction = Direction.bullish
        winning_score = bullish_score
        losing_score = bearish_score
        winning_hits = bullish_hits
        direction_label = "利好"
    elif bearish_score > bullish_score:
        direction = Direction.bearish
        winning_score = bearish_score
        losing_score = bullish_score
        winning_hits = bearish_hits
        direction_label = "利空"
    else:
        direction = Direction.uncertain
        winning_score = max(bullish_score, bearish_score)
        losing_score = min(bullish_score, bearish_score)
        winning_hits = bullish_hits + bearish_hits
        direction_label = "方向不明"

    link = signal.metadata.get("link", {})
    matched_terms = link.get("matched_terms", [])
    signal.direction = direction
    signal.primary_horizon = infer_horizon(text)
    signal.brief = _brief(direction_label, winning_hits, matched_terms)
    signal.score_breakdown.direction_confidence = _direction_confidence(
        direction, winning_score, losing_score
    )
    signal.score_breakdown.source_credibility = _source_credibility(news.source_type, source_scores)
    signal.score_breakdown.impact = _impact_score(winning_score, signal.score_breakdown.relevance)
    signal.score_breakdown.freshness = freshness_score(news.published_at)
    signal.score_breakdown.market_validation = 50
    signal.score_breakdown.uncertainty_penalty = _uncertainty_penalty(
        direction, link, bullish_score, bearish_score
    )
    signal.metadata["classification"] = {
        "bullish_hits": [keyword for keyword, _ in bullish_hits],
        "bearish_hits": [keyword for keyword, _ in bearish_hits],
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "event_type": infer_news_type(text).value,
    }
    signal.metadata["rule_classification"] = {
        "direction": direction.value,
        "horizon": signal.primary_horizon.value,
        "confidence": signal.score_breakdown.direction_confidence,
        "impact": signal.score_breakdown.impact,
        "uncertainty_penalty": signal.score_breakdown.uncertainty_penalty,
        "brief": signal.brief,
        "bullish_hits": [keyword for keyword, _ in bullish_hits],
        "bearish_hits": [keyword for keyword, _ in bearish_hits],
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "event_type": infer_news_type(text).value,
    }
    if direction == Direction.uncertain:
        signal.warnings.append("direction_uncertain")
    if link.get("link_type") == "theme":
        signal.warnings.append("theme_link_only")


def infer_horizon(text: str) -> Horizon:
    if any(keyword in text for keyword in LONG_HORIZON_KEYWORDS):
        return Horizon.long
    if any(keyword in text for keyword in SHORT_HORIZON_KEYWORDS):
        return Horizon.short
    if any(keyword in text for keyword in MID_HORIZON_KEYWORDS):
        return Horizon.mid
    return Horizon.mid


def infer_news_type(text: str) -> NewsType:
    if any(keyword in text for keyword in ("公告", "披露", "晚间公告", "定增", "回购")):
        return NewsType.announcement
    if any(keyword in text for keyword in ("政策", "规划", "发改委", "工信部", "财政部", "央行")):
        return NewsType.policy
    if any(
        keyword in text
        for keyword in ("美股", "纳指", "美元", "美债", "苹果", "英伟达", "美光", "费城半导体")
    ):
        return NewsType.overseas
    if any(keyword in text for keyword in ("黄金", "铜", "原油", "锂", "稀土", "商品")):
        return NewsType.commodity
    if any(keyword in text for keyword in ("研报", "机构", "评级", "目标价")):
        return NewsType.research
    if any(
        keyword in text
        for keyword in (
            "芯片",
            "半导体",
            "AI",
            "人工智能",
            "机器人",
            "算力",
            "PCB",
            "CPO",
            "新能源",
        )
    ):
        return NewsType.industry
    return NewsType.media


def freshness_score(published_at: datetime | None) -> float:
    if published_at is None:
        return 45
    now = datetime.now(published_at.tzinfo or timezone.utc)
    age_hours = max((now - published_at).total_seconds() / 3600, 0)
    if age_hours <= 2:
        return 100
    if age_hours <= 12:
        return 90
    if age_hours <= 24:
        return 75
    if age_hours <= 72:
        return 55
    return 35


def _keyword_hits(text: str, keywords: dict[str, int]) -> list[tuple[str, int]]:
    return [(keyword, weight) for keyword, weight in keywords.items() if keyword in text]


def _direction_confidence(direction: Direction, winning_score: int, losing_score: int) -> float:
    if direction == Direction.uncertain:
        return 42 if winning_score else 35
    return min(92, 55 + (winning_score - losing_score) * 8 + winning_score * 2)


def _source_credibility(source_type: str, source_scores: dict[str, Any] | None) -> float:
    if not source_scores:
        return 60
    return float(source_scores.get(source_type, 60))


def _impact_score(winning_score: int, relevance: float) -> float:
    return min(92, 42 + winning_score * 7 + relevance * 0.12)


def _uncertainty_penalty(
    direction: Direction,
    link: dict[str, Any],
    bullish_score: int,
    bearish_score: int,
) -> float:
    penalty = 8
    if direction == Direction.uncertain:
        penalty += 22
    if link.get("link_type") == "theme":
        penalty += 10
    if bullish_score and bearish_score:
        penalty += min(12, abs(bullish_score - bearish_score) + 4)
    return penalty


def _brief(direction_label: str, hits: list[tuple[str, int]], matched_terms: list[str]) -> str:
    hit_text = "、".join(keyword for keyword, _ in hits[:3]) or "无明确关键词"
    match_text = "、".join(matched_terms[:4]) or "无明确匹配"
    return f"{direction_label}关键词: {hit_text}; 关联: {match_text}。"
