from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from app.config import load_scoring_weights, load_watchlist
from app.models import Direction, EventItem, Horizon, NewsItem, NewsType, ScoreBreakdown, SignalItem
from app.report.render import render_html_report, render_markdown_report
from app.scoring.explainable import final_score
from app.workflow.engine import DEFAULT_NODE_ORDER
from app.workflow.schemas import NodeStatus, WorkflowNodeRun


@dataclass
class DemoContext:
    project_root: Path
    run_id: str
    weights: dict[str, float]
    news_items: list[NewsItem] = field(default_factory=list)
    events: list[EventItem] = field(default_factory=list)
    signals: list[SignalItem] = field(default_factory=list)
    bullish_top: list[SignalItem] = field(default_factory=list)
    bearish_top: list[SignalItem] = field(default_factory=list)
    nodes: list[WorkflowNodeRun] = field(default_factory=list)


def run_demo_workflow(project_root: Path) -> dict[str, str]:
    project_root = project_root.resolve()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    weights = load_scoring_weights(project_root)
    context = DemoContext(project_root=project_root, run_id=run_id, weights=weights)

    for node_name in DEFAULT_NODE_ORDER:
        runner = NODE_RUNNERS[node_name]
        started_at = datetime.now(timezone.utc)
        output_ids = runner(context)
        context.nodes.append(
            WorkflowNodeRun(
                node_name=node_name,
                status=NodeStatus.success,
                output_ids=output_ids,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                decisions=_node_decisions(node_name, context),
            )
        )

    reports_dir = project_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = reports_dir / "latest.md"
    html_path = reports_dir / "latest.html"
    markdown_path.write_text(render_markdown_report(context), encoding="utf-8")
    html_path.write_text(render_html_report(context), encoding="utf-8")

    return {
        "run_id": run_id,
        "markdown_path": str(markdown_path),
        "html_path": str(html_path),
    }


def collect(context: DemoContext) -> list[str]:
    now = datetime.now(timezone.utc)
    rows = [
        {
            "title": "半导体存储方向成交活跃但高位分化",
            "summary": "存储芯片链出现放量波动，部分高位个股回撤。",
            "source": "mock-eastmoney",
            "source_type": "data_vendor",
            "event_type": NewsType.industry,
            "targets": [
                {
                    "symbol": "603986",
                    "direction": Direction.bearish,
                    "horizon": Horizon.short,
                    "brief": "暂无公司公告利空，更偏板块高位分化和交易拥挤。",
                    "relevance": 100,
                    "impact": 78,
                    "market_validation": 88,
                    "uncertainty_penalty": 18,
                },
                {
                    "symbol": "159995",
                    "direction": Direction.bearish,
                    "horizon": Horizon.short,
                    "brief": "芯片主题受高位分化拖累，短期信号偏负。",
                    "relevance": 80,
                    "impact": 72,
                    "market_validation": 76,
                    "uncertainty_penalty": 12,
                },
            ],
        },
        {
            "title": "功率半导体企业加速布局 SiC 车规产品",
            "summary": "SiC、IGBT 和车规功率器件景气度被多家机构关注。",
            "source": "mock-industry",
            "source_type": "mainstream_media",
            "event_type": NewsType.industry,
            "targets": [
                {
                    "symbol": "300373",
                    "direction": Direction.bullish,
                    "horizon": Horizon.mid,
                    "brief": "产业新闻与公司业务标签匹配，中期利好权重较高。",
                    "relevance": 90,
                    "impact": 82,
                    "market_validation": 65,
                    "uncertainty_penalty": 10,
                }
            ],
        },
        {
            "title": "中证A500成长权重波动加大",
            "summary": "部分大盘成长和AI硬件权重波动放大，宽基指数承压。",
            "source": "mock-market",
            "source_type": "data_vendor",
            "event_type": NewsType.media,
            "targets": [
                {
                    "symbol": "512050",
                    "direction": Direction.bearish,
                    "horizon": Horizon.short,
                    "brief": "A500权重里的成长资产承压，短期拖累宽基。",
                    "relevance": 100,
                    "impact": 70,
                    "market_validation": 82,
                    "uncertainty_penalty": 8,
                }
            ],
        },
        {
            "title": "国产替代与先进封装政策预期升温",
            "summary": "半导体自主可控、先进封装和国产替代方向出现中长期催化。",
            "source": "mock-policy",
            "source_type": "mainstream_media",
            "event_type": NewsType.policy,
            "targets": [
                {
                    "symbol": "159995",
                    "direction": Direction.bullish,
                    "horizon": Horizon.long,
                    "brief": "政策预期更偏中长期，对芯片ETF构成主题支撑。",
                    "relevance": 75,
                    "impact": 76,
                    "market_validation": 58,
                    "uncertainty_penalty": 15,
                },
                {
                    "symbol": "603986",
                    "direction": Direction.bullish,
                    "horizon": Horizon.long,
                    "brief": "国产替代叙事与存储芯片相关，但需要后续公告或订单验证。",
                    "relevance": 65,
                    "impact": 70,
                    "market_validation": 52,
                    "uncertainty_penalty": 20,
                },
            ],
        },
    ]
    context.news_items = []
    for row in rows:
        digest = sha256(row["title"].encode("utf-8")).hexdigest()[:16]
        context.news_items.append(
            NewsItem(
                id=f"news_{digest}",
                title=row["title"],
                summary=row["summary"],
                source=row["source"],
                source_type=row["source_type"],
                published_at=now,
                metadata={"event_type": row["event_type"], "targets": row["targets"]},
            )
        )
    return [item.id for item in context.news_items]


def normalize(context: DemoContext) -> list[str]:
    return [item.id for item in context.news_items]


def dedup(context: DemoContext) -> list[str]:
    seen: set[str] = set()
    deduped: list[NewsItem] = []
    for item in context.news_items:
        key = item.title.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    context.news_items = deduped
    return [item.id for item in context.news_items]


def event_cluster(context: DemoContext) -> list[str]:
    context.events = []
    for item in context.news_items:
        event_type = item.metadata.get("event_type", NewsType.unknown)
        context.events.append(
            EventItem(
                id=item.id.replace("news_", "event_"),
                title=item.title,
                summary=item.summary,
                news_ids=[item.id],
                event_type=event_type,
                first_seen_at=item.published_at,
                last_seen_at=item.published_at,
            )
        )
    return [event.id for event in context.events]


def entity_link(context: DemoContext) -> list[str]:
    watchlist = {item.symbol: item for item in load_watchlist(context.project_root)}
    linked_ids: list[str] = []
    signals: list[SignalItem] = []
    news_by_id = {item.id: item for item in context.news_items}
    for event in context.events:
        news = news_by_id[event.news_ids[0]]
        for target in news.metadata.get("targets", []):
            symbol = target["symbol"]
            if symbol not in watchlist:
                continue
            signal_id = _signal_id(event.id, symbol, target["direction"])
            signals.append(
                SignalItem(
                    id=signal_id,
                    event_id=event.id,
                    symbol=symbol,
                    name=watchlist[symbol].name,
                    direction=target["direction"],
                    primary_horizon=target["horizon"],
                    final_score=0,
                    brief=target["brief"],
                    score_breakdown=ScoreBreakdown(relevance=target["relevance"]),
                    evidence_news_ids=event.news_ids,
                    metadata={"target": target},
                )
            )
            linked_ids.append(signal_id)
    context.signals = signals
    return linked_ids


def classify(context: DemoContext) -> list[str]:
    for signal in context.signals:
        target = signal.metadata["target"]
        signal.score_breakdown.direction_confidence = target.get("direction_confidence", 86)
        signal.score_breakdown.source_credibility = _source_score(signal, context)
        signal.score_breakdown.impact = target["impact"]
    return [signal.id for signal in context.signals]


def horizon(context: DemoContext) -> list[str]:
    return [signal.id for signal in context.signals]


def score(context: DemoContext) -> list[str]:
    for signal in context.signals:
        target = signal.metadata["target"]
        signal.score_breakdown.freshness = 95
        signal.score_breakdown.corroboration = 55
        signal.score_breakdown.market_validation = target["market_validation"]
        signal.score_breakdown.uncertainty_penalty = target["uncertainty_penalty"]
        signal.final_score = round(final_score(signal.direction, signal.score_breakdown, context.weights), 2)
        signal.horizon_scores = _horizon_scores(signal)
    return [signal.id for signal in context.signals]


def market_validate(context: DemoContext) -> list[str]:
    for signal in context.signals:
        validation = signal.score_breakdown.market_validation
        if validation < 60:
            signal.warnings.append("market_validation_weak")
        if signal.score_breakdown.uncertainty_penalty >= 18:
            signal.warnings.append("high_uncertainty")
    return [signal.id for signal in context.signals]


def rank(context: DemoContext) -> list[str]:
    context.bullish_top = sorted(
        [signal for signal in context.signals if signal.final_score > 0],
        key=lambda item: item.final_score,
        reverse=True,
    )[:10]
    context.bearish_top = sorted(
        [signal for signal in context.signals if signal.final_score < 0],
        key=lambda item: item.final_score,
    )[:10]
    return [signal.id for signal in context.bullish_top + context.bearish_top]


def notify(context: DemoContext) -> list[str]:
    # First runnable version keeps notification in dry-run mode.
    return []


def memory_store(context: DemoContext) -> list[str]:
    return []


NODE_RUNNERS = {
    "collect": collect,
    "normalize": normalize,
    "dedup": dedup,
    "event_cluster": event_cluster,
    "entity_link": entity_link,
    "classify": classify,
    "horizon": horizon,
    "score": score,
    "market_validate": market_validate,
    "rank": rank,
    "memory_store": memory_store,
    "notify": notify,
}


def _source_score(signal: SignalItem, context: DemoContext) -> float:
    event = next(item for item in context.events if item.id == signal.event_id)
    news = next(item for item in context.news_items if item.id == event.news_ids[0])
    return {
        "official": 100,
        "exchange": 100,
        "regulator": 100,
        "mainstream_media": 80,
        "data_vendor": 75,
        "normal_media": 60,
        "rumor": 25,
    }.get(news.source_type, 50)


def _horizon_scores(signal: SignalItem) -> dict[Horizon, float]:
    multipliers = {
        Horizon.short: {Horizon.short: 1.0, Horizon.mid: 0.55, Horizon.long: 0.25},
        Horizon.mid: {Horizon.short: 0.55, Horizon.mid: 1.0, Horizon.long: 0.65},
        Horizon.long: {Horizon.short: 0.35, Horizon.mid: 0.75, Horizon.long: 1.0},
    }
    return {
        horizon: round(signal.final_score * multiplier, 2)
        for horizon, multiplier in multipliers[signal.primary_horizon].items()
    }


def _signal_id(event_id: str, symbol: str, direction: Direction) -> str:
    raw = f"{event_id}:{symbol}:{direction.value}"
    return f"signal_{sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _node_decisions(node_name: str, context: DemoContext) -> list[dict[str, Any]]:
    if node_name == "collect":
        return [{"label": "mock_news_collected", "value": len(context.news_items)}]
    if node_name == "event_cluster":
        return [{"label": "events_created", "value": len(context.events)}]
    if node_name == "entity_link":
        return [{"label": "signals_created", "value": len(context.signals)}]
    if node_name == "rank":
        return [
            {"label": "bullish_top_count", "value": len(context.bullish_top)},
            {"label": "bearish_top_count", "value": len(context.bearish_top)},
        ]
    return []
