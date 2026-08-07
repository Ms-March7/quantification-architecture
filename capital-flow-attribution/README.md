# A股资金行为归因工程

状态：`G1修复 + S2/S3研究骨架 / DONE`  
阶段门：`G1 REVIEW`  
基准日期：`2026-07-19`  
项目根：`${QUANTIFICATION_ROOT}\capital-flow-attribution`

## 1. 项目目标

本工程建立一套可审计、可复现、按时间点约束的中国内地相关市场资金行为证据体系。工程最终研究四类问题：机构权益敞口、散户风险敞口、科技资金可能去向，以及1日、5日、20日的资金可切换性和冲击成本。

工程不承诺从公开数据精确还原每日机构或散户真实净买卖。无法识别的部分必须保留为 `NOT_IDENTIFIED` 或未知资金，不允许为了得到完整叙事而强制归因。

## 2. 当前阶段

`S0/G0`研究合同、`S1`六个工作包和`S1R`四个工作包均已完成。2026-07-19又完成独立的 `20260719-g1-research-v22`：补齐北交所融资融券、沪深ETF官方日份额和79/80条互联互通通道日记录，并搭建未知资金守恒、迁移图、可切换性上下界和研究型滚动回测。

G1仍为 `REVIEW`；S2/S3产物只是可测试骨架和 `RESEARCH_ONLY` 回测，不代表G2/G3通过。工程不把融资、ETF或互联互通代理解释为精确机构/散户身份，也未向多因子发布任何特征。

## 3. 市场范围

市场范围以中国内地资金相关性为原则，分为三层：

1. 内地核心市场：上交所、深交所、北交所的历史时点证券与全部内地上市ETF。
2. 互联互通：沪股通、深股通、港股通和ETF互联互通的历史合资格范围及可合法取得的公开字段。
3. 港股背景：港股通历史合资格证券的行情、行业、交易日历、人民币/港币换算及必要市场基准。

首轮不收集与内地资金联系不足的完整香港股票市场。某个渠道没有公开字段时，记录 `MISSING` 或 `NOT_IDENTIFIED`，不得反推为零。

## 4. 证据语言

所有研究输出必须标记证据层级：

- `OBSERVED`：来源直接披露且已通过时间点和质量检查的事实；
- `PROXY`：与研究对象有关但不能等同于研究对象的代理；
- `PARTIALLY_IDENTIFIED`：只能给出区间、上下界、概率或证据强度；
- `NOT_IDENTIFIED`：当前数据不能识别。

工作流状态使用 `DONE / REVIEW / BLOCKED`；测试状态使用 `PASS / FAIL / ERROR / SKIP`；数据状态使用 `OK / MISSING / STALE / CONFLICT / UNLICENSED`。这些状态不得混用。

## 5. 项目入口

- [路线图](ROADMAP.md)：阶段、阶段门和推进顺序；
- [当前状态](STATUS.md)：工作包完成情况、审查项和下一步；
- [决策记录](DECISIONS.md)：只追加用户已经批准的口径；
- [数据目录](DATA_CATALOG.md)：现有、历史和计划数据集登记；
- [研究协议](protocol/RESEARCH_PROTOCOL.md)：H1—H4、识别层级和禁止推断；
- [数据合同](protocol/DATA_CONTRACT.md)：市场、代码、字段、许可和存储合同；
- [时间点合同](protocol/POINT_IN_TIME_CONTRACT.md)：可用时间、修订和跨市场日历；
- [会计合同](protocol/ACCOUNTING_CONTRACT.md)：ETF、融资及防重复记账；
- [日频闭环合同](protocol/DAILY_LOOP_CONTRACT.md)：S1第一批输入、输出和排除项；
- [验证合同](protocol/VALIDATION_CONTRACT.md)：G0/G1验收及状态语义；
- [报告合同](protocol/REPORTING_CONTRACT.md)：报告结构、证据措辞和过度归因拦截。
- [S1来源探针](docs/S1_SOURCE_PROBE.md)：官方来源、字段和缺失裁决；
- [S1验收合同](docs/S1_ACCEPTANCE.md)：六个工作包与G1判定；
- [S1固定配置](configs/s1_pilot.json)：规范运行和同日raw重建参数；
- [S1R架构基线](docs/S1R_ARCHITECTURE.md)：组件复用、自研边界、工作包和验收；
- [S1R验收报告](docs/S1R_ACCEPTANCE.md)：模块拆分、适配器探针、v10/v11比较和残余风险；
- [来源优先级配置](configs/source_policy.json)：官方、Tushare、AKShare和Qlib的机器可读角色。
- [v22验收报告](docs/G1_RESEARCH_V22_ACCEPTANCE.md)：G1修复、S2骨架、S3回测和残余缺口；
- [开源组件登记](docs/OPEN_SOURCE_COMPONENTS.md)：复用组件、用途和不可替代的数据合同；

## 6. 数据与输出边界

统一数据根为 `${QUANTIFICATION_ROOT}\data`。项目代码目录不保存大型正式数据。S1规范运行保持冻结；当前研究候选位于 `${QUANTIFICATION_ROOT}\data\capital-flow-attribution\research\20260719-g1-research-v22`。

工作区数据发现与新运行绑定统一使用 `${QUANTIFICATION_ROOT}\data\catalog\dataset_cli.py`。历史 `v10`、`v22` 配置中的 `qlib_store` 是冻结运行证据，保持不改；两个旧 Qlib 数据本体已于 2026-07-20 删除，旧运行不能按原路径完整复现。新配置不得再绑定它们，必须等待按新来源政策生成的中央事实快照和新研究投影。

```powershell
${QUANTIFICATION_ROOT}\data\catalog\qdata.cmd list --consumer capital-flow-attribution
```

上述命令仅用于历史依赖和清理审计；`--intent research` 必须被拒绝。

规划输出位置：

```text
${QUANTIFICATION_ROOT}\capital-flow-attribution\          协议、配置、代码和小型夹具
${QUANTIFICATION_ROOT}\data\                              共享正式数据
${QUANTIFICATION_ROOT}\reports\capital-flow-attribution\s1\20260718-s1-pilot-v10  S1验收报告
${QUANTIFICATION_ROOT}\reports\capital-flow-attribution\research\20260719-g1-research-v22  v22研究验收报告
${QUANTIFICATION_ROOT}\tmp\capital-flow-attribution\     临时运行产物
```

存储预算：S0不新增正式数据且文档不超过100MB；S1硬上限10GB；G盘必须至少保留120GB可用空间；分钟数据预计占用超过50GB时必须另行批准。

## 7. 隔离边界

以下数据与项目保持隔离；Qlib、多因子和现有技能未修改、移动、复制或建立链接：

```text
${QUANTIFICATION_ROOT}\多因子
${QUANTIFICATION_ROOT}\资金流向skill
${QUANTIFICATION_ROOT}\.agents\skills\a-share-capital-flow-monitor
```

现有Harness只新增了 `capital_flow_attribution` 目标、两份小夹具、一个JSON合同和一个通用断言；没有建立第二套Harness，也没有修改现有资金监控技能。

## 8. 当前禁止事项

- 不购买数据或登录商业账户；
- 不迁移、复制或删除Qlib；
- 不把历史“主力/大单”标签解释为真实投资者身份；
- 不训练机构或散户分类器；
- 不建设多年分钟、逐笔或L2数据；
- 不生成正式投资建议；
- 不向多因子发布未经G3验证的资金行为特征；
- 不自动跨越阶段门。

## 9. S1运行入口

```powershell
py -3.11 -m unittest discover -s tests -v
py -3.11 scripts\run_s1.py online --config configs\s1_pilot.json
```

当前固定配置启用 `offline_raw_only`，用于从同日已保存raw重建规范 `v10`，不会再次访问网络。新鲜采集必须创建新配置与新运行号，不得覆盖 `v10`。

## 10. S1R依赖入口

核心组件和可选中国市场适配器安装在项目隔离环境中：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[china-sources]"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tushare token只允许通过 `TUSHARE_TOKEN` 环境变量提供；没有token时适配器必须显示为未配置或SKIP，不影响官方源和离线测试。

`WP-S1R-01—04`已经完成，v22新增研究依赖组为 NetworkX、scikit-learn 和 ruptures。当前测试为39/39 PASS；Tushare无token时明确SKIP。正式结果仍受G1数据覆盖合同约束。

## 11. v22研究入口

```powershell
.\.venv\Scripts\python.exe scripts\run_research.py --config configs\g1_research.json --run-id <new-immutable-run-id>
.\.venv\Scripts\python.exe scripts\audit_research_run.py ${QUANTIFICATION_ROOT}\data\capital-flow-attribution\research\20260719-g1-research-v22
```
