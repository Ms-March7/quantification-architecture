#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from capital_flow_attribution.comparison import compare_run_roots  # noqa: E402
from capital_flow_attribution.reporting import write_json  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare a frozen S1 baseline with an S1R candidate run.")
    parser.add_argument("--baseline-root", required=True, type=Path)
    parser.add_argument("--candidate-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError(f"immutable comparison output already exists: {args.output_dir}")
    result = compare_run_roots(args.baseline_root, args.candidate_root)
    write_json(args.output_dir / "data" / "comparison.json", result)
    summary = args.output_dir / "results" / "summary.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# S1 v10与S1R候选运行对比",
        "",
        f"- 测试状态：`{result['test_status']}`",
        f"- 基准：`{result['baseline_root']}`",
        f"- 候选：`{result['candidate_root']}`",
        f"- raw哈希一致：`{result['raw']['hashes_equal']}`",
        f"- Pandera：`{result['candidate_s1r_checks']['pandera_daily_state']}`",
        f"- Parquet与DuckDB行数一致：`{result['candidate_s1r_checks']['parquet_duckdb_counts_equal']}`",
        "",
        "| 核心表 | 字节一致 | 基准字节 | 候选字节 |",
        "|---|---|---:|---:|",
    ]
    for table in result["tables"]:
        lines.append(
            f"| {table['relative_path']} | {table['byte_identical']} | {table['baseline_bytes']} | {table['candidate_bytes']} |"
        )
    lines.extend(
        [
            "",
            "该比较只证明架构重整没有改变冻结S1输出；不会把原有MISSING或CONFLICT自动升级为OK。",
            "不构成投资建议。",
        ]
    )
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if result["test_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
