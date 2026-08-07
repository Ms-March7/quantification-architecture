"""Command line entry point for committed/aborted factor-run reports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .reporting import IntegrityError, ReportingError, write_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m research_reporting",
        description="校验并汇总一个已 COMMITTED 或 ABORTED 的因子研究运行。",
    )
    parser.add_argument("--run", required=True, type=Path, help="运行目录")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="报告目录；默认是运行目录同级的 reports 子目录",
    )
    parser.add_argument("--top-n", type=int, default=10, help="候选和事件回测最多展示条数")
    parser.add_argument(
        "--basename",
        help="输出文件基名（不得包含目录）；默认 <run-name>-research-report",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    run_dir = args.run.expanduser().resolve()
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else run_dir.parent / "reports"
    )
    try:
        json_path, markdown_path, report = write_report(
            run_dir,
            output_dir,
            top_n=args.top_n,
            basename=args.basename,
        )
    except IntegrityError as exc:
        print(f"完整性校验失败：{exc}", file=sys.stderr)
        return 3
    except ReportingError as exc:
        print(f"无法生成报告：{exc}", file=sys.stderr)
        return 2

    outcome = report["outcome"]
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    print(f"结论: {outcome['verdict_cn']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
