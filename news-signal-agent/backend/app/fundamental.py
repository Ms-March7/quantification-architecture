"""Build a dated fundamental research brief from disclosures and public feeds."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app.fundamental_report import render_report
from app.fundamental_sources import collect_materials
from app.research_review import parse_timestamp


def model_appendix(profile, materials, cutoff):
    from app.config import load_scoring_config
    from app.research_draft import Materials, generate
    from app.research_review import Source, render

    sources = [{k: item[k] for k in Source.model_fields} for item in materials]
    request = Materials(
        question=profile["question"], sources=sources, synthetic=bool(profile.get("synthetic"))
    )
    config = load_scoring_config(Path(__file__).resolve().parents[2]).get("llm", {})
    case = generate(request, cutoff, config.get("research_draft", config.get("event_card", {})))
    return render(case, case.opened_at)


def build_brief(profile, *, as_of=None, previous=None, refresh=False, cache_dir=None, draft=False):
    if not isinstance(profile, dict) or any(
        not isinstance(profile.get(k), str) or not profile[k].strip() for k in ("target", "question")
    ):
        raise ValueError("研究配置须提供 target 和 question")
    now = datetime.now(timezone.utc)
    if as_of and as_of > now:
        raise ValueError("研究时点晚于当前时间")
    if previous and previous >= (as_of or now):
        raise ValueError("对照时点须早于研究时点")
    if refresh and as_of is not None:
        raise ValueError("历史回放请使用已核验的材料和既有观测；实时刷新请省略 --as-of")
    materials, issues = collect_materials(profile, as_of, refresh, cache_dir)
    cutoff = as_of or datetime.now(timezone.utc)
    baseline = None
    if previous is not None:
        baseline, _ = collect_materials(profile, previous, False, cache_dir)
    report = render_report(profile, materials, issues, cutoff, previous, previous_materials=baseline)
    if draft:
        report += "\n## 模型候选分析\n\n" + model_appendix(profile, materials, cutoff)
    return report


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="按研究时点生成基本面覆盖、证据与推理分支报告")
    parser.add_argument("profile", type=Path, help="含研究标的、问题、来源与可选推理分支的 YAML")
    parser.add_argument("--as-of", type=parse_timestamp, help="历史截止时间，须含 UTC 偏移")
    parser.add_argument("--since", type=parse_timestamp, help="比较新增证据的起始时点")
    parser.add_argument("--refresh", action="store_true", help="现在从配置的公开订阅源获取材料")
    parser.add_argument("--cache-dir", type=Path, help="保存订阅观测，供之后按时点回放")
    parser.add_argument("--draft", action="store_true", help="用已配置的模型追加待审阅的推理网络")
    parser.add_argument("--output", type=Path, help="新建 UTF-8 Markdown 文件，默认输出到终端")
    args = parser.parse_args(argv)
    try:
        if args.output and args.output.exists():
            raise ValueError("输出文件已存在，请使用新的文件名")
        profile = yaml.safe_load(args.profile.read_text(encoding="utf-8"))
        report = build_brief(profile, as_of=args.as_of, previous=args.since,
                             refresh=args.refresh, cache_dir=args.cache_dir, draft=args.draft)
        if args.output:
            with args.output.open("x", encoding="utf-8") as output:
                output.write(report)
        else:
            print(report)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.exit(2, f"无法生成研究报告：{exc}\n")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
