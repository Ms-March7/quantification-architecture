"""Render a point-in-time fundamental brief from a declared research scope."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.research_review import (
    EVIDENCE_LABELS, Identifier, Source, Timestamp, graph_label, markdown, parse_timestamp,
)


# These horizons are reporting defaults, not evidence of a change in the business.
DIMENSIONS = {
    "company": ("公司经营", 125),
    "supply": ("上游供给与成本", 45),
    "demand": ("下游需求", 45),
    "competition": ("竞争格局", 90),
    "policy": ("政策与治理", 60),
    "market": ("市场定价", 3),
}


class Material(Source):
    # Collectors may use URL hashes or external identifiers outside the case ID syntax.
    id: str = Field(min_length=1)
    dimension: Literal["company", "supply", "demand", "competition", "policy", "market"]
    observed_at: Timestamp | None = None
    published_at: Timestamp | None = None

    @property
    def visible_at(self) -> datetime:
        return max(t for t in (self.available_at, self.observed_at, self.published_at) if t)


class EvidenceLink(BaseModel):
    source_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    stance: Literal["supports", "challenges"]
    interpretation: str | None = None
    recorded_at: Timestamp | None = None


class Branch(BaseModel):
    id: Identifier
    from_node: str = Field(min_length=1)
    to_node: str = Field(min_length=1)
    condition: str = Field(min_length=1)
    next_check: str = Field(min_length=1)
    effect: Literal["supports", "constrains"] = "supports"
    falsifier: str | None = None
    recorded_at: Timestamp | None = None
    evidence: list[EvidenceLink] = Field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{markdown(self.from_node)} → {markdown(self.to_node)}"

    @property
    def direction(self) -> str:
        return "制约路径" if self.effect == "constrains" else "支撑路径"


@dataclass
class EvaluatedBranch:
    branch: Branch
    matches: list[tuple[EvidenceLink, Material]]

    @property
    def status(self) -> str:
        return EVIDENCE_LABELS[frozenset(link.stance for link, _ in self.matches)]

    @property
    def signatures(self) -> set[tuple]:
        return {(str(m.url), m.visible_at, link.quote, link.stance) for link, m in self.matches}


def _visible_materials(
    materials: list[Material], cutoff: datetime
) -> tuple[dict[str, Material], set[str]]:
    selected: dict[str, Material] = {}
    ambiguous = set()
    for item in sorted(materials, key=lambda entry: entry.visible_at):
        if item.visible_at > cutoff:
            break
        prior = selected.get(item.id)
        if prior and item.visible_at == prior.visible_at:
            if item != prior:
                ambiguous.add(item.id)
            continue
        ambiguous.discard(item.id)
        selected[item.id] = item
    for source_id in ambiguous:
        del selected[source_id]
    return selected, ambiguous


def _evaluate(
    branches: list[Branch], materials: dict[str, Material], cutoff: datetime
) -> dict[str, EvaluatedBranch]:
    result = {}
    for branch in branches:
        if branch.recorded_at and branch.recorded_at > cutoff:
            continue
        matches = []
        seen = set()
        for link in branch.evidence:
            if link.recorded_at and link.recorded_at > cutoff:
                continue
            material = materials.get(link.source_id)
            if material is None or not link.quote.strip() or link.quote not in material.excerpt:
                continue
            key = (str(material.url), link.quote, link.stance)
            if key not in seen:
                matches.append((link, material))
                seen.add(key)
        result[branch.id] = EvaluatedBranch(branch, matches)
    return result


def _parse_materials(raw_materials: list[dict]) -> tuple[list[Material], int]:
    entries = []
    rejected = 0
    for raw in raw_materials:
        try:
            fields = {key: value for key, value in raw.items() if key in Material.model_fields}
            entries.append(Material.model_validate(fields))
        except (AttributeError, ValidationError):
            rejected += 1
    return entries, rejected


def _horizons(profile: dict) -> dict[str, int]:
    limits = profile.get("stale_after_days", {})
    if not isinstance(limits, (int, dict)):
        raise ValueError("stale_after_days 须为正整数或维度映射")
    if isinstance(limits, dict) and set(limits) - DIMENSIONS.keys():
        raise ValueError("stale_after_days 包含未知维度")
    result = {}
    for key, (_, default) in DIMENSIONS.items():
        days = limits.get(key, default) if isinstance(limits, dict) else limits
        if type(days) is not int or days < 1:
            raise ValueError("材料有效期须为正整数天数")
        result[key] = days
    return result


def _stale(m: Material, cutoff: datetime, horizons: dict[str, int]) -> bool:
    age_seconds = (cutoff - (m.published_at or m.available_at)).total_seconds()
    return age_seconds > horizons[m.dimension] * 86400


def _citation(m: Material) -> str:
    link = f"[{markdown(m.title)}](<{m.url}>)"
    timing = f"可得 {m.visible_at.isoformat()}"
    if m.published_at and m.published_at != m.visible_at:
        timing += f"，发布 {m.published_at.isoformat()}"
    return f"{link}（{markdown(m.scope)}，{timing}）"


def _graph(results: dict[str, EvaluatedBranch]) -> list[str]:
    labels = {}
    for item in results.values():
        for node in (item.branch.from_node, item.branch.to_node):
            if node not in labels:
                labels[node] = f"n{len(labels)}"
    lines = ["```mermaid", "flowchart LR"]
    for name, key in labels.items():
        lines.append(f'    {key}["{graph_label(name)}"]')
    for item in results.values():
        b = item.branch
        arrow = "-->" if item.matches else "-.->"
        lines.append(f"    {labels[b.from_node]} {arrow}|{b.direction} · {item.status}| {labels[b.to_node]}")
    return lines + ["```", ""]


def _changes(before: dict[str, EvaluatedBranch], after: dict[str, EvaluatedBranch]) -> list[str]:
    direct = {
        key for key, item in after.items()
        if item.signatures != (before[key].signatures if key in before else set())
    }
    added = [item.branch.label for key, item in after.items() if key not in before]
    lines = []
    if added:
        lines.append("新增研究分支：" + "；".join(added) + "。")
    for key in after:
        if key in direct:
            prior = before[key].status if key in before else "尚未记录"
            lines.append(f"- 直接证据更新：{after[key].branch.label}（{prior} → {after[key].status}）")
            old_matches = before[key].matches if key in before else []
            old_text = {(str(m.url), link.quote, link.stance) for link, m in old_matches}
            new_text = {(str(m.url), link.quote, link.stance) for link, m in after[key].matches}
            if old_text - new_text:
                lines.append("  - 原有引句在本期材料中无法再核验，已撤回相应依据。")
    if not direct:
        return lines + ["给定材料中，能够逐字核验的分支依据没有变化。", ""]

    outgoing = defaultdict(list)
    for key, item in after.items():
        outgoing[item.branch.from_node].append(key)
    queue = deque(after[key].branch.to_node for key in direct)
    visited = set()
    downstream = set()
    while queue:
        node = queue.popleft()
        if node in visited:
            continue
        visited.add(node)
        for key in outgoing[node]:
            downstream.add(key)
            queue.append(after[key].branch.to_node)
    for key in after:
        if key in downstream - direct:
            lines.append(f"- 下游待复核：{after[key].branch.label}（自身证据状态：{after[key].status}）")
    return lines + [""]


def render_report(
    profile: dict, materials: list[dict], issues: list[str], cutoff: datetime,
    previous: datetime | None = None, *, previous_materials: list[dict] | None = None,
) -> str:
    """Produce an offline memo. Branches lacking recorded_at are standing analyst templates."""
    cutoff = parse_timestamp(cutoff)
    if previous is not None:
        previous = parse_timestamp(previous)
        if previous >= cutoff:
            raise ValueError("对照时点须早于研究时点")
    horizons = _horizons(profile)
    entries, rejected = _parse_materials(materials)
    raw_branches = profile.get("branches", [])
    if not isinstance(raw_branches, list):
        raise ValueError("branches 须为列表")
    branches = [Branch.model_validate(raw) for raw in raw_branches]
    if len({b.id for b in branches}) != len(branches):
        raise ValueError("研究分支 ID 重复")

    visible, ambiguous = _visible_materials(entries, cutoff)
    evaluated = _evaluate(branches, visible, cutoff)
    dims = defaultdict(list)
    for m in visible.values():
        dims[m.dimension].append(m)

    lines = [f"# {markdown(str(profile.get('target') or '研究标的'))}｜动态基本面研判", "",
             markdown(str(profile.get("question") or "哪些经营判断有了新证据？")), "",
             f"截至 {cutoff.isoformat()}", "", "## 当前结论", ""]
    if not visible:
        lines.append("截至该时点尚无可核验材料，经营判断和市场定价分支均待补证。")
    elif not evaluated:
        lines.append("现有材料可供查阅；尚无可核验的研究分支，暂不足以形成条件判断。")
    for item in evaluated.values():
        msg = f"- **{item.status}**　{item.branch.label}（{item.branch.direction}）"
        if item.matches and all(_stale(m, cutoff, horizons) for _, m in item.matches):
            msg += "（所引材料均已超出核验周期）"
        if not item.matches or item.status == "兼有支持与质疑":
            msg += f"。下一步核对：{markdown(item.branch.next_check)}"
        lines.append(msg)

    lines.extend(["", "## 研究覆盖", ""])
    for dim, (label, _) in DIMENSIONS.items():
        if not dims[dim]:
            lines.append(f"- **{label}**　缺口：截至此时无可用材料。")
            continue
        latest = max(dims[dim], key=lambda m: (m.published_at or m.available_at, m.visible_at))
        stale = "超过核验周期" if _stale(latest, cutoff, horizons) else "核验周期内"
        summary = latest.excerpt if len(latest.excerpt) <= 220 else latest.excerpt[:219] + "…"
        lines.append(f"- **{label}**　{stale}（{horizons[dim]} 天）。{markdown(summary)}　{_citation(latest)}")
    if previous is not None:
        older_entries = entries if previous_materials is None else _parse_materials(previous_materials)[0]
        older_materials, _ = _visible_materials(older_entries, previous)
        before = _evaluate(branches, older_materials, previous)
        lines.extend(["", f"## 较 {previous.isoformat()} 的变化", ""])
        lines.extend(_changes(before, evaluated))

    if evaluated:
        lines.extend(["", "## 推理网络", ""])
        if any(item.branch.recorded_at is None for item in evaluated.values()):
            lines.extend(["未标记录时间的分支按预设研究问题展示；引句仍按来源可得时间筛选。", ""])
        lines.extend(_graph(evaluated))
    for item in evaluated.values():
        b = item.branch
        lines.extend([f"### {b.label}", "", f"- 成立条件：{markdown(b.condition)}",
                      f"- 下一项验证：{markdown(b.next_check)}"])
        if b.falsifier:
            lines.append(f"- 推翻条件：{markdown(b.falsifier)}")
        for link, m in item.matches:
            stance = "支持" if link.stance == "supports" else "质疑"
            lines.append(f"- {stance}依据：“{markdown(link.quote)}”　{_citation(m)}")
            if link.interpretation:
                lines.append(f"  - 研究者解读：{markdown(link.interpretation)}")
        if not item.matches:
            lines.append("- 证据缺口：尚无可见且可逐字匹配的引句。")
        lines.append("")
    if rejected or ambiguous or issues:
        lines.extend(["", "## 数据可用性", ""])
        if rejected:
            lines.append(f"- {rejected} 条输入缺少有效来源或时间等必要字段，已排除。")
        for source_id in sorted(ambiguous):
            lines.append(f"- 同一可得时点存在互相冲突的来源版本，已排除：{markdown(source_id)}。")
        lines.extend("- " + markdown(str(issue)) for issue in issues)
    return "\n".join(lines).rstrip() + "\n"
