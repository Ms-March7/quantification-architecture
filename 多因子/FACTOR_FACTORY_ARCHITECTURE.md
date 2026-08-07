# 自动多因子研究工厂：架构与晋级协议

## 目标

本项目把因子研究做成可重复、可审计的闭环。挖掘程序不能直接进入券商；系统最高自动状态为 `SHADOW_ONLY`，公开多源 Qlib 快照另行锁定为 `RESEARCH_ONLY`。

```text
固定版本研究快照
  -> 无量纲安全表达式生成 / 变异 / 复杂度惩罚
  -> discovery 初筛与遗传
  -> horizon+1 purge
  -> validation：IC、稳定性、FDR、五绝对相位事件回测
  -> 只在当日 eligible 样本上去冗余
  -> 2–5 个低相关组件做完整样本等权标准化组合
  -> 写入不可变 freeze.json
  -> 第一阶段停止；留出集行情未被物化
  -> 人工核对精确 freeze hash 后运行独立 open-holdout
  -> 共享 append-only 账本原子消费 holdout
  -> holdout 一次性统计复核与五相位交错子组合
  -> 数据源状态上限
```

## 核心合同

1. 决策日 `t` 的表达式只能读 `t` 及以前数据。
2. 标签与执行对齐为 `open(t+1)` 到 `open(t+horizon+1)`；固定退出日停牌用截至该日最近收盘估值，终止上市或数据失效按零回收压力事件处理。
3. discovery、validation、holdout 之间保留 `horizon+1` 个交易日的 purge。
4. validation 对冻结候选全集做块符号置换与 Benjamini–Yekutieli 任意依赖 FDR；常量或评估日期不足 20 天的因子不能通过。
5. 事件回测使用五个绑定绝对交易日的换仓相位，并以五个等资金交错子组合的合并净值作为硬门；不把五条重叠曲线当独立统计证据。
6. 相关性去冗余只读 validation 当日 `eligible` 成员；未上市、已退市、隔离和非研究股票不能影响组件选择。
7. Qlib 候选根表达式必须无量纲；禁止裸复权价格横截面比较，未知字段单位直接拒绝。
8. 多因子组合要求 2–5 个完整可见组件。任一股票缺少某个组件时，不允许动态退化成单因子。
9. holdout 标签只在冻结文件校验成功、共享账本首次消费后生成。同一 A 股重叠时期不能通过换种子、股票池、数据源或输出目录再次晋级。
10. 已完成目录不可覆盖；新实验必须使用新目录和新的未见时期。
11. 第一阶段 Qlib reader 只物化到 validation 后的最后一个 purge 日。冻结同时保存 promotion policy、面板结构、快照 SHA、Python/NumPy 版本及完整研究引擎源码；任何上限升级、环境漂移或源码漂移都会拒绝开封。

## A 股回测边界

回测为多头、无杠杆、信号次日开盘执行，包含佣金、卖出印花税、滑点、buyable/sellable、T+1、停牌、终止上市与数据失效压力核销。Qlib 标准化复权价强制 `lot_size=1`，只能解释为资金权重研究，不能声称真实整数手、排队成交或容量已复现。

公开快照缺历史 ST、官方涨跌停状态、公司行动独立账本、ADV 冲击模型、行业/市值/beta 暴露，因此最高为 `RESEARCH_ONLY`。五相位回测是调仓起点稳健性检查，不是五份独立样本，也不是 10 万元分钟级试水仿真。

## 目录

```text
factor_factory/
  expressions.py   因果算子、量纲与复杂度
  generation.py    种子、语法生成、变异、交叉、血缘
  evaluation.py    执行对齐标签、IC、稳定性、置换、FDR
  selection.py     硬门槛和当日 eligible 去冗余
  ensemble.py      完整样本等权多因子组合
  backtest.py      A 股事件回测、绝对相位和终止事件
  registry.py      原子研究产物
  pipeline.py      两阶段冻结、一次性 holdout 账本与全流程
  qlib_store.py    Qlib 二进制、价格可用区间与因果隔离
  qmt_source.py     无交易方法的 QMT 只读数据边界
```

## 真实快照运行

```powershell
.venv\Scripts\python.exe -m factor_factory.cli `
  --config configs\factor_factory_real_2018_2026.json `
  from-qlib `
  --root data\market\snapshots\2026-07-14\qlib_bin `
  --start-date 2018-01-02 --end-date 2026-07-14 `
  --universe all --exchanges SH,SZ `
  --output research_runs\factor_factory\full-shsz-2018-2026-v2-research
```

该命令只完成 discovery/validation 并产生 `FROZEN_AWAITING_HOLDOUT`；V2 不会创建 holdout 治理库。V3 接通后会在读取 validation 前把研究尝试写入项目内的 `.state`。正式产物必须保存在 `research_runs` 这类保留目录并另行备份，不能放入 `.tmp`。

只在代码、配置、数据和环境均不再改动后，从 `freeze.json` 人工抄录完整 64 位哈希，执行唯一第二阶段：

```powershell
.venv\Scripts\python.exe -m factor_factory.cli `
  open-holdout `
  --root data\market\snapshots\2026-07-14\qlib_bin `
  --research research_runs\factor_factory\full-shsz-2018-2026-v2-research `
  --confirm-freeze-hash <完整64位freeze_hash> `
  --output research_runs\factor_factory\full-shsz-2018-2026-v2-holdout
```

若进程被硬终止，先计算 `.run.lock` 的 SHA-256 并确认原 PID 已死亡，再用 `recover-lock --output <目录> --confirm-lock-sha256 <哈希>` 将锁归档；不得手工删除。治理账本采用 SQLite + 外部 anchor 预提交日志，可恢复数据库提交前后两个崩溃窗口。

`from-json` 使用共同日期交集，只允许 `ENGINEERING_ONLY`，不能形成研究或交易结论，更不能打开 holdout。公开 Qlib 的 holdout 即使通过，状态仍只能是 `RESEARCH_ONLY`，不能直接触发影子仓位或实盘。
