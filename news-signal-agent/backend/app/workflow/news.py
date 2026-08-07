from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from app.classify.llm import apply_llm_classification, classify_signal_with_llm
from app.classify.rules import classify_signal_with_rules, infer_news_type
from app.collectors.cninfo import collect_cninfo_announcements
from app.collectors.eastmoney import EastmoneyColumn, collect_eastmoney_column_news
from app.collectors.global_markets import collect_global_market_moves
from app.collectors.quotes import fetch_eastmoney_quotes
from app.collectors.rss import collect_rss_feeds
from app.collectors.search import collect_web_search_news
from app.config import load_scoring_config, load_scoring_weights, load_sources_config, load_watchlist
from app.linker.rules import link_events_to_watchlist
from app.models import EventItem, Horizon, NewsItem, SignalItem
from app.notify.dispatcher import dispatch_notifications
from app.report.render import render_html_report, render_markdown_report
from app.scoring.explainable import final_score
from app.storage.sqlite import (
    active_user_overrides,
    apply_signal_override,
    load_latest_workflow_artifact,
    store_event_cards,
    store_rank_history,
    store_raw_news_items,
    store_retained_signals,
    store_workflow_artifact,
    upsert_event_store,
)
from app.validation.backtest import validate_signals_with_event_study
from app.workflow.demo import collect as collect_mock
from app.workflow.cluster import cluster_news_items
from app.workflow.event_cards import build_event_cards
from app.workflow.engine import DEFAULT_NODE_ORDER
from app.workflow.schemas import NodeStatus, WorkflowNodeRun

ProgressCallback = Callable[[dict[str, Any]], None]


@dataclass
class NewsWorkflowContext:
    project_root: Path
    run_id: str
    weights: dict[str, float]
    scoring_config: dict[str, Any]
    run_mode: str = "fast"
    allow_mock_fallback: bool = False
    report_title: str = "News Signal Agent Report"
    news_items: list[NewsItem] = field(default_factory=list)
    events: list[EventItem] = field(default_factory=list)
    signals: list[SignalItem] = field(default_factory=list)
    bullish_top: list[SignalItem] = field(default_factory=list)
    bearish_top: list[SignalItem] = field(default_factory=list)
    nodes: list[WorkflowNodeRun] = field(default_factory=list)
    retained_events: list[dict[str, Any]] = field(default_factory=list)
    notifications: list[dict[str, Any]] = field(default_factory=list)
    collection_errors: list[str] = field(default_factory=list)
    source_counts: dict[str, int] = field(default_factory=dict)
    event_cards: list[dict[str, Any]] = field(default_factory=list)
    backtest_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    fallback_used: bool = False
    llm_runtime: dict[str, Any] = field(default_factory=dict)
    progress_callback: ProgressCallback | None = None
    progress_node_index: int = 0
    progress_total_nodes: int = 0
    progress_node_started_at: datetime | None = None


def run_news_workflow(project_root: Path, allow_mock_fallback: bool = False) -> dict[str, str]:
    context, report_paths = _run_news_context(project_root, allow_mock_fallback)
    return {
        "run_id": context.run_id,
        "markdown_path": str(report_paths["markdown_path"]),
        "html_path": str(report_paths["html_path"]),
        "news_items": str(len(context.news_items)),
        "signals": str(len(context.signals)),
    }


def run_news_workflow_snapshot(
    project_root: Path,
    allow_mock_fallback: bool = False,
    start_from: str | None = None,
    base_run_id: str | None = None,
    progress_callback: ProgressCallback | None = None,
    run_mode: str = "fast",
) -> dict[str, Any]:
    context, report_paths = _run_news_context(
        project_root,
        allow_mock_fallback,
        start_from,
        base_run_id,
        progress_callback,
        run_mode,
    )
    watchlist = load_watchlist(context.project_root)
    return {
        "run_id": context.run_id,
        "run_mode": context.run_mode,
        "stats": {
            "news_items": len(context.news_items),
            "events": len(context.events),
            "signals": len(context.signals),
            "bullish": len(context.bullish_top),
            "bearish": len(context.bearish_top),
            "retained": len(context.retained_events),
            "notifications": len(context.notifications),
            "event_cards": len(context.event_cards),
        },
        "report_paths": {key: str(value) for key, value in report_paths.items()},
        "watchlist": [item.model_dump(mode="json") for item in watchlist],
        "nodes": [node.model_dump(mode="json") for node in context.nodes],
        "news_items": [item.model_dump(mode="json") for item in context.news_items],
        "events": [item.model_dump(mode="json") for item in context.events],
        "signals": [_signal_snapshot(signal, context) for signal in context.signals],
        "bullish_top": [_signal_snapshot(signal, context) for signal in context.bullish_top],
        "bearish_top": [_signal_snapshot(signal, context) for signal in context.bearish_top],
        "retained_events": context.retained_events,
        "notifications": context.notifications,
        "event_cards": context.event_cards,
    }


def _run_news_context(
    project_root: Path,
    allow_mock_fallback: bool = False,
    start_from: str | None = None,
    base_run_id: str | None = None,
    progress_callback: ProgressCallback | None = None,
    run_mode: str = "fast",
) -> tuple[NewsWorkflowContext, dict[str, Path]]:
    project_root = project_root.resolve()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    normalized_mode = _normalize_run_mode(run_mode)
    scoring_config = _scoring_config_for_mode(load_scoring_config(project_root), normalized_mode)
    context = NewsWorkflowContext(
        project_root=project_root,
        run_id=run_id,
        weights=load_scoring_weights(project_root),
        scoring_config=scoring_config,
        run_mode=normalized_mode,
        allow_mock_fallback=allow_mock_fallback,
        progress_callback=progress_callback,
    )
    llm_budget = context.scoring_config.get("llm", {}).get("budget", {})
    context.llm_runtime = {
        "calls": 0,
        "cache_hits": 0,
        "budget_skips": 0,
        "usage": [],
        "max_calls_per_run": int(llm_budget.get("max_calls_per_run", 80)),
    }

    node_order = list(DEFAULT_NODE_ORDER)
    if start_from in DEFAULT_NODE_ORDER:
        hydrated = _hydrate_context_from_previous_artifact(context, start_from, base_run_id)
        if hydrated:
            node_order = DEFAULT_NODE_ORDER[DEFAULT_NODE_ORDER.index(start_from) :]

    total_nodes = len(node_order)
    context.progress_total_nodes = total_nodes
    _emit_progress(
        progress_callback,
        context=context,
        status=NodeStatus.running,
        current_node=node_order[0] if node_order else None,
        node_index=0,
        total_nodes=total_nodes,
    )

    for index, node_name in enumerate(node_order):
        runner = NODE_RUNNERS[node_name]
        started_at = datetime.now(timezone.utc)
        context.progress_node_index = index
        context.progress_node_started_at = started_at
        _emit_progress(
            progress_callback,
            context=context,
            status=NodeStatus.running,
            current_node=node_name,
            node_index=index,
            total_nodes=total_nodes,
        )
        try:
            output_ids = runner(context)
            status = NodeStatus.success
            warnings = _node_warnings(node_name, context)
        except Exception as exc:  # pragma: no cover - kept for run audit.
            output_ids = []
            status = NodeStatus.failed
            warnings = [f"{type(exc).__name__}: {exc}"]
        context.nodes.append(
            WorkflowNodeRun(
                node_name=node_name,
                status=status,
                output_ids=output_ids,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                decisions=_node_decisions(node_name, context),
                warnings=warnings,
            )
        )
        _safe_store_artifact(context, node_name)
        _emit_progress(
            progress_callback,
            context=context,
            status=status,
            current_node=node_name,
            node_index=index + 1,
            total_nodes=total_nodes,
        )
        if status == NodeStatus.failed:
            break

    failed_node_name = next((node.node_name for node in context.nodes if node.status == NodeStatus.failed), None)
    if not failed_node_name:
        _emit_progress(
            progress_callback,
            context=context,
            status=NodeStatus.running,
            current_node="render_report",
            node_index=min(len(context.nodes), total_nodes),
            total_nodes=total_nodes,
            progress=98 if context.nodes else 1,
        )

    reports_dir = project_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = reports_dir / "latest.md"
    html_path = reports_dir / "latest.html"
    markdown_path.write_text(render_markdown_report(context), encoding="utf-8")
    html_path.write_text(render_html_report(context), encoding="utf-8")

    final_status = NodeStatus.failed if failed_node_name else NodeStatus.success
    _emit_progress(
        progress_callback,
        context=context,
        status=final_status,
        current_node=None if final_status == NodeStatus.success else failed_node_name,
        node_index=total_nodes if final_status == NodeStatus.success else len(context.nodes),
        total_nodes=total_nodes,
        progress=100 if final_status == NodeStatus.success else None,
    )

    return context, {"markdown_path": markdown_path, "html_path": html_path}


def _emit_progress(
    progress_callback: ProgressCallback | None,
    *,
    context: NewsWorkflowContext,
    status: NodeStatus,
    current_node: str | None,
    node_index: int,
    total_nodes: int,
    progress: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    if not progress_callback:
        return
    total = max(total_nodes + 1, 1)
    percent = progress if progress is not None else round((max(0, node_index) / total) * 100)
    payload = {
        "run_id": context.run_id,
        "status": status.value,
        "current_node": current_node,
        "node_index": min(max(node_index, 0), total_nodes),
        "total_nodes": total_nodes,
        "completed_nodes": len(context.nodes),
        "progress": max(0, min(100, int(percent))),
        "nodes": [node.model_dump(mode="json") for node in context.nodes],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        payload.update(extra)
    try:
        progress_callback(payload)
    except Exception:
        return


def _emit_node_item_progress(
    context: NewsWorkflowContext,
    *,
    node_name: str,
    detail: str,
    item_index: int,
    item_total: int,
    started_at: datetime,
    timeout_seconds: float,
    completed_items: int,
    max_wait_items: int | None = None,
) -> None:
    if not context.progress_callback:
        return
    total = max(context.progress_total_nodes + 1, 1)
    node_fraction = max(0, min(1, completed_items / max(item_total, 1)))
    progress = round(((context.progress_node_index + node_fraction) / total) * 100)
    now = datetime.now(timezone.utc)
    elapsed = max(0.0, (now - started_at).total_seconds())
    remaining = max(item_total - completed_items, 0)
    blocking_items = remaining if max_wait_items is None else max(0, min(max_wait_items, remaining))
    if completed_items > 0:
        eta_seconds = round((elapsed / completed_items) * remaining)
    else:
        eta_seconds = round(timeout_seconds * blocking_items)
    max_wait_seconds = round(timeout_seconds * blocking_items)
    _emit_progress(
        context.progress_callback,
        context=context,
        status=NodeStatus.running,
        current_node=node_name,
        node_index=context.progress_node_index,
        total_nodes=context.progress_total_nodes,
        progress=progress,
        extra={
            "detail": detail,
            "item_index": item_index,
            "item_total": item_total,
            "elapsed_seconds": round(elapsed),
            "eta_seconds": eta_seconds,
            "max_wait_seconds": max_wait_seconds,
            "llm_calls": int(context.llm_runtime.get("calls", 0)),
            "llm_cache_hits": int(context.llm_runtime.get("cache_hits", 0)),
            "llm_budget_skips": int(context.llm_runtime.get("budget_skips", 0)),
        },
    )


def _normalize_run_mode(run_mode: str | None) -> str:
    value = str(run_mode or "fast").strip().lower()
    if value in {"deep", "full", "llm"}:
        return "deep"
    if value in {"balanced", "normal"}:
        return "balanced"
    return "fast"


def _scoring_config_for_mode(config: dict[str, Any], run_mode: str) -> dict[str, Any]:
    tuned = deepcopy(config)
    llm = tuned.setdefault("llm", {})
    if run_mode == "fast":
        llm.setdefault("event_card", {})["enabled"] = False
        llm.setdefault("classify", {})["enabled"] = False
        tuned.setdefault("backtest", {})["enabled"] = False
        tuned["_run_mode_note"] = "fast mode: llm and backtest disabled for realtime scanning"
    elif run_mode == "balanced":
        llm.setdefault("event_card", {})["enabled"] = False
        classify = llm.setdefault("classify", {})
        classify["max_calls_per_run"] = min(int(classify.get("max_calls_per_run") or 3), 3)
        tuned["_run_mode_note"] = "balanced mode: rule scan plus limited llm classification"
    else:
        llm.setdefault("event_card", {})["enabled"] = False
        classify = llm.setdefault("classify", {})
        classify["max_calls_per_run"] = min(int(classify.get("max_calls_per_run") or 3), 3)
        classify["max_items_per_run"] = min(int(classify.get("max_items_per_run") or 3), 3)
        classify["timeout_seconds"] = min(float(classify.get("timeout_seconds") or 25), 25)
        tuned.setdefault("backtest", {})["enabled"] = False
        tuned["_run_mode_note"] = (
            "deep mode: skip batch llm event cards, classify top candidates only, "
            "and defer backtest for dashboard speed"
        )
    return tuned


def collect(context: NewsWorkflowContext) -> list[str]:
    source_config = load_sources_config(context.project_root).get("sources", {})
    eastmoney_config = source_config.get("eastmoney_news", {})
    cninfo_config = source_config.get("cninfo_announcements", {})
    search_config = source_config.get("web_search", {})
    rss_config = source_config.get("rss_feeds", {})
    overseas_config = source_config.get("overseas_market", {})
    commodity_config = source_config.get("commodity", {})
    watchlist = load_watchlist(context.project_root)
    items: list[NewsItem] = []
    if eastmoney_config.get("enabled", False):
        columns = [
            EastmoneyColumn(id=int(row["id"]), name=str(row.get("name") or row["id"]))
            for row in eastmoney_config.get("columns", [])
        ]
        try:
            items.extend(
                collect_eastmoney_column_news(
                    columns=columns,
                    page_size=int(eastmoney_config.get("page_size", 30)),
                    timeout_seconds=float(eastmoney_config.get("timeout_seconds", 10)),
                )
            )
        except Exception as exc:
            context.collection_errors.append(f"eastmoney_news: {type(exc).__name__}: {exc}")

    if cninfo_config.get("enabled", False):
        try:
            items.extend(
                collect_cninfo_announcements(
                    watchlist=watchlist,
                    page_size=int(cninfo_config.get("page_size", 20)),
                    lookback_days=int(cninfo_config.get("lookback_days", 14)),
                    timeout_seconds=float(cninfo_config.get("timeout_seconds", 10)),
                    extract_body=bool(cninfo_config.get("extract_body", False)),
                    max_body_chars=int(cninfo_config.get("max_body_chars", 4000)),
                )
            )
        except Exception as exc:
            context.collection_errors.append(f"cninfo_announcements: {type(exc).__name__}: {exc}")

    if search_config.get("enabled", False):
        try:
            search_items, warnings = collect_web_search_news(
                watchlist=watchlist,
                provider=str(search_config.get("provider", "disabled")),
                max_queries=int(search_config.get("max_queries", 8)),
                timeout_seconds=float(search_config.get("timeout_seconds", 10)),
            )
            items.extend(search_items)
            context.collection_errors.extend(warnings)
        except Exception as exc:
            context.collection_errors.append(f"web_search: {type(exc).__name__}: {exc}")

    if rss_config.get("enabled", False):
        try:
            rss_items, warnings = collect_rss_feeds(
                feeds=list(rss_config.get("feeds", [])),
                timeout_seconds=float(rss_config.get("timeout_seconds", 10)),
            )
            items.extend(rss_items)
            context.collection_errors.extend(warnings)
        except Exception as exc:
            context.collection_errors.append(f"rss_feeds: {type(exc).__name__}: {exc}")

    if overseas_config.get("enabled", False):
        overseas_items, warnings = collect_global_market_moves(
            instruments=list(overseas_config.get("instruments", [])),
            source_name="overseas_market",
            threshold_pct=float(overseas_config.get("threshold_pct", 1.0)),
            timeout_seconds=float(overseas_config.get("timeout_seconds", 8)),
        )
        items.extend(overseas_items)
        context.collection_errors.extend(warnings)

    if commodity_config.get("enabled", False):
        commodity_items, warnings = collect_global_market_moves(
            instruments=list(commodity_config.get("instruments", [])),
            source_name="commodity",
            threshold_pct=float(commodity_config.get("threshold_pct", 1.0)),
            timeout_seconds=float(commodity_config.get("timeout_seconds", 8)),
        )
        items.extend(commodity_items)
        context.collection_errors.extend(warnings)

    context.news_items = sorted(
        items,
        key=lambda item: item.published_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    context.source_counts = _source_counts(context.news_items)
    if not context.news_items and context.allow_mock_fallback:
        collect_mock(context)  # type: ignore[arg-type]
        context.fallback_used = True
        context.source_counts = _source_counts(context.news_items)
    store_raw_news_items(
        project_root=context.project_root,
        run_id=context.run_id,
        items=context.news_items,
        scoring_config=context.scoring_config,
    )
    return [item.id for item in context.news_items]


def normalize(context: NewsWorkflowContext) -> list[str]:
    for item in context.news_items:
        item.title = " ".join(item.title.split())
        if item.summary:
            item.summary = " ".join(item.summary.split())
    return [item.id for item in context.news_items]


def dedup(context: NewsWorkflowContext) -> list[str]:
    seen: set[str] = set()
    deduped: list[NewsItem] = []
    for item in context.news_items:
        key = (item.url or item.title).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    context.news_items = deduped
    return [item.id for item in context.news_items]


def update_event_store(context: NewsWorkflowContext) -> list[str]:
    clustered_events = cluster_news_items(context.news_items, load_watchlist(context.project_root))
    context.events = upsert_event_store(
        project_root=context.project_root,
        run_id=context.run_id,
        events=clustered_events,
        news_by_id={item.id: item for item in context.news_items},
    )
    return [event.id for event in context.events]


def generate_event_card(context: NewsWorkflowContext) -> list[str]:
    started_at = datetime.now(timezone.utc)
    llm_config = _llm_node_config(context, "event_card")
    timeout_seconds = float(llm_config.get("timeout_seconds") or 45)

    def on_card_progress(index: int, total: int, event: EventItem) -> None:
        item_index = min(max(index, 1), max(total, 1))
        completed = min(max(index - 1, 0), total)
        detail = f"事件卡 {item_index}/{total}: {event.title[:32]}"
        _emit_node_item_progress(
            context,
            node_name="generate_event_card",
            detail=detail,
            item_index=item_index,
            item_total=total,
            started_at=started_at,
            timeout_seconds=timeout_seconds,
            completed_items=completed,
            max_wait_items=_remaining_llm_call_budget(context, llm_config, total - completed),
        )

    context.event_cards = build_event_cards(
        events=context.events,
        news_by_id={item.id: item for item in context.news_items},
        watchlist=load_watchlist(context.project_root),
        llm_config=llm_config,
        progress_callback=on_card_progress,
    )
    store_event_cards(context.project_root, context.run_id, context.events)
    return [item["event_id"] for item in context.event_cards]


def entity_link(context: NewsWorkflowContext) -> list[str]:
    watchlist = load_watchlist(context.project_root)
    news_by_id = {item.id: item for item in context.news_items}
    context.signals = link_events_to_watchlist(context.events, news_by_id, watchlist)
    return [signal.id for signal in context.signals]


def rule_classification(context: NewsWorkflowContext) -> list[str]:
    news_by_id = {item.id: item for item in context.news_items}
    events_by_id = {item.id: item for item in context.events}
    source_scores = context.scoring_config.get("source_scores", {})
    for signal in context.signals:
        event = events_by_id[signal.event_id]
        news = _aggregate_event_news(event, news_by_id)
        classify_signal_with_rules(signal, news, source_scores=source_scores)
        signal.metadata["event_last_seen_at"] = (
            event.last_seen_at.isoformat() if event.last_seen_at else None
        )
    return [signal.id for signal in context.signals]


def llm_classification(context: NewsWorkflowContext) -> list[str]:
    news_by_id = {item.id: item for item in context.news_items}
    events_by_id = {item.id: item for item in context.events}
    llm_config = _llm_node_config(context, "classify")
    if not llm_config.get("enabled", False):
        return []
    classified: list[str] = []
    started_at = datetime.now(timezone.utc)
    max_items = int(llm_config.get("max_items_per_run") or len(context.signals) or 0)
    candidates = sorted(context.signals, key=_llm_candidate_priority, reverse=True)[:max_items]
    total = len(candidates)
    timeout_seconds = float(llm_config.get("timeout_seconds") or 45)
    for index, signal in enumerate(candidates, start=1):
        event = events_by_id[signal.event_id]
        news = _aggregate_event_news(event, news_by_id)
        _emit_node_item_progress(
            context,
            node_name="llm_classification",
            detail=f"LLM判断 {index}/{total}: {signal.symbol} {signal.name or ''}",
            item_index=index,
            item_total=total,
            started_at=started_at,
            timeout_seconds=timeout_seconds,
            completed_items=index - 1,
            max_wait_items=_remaining_llm_call_budget(context, llm_config, total - index + 1),
        )
        if classify_signal_with_llm(signal, news, llm_config, apply=False):
            classified.append(signal.id)
        _emit_node_item_progress(
            context,
            node_name="llm_classification",
            detail=f"LLM判断 {index}/{total}: {signal.symbol} {signal.name or ''}",
            item_index=index,
            item_total=total,
            started_at=started_at,
            timeout_seconds=timeout_seconds,
            completed_items=index,
            max_wait_items=_remaining_llm_call_budget(context, llm_config, total - index),
        )
    return classified


def classification_merge(context: NewsWorkflowContext) -> list[str]:
    for signal in context.signals:
        _merge_classification(signal)
    return [signal.id for signal in context.signals]


def feedback_override(context: NewsWorkflowContext) -> list[str]:
    overrides = active_user_overrides(context.project_root)
    changed: list[str] = []
    for signal in context.signals:
        for override in overrides:
            if apply_signal_override(signal, override):
                changed.append(signal.id)
    return changed


def score(context: NewsWorkflowContext) -> list[str]:
    counts_by_symbol: dict[str, int] = {}
    for signal in context.signals:
        counts_by_symbol[signal.symbol] = counts_by_symbol.get(signal.symbol, 0) + 1
    for signal in context.signals:
        signal.score_breakdown.corroboration = min(82, 45 + (counts_by_symbol[signal.symbol] - 1) * 8)
        signal.final_score = round(final_score(signal.direction, signal.score_breakdown, context.weights), 2)
        signal.horizon_scores = _horizon_scores(signal)
    return [signal.id for signal in context.signals]


def market_validate(context: NewsWorkflowContext) -> list[str]:
    source_config = load_sources_config(context.project_root).get("sources", {})
    quote_config = source_config.get("eastmoney_quote", {})
    quotes = {}
    if quote_config.get("enabled", False):
        quotes = fetch_eastmoney_quotes(
            load_watchlist(context.project_root),
            timeout_seconds=float(quote_config.get("timeout_seconds", 8)),
        )
    for signal in context.signals:
        quote = quotes.get(signal.symbol)
        if quote:
            validation = _market_validation_score(signal, quote)
            signal.score_breakdown.market_validation = validation
            signal.metadata["market_validation"] = quote
            if validation >= 65:
                signal.warnings.append("market_confirmed")
            elif validation <= 35:
                signal.warnings.append("market_disagrees")
            signal.final_score = round(final_score(signal.direction, signal.score_breakdown, context.weights), 2)
            signal.horizon_scores = _horizon_scores(signal)
        else:
            signal.warnings.append("market_data_missing")
        if signal.score_breakdown.uncertainty_penalty >= 25:
            signal.warnings.append("high_uncertainty")
    return [signal.id for signal in context.signals]


def backtest_validate(context: NewsWorkflowContext) -> list[str]:
    config = context.scoring_config.get("backtest", {})
    if not config.get("enabled", True):
        return []
    context.backtest_results = validate_signals_with_event_study(
        signals=context.signals,
        watchlist=load_watchlist(context.project_root),
        benchmark_symbol=str(config.get("benchmark_symbol") or ""),
        timeout_seconds=float(config.get("timeout_seconds", 8)),
    )
    for signal in context.signals:
        result = context.backtest_results.get(signal.id)
        if result:
            signal.metadata["backtest"] = result
    return list(context.backtest_results.keys())


def rank(context: NewsWorkflowContext) -> list[str]:
    min_score = float(context.scoring_config.get("thresholds", {}).get("top_list_min_abs_score", 40))
    context.bullish_top = sorted(
        [signal for signal in context.signals if signal.final_score >= min_score],
        key=lambda item: item.final_score,
        reverse=True,
    )[:10]
    context.bearish_top = sorted(
        [signal for signal in context.signals if signal.final_score <= -min_score],
        key=lambda item: item.final_score,
    )[:10]
    store_rank_history(context.project_root, context.run_id, context.bullish_top, context.bearish_top)
    return [signal.id for signal in context.bullish_top + context.bearish_top]


def memory_store(context: NewsWorkflowContext) -> list[str]:
    context.retained_events = store_retained_signals(
        project_root=context.project_root,
        run_id=context.run_id,
        signals=context.bullish_top + context.bearish_top,
        scoring_config=context.scoring_config,
        news_by_id={item.id: item for item in context.news_items},
        events_by_id={item.id: item for item in context.events},
    )
    return [item["id"] for item in context.retained_events]


def notify(context: NewsWorkflowContext) -> list[str]:
    context.notifications = dispatch_notifications(
        project_root=context.project_root,
        signals=context.bullish_top + context.bearish_top,
        retained_events=context.retained_events,
    )
    return [item["id"] for item in context.notifications]


NODE_RUNNERS = {
    "collect": collect,
    "normalize": normalize,
    "dedup": dedup,
    "update_event_store": update_event_store,
    "generate_event_card": generate_event_card,
    "entity_link": entity_link,
    "rule_classification": rule_classification,
    "llm_classification": llm_classification,
    "classification_merge": classification_merge,
    "feedback_override": feedback_override,
    "score": score,
    "market_validate": market_validate,
    "backtest_validate": backtest_validate,
    "rank": rank,
    "memory_store": memory_store,
    "notify": notify,
}


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


def _llm_node_config(context: NewsWorkflowContext, node_key: str) -> dict[str, Any]:
    config = deepcopy(context.scoring_config.get("llm", {}).get(node_key, {}))
    config["_project_root"] = str(context.project_root)
    config["_runtime"] = context.llm_runtime
    if context.llm_runtime.get("max_calls_per_run") and "max_calls_per_run" not in config:
        config["max_calls_per_run"] = context.llm_runtime["max_calls_per_run"]
    return config


def _remaining_llm_call_budget(context: NewsWorkflowContext, llm_config: dict[str, Any], remaining_items: int) -> int:
    enabled = llm_config.get("enabled", "auto")
    if enabled is False or str(enabled).lower() in {"false", "off", "disabled"}:
        return 0
    max_calls = int(llm_config.get("max_calls_per_run") or context.llm_runtime.get("max_calls_per_run") or 0)
    if not max_calls:
        return max(0, remaining_items)
    calls = int(context.llm_runtime.get("calls", 0))
    return max(0, min(remaining_items, max_calls - calls))


def _llm_candidate_priority(signal: SignalItem) -> float:
    breakdown = signal.score_breakdown
    return (
        float(breakdown.relevance) * 0.35
        + float(breakdown.impact) * 0.30
        + float(breakdown.direction_confidence) * 0.25
        + float(breakdown.source_credibility) * 0.10
        - float(breakdown.uncertainty_penalty) * 0.35
    )


def _merge_classification(signal: SignalItem) -> None:
    rule = signal.metadata.get("rule_classification") or {}
    llm = signal.metadata.get("llm_classification") or {}
    llm_status = signal.metadata.get("llm_classification_status")
    rule_direction = str(rule.get("direction") or signal.direction.value)
    llm_direction = str(llm.get("direction") or "")
    rule_confidence = _float(rule.get("confidence"), 0)
    llm_confidence = _float(llm.get("confidence"), 0)
    disagreement = bool(llm_direction and rule_direction != llm_direction)

    final_source = "rule"
    reason = "llm_unavailable"
    if llm_status == "ok":
        if llm_direction == "uncertain" and rule_direction not in {"uncertain", "neutral"} and rule_confidence >= 68:
            final_source = "rule"
            reason = "llm_uncertain_kept_rule"
        else:
            apply_llm_classification(signal, llm)
            final_source = "llm"
            reason = "llm_selected"
            if "llm_classified" not in signal.warnings:
                signal.warnings.append("llm_classified")

    if disagreement and "classification_disagreement" not in signal.warnings:
        signal.warnings.append("classification_disagreement")
    signal.metadata["classification_merge"] = {
        "final_source": final_source,
        "reason": reason,
        "rule_direction": rule_direction,
        "rule_confidence": rule_confidence,
        "llm_direction": llm_direction or None,
        "llm_confidence": llm_confidence,
        "disagreement": disagreement,
    }


def _aggregate_event_news(event: EventItem, news_by_id: dict[str, NewsItem]) -> NewsItem:
    rows = [news_by_id[news_id] for news_id in event.news_ids if news_id in news_by_id]
    source_type = _best_source_type([item.source_type for item in rows])
    source = " / ".join(sorted({item.source for item in rows})[:3]) if rows else "unknown"
    raw_text = " ".join(f"{item.title} {item.summary or ''}" for item in rows)
    return NewsItem(
        id=f"aggregate_{event.id}",
        title=event.title,
        summary=event.summary,
        source=source,
        source_type=source_type,
        url=event.metadata.get("latest_url"),
        published_at=event.last_seen_at,
        raw_text=raw_text,
        metadata={"event_news_count": len(rows)},
    )


def _best_source_type(source_types: list[str]) -> str:
    rank = {
        "official": 100,
        "exchange": 95,
        "regulator": 90,
        "data_vendor": 75,
        "mainstream_media": 70,
        "normal_media": 55,
        "rumor": 20,
    }
    if not source_types:
        return "normal_media"
    return max(source_types, key=lambda item: rank.get(item, 50))


def _market_validation_score(signal: SignalItem, quote: dict[str, Any]) -> float:
    pct_change = quote.get("pct_change")
    if pct_change is None:
        return 50
    sign = 1 if signal.direction.value == "bullish" else -1 if signal.direction.value == "bearish" else 0
    alignment = float(pct_change) * sign
    amount = quote.get("amount") or 0
    amount_boost = 0
    if amount >= 1_000_000_000:
        amount_boost = 8
    elif amount >= 300_000_000:
        amount_boost = 4
    score = 50 + alignment * 12 + amount_boost
    return max(10, min(92, round(score, 2)))


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _signal_snapshot(signal: SignalItem, context: NewsWorkflowContext) -> dict[str, Any]:
    events_by_id = {item.id: item for item in context.events}
    news_by_id = {item.id: item for item in context.news_items}
    event = events_by_id.get(signal.event_id)
    evidence = [
        news_by_id[news_id].model_dump(mode="json")
        for news_id in signal.evidence_news_ids
        if news_id in news_by_id
    ]
    payload = signal.model_dump(mode="json")
    payload["event"] = event.model_dump(mode="json") if event else None
    payload["evidence"] = evidence
    return payload


def _safe_store_artifact(context: NewsWorkflowContext, node_name: str) -> None:
    try:
        store_workflow_artifact(
            project_root=context.project_root,
            run_id=context.run_id,
            node_name=node_name,
            payload=_context_artifact(context),
        )
    except Exception as exc:
        context.collection_errors.append(f"artifact_store:{node_name}:{type(exc).__name__}: {exc}")


def _context_artifact(context: NewsWorkflowContext) -> dict[str, Any]:
    return {
        "news_items": [item.model_dump(mode="json") for item in context.news_items],
        "events": [item.model_dump(mode="json") for item in context.events],
        "signals": [item.model_dump(mode="json") for item in context.signals],
        "bullish_top": [item.model_dump(mode="json") for item in context.bullish_top],
        "bearish_top": [item.model_dump(mode="json") for item in context.bearish_top],
        "event_cards": context.event_cards,
        "backtest_results": context.backtest_results,
        "source_counts": context.source_counts,
        "fallback_used": context.fallback_used,
        "llm_runtime": context.llm_runtime,
    }


def _hydrate_context_from_previous_artifact(
    context: NewsWorkflowContext,
    start_from: str,
    base_run_id: str | None,
) -> bool:
    index = DEFAULT_NODE_ORDER.index(start_from)
    if index <= 0:
        return False
    previous_node = DEFAULT_NODE_ORDER[index - 1]
    payload = load_latest_workflow_artifact(context.project_root, previous_node, base_run_id)
    if not payload:
        return False
    context.news_items = [NewsItem(**item) for item in payload.get("news_items", [])]
    context.events = [EventItem(**item) for item in payload.get("events", [])]
    context.signals = [SignalItem(**item) for item in payload.get("signals", [])]
    context.bullish_top = [SignalItem(**item) for item in payload.get("bullish_top", [])]
    context.bearish_top = [SignalItem(**item) for item in payload.get("bearish_top", [])]
    context.event_cards = list(payload.get("event_cards", []))
    context.backtest_results = dict(payload.get("backtest_results", {}))
    context.source_counts = dict(payload.get("source_counts", {}))
    context.fallback_used = bool(payload.get("fallback_used", False))
    context.llm_runtime.update(dict(payload.get("llm_runtime", {})))
    return True


def _node_decisions(node_name: str, context: NewsWorkflowContext) -> list[dict[str, Any]]:
    if node_name == "collect":
        return [
            {"label": "news_collected", "value": len(context.news_items)},
            {"label": "source_counts", "value": context.source_counts},
            {"label": "fallback_used", "value": context.fallback_used},
        ]
    if node_name == "update_event_store":
        status_counts: dict[str, int] = {}
        for event in context.events:
            status = str(event.metadata.get("event_store_status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
        return [
            {"label": "events_updated", "value": len(context.events)},
            {"label": "event_status", "value": status_counts},
        ]
    if node_name == "generate_event_card":
        generators: dict[str, int] = {}
        for card in context.event_cards:
            generator = str(card.get("generator") or "unknown")
            generators[generator] = generators.get(generator, 0) + 1
        return [
            {"label": "event_cards", "value": len(context.event_cards)},
            {"label": "generators", "value": generators},
        ]
    if node_name == "entity_link":
        return [{"label": "signals_created", "value": len(context.signals)}]
    if node_name == "rule_classification":
        return [
            {"label": "rule_classified", "value": len(context.signals)},
            {"label": "rule_directions", "value": _direction_counts(context.signals, "rule_classification")},
        ]
    if node_name == "llm_classification":
        return [
            {
                "label": "llm_classified",
                "value": sum(
                    1 for signal in context.signals if signal.metadata.get("llm_classification_status") == "ok"
                ),
            },
            {"label": "llm_calls", "value": context.llm_runtime.get("calls", 0)},
            {"label": "llm_cache_hits", "value": context.llm_runtime.get("cache_hits", 0)},
            {"label": "llm_budget_skips", "value": context.llm_runtime.get("budget_skips", 0)},
        ]
    if node_name == "classification_merge":
        final_sources: dict[str, int] = {}
        disagreements = 0
        for signal in context.signals:
            merge = signal.metadata.get("classification_merge") or {}
            source = str(merge.get("final_source") or "unknown")
            final_sources[source] = final_sources.get(source, 0) + 1
            if merge.get("disagreement"):
                disagreements += 1
        return [
            {"label": "final_sources", "value": final_sources},
            {"label": "classification_disagreements", "value": disagreements},
        ]
    if node_name == "feedback_override":
        applied = sum(1 for signal in context.signals if "user_override_applied" in signal.warnings)
        return [{"label": "overrides_applied", "value": applied}]
    if node_name == "rank":
        return [
            {"label": "bullish_top_count", "value": len(context.bullish_top)},
            {"label": "bearish_top_count", "value": len(context.bearish_top)},
        ]
    if node_name == "backtest_validate":
        ok_count = sum(1 for item in context.backtest_results.values() if item.get("status") == "ok")
        return [
            {"label": "backtest_checked", "value": len(context.backtest_results)},
            {"label": "backtest_ok", "value": ok_count},
        ]
    if node_name == "memory_store":
        return [{"label": "retained_event_count", "value": len(context.retained_events)}]
    if node_name == "notify":
        return [{"label": "notification_count", "value": len(context.notifications)}]
    return []


def _source_counts(items: list[NewsItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        source = str(item.metadata.get("collector") or item.source)
        counts[source] = counts.get(source, 0) + 1
    return counts


def _direction_counts(signals: list[SignalItem], metadata_key: str | None = None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for signal in signals:
        if metadata_key:
            source = signal.metadata.get(metadata_key) or {}
            direction = str(source.get("direction") or "unknown")
        else:
            direction = signal.direction.value
        counts[direction] = counts.get(direction, 0) + 1
    return counts


def _node_warnings(node_name: str, context: NewsWorkflowContext) -> list[str]:
    if node_name == "collect":
        warnings = list(context.collection_errors)
        if not context.news_items:
            warnings.append("no_news_collected")
        return warnings
    if node_name == "update_event_store" and not context.events:
        return ["no_events_created"]
    if node_name == "entity_link" and not context.signals:
        return ["no_watchlist_links"]
    if node_name == "rank" and not context.bullish_top and not context.bearish_top:
        return ["no_ranked_signals"]
    return []
