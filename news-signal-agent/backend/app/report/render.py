from __future__ import annotations

from html import escape
from typing import Iterable

from app.models import Horizon, NewsItem, ScoreBreakdown, SignalItem
from app.scoring.explainable import bar


def render_markdown_report(context) -> str:
    title = getattr(context, "report_title", "News Signal Agent Demo Report")
    news_by_id = {item.id: item for item in context.news_items}
    lines: list[str] = [
        f"# {title}",
        "",
        f"- Run ID: `{context.run_id}`",
        f"- News items: `{len(context.news_items)}`",
        f"- Events: `{len(context.events)}`",
        f"- Signals: `{len(context.signals)}`",
        "",
        "## Workflow",
        "",
        "| Node | Status | Output Count | Decisions |",
        "| --- | --- | ---: | --- |",
    ]
    for node in context.nodes:
        decisions = "; ".join(f"{item['label']}={item['value']}" for item in node.decisions)
        lines.append(f"| `{node.node_name}` | {node.status.value} | {len(node.output_ids)} | {decisions} |")

    lines.extend(["", "## Bullish Top10", ""])
    lines.extend(_markdown_signal_list(context.bullish_top, empty="No bullish signals."))
    lines.extend(["", "## Bearish Top10", ""])
    lines.extend(_markdown_signal_list(context.bearish_top, empty="No bearish signals."))
    lines.extend(["", "## Signal Details", ""])
    for signal in context.bullish_top + context.bearish_top:
        lines.extend(_markdown_signal_detail(signal, news_by_id))
    return "\n".join(lines) + "\n"


def render_html_report(context) -> str:
    title = getattr(context, "report_title", "News Signal Agent Demo Report")
    news_by_id = {item.id: item for item in context.news_items}
    workflow = "\n".join(
        f"<div class='node'><strong>{escape(node.node_name)}</strong><span>{node.status.value}</span></div>"
        for node in context.nodes
    )
    bullish = _html_signal_table(context.bullish_top)
    bearish = _html_signal_table(context.bearish_top)
    details = "\n".join(
        _html_signal_card(signal, news_by_id) for signal in context.bullish_top + context.bearish_top
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>{escape(title)}</title>
  <style>
    body {{ font-family: Arial, "Microsoft YaHei", sans-serif; margin: 24px; color: #20242a; }}
    .workflow {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 24px; }}
    .node {{ border: 1px solid #d8dde6; border-radius: 8px; padding: 10px 12px; background: #f8fafc; }}
    .node span {{ display: block; color: #3f7d20; font-size: 12px; margin-top: 4px; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0 24px; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; padding: 8px; text-align: left; }}
    .score-pos {{ color: #0b7a30; font-weight: 700; }}
    .score-neg {{ color: #b42318; font-weight: 700; }}
    .card {{ border: 1px solid #d8dde6; border-radius: 8px; padding: 14px; margin: 12px 0; }}
    .bars {{ display: grid; grid-template-columns: 150px 1fr 50px; gap: 8px; font-family: Consolas, monospace; }}
  </style>
</head>
<body>
  <h1>{escape(title)}</h1>
  <p>Run ID: <code>{escape(context.run_id)}</code></p>
  <h2>Workflow</h2>
  <div class="workflow">{workflow}</div>
  <h2>Bullish Top10</h2>
  {bullish}
  <h2>Bearish Top10</h2>
  {bearish}
  <h2>Signal Details</h2>
  {details}
</body>
</html>
"""


def _markdown_signal_list(signals: Iterable[SignalItem], empty: str) -> list[str]:
    items = list(signals)
    if not items:
        return [empty]
    lines = ["| Rank | Symbol | Name | Score | Horizon | Brief |", "| ---: | --- | --- | ---: | --- | --- |"]
    for idx, signal in enumerate(items, start=1):
        lines.append(
            f"| {idx} | `{signal.symbol}` | {signal.name or ''} | {signal.final_score:.2f} | "
            f"{signal.primary_horizon.value} | {signal.brief} |"
        )
    return lines


def _markdown_signal_detail(signal: SignalItem, news_by_id: dict[str, NewsItem]) -> list[str]:
    b = signal.score_breakdown
    lines = [
        f"### {signal.symbol} {signal.name or ''} | {signal.final_score:.2f} | {signal.primary_horizon.value}",
        "",
        signal.brief,
        "",
        "| Component | Bar | Value |",
        "| --- | --- | ---: |",
    ]
    for label, value in _breakdown_rows(b):
        lines.append(f"| {label} | `{bar(abs(value))}` | {value:.0f} |")
    lines.extend(
        [
            "",
            f"- Short: {signal.horizon_scores.get(Horizon.short, 0):.2f}",
            f"- Mid: {signal.horizon_scores.get(Horizon.mid, 0):.2f}",
            f"- Long: {signal.horizon_scores.get(Horizon.long, 0):.2f}",
            f"- Warnings: {', '.join(signal.warnings) if signal.warnings else 'none'}",
            f"- Evidence: {_markdown_evidence(signal, news_by_id)}",
            "",
        ]
    )
    return lines


def _html_signal_table(signals: list[SignalItem]) -> str:
    if not signals:
        return "<p>No signals.</p>"
    rows = []
    for idx, signal in enumerate(signals, start=1):
        klass = "score-pos" if signal.final_score > 0 else "score-neg"
        rows.append(
            "<tr>"
            f"<td>{idx}</td><td>{escape(signal.symbol)}</td><td>{escape(signal.name or '')}</td>"
            f"<td class='{klass}'>{signal.final_score:.2f}</td>"
            f"<td>{signal.primary_horizon.value}</td><td>{escape(signal.brief)}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Rank</th><th>Symbol</th><th>Name</th><th>Score</th>"
        "<th>Horizon</th><th>Brief</th></tr></thead><tbody>"
        + "\n".join(rows)
        + "</tbody></table>"
    )


def _html_signal_card(signal: SignalItem, news_by_id: dict[str, NewsItem]) -> str:
    score_class = "score-pos" if signal.final_score > 0 else "score-neg"
    rows = "\n".join(
        f"<div>{escape(label)}</div><div>{bar(abs(value))}</div><div>{value:.0f}</div>"
        for label, value in _breakdown_rows(signal.score_breakdown)
    )
    warnings = ", ".join(signal.warnings) if signal.warnings else "none"
    evidence = _html_evidence(signal, news_by_id)
    return f"""
<section class="card">
  <h3>{escape(signal.symbol)} {escape(signal.name or "")}
    <span class="{score_class}">{signal.final_score:.2f}</span>
  </h3>
  <p>{escape(signal.brief)}</p>
  <div class="bars">{rows}</div>
  <p>Short {signal.horizon_scores.get(Horizon.short, 0):.2f} /
     Mid {signal.horizon_scores.get(Horizon.mid, 0):.2f} /
     Long {signal.horizon_scores.get(Horizon.long, 0):.2f}</p>
  <p>Warnings: {escape(warnings)}</p>
  <p>Evidence: {evidence}</p>
</section>
"""


def _breakdown_rows(breakdown: ScoreBreakdown) -> list[tuple[str, float]]:
    return [
        ("direction_confidence", breakdown.direction_confidence),
        ("relevance", breakdown.relevance),
        ("source_credibility", breakdown.source_credibility),
        ("impact", breakdown.impact),
        ("freshness", breakdown.freshness),
        ("corroboration", breakdown.corroboration),
        ("market_validation", breakdown.market_validation),
        ("uncertainty_penalty", -breakdown.uncertainty_penalty),
    ]


def _markdown_evidence(signal: SignalItem, news_by_id: dict[str, NewsItem]) -> str:
    fragments: list[str] = []
    for news_id in signal.evidence_news_ids:
        news = news_by_id.get(news_id)
        if not news:
            continue
        source = news.source
        published = news.published_at.strftime("%Y-%m-%d %H:%M") if news.published_at else "unknown time"
        if news.url:
            fragments.append(f"[{news.title}]({news.url}) - {source}, {published}")
        else:
            fragments.append(f"{news.title} - {source}, {published}")
    return "; ".join(fragments) if fragments else "none"


def _html_evidence(signal: SignalItem, news_by_id: dict[str, NewsItem]) -> str:
    fragments: list[str] = []
    for news_id in signal.evidence_news_ids:
        news = news_by_id.get(news_id)
        if not news:
            continue
        source = escape(news.source)
        published = news.published_at.strftime("%Y-%m-%d %H:%M") if news.published_at else "unknown time"
        title = escape(news.title)
        if news.url:
            fragments.append(f"<a href='{escape(news.url)}' target='_blank'>{title}</a> - {source}, {published}")
        else:
            fragments.append(f"{title} - {source}, {published}")
    return "; ".join(fragments) if fragments else "none"
