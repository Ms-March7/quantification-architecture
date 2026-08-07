#!/usr/bin/env python3
"""Build a reproducible factor-driven data-unlock matrix from existing receipts."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path


def read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def write_new(path: Path, value: dict[str, object]) -> None:
    if path.exists():
        raise FileExistsError(f"refuse overwrite: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def row(
    data_family: str,
    unlocks: list[str],
    observed: str,
    formal: str,
    next_action: str,
    evidence: list[str],
) -> dict[str, object]:
    return {
        "data_family": data_family,
        "unlocks": unlocks,
        "bounded_capability_observed": observed,
        "formal_research_status": formal,
        "next_smallest_closure_action": next_action,
        "evidence": evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--registry-summary", type=Path, required=True)
    parser.add_argument("--rq-factor-metadata", type=Path, required=True)
    parser.add_argument("--market-reference-receipt", type=Path, required=True)
    parser.add_argument("--financial-pit-receipt", type=Path, required=True)
    parser.add_argument("--fund-etf-receipt", type=Path, required=True)
    parser.add_argument("--smoke-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    registry = read(args.registry_summary)
    rq = read(args.rq_factor_metadata)
    market = read(args.market_reference_receipt)
    finance = read(args.financial_pit_receipt)
    fund = read(args.fund_etf_receipt)
    smoke = read(args.smoke_receipt)
    if registry["record_count"] != 14385 or rq["all_factor_name_count"] != 13483:
        raise ValueError("unexpected registry or RQ factor count")
    if market["permission_summary"]["allowed"] != 8:
        raise ValueError("market reference capability receipt drifted")
    if finance["r1_gate_status"] != "NOT_MET":
        raise ValueError("financial PIT gate status drifted")
    if smoke["ic_runs"] != 0 or smoke["holdout_opened"]:
        raise ValueError("smoke crossed research boundary")

    evidence_root = "reports/data-governance"
    rows = [
        row(
            "沪深动态股票池与价量标签",
            ["动量", "反转", "波动", "流动性", "量价技术因子"],
            "正式窄口径R0面板已可qdata研究解析；10因子、140日、5221证券工程冒烟通过",
            "R0_SHSZ_PV_OPEN__CANONICAL_CORE01_TO_06_NOT_MET",
            "在冻结协议下另行启动Discovery；不得把冒烟当因子有效性结果",
            [str(args.smoke_receipt.relative_to(args.workspace_root)).replace("\\", "/")],
        ),
        row(
            "身份、交易日历、停牌/ST/涨跌停与公司行动",
            ["动态样本", "可交易标签", "复权一致性", "真实交易模拟"],
            "窄R0面板含动态上市片段和仅供成交模拟的掩码；未复权价格可用于工程",
            "PARTIAL__CANONICAL_IDENTITY_ACTION_STATUS_AND_ADJUSTMENT_NOT_CLOSED",
            "优先闭合因子真正需要的历史状态、行动修订与不封板成交语义，再发布不可变快照",
            ["data/catalog/README.md", "多因子/factor_factory/r0_shsz_panel_input.py"],
        ),
        row(
            "历史行业、指数成分与权重",
            ["板块轮动", "行业中性", "行业相对强弱", "基准归因"],
            "米筐有界探针中行业变更/映射、指数成分/权重和指数行情可调用",
            "INSPECTION_ONLY__NO_IMMUTABLE_PIT_SNAPSHOT",
            "按C-v1.1先选行业口径与日度轮动需求，再小批导出并验证PIT/权利",
            [f"{evidence_root}/ricequant-r1-market-reference-probe-20260804-v1/RECEIPT.json"],
        ),
        row(
            "历史股本、换手率与市值",
            ["规模", "换手", "流动性", "市值中性"],
            "米筐get_shares与换手率有界调用成功；市值尚需价格与股本语义校准",
            "INSPECTION_ONLY__NO_FORMAL_SNAPSHOT",
            "冻结字段、单位、复权/股本口径和available_at后导出最小快照",
            [f"{evidence_root}/ricequant-r1-market-reference-probe-20260804-v1/RECEIPT.json"],
        ),
        row(
            "PIT财务报表与派生财务因子",
            ["价值", "质量", "成长", "盈利能力", "资产负债表稳健性"],
            "米筐as-of财务端点有界正样本通过；目录中10140个米筐因子被保守归为R1财务依赖",
            "PARTIAL__AVAILABLE_AT_AND_REVISION_CHAIN_NOT_CLOSED",
            "选定首批财务因子的最小原始字段，闭合首次可见日、修订链、覆盖和导出权利",
            [f"{evidence_root}/ricequant-r1-financial-pit-probe-20260804-v1/RESULT.json"],
        ),
        row(
            "历史估值与市场参考",
            ["市盈率", "市净率", "市销率", "股息率", "估值分位"],
            "米筐目录有41个估值/市场参考依赖因子；因子值端点样本可调用",
            "METADATA_AND_BOUNDED_VALUE_VISIBLE__PIT_NOT_ADMITTED",
            "优先获取公式/字段语义；研究输入使用可审计原始字段或合法冻结因子快照",
            [str(args.rq_factor_metadata.relative_to(args.workspace_root)).replace("\\", "/")],
        ),
        row(
            "业绩快报、业绩预告与分析师预期",
            ["盈利预期", "预期修订", "业绩超预期"],
            "快报和预测端点有界as-of差异通过",
            "PARTIAL__TIMESTAMP_REVISION_AND_COVERAGE_NOT_CLOSED",
            "先定义可交易时点，再核验公布时间、修订、覆盖和去重规则",
            [f"{evidence_root}/ricequant-r1-financial-pit-probe-20260804-v1/RESULT.json"],
        ),
        row(
            "审计意见",
            ["财务可靠性", "风险过滤"],
            "米筐端点权限拒绝",
            "BLOCKED_AT_RICEQUANT_ACCOUNT",
            "从已下载CSMAR候选或校内合法来源做最小PIT准入，不降低门槛",
            [f"{evidence_root}/ricequant-r1-financial-pit-probe-20260804-v1/RESULT.json"],
        ),
        row(
            "ETF历史成分与基金持仓",
            ["ETF穿透", "重复持股", "板块/个股真实暴露"],
            "米筐fund holdings与ETF components均权限拒绝",
            "BLOCKED_AT_RICEQUANT_ACCOUNT",
            "可另行评估聚宽或基金官方定期/每日篮子；必须按日期与披露下限管理",
            [f"{evidence_root}/ricequant-live-capability-probe-20260803-v1/RESULT.json"],
        ),
        row(
            "风险模型暴露",
            ["风格中性", "边际风险贡献", "组合优化"],
            "米筐风险暴露端点权限拒绝",
            "BLOCKED_AT_RICEQUANT_ACCOUNT",
            "先用可审计本地字段自建简化风险暴露；商业模型另行授权后再比较",
            [f"{evidence_root}/ricequant-r1-market-reference-probe-20260804-v1/RECEIPT.json"],
        ),
        row(
            "宏观、利率与海外市场状态",
            ["牛熊状态", "防御/进攻切换", "全球风险开关"],
            "米筐收益率曲线有界调用成功；完整GCTX未建立",
            "INSPECTION_ONLY__R2_NOT_MET",
            "按市场状态因子清单选择最小国内利率、汇率、商品和海外指数集，再做时区/PIT校准",
            [f"{evidence_root}/ricequant-r1-market-reference-probe-20260804-v1/RECEIPT.json"],
        ),
    ]
    result: dict[str, object] = {
        "schema_version": 1,
        "matrix_id": "factor-driven-data-unlock-matrix-20260805-v1",
        "date": str(date.today()),
        "status": "CURRENT_CAPABILITY_MAPPED__R1_NOT_CLOSED__NO_FORMAL_PROMOTION",
        "factor_registry_counts": registry["source_counts"],
        "dependency_level_counts": registry["dependency_level_counts"],
        "currently_local_executable_factor_count": registry["local_compute_status_counts"]["LOCAL_EXPRESSION_EXECUTABLE"],
        "ricequant_factor_names_visible": rq["all_factor_name_count"],
        "ricequant_probe_categories": len(rq["factor_types"]),
        "rows": rows,
        "decision": {
            "can_start_r0_price_volume_factor_research_after_separate_run_authorization": True,
            "can_start_canonical_r1_factor_research": False,
            "recommended_parallelism": "A线按已闭合依赖启动价量因子；B线只补被首批因子实际需要的数据",
        },
        "formal_effect": "NONE",
        "catalog_promotions": 0,
        "bulk_downloads": 0,
        "research_runs": 0,
    }
    write_new(args.output.resolve(strict=False), result)
    print(json.dumps({"status": result["status"], "rows": len(rows), "currently_local_executable_factor_count": result["currently_local_executable_factor_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
