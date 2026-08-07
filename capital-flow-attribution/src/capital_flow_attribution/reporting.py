from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .utils import now_iso


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def size_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def render_report(
    report_root: Path,
    *,
    run_id: str,
    as_of: str,
    dates: tuple[str, ...],
    coverage: list[dict[str, Any]],
    paths: dict[str, Path],
    manifest_count: int,
    data_bytes: int,
) -> None:
    missing = [row for row in coverage if row["quality"] != "OK"]
    lines = [
        "# S1R 日频资金状态闭环验收报告",
        "",
        f"- 运行：`{run_id}`",
        f"- 生成时间：`{now_iso()}`",
        f"- 时间点截止：`{as_of}`；仅纳入不晚于该时点的证据",
        f"- 固定窗口：`{dates[0]}` 至 `{dates[-1]}`，{len(dates)} 个交易日",
        "- 阶段结论：`REVIEW`",
        "- 研究用途：只陈述观测、代理与缺失，不构成投资建议",
        f"- raw 响应数：{manifest_count}",
        f"- 本次数据体积：{data_bytes:,} bytes",
        "",
        "## 结论先行",
        "",
        "沪深融资融券、市场宽度和可得互联互通成交字段已经形成可审计的20日账本。",
        "北交所融资融券、完整ETF连续份额+NAV、历史互联互通资格和申万PIT映射仍缺失，故G1不能判定PASS。",
        "这些缺失被保留为未知，没有由第三方标签或当前截面回填历史。",
        "",
        "## 来源覆盖",
        "",
        "| 数据族 | 来源 | 覆盖 | 证据 | 质量 | 许可/再利用状态 | 说明 |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in coverage:
        lines.append(f"| {row['family']} | {row['source']} | {row['coverage']} | {row['evidence_state']} | {row['quality']} | {row['license']} | {row['notes']} |")
    lines.extend(["", "## REVIEW项", ""])
    for row in missing:
        lines.append(f"- `{row['family']}`：{row['notes']}")
    lines.extend(["", "## 可审计产物", ""])
    for label, path in paths.items():
        lines.append(f"- {label}: `{path.as_posix()}`")
    lines.extend(
        [
            "",
            "## 推断边界",
            "",
            "- 融资余额仅代表融资交易风险敞口的一部分，不代表全部散户。",
            "- ETF当前规模、份额或二级成交不自动等于申赎流，也不代表机构身份。",
            "- 互联互通成交额不自动等于净流入，当前资格清单不回填历史资格。",
            "- Qlib行情只作RESEARCH_ONLY背景，不作为官方历史证券主表。",
        ]
    )
    summary = report_root / "results" / "summary.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    escaped = html.escape("\n".join(lines))
    (report_root / "results" / "report.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>S1R验收</title>"
        "<style>body{font:15px/1.55 system-ui;max-width:1100px;margin:40px auto;padding:0 24px;white-space:pre-wrap}</style>"
        f"<body>{escaped}</body>",
        encoding="utf-8",
    )
