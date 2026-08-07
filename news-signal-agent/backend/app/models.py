from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AssetType(str, Enum):
    stock = "stock"
    etf = "etf"
    fund = "fund"
    reit = "reit"
    index = "index"
    unknown = "unknown"


class Direction(str, Enum):
    bullish = "bullish"
    bearish = "bearish"
    neutral = "neutral"
    uncertain = "uncertain"


class Horizon(str, Enum):
    short = "short"
    mid = "mid"
    long = "long"


class NewsType(str, Enum):
    announcement = "announcement"
    policy = "policy"
    industry = "industry"
    overseas = "overseas"
    commodity = "commodity"
    regulator = "regulator"
    research = "research"
    rumor = "rumor"
    media = "media"
    unknown = "unknown"


class WatchSymbol(BaseModel):
    symbol: str
    name: str | None = None
    asset_type: AssetType = AssetType.unknown
    market: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NewsItem(BaseModel):
    id: str
    title: str
    summary: str | None = None
    source: str
    source_type: str
    url: str | None = None
    published_at: datetime | None = None
    raw_text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EventItem(BaseModel):
    id: str
    title: str
    summary: str | None = None
    news_ids: list[str] = Field(default_factory=list)
    event_type: NewsType = NewsType.unknown
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScoreBreakdown(BaseModel):
    direction_confidence: float = 0
    relevance: float = 0
    source_credibility: float = 0
    impact: float = 0
    freshness: float = 0
    corroboration: float = 0
    market_validation: float = 0
    uncertainty_penalty: float = 0

    def weighted_score(self, weights: dict[str, float]) -> float:
        return (
            self.relevance * weights.get("relevance", 0)
            + self.source_credibility * weights.get("source_credibility", 0)
            + self.impact * weights.get("impact", 0)
            + self.freshness * weights.get("freshness", 0)
            + self.corroboration * weights.get("corroboration", 0)
            + self.market_validation * weights.get("market_validation", 0)
            + self.direction_confidence * weights.get("direction_confidence", 0)
        )


class SignalItem(BaseModel):
    id: str
    event_id: str
    symbol: str
    name: str | None = None
    direction: Direction
    primary_horizon: Horizon
    final_score: float
    horizon_scores: dict[Horizon, float] = Field(default_factory=dict)
    brief: str
    score_breakdown: ScoreBreakdown
    evidence_news_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class UserOverride(BaseModel):
    id: str
    target_type: str
    target_id: str
    field: str
    old_value: Any
    new_value: Any
    reason: str
    apply_scope: str = "this_event_only"
    created_at: datetime = Field(default_factory=datetime.utcnow)
