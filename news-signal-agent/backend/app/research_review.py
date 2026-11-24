"""Replay an analyst's evidence graph using only information recorded by the cutoff."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    BaseModel, BeforeValidator, ConfigDict, Field, HttpUrl, ValidationError, model_validator,
)


def parse_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, str):
        if value.endswith("Z"):  # Python 3.10 does not accept the UTC suffix.
            value = value[:-1] + "+00:00"
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("时间须含 UTC 偏移，例如 2026-04-30T10:00:00+08:00")
    return value


Timestamp = Annotated[datetime, BeforeValidator(parse_timestamp)]
Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,47}$")]
EVIDENCE_LABELS = {
    frozenset(): "待证",
    frozenset({"supports"}): "有支持材料",
    frozenset({"challenges"}): "有质疑材料",
    frozenset({"supports", "challenges"}): "兼有支持与质疑",
}


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", str_min_length=1, str_strip_whitespace=True)


class Node(Record):
    id: Identifier
    kind: Literal["observation", "hypothesis", "conclusion"]
    label: str
    recorded_at: Timestamp


class Edge(Record):
    id: Identifier
    from_node: Identifier
    to_node: Identifier
    effect: Literal["supports", "constrains"]
    condition: str
    next_check: str | None = None
    recorded_at: Timestamp


class Source(Record):
    id: Identifier
    title: str
    scope: str
    url: HttpUrl
    available_at: Timestamp
    excerpt: str


class Evidence(Record):
    edge_id: Identifier
    source_id: Identifier
    stance: Literal["supports", "challenges"]
    quote: str
    interpretation: str
    recorded_at: Timestamp


class DraftOrigin(Record):
    materials_as_of: Timestamp
    generated_at: Timestamp


class ResearchCase(Record):
    question: str
    opened_at: Timestamp
    synthetic: bool = False
    draft: DraftOrigin | None = None
    nodes: list[Node]
    edges: list[Edge]
    sources: list[Source]
    evidence: list[Evidence]

    @model_validator(mode="after")
    def check_references(self) -> ResearchCase:
        nodes = {item.id: item for item in self.nodes}
        edges = {item.id: item for item in self.edges}
        sources = {item.id: item for item in self.sources}
        if self.draft:
            origin = self.draft
            if origin.materials_as_of > origin.generated_at:
                raise ValueError("草稿的材料截止时间晚于生成时间")
            if any(s.available_at > origin.materials_as_of for s in self.sources):
                raise ValueError("模型草稿含有截止后才可获得的材料")
            groups = (self.nodes, self.edges, self.evidence)
            first_record = min((r.recorded_at for group in groups for r in group),
                               default=self.opened_at)
            if min(self.opened_at, first_record) < origin.generated_at:
                raise ValueError("模型草稿中的判断不能早于实际生成时间")
        if (len(nodes), len(edges), len(sources)) != (
            len(self.nodes), len(self.edges), len(self.sources)
        ):
            raise ValueError("节点、边和来源各自的 ID 必须唯一")
        for edge in self.edges:
            if edge.from_node not in nodes or edge.to_node not in nodes:
                raise ValueError(f"边 {edge.id} 引用了未知节点")
            endpoints = (nodes[edge.from_node], nodes[edge.to_node])
            if any(edge.recorded_at < node.recorded_at for node in endpoints):
                raise ValueError(f"边 {edge.id} 的记录早于其节点")
        for note in self.evidence:
            if note.edge_id not in edges or note.source_id not in sources:
                raise ValueError(f"证据引用了未知的边或来源：{note.edge_id} / {note.source_id}")
            source = sources[note.source_id]
            if note.recorded_at < max(edges[note.edge_id].recorded_at, source.available_at):
                raise ValueError(f"证据记录早于边或来源可得时间：{note.source_id}")
            if note.quote not in source.excerpt:
                raise ValueError(f"引句无法逐字匹配来源摘录：{note.source_id}")
        return self


def markdown(text: str) -> str:
    return re.sub(r"([\\`*_\[\]~])", r"\\\1", escape(" ".join(text.split()), quote=False))


def graph_label(text: str) -> str:
    return "".join(c if c.isalnum() or c in " ，、？" else " " for c in text)[:64]


def snapshot(case: ResearchCase, cutoff: datetime) -> ResearchCase:
    cutoff = parse_timestamp(cutoff)
    if cutoff < case.opened_at:
        raise ValueError("该时点尚无已记录的研究问题")
    # Reference validation guarantees each visible edge/note has visible dependencies.
    return case.model_copy(update={
        "nodes": [n for n in case.nodes if n.recorded_at <= cutoff],
        "edges": [e for e in case.edges if e.recorded_at <= cutoff],
        "sources": [s for s in case.sources if s.available_at <= cutoff],
        "evidence": [n for n in case.evidence if n.recorded_at <= cutoff],
    })


def render(case: ResearchCase, cutoff: datetime) -> str:
    case = snapshot(case, cutoff)
    nodes = {n.id: n for n in case.nodes}
    edges = case.edges
    sources = {s.id: s for s in case.sources}
    lines = [f"# {markdown(case.question)}", "", f"复盘截止时间 {cutoff.isoformat()}", ""]
    if case.synthetic:
        lines.extend(["合成教学案例，全部引句、日期和来源均为示例。", ""])
    if case.draft:
        lines.extend([
            "模型生成的候选逻辑，待人工复核。",
            f"材料截止 {case.draft.materials_as_of.isoformat()} "
            f"· 草稿生成 {case.draft.generated_at.isoformat()}",
            "",
        ])
    else:
        lines.extend(["节点与连线为研究者记录的判断。下方引句按材料可得时间和判断记录时间筛选。", ""])
    kinds = {"observation": "观察", "hypothesis": "中间假设", "conclusion": "结论假设"}
    effects = {"supports": "支撑路径", "constrains": "制约路径"}
    by_edge: dict[str, list[Evidence]] = {e.id: [] for e in edges}
    for note in case.evidence:
        by_edge[note.edge_id].append(note)
    if nodes:
        lines.extend(["```mermaid", "flowchart LR"])
        for node in nodes.values():
            lines.append(f'    n_{node.id}["{kinds[node.kind]} · {graph_label(node.label)}"]')
        for edge in edges:
            stances = frozenset(n.stance for n in by_edge[edge.id])
            label = f"{effects[edge.effect]} · {EVIDENCE_LABELS[stances]}"
            arrow = "-->" if stances else "-.->"
            lines.append(f"    n_{edge.from_node} {arrow}|{label}| n_{edge.to_node}")
        lines.extend(["```", ""])
    if not edges:
        lines.append("截至此时，尚未记录逻辑连线。")
    for edge in edges:
        scoped = by_edge[edge.id]
        origin = markdown(nodes[edge.from_node].label)
        target = markdown(nodes[edge.to_node].label)
        lines.extend([
            f"## {edge.id}　{origin} → {target}",
            "",
            f"{effects[edge.effect]} · 连线记录 {edge.recorded_at.isoformat()}",
            "",
            f"成立条件：{markdown(edge.condition)}",
        ])
        if edge.next_check:
            lines.append(f"如何检验：{markdown(edge.next_check)}")
        lines.append("")
        if not scoped:
            lines.append("待证。尚无当时可用且已记录的引句。")
        for note in scoped:
            s = sources[note.source_id]
            stance = "支持该连线" if note.stance == "supports" else "质疑该连线"
            lines.extend([
                f"- **{stance}**　{markdown(note.interpretation)}",
                f"  - 原文：“{markdown(note.quote)}”",
                f"  - [{markdown(s.title)}](<{s.url}>) · {markdown(s.scope)}"
                f" · 材料可得 {s.available_at.isoformat()} · 判断记录 {note.recorded_at.isoformat()}",
            ])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="按信息可得时点复盘一项投研逻辑网络")
    parser.add_argument("case", type=Path, help="YAML 研究案例")
    parser.add_argument("--as-of", required=True, help="含时区的截止时间（含该时点）")
    args = parser.parse_args()
    try:
        cutoff = parse_timestamp(args.as_of)
        data = yaml.safe_load(args.case.read_text(encoding="utf-8"))
        case = ResearchCase.model_validate(data)
        memo = render(case, cutoff)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        message = str(exc)
        if isinstance(exc, ValidationError):
            message = "; ".join(
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                for e in exc.errors(include_input=False)
            )
        parser.exit(2, f"无法生成复盘：{message}\n")
    print(memo, end="")


if __name__ == "__main__":
    # Emit UTF-8 Markdown even when Windows redirects stdout to a file or pipe.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()
