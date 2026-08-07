from __future__ import annotations

from typing import Any

from app.llm.client import llm_json
from app.models import Direction, Horizon, NewsItem, SignalItem


def classify_signal_with_llm(
    signal: SignalItem,
    news: NewsItem,
    llm_config: dict[str, Any],
    apply: bool = True,
) -> bool:
    payload = llm_json(
        system_prompt="你是A股新闻事件分类助手。只输出JSON，不输出投资建议。",
        user_prompt=_prompt(signal, news),
        config=llm_config,
    )
    if not payload:
        signal.metadata["llm_classification_status"] = "unavailable"
        return False
    try:
        direction = Direction(str(payload.get("direction", "uncertain")))
        horizon = Horizon(str(payload.get("horizon", "mid")))
    except ValueError:
        signal.metadata["llm_classification_status"] = "invalid_payload"
        return False
    confidence = _bounded(payload.get("confidence"), default=55)
    impact = _bounded(payload.get("impact"), default=60)
    uncertainty = _bounded(payload.get("uncertainty_penalty"), default=12)
    reason = str(payload.get("reason") or "大模型给出方向判断")
    result = {
        "direction": direction.value,
        "horizon": horizon.value,
        "confidence": confidence,
        "impact": impact,
        "uncertainty_penalty": uncertainty,
        "reason": reason[:80],
        "raw": payload,
        "meta": payload.get("_llm_meta") or {},
    }
    signal.metadata["llm_classification"] = result
    signal.metadata["llm_classification_status"] = "ok"
    if apply:
        apply_llm_classification(signal, result)
        signal.warnings.append("llm_classified")
    return True


def apply_llm_classification(signal: SignalItem, result: dict[str, Any]) -> None:
    direction = Direction(str(result.get("direction", "uncertain")))
    horizon = Horizon(str(result.get("horizon", "mid")))
    confidence = _bounded(result.get("confidence"), default=55)
    impact = _bounded(result.get("impact"), default=60)
    uncertainty = _bounded(result.get("uncertainty_penalty"), default=12)
    reason = str(result.get("reason") or "大模型给出方向判断")
    signal.direction = direction
    signal.primary_horizon = horizon
    signal.brief = f"LLM判断: {reason}"
    signal.score_breakdown.direction_confidence = confidence
    signal.score_breakdown.impact = max(signal.score_breakdown.impact, impact)
    signal.score_breakdown.uncertainty_penalty = uncertainty


def _prompt(signal: SignalItem, news: NewsItem) -> str:
    link = signal.metadata.get("link", {})
    return f"""
请判断以下事件对A股标的的影响，只输出JSON。

标的：{signal.symbol} {signal.name or ""}
匹配信息：{link}
标题：{news.title}
摘要：{news.summary or ""}
正文片段：{(news.raw_text or "")[:1200]}

JSON字段：
direction: bullish/bearish/neutral/uncertain
confidence: 0-100
horizon: short/mid/long
impact: 0-100
uncertainty_penalty: 0-40
reason: 40字以内中文理由
"""


def _bounded(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(100, number))
