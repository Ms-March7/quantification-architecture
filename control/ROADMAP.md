# Quantification 工作区总 Roadmap

更新时间：2026-08-06  
当前活动层：`L1_DATA + L2_R0_SHSZ_PV_DISCOVERY_CLOSED`  
当前总阶段门：`G-DATA = REVIEW`；`L0_INSPECTION = AVAILABLE_ONLY`；`R0/PV = NOT_MET`；`R1/CLASSIC_CN = NOT_MET`；`R2/A_SHARE_GLOBAL = NOT_MET`  
执行策略：`一次只细化和推进一层；后续层仅保留边界与出口`

当前有界执行包：`R0_SHSZ_PV_V2_COMPOSITE_PUBLICATION_AND_L2_INPUT_ACCEPTANCE = PASS__NARROW_RESEARCH_INPUT_OPEN`。2026-08-04 已正式发布 `cn-a-share-r0-shsz-pv-panel@20050104-20260803-v1`，内容哈希 `26309d36b7c08096fff55df100f1d27df8412b106ea48f282ce0f7caad37084b`；精确 `qdata --intent research`、五份 authority/payload 哈希、原子发布、真实 `PanelData` 小样本和至少一交易日滞后门均通过。它仍是独立于 canonical `R0/PV` 的沪深普通 A 股窄门：只允许未复权价量、已实现总收益和仅供仿真的可执行性掩码；不含北交所、B 股、CDR，并失败关闭排除已知身份冲突记录 `600849.XSHG`。因此只开放价量单因子 Discovery 和预注册验证准备，不改变 canonical `R0/PV`、`R1/CLASSIC_CN`、`R2/A_SHARE_GLOBAL` 或 CORE-01～20 的完成计数。

最近完成的并行有界工作包：`L1L2-FACTOR-AB-CV1_1-20260805 = COMPLETED__PROTOCOL_REGISTRY_DEPENDENCY_CAPABILITY_AND_ENGINEERING_SMOKE_PASS__NO_RESEARCH_RUN`。新的 C-v1.1 已冻结为市场状态层—板块轮动层—个股单因子层—组合与持仓决策层四层中文协议；统一因子元数据目录登记本地 512、聚宽 390、米筐 13,483，共 14,385 条来源限定记录，不含历史因子值。米筐 13 类因子极小样本调用均成功、12 类有非空值，未保存值或凭据；因子驱动的数据解锁矩阵形成 11 个数据族的当前能力、正式缺口与最小闭合动作。精确 `R0_SHSZ_PV` 快照上的 10 因子全市场工程 smoke 覆盖 2026-01-05—2026-08-03、140 日、5,221 证券，面板加载 21.199 秒、因子计算 0.423 秒；公式等价检查最大误差 0。17 项相关测试通过，`qdata doctor` 为 0 errors、13 个既有非本批 warnings。全过程未批量下载米筐历史因子值、未写 `data/published/`、未晋级 `datasets.json`、未计算 IC/分组收益/回测、未消费 Validation、未打开 Holdout；canonical R0/R1/R2 与 CORE 计数不变。回执见 `reports/data-governance/factor-ab-capability-20260805-v1/`。

最近完成的并行执行包：`L1L2-FACTOR-ABC-DISCOVERY-20260805 = COMPLETED__A1_EXECUTION_SAFETY__A2_96_CALIBRATION__A3_C1_512_DISCOVERY__50_FACTOR_STABILITY_DESCRIPTION__B1_B2_RQDATA_CANDIDATES__NO_VALIDATION_NO_HOLDOUT`。本包只对精确正式快照 `cn-a-share-r0-shsz-pv-panel@20050104-20260803-v1` 开展沪深普通 A 股价量 Discovery：含公司行动总收益的标签、最低交易年龄、T+1、成本、可交易掩码隔离和 CPU/GPU 等价门已闭合；96 因子校准与 512/512 因子 GPU Discovery 已 `COMMITTED`。512 个定义中 68 个属于公司行动不敏感层，50 个同时达到 Discovery BY-FDR≤10%；这 50 个又完成 3/5/8 年滚动窗及扩展窗、2009—2021 年年度测试、20 日 purge、5 日测试起点 embargo 与 15/30/50bps 成本描述，45 个在四方案下保持正平均 IC，6 个同时保持正平均 30bps 成本后 Top 20% 超额。6 个明显集中在成交额活跃度与日内价格位置家族，尚未相关性去重、行业/规模中性或独立 Validation，不能称为可靠因子或交易结论。因子工厂 154/154、米筐候选构建 7/7 回归通过，`qdata doctor` 0 errors；Validation 未消费、Holdout 未打开、未写 `data/published/`、未自动交易。

并行 B 线形成两个独立 `INSPECTION_ONLY/NOT_ADMITTED` 候选：中信 2019 一级行业历史为 30 个行业、9,222 条成员区间、5,496 个唯一证券，且 2026-08-03 窄门面板 5,201 个证券全部可连接；指数/市场参考为 7,385 条锚点日指数主档、8 个核心指数 28,250 条 2005-01-04—2022-01-28 日行情和 4,274 条中债收益率曲线。它们仍缺历史 `available_at`/修订链、申万研究主口径、完整指数家族和保存/派生权复核，未进入 `datasets.json`，不能支撑正式行业轮动或改变 canonical R0/R1/R2、CORE 计数。收口见 `reports/data-governance/r0-shsz-pv-c-v1-1-discovery-20260805-v1/`、`reports/data-governance/ricequant-core11-industry-history-candidate-20260805-v1/` 与 `reports/data-governance/ricequant-r1-index-market-candidate-20260805-v1/`。

下一有界研究包更新为 `L2-R0-CLASSIC-CLUSTER-SHORTLIST-DIAGNOSIS = NEXT__NOT_STARTED__NO_VALIDATION`：以本次 130 个公司行动尺度安全且 30bps 描述为正的规格、43 个相关簇为母集，以同时通过 12 种 walk-forward 最差年度折、四预测期和六市场状态方向门的 77 个规格、27 个相关簇为重点；只在同一 Discovery 窗口完成每簇唯一代表、公式同义去重、条件增量 IC、持仓相关性和唯一 shortlist freeze，不得消费 Validation 或打开 Holdout。并行数据包仍是米筐/聚宽/校内候选的来源权利、`available_at`/修订链、申万主口径和正式原子快照闭合。两包完成前，不启动行业轮动结论、组合权重学习或持仓交易建议。

最近完成的执行包：`L1L2-ABC-EXTERNAL-CLASSIC-FACTOR-DISCOVERY-20260806 = COMPLETED__426_OF_426__281_ACTION_SAFE_FDR__130_ACTION_SAFE_30BPS__43_SAFE_COST_CLUSTERS__NO_VALIDATION_NO_HOLDOUT`。A 线不重跑旧 512 目录，复用已封存的米筐元数据与能力回执，以聚宽公开 Alpha101/191、公开技术公式、米筐可见价量/技术名称和学术经典建立 426 个去重新规格；每个绑定来源、公式、输入、参数、因果时点、公司行动敏感性和定义哈希，所有数值均由本地透明公式计算，未批量下载米筐历史因子值。B 线只执行有证据的确定性处理并保持候选 `INSPECTION_ONLY/NOT_ADMITTED`。C 线以精确正式输入 `cn-a-share-r0-shsz-pv-panel@20050104-20260803-v1` 开展 Discovery：动态历史股票池、`realized_total_return`、T+1/次日执行、5/10/20/60 日标签、3/5/8 年滚动与扩展窗、purge/gap、成本、换手、容量、衰减、市场状态稳定性、BY-FDR 和相关聚类；旧 512 只作冻结对照，未重跑。

正式 GPU 运行 `r0-shsz-pv-classic-discovery-426-20260806-v1` 已 `COMMITTED`：426/426 机械完成、0 失败；376 个达到 Discovery BY-FDR≤10%，其中 281 个通过运行时公司行动尺度安全门；186 个通过 30bps 成本描述，其中 130 个同时通过公司行动尺度安全门，去重后为 43 个相关簇。130 个中，89 个在全部 12 种 walk-forward 方案下连最差年度折 IC 也为正，127 个在 5/10/20/60 日方向均为正，93 个在六市场状态方向均为正；三项交集为 77 个规格、27 个相关簇。该结果只形成 shortlist 母集，不表示 130 或 77 个可靠/可交易因子；可交易结论固定为 0。这里的“尺度安全”只表示该公式不依赖任意绝对价格/股数基准，并不等于完整公司行动审计、可执行或可投资。运行未写 `data/published/`，Validation 未消费，Holdout 物理未加载，自动交易 false，canonical R0/R1/R2 与 CORE 计数不变。A/B/C 收口见 `reports/data-governance/abc-classic-factor-discovery-20260806-v1/`。

同批 B 线已把工作日 19:20 的统一候选维护扩展为“沪深价量 → B 路径 → 财务 PIT → JoinQuant 股本 PIT → 中央候选索引”。2026-08-06 真实五步闭环 PASS：四个数据步骤均为 `NO_CHANGE/NO_NETWORK`，索引为 28 根/107 evidence/82 manifest，正式 catalog 哈希前后不变；财务队列为 14/14、131,777 条原始行和 263,554 条本地修订事件，JoinQuant 股本候选为 196,693 条 PIT 区间。自动化仍只维护候选，不自动登录 JoinQuant、不自动晋级正式 catalog 或快照。

2026-08-06 完成公司行动字段可解性有界测试 `L1-CA-FIELD-SOLVABILITY-AUDIT-20260806 = COMPLETED__SOLVABILITY_QUANTIFIED__NO_FIELD_BACKFILL__INSPECTION_ONLY`。对 `cn-a-share-corporate-actions-multisource@candidate-20260805-v2` 全量 1,787,017 条观察进行了只读校验：1,448,849 条有日级公开日期证据，338,168 条缺日级证据；直接永久身份 112,203 条，六位本地代码 1,786,609 条；`revision_id` 118 条、`supersedes_revision_id` 0 条；跨源数值冲突键 52,670 个，其中 52,391 个为 10 的整次幂单位差候选；JQ 实施说明可解析标准条款 56,767 条，不含配股选择的简单股份乘数前提候选 88,686 条。新增失败关闭审计工具及 9 项专项测试，并与修订事件账本和v2既有测试合计 27/27 PASS；报告 manifest 哈希复核 PASS，`datasets.json` 前后哈希不变。回执位于 `reports/data-governance/corporate-action-field-solvability-audit-20260806-v1/`；本批未批量访问外部网络、未回填业务行、未写正式 catalog、未晋级质量、未运行因子/回测、未消费 Validation/Holdout。

2026-08-05 公司行动子包已完成研究价格处理闭环并保留正式事件审计缺口。七张 RESSET 公司行动表共 614,977 条源记录形成 614,976 条候选观察，28 条逗号宽度异常按表级确定规则修复，1 条 APLA 歧义记录保留原始 token 并隔离；与既有 CSMAR/CNINFO、RQData 候选合并为 1,073,145 行逐来源观察账本，67,185 个跨来源日期键只标记重叠/冲突而不静默选源。正式 `cn-a-share-r0-shsz-pv-panel@20050104-20260803-v1` 的 Discovery 截止切片（2005-01-04—2022-01-28，4,151×4,753）完成因果总收益信号价格验收：10,052,188 个有效证券日、9,991,906 条连续链接，最大收益重建误差 `2.22e-16`、OHLC 几何违规 0、原始未复权执行价哈希不变、Validation/Holdout 均未读取。由此，去除个股任意基准尺度后的跨期价格/总收益动量、反转、波动率和价格位置因子不再受除权跳变阻断；投影绝对价格横截面、未经股数调整的成交量跨期因子及公告时点/修订事件因子仍受限。公司行动账本继续为 `INSPECTION_ONLY/NOT_ADMITTED`，因为逐版本首次 `available_at`、完整更正/撤回/`supersedes`、多源条款单位裁决和部分供应商保存派生权尚未闭合；该缺口不再阻断上述研究价格因子，但 CORE-04/05 正式计数不变。证据见 `reports/data-governance/corporate-action-research-closure-20260805-v1/`，候选见 `data/market/quarantine/cn-a-share-corporate-actions-multisource/candidate-20260805-v1/`。

2026-08-05 又完成米筐/聚宽公司行动补缺与额度有界探针。米筐教育版当日 200 MiB 中剩余 201,827,478 B（192.478 MiB），且本探针没有新增业务下载；既有 60,244 条行动和 51,959 条除权因子无需重复拉取。聚宽在线研究环境真实查询 `finance.STK_XR_XD`、`finance.STK_CAPITAL_CHANGE` 均 PASS：在 `600519.XSHG`、`603986.XSHG` 两证券样本上分别返回 75 条分红送转和 151 条股本变动记录，确认董事会/股东大会/实施公告日、登记/除权日、取消公告字段以及股本变动公告日可作为日级 PIT 补源。在线 `jqdata` 不提供 `get_query_count()`，环境也未安装 `jqdatasdk`，故聚宽外部 SDK 的剩余调用条数仍未验证；该先行探针本身未做全市场导出、未写正式 catalog、未晋级质量，也不改变 CORE-04/05 或 R0/R1/R2。回执见 `reports/data-governance/rqdata-joinquant-company-action-fill-probe-20260805-v1/`。

2026-08-05 随后在用户明确授权的新数据下载范围内完成聚宽全市场公司行动回补与多源 v2 候选封口。在线研究环境分片导出的 `STK_XR_XD` 150,087 行、`STK_CAPITAL_CHANGE` 233,526 行已封存在 `data/market/quarantine/joinquant-company-actions/intake-20260805-full-shsz-v1/`；ZIP SHA-256 为 `7c51e5e5834892d57afe695f91799702285237717f0b1c104613550162ab2380`，129 个数据分片及全部 ZIP 成员的哈希、字节数、行数、ID 范围和表头均通过。v2 将董事会、股东大会、实施、登记、除权、到账、上市、取消及资本变化拆成 713,872 条阶段观察，并与不变的 v1 账本合并为 1,787,017 条逐来源观察；222,572 个独立来源家族重叠键和 52,670 个未裁决数值冲突键均保留，未静默择源。713,851 条日期级 PIT 候选只实施“公告日下一日历日后再绑定决策日历”的保守投影，未伪造供应商 `available_at`；正确合并 NULL/空串后 1,787,017/1,787,017 条仍缺正式 `available_at`。B 股及 347 条供应商非标准/临时代码原样保留并显式标记，尚待永久证券身份映射。该回补解决了聚宽阶段事件与股本行数缺口，但未解决公告时分秒、同阶段更正/撤回/`supersedes` 链、单位冲突裁决和完整保存/派生权，故仍为 `INSPECTION_ONLY/NOT_ADMITTED`，不进入 `datasets.json`，不改变 CORE-04/05、canonical R0/R1/R2、Validation 或 Holdout。证据见 `reports/data-governance/corporate-action-multisource-fill-20260805-v2/`，候选见 `data/market/quarantine/cn-a-share-corporate-actions-multisource/candidate-20260805-v2/`。

2026-08-05 完成 `RQDATA_B_PATH_DAILY_CANDIDATE_UPDATER_V1` 的真实候选运行与任务计划接线。2026-08-04—08-05 两日封存 5,202 行证券主档、10,404 行 ST/停牌/涨跌停状态、10,404 行申万 `sws` 行业归属、2,282 行公司行动滚动观察，以及沪深300/中证500/中证1000/科创50/创业板指 10 个指数—日期成分权重对；全部机械检查通过，行业逐日覆盖 100%，133 次业务调用实测流量 3,740,354 B。原 Windows 任务 `Quantification-RQData-B-Path-Daily` 的 19:10 smoke 返回 0，随后已由统一 19:20 任务取代并禁用。程序还生成 14 项、覆盖 5,202 证券的财务 PIT 可续跑队列，但本批财务 API 调用为 0；财务首次可见时间和历史修订仍未闭合。所有输出保持 `INSPECTION_ONLY/NOT_ADMITTED`，不自动写 catalog、不正式发布、不跑因子；回执见 `reports/data-governance/rqdata-b-path-daily-updater-20260805-v1/`。

财务 PIT 后继已形成独立可续跑 worker、合同和 8 项测试；与 B 更新器合计 14/14 测试通过。5 证券、2 字段、4 季度真实诊断返回 19 行、生成 38 条本地修订事件，并证明 `info_date` 后下一交易日 09:30 的保守 `available_at` 与确定性 `revision_id/supersedes` 可机械构建；`rice_create_tm` 只保留为供应商元数据。关键失败关闭是完整进程流量从 16,607,802 增至 19,935,352 B，即 3,327,550 B，而 worker 初始化后口径仅观测到 988 B；因此400证券任务不得直接调度，正式队列保持 0/14。米筐 `get_forecast_report_date` 极小权限探针另被明确拒绝，不能借该低流量端点给本地 CSMAR 全表补首次披露日。下一步必须先修复持久化的初始化前流量基线、缩小批次，并用聚宽财务表 `pub_date/report_date` 与米筐 PIT 多版本、本地 CSMAR 数值/更正记录拼接；本诊断不改变 R1 或正式 CORE 计数。证据见 `reports/data-governance/rqdata-financial-pit-daily-smoke-20260805-v1/`。

2026-08-05 候选资产管理与无人值守维护已闭合。`data/catalog/candidate_assets.json` 现在以受限 evidence JSON 为依据，中央登记 26 个 quarantine 顶层候选根、92 个证据文件和 67 个 manifest；它不读取业务 payload，也不授予研究权限。统一入口 `control/tools/run_candidate_daily_maintenance.py` 按“沪深价量候选 → B 路径候选 → 中央候选索引”运行，Windows 任务 `Quantification-Candidate-Daily-Maintenance` 已启用为工作日 19:20，旧 18:30/19:10 任务已禁用但未删除。真实入口与任务计划 smoke 均 PASS，25 项回归含两个硬崩锁恢复用例全部通过；本次两个数据步骤因水位已到 2026-08-05 均为零网络，正式 `datasets.json` 前后哈希一致。该自动化只维护 `INSPECTION_ONLY/NOT_ADMITTED` 候选，不自动发布正式快照、晋级质量或运行因子。聚宽 ZIP 的 C 盘精确重复件已在 G 盘副本哈希复核后送入回收站，未处理其他 C 盘文件。证据见 `reports/data-governance/candidate-daily-maintenance-closure-20260805-v1/` 与 `reports/data-governance/joinquant-download-duplicate-cleanup-20260805-v1/`。

## 1. 本文件管什么

本文件是工作区“下一步做什么”的最高路线文件。它不替代其他权威文件：

- `WORKSPACE_INDEX.md` 回答项目、目录和架构现在是什么；
- `projects.yaml` 登记机器可读的当前状态与依赖；
- `MIGRATION_PLAN.md` 只管理物理移动，不代表研究进度；
- 各项目自己的 `ROADMAP.md`、协议、manifest 和 `STATUS.md` 管项目内部事实。

当前仍只完整细化 `L1 数据层`，并对独立 `R0_SHSZ_PV` 窄门执行有界 L2 Discovery；该窄门不等于 canonical R0/R1/R2。`L2-P0_QDATA_FACTOR_INPUT_ENGINEERING` 的统一读取与拒绝越权合同已经从 fixture 扩展到上述正式面板；C-v1.1 的 512 个价量候选 Discovery 和 50 候选稳定性描述已完成，但没有消费 Validation，也没有打开 Holdout。canonical R0、R1、R2 继续为 `NOT_MET`；行业/基本面/全球环境 L2 与完整 L3—L5 仍由各自上游阶段门阻断。

### 1.1 强制维护规则

以后每一批涉及 L2—L5 的设计、实现、验证、发布或运维工作，都必须同步删改本文件：

1. 开始该批工作前，先把目标层从粗略轮廓改成当前可执行的工作包、依赖、产物和验收条件；
2. 原有占位、过期假设和已经被新决定替代的内容必须删除或改写，不得只在文末追加一个与旧规划冲突的新版本；
3. 工作中若范围、顺序、阶段门或阻断项发生变化，必须在同一批次立即更新本文件；
4. 工作结束后，按实际证据更新状态、完成项、失败项、残余风险和下一唯一工作包；失败或否证结果不得改写成 `DONE`；
5. 如果项目关系或机器状态同时变化，还要同步更新 `WORKSPACE_INDEX.md` 和 `projects.yaml`。

未完成上述 Roadmap 同步的 L2—L5 工作视为管理未完成，不得据此进入下一工作包或下一层。

## 2. 五层主线

```text
L1 数据层
  行情 / 证券主数据 / 公告 / 新闻 / 资金流原始数据
  统一身份、不可变快照、PIT 时间、质量与许可
        ↓ G-DATA
L2 特征与算法层
  基础市场特征 + 资金流特征 + 新闻事件特征 + 多因子候选
        ↓ G-ALG
L3 验证与阶段门层
  走样本外、成本、压力测试、Holdout、风险审查与否证
        ↓ G-VAL
L4 发布与 Shadow 层
  合同化特征发布、组合构建、容量 / 执行仿真、Shadow Only
        ↓ G-SHADOW
L5 报告、监控与反馈层
  数据质量、漂移、失败恢复、复盘和人工决策
```

治理、manifest、审计、人工批准和安全边界贯穿五层，不单独算第六层。本工作区当前不是自动交易系统。

| 层 | 当前状态 | 本轮详细程度 | 出口 |
|---|---|---|---|
| L1 数据 | `IN_PROGRESS` | 详细工作包与验收条件 | `G-DATA` |
| L2 特征与算法 | `R0_SHSZ_PV_INPUT_OPEN__C_V1_1_FROZEN__14385_FACTOR_METADATA_REGISTERED__TEN_FACTOR_ENGINEERING_SMOKE_PASS__NO_ECONOMIC_RESEARCH__CANONICAL_R0_R1_R2_NOT_MET` | 沪深普通 A 股价量窄门可另行预注册后进入 Discovery；512 本地表达式可执行，聚宽/米筐定义按依赖逐批实现；完整 R0/R1/R2 未开放 | `G-ALG` |
| L3 验证与阶段门 | `OUTLINE_ONLY`；已有项目级证据 | 只写目标和边界 | `G-VAL` |
| L4 发布与 Shadow | `BLOCKED_BY_UPSTREAM` | 只写目标和边界 | `G-SHADOW` |
| L5 报告、监控与反馈 | `OUTLINE_ONLY`；现有报告尚未形成闭环 | 只写目标和边界 | `G-OPS` |

状态语义：`DONE` 表示该工作包的产物和最小验收均存在；`PARTIAL` 表示已有可复用基础但未满足出口；`NEXT` 表示下一执行包；`PLANNED` 表示尚未开始；`BLOCKED` 表示被明确依赖阻断；`EXTERNAL` 表示不由本工作区开发。

## 3. L1 数据层：目标与现状

### 3.1 目标

建立一套任何新项目都能安全复用的**完整 A 股 + 全球环境 + 港股 + 全球关联个股多因子数据底座**：国内核心覆盖行情、主数据、可交易性、公司行动、指数/行业、PIT 基本面和股本估值；外盘必建范围覆盖 GCTX-8 全球市场环境、HK-4 港股全市场事实，以及定义范围内的 GLEQ-3 中国/产业链关联全球个股；全域规划继续包含基金/所有权、公告新闻、资金流、衍生品和高频执行数据，而不是只凑出 OHLCV 的最小回测集合。每个研究输入都有明确的 `dataset_id + snapshot_id`、所有者、物理位置、schema、内容哈希、时间可用性、许可、质量状态、用途上限和消费者；可变数据必须先冻结，研究代码不得靠目录名或 `latest` 猜数据。

数据统一管理不等于把全部旧数据移动到一个目录。用户已决定不把既有社区 Qlib 作为过渡数据；两个旧快照的数据本体已于 2026-07-20 删除，并从活动目录移入 `retired_datasets` tombstone。release manifest、provenance、项目 release catalog 和冻结配置继续保留为历史证据，新合规快照进入中央共享根。

用户已确认目标维护模型：来源 raw 只追加，标准事实写入 curated Parquet，不可变 snapshot manifest 引用分区，DuckDB/Qlib 作为查询或研究投影；日常写入日增量，月末只做小文件 compaction，不把日频变成月频，也不自动删除旧快照引用。详细合同见 `data/catalog/MAINTENANCE_RUNBOOK.md`。

用户已确认来源与研究口径：官方直接来源优先，开源适配器负责采集，多来源允许相互校准；工作区内部允许保存和跨项目共享，但底层来源许可仍是强制边界；股票池覆盖沪深北、退市证券和代码历史，历史取合法且可验证的最大范围；L1 保存未复权事实与公司行动，研究投影默认前复权。详细合同见 `data/catalog/SOURCE_POLICY.md` 和 `DATA_QUALITY_POLICY.md`。

用户于 2026-07-20 批准先完成 20 项 `G-DATA-CORE`，并于 2026-07-24 确认 8 项 `G-DATA-GLOBAL-CONTEXT`、4 项 `G-DATA-HK-EQUITY` 和定义范围内的 3 项 `G-DATA-GLOBAL-EQUITY` 都必须完整建立；全域计划因此扩展为 52 项。2026-08-01 用户进一步批准把研究入口改成分层门：CORE-01～06 支撑 `R0/PV`，CORE-20 支撑 `R1/CLASSIC_CN`，CORE-20 + GCTX-8 支撑 `R2/A_SHARE_GLOBAL`；HK-4 与 GLEQ-3 仍是完整研究数据库的必建证券层，定义范围外的全球全股票横截面仍需另行批准。当前不新购付费数据：正式事实以官方来源为优先锚点，既有校内学习研究权限下的 CSMAR 人工导出只作为结构化候选源，并须与官方身份、日历、披露、公司行动和 PIT 证据校准；候选可取得不等于来源已正式准入。证监会行业作为官方基线、申万行业作为研究主口径，并允许多口径行业版本化共存；同时保存官方流通股本事实和独立可重建的自由流通口径；指数采用多提供商、尽可能完整的合法可验证覆盖；同时保存短端国债与银行间短期利率；最大历史的大规模回补必须先提交容量报告；关键冲突、许可不清或 PIT 缺失一律阻断且不自动豁免。完整建立是范围决定，不自动授权付费供应商、密钥、具体外盘端点、全历史批量回补、质量晋级/豁免、新闻重启或高频/券商启用。

用户于 2026-07-26 决定不建设独立数据备份，并接受主盘不可恢复故障可能导致历史 raw 和精确快照丢失的风险。备份容量、备份介质和备份许可不再是当前准入或阶段门；主盘内的不可变版本链、PIT 截断、原子发布、中断恢复、哈希校验和投影重建仍属于强制合同。

### 3.2 当前基线

- 已建立 `data/catalog/datasets.json`、`qdata.cmd` 和只读解析器；中央根为 `data/market/` 与 `data/published/`。
- 已建立 `data/catalog/DATABASE_MANUAL.md` 总说明书和 `planned_datasets.json` 计划目录；计划覆盖 52 个数据家族，其中 GCTX-8、HK-4、GLEQ-3 已升级为必建层；`validate_planned_dataset_catalog.py` 锁定 20+8+4+3+4+7+2+4 分组和 28/35/52 完成定义。计划项不可解析，也不代表已经下载或通过质量门。
- 已完成 `L1-D3A` 字段级核心合同：20 个核心数据集、45 张逻辑表、588 个含继承字段定义和 27 个来源；19 个未获准或条件不足的来源绑定保持阻断。合同完成不表示数据已经下载。
- 已建立 `data/catalog/MAINTENANCE_RUNBOOK.md`；`L1-D12A-01` 已于 2026-07-27 完成隔离基础设施修复。2026-08-04/05 已建成 RQData 沪深价量和 B 路径候选更新器；2026-08-05 又以统一入口和工作日 19:20 任务将二者与中央候选索引串联，旧分立任务已禁用。统一任务真实 smoke 返回 0，候选水位已到 2026-08-05，25 项回归通过。它只自动生成、封存和索引 `INSPECTION_ONLY/NOT_ADMITTED` 候选；自动正式 catalog 集成、正式 compaction、跨源冲突裁决和持续质量晋级仍未实现。
- `L1-D2A-01` 历史口径：早期完成一轮受控 inspection/smoke；2026-07-27 又执行官方/公开、无密钥真实小样本准入 Pilot。最终运行 `d2a01-20260727-v3` 的 11 个获准 capture 全部完成，另有 3 类业务请求在 fetch 前阻断；随后在来源权利、关键字段语义和 PIT 冲突处按授权暂停。CORE-01/02/04 只有局部 `INSPECTION_ONLY` 证据，CORE-03 阻断，BSE 主档因 raw schema/权利未验证而保守阻断。后续用户批准的 BaoStock 日行情覆盖子 Pilot 只验证了匿名登录；第一条压缩全 A 截面响应的帧尾合同仍未闭合，业务结果接受数为 `0/25`，不能判断沪深北、退市、历史和稳定性覆盖。当时 RQData、Choice 和 iFinD 均只保留为历史缺口补源/校准候选；RQData 的当前地位由下一条 2026-08-03 更新覆盖。回执见 `reports/data-governance/d2a-01-source-admission-pilot-20260727/README.md` 与 `reports/data-governance/baostock-daily-coverage-pilot-20260727/README.md`。
- 2026-08-03 用户授权建立米筐独立环境，并允许按能力证据提升来源地位。`L1-RQDATA-01_CAPABILITY_AND_SOURCE_SUITABILITY_PROBE` 已完成离线 Phase A，并完成用户本机最小 live Phase B：独立环境锁定 `rqsdk 1.7.4`、`rqdatac 3.6.1`、`rqdatac_fund 1.0.44` 及共 33 个精确包；用户交互初始化成功，交易日历和一只股票、一只ETF的5日窗口日行情通过；公募基金持仓因 `fund.all_instruments` 权限拒绝，ETF每日申赎清单因 `etf.get_components` 权限拒绝；RQFactor、RQOptimizer、RQAlpha Plus 仍未安装验证。RQData 据此提升为 `PRIORITY_CORE_PV_PROBE_AND_GAP_FILL_CANDIDATE`，但用途上限仍为 `INSPECTION_ONLY`、正式准入仍为 false。后续只用一只证券、两个日期的摘要探针验证当前身份、未复权日行情和流量；动态历史股票池、公司行动/复权、历史可交易性、财务 `available_at`/修订链、保存派生权利及不可变导出仍是晋级阻断项。合同见 `data/catalog/contracts/ricequant_capability_probe_v1/contract.json`，在线回执见 `reports/data-governance/ricequant-live-capability-probe-20260803-v1/`。本批未修改 `datasets.json`、未批量下载、未运行因子/回测/交易，`G-DATA`、R0/R1/R2 和 D2B 状态均不变。
- 2026-08-03 `L1-RQDATA-02_R0_CORE01_TO_CORE06_BOUNDED_CLOSURE_PROBE` 已完成：认证、17 次业务 API 调用和硬边界均 PASS；14 项返回非空结构化结果，拆分、配股和特殊处理 3 项为 `PASS_EMPTY`，0 项报错，单次复核增量 489,119 B。已证实当前全 A 活跃主档 5,535 只（沪 2,310、深 2,893、北 332）、近期未复权日行情/日历/会话、分红、除权因子、ST/停牌接口能力；整数索引误判日期和空结果误标普通 PASS 的摘要缺陷已修复并由 7 项测试锁定。状态仅晋级为 `PRIORITY_R0_STRUCTURED_SOURCE_CANDIDATE__BOUNDED_ENDPOINT_AND_SCHEMA_CAPABILITY_PASS__KEEP_INSPECTION_ONLY`；前复权价只作 CORE-05 校准。它不证明历史主档 PIT、完整公司行动修订链、保存/派生权、正式快照、catalog、L2 或 R0，正式 CORE-01～06 与 R0/R1/R2 仍为 0/6/未开启。合同见 `data/catalog/contracts/ricequant_r0_closure_probe_v1/contract.json`，持久回执见 `reports/data-governance/ricequant-r0-live-closure-probe-20260803-v1/README.md`。
- 2026-08-03 `L1-RQDATA-03_R0_COVERAGE_PIT_VOLUME_PROBE` 已完成。最新属性主档返回 5,901 个唯一证券（活跃 5,535、退市 343、未知 23），2000-01-04 至 2026-08-03 返回 6,441 个交易日；公司行动正例为 dividend-info 48、现金分红 22、送转/拆分 5、配股 1、除权因子 23 行，000001 2018Q4 的三时点 `as_of` 行数为 0/1/3。沪深未复权价两样本共返回 14,730 行、10 字段无空值；北交所主档可读但日行情明确 `PermissionDenied`。进程内 quota 计数器未同步刷新，另启新进程复核后，本批在主档/日历之后新增 3,103,097 B；按返回主档估计 17,754,670 个历史证券日和样本传输率线性外推约 3.48 GiB，即当前 200 MiB 日配额的 17.84 倍，只作容量规划。探针仍未保存 payload；历史主档 PIT、完整行动修订链、北交所行情、ETF 成分、保存/派生权、正式快照与 L2 均未闭合，因此正式 CORE 与 qdata 仍为 0/6，R0/R1/R2 不开启。回执见 `reports/data-governance/ricequant-r0-coverage-probe-20260803-v1/README.md`。
- 2026-08-04 完成独立窄门 `R0_SHSZ_PV_V2_COMPOSITE_PUBLICATION_AND_L2_INPUT_ACCEPTANCE`。正式快照 `cn-a-share-r0-shsz-pv-panel@20050104-20260803-v1` 已原子发布到中央 snapshot 根并登记为 `RESEARCH_ONLY/OK/immutable/frozen`；精确 qdata 解析、正式 PanelData smoke 和 v2 深验收全部 PASS。目录现有 14 个可解析身份，其中该窄门 1 个达到 `quality_status=OK`；它不是 CORE-01～06 bundle，因此 CORE/canonical R0/R1/R2 计数保持不变。维护侧已实现候选型 RQData 日更和调度，首批 2026-08-04 delta 为 5,202 行、3,446,687 B，并保持正式 catalog 写入 false。研究侧已冻结 512 个确定性价量候选（48 seed + 464 grammar，目录内容哈希 `6216ca0af144998d97032de715467ba61cec46cdb877af9bbb419ed582e0cb64`）和单因子协议；尚未运行任何因子、IC 或回测，Validation/Holdout 均未消费。验收见 `reports/data-governance/r0-shsz-pv-v2-formal-acceptance-20260804-v1/`、`reports/data-governance/rqdata-shsz-daily-updater-20260804-v1/` 与 `reports/data-governance/r0-shsz-pv-factor-catalog-preregistration-20260804-v1/`。
- 2026-07-28 用户确认拥有校内学习研究权限后，完成 `L1-D2A-01B_CSMAR_LOCAL_RESEARCH_BOOTSTRAP`：两份人工下载的真实 CSMAR ZIP 通过本地候选接入器。`TRD_Co` 为 6,013 行；`TRD_Dalyr` 为 98,229 行、24 个源字段、5,476 个代码和 18 个交易日；两者均通过类型、必填和主键机械门，并生成 raw 副本、ZSTD Parquet、manifest、provenance、质量摘要和查询入口。候选固定为 `INSPECTION_ONLY/NOT_ADMITTED`，正式 catalog 哈希未变，CORE-20/GCTX/HK/GLEQ 正式快照仍为 0。回执见 `reports/data-governance/csmar-local-research-bootstrap-20260728/`。
- 2026-07-28 执行 `L1-D2A-02` 后，一个 2020-01-01 至 2024-12-31 的全字段 `TRD_Dalyr` 包已完成：下载包 340,147,343 B、全量 5,852,287 行、5,659 个证券、24 个源字段、主键重复 0；生成 60 个年月 Parquet 分区，候选总占用 693,892,239 B，机械检查通过且正式 catalog 哈希未变。专门的“年、中、季报公布日期”库在当前机构账号下无权限；可访问的财务事实表没有已验证的首次 `available_at`，因此本包按 `MISSING_PIT_OR_REVISION_EVIDENCE` 停止条件暂停。日行情通过不等于 D2A-02 完成，D2B 未自动启动。回执见 `reports/data-governance/d2a-02-csmar-priority-table-pilot-20260728/`。
- 用户于 2026-07-28 进一步明确要求按标准流程继续，已执行并按停止条件暂停 `L1-D2A-03_OFFICIAL_DISCLOSURE_PIT_AND_MINIMUM_CORE_CANDIDATE_SLICE`。官方 PIT v2 以 4 次串行请求保存 41,794 B：深交所 2 条、上交所 7 条、巨潮 5 条校准记录；北交所只完成 discovery，业务 JSONP 合同未闭合。CSMAR 资产负债表元数据可读，但字段交互超时且没有下载财务样本。16 证券隔离切片生成 509 行、15 个有行情证券和 1 个负向控制；只有 CORE-03 源观测被保留，CORE-01/02/04/06 进入 quarantine，CORE-05 明确零输出。用户随后明确要求开始建库和多因子工程，`L1-D2A-04_CORE01_TO_CORE06_CANDIDATE_CLOSURE_AND_SELECTED_SOURCE_PILOT` 已按有限范围计划执行 Phase A：新增 568,246 B 的轻量候选，哈希引用 60 个上游分区和 5,852,287 行而不复制全表；CORE-01/02/03/04/06 的观察与缺口产物已生成，CORE-05 保持 0 行。永久身份、官方日历/时段、公司行动条款与修订链、历史状态规则、三项 Limit 字段语义和独立保存权仍未闭合，因此 D2A-04 按停止条件暂停，D2B 继续阻断。回执见 `reports/data-governance/d2a-04-core01-to-core06-candidate-20260728/`。
- D2A-01 的历史 runner 合同继续保留在 `data/catalog/contracts/source_admission_v1/source_matrix.json`；跨 D2A-01～04C 与 CSMAR 人工导出候选运行的当前状态统一登记在同目录 `current_source_registry.json`。2026-07-29 的 33 包本地事务迁移、候选构建和本地闭合已经完成；该覆盖表不授权继续下载、正式 catalog 写入、质量晋级或研究读取，下一次外部采集仍需新的明确启动。
- 当前登记 13 个 store、14 个可解析数据身份和 2 个已删除 tombstone；其中独立 `R0_SHSZ_PV` 面板 1 个达到 `quality_status=OK`，其余身份不因此晋级。
- 计划目录与实际 catalog 的精确交集为：CORE `0/20`、GCTX `0/8`、HK `0/4`、GLEQ `0/3`；当前没有任何新国内核心或外盘正式快照。
- 两个旧日频 Qlib 的数据本体已删除；旧身份不再能由 `qdata` 解析，历史 manifest、provenance 和删除回执保留，旧运行不能按原路径完整复现。
- Capital Flow v22 为不可变研究候选，但 `G1=REVIEW`、许可状态混合，不能向多因子发布。
- News Signal Agent 的 SQLite 是可变工作库，只能 `ENGINEERING_ONLY`；尚无不可变新闻/公告或事件快照。
- 用户已决定当前暂停新闻原文和新闻事件特征建设；计划条目保留为 `DEFERRED_BY_USER`，不进入当前执行队列。PIT 财务所需公告时间和修订元数据仍属于核心数据合同。
- `data/market/snapshots/` 当前有 1 个通过合同的独立 `R0_SHSZ_PV` 正式面板；`data/published/` 仍没有跨项目派生发布数据集。
- CWOPT 的 history/router 数据是外部协作项目的可变实验缓存，只能 `ENGINEERING_ONLY`，不属于主线数据源。

## 4. L1 工作包

完整目标以 `data/catalog/DATABASE_MANUAL.md` 和 `planned_datasets.json` 为准。工作包采用分层阶段门：国内核心、全球环境、港股和全球关联个股都必须建设，但各自保留独立许可、容量和质量验收；新闻、高频或昂贵供应商不应被误写成已经完成，也不阻塞已满足上游合同的阶段性工程验证。

### L1-D0：统一目录、数据库说明书与访问控制

状态：`DONE`

已有产物：实际数据登记表、计划数据登记表、数据库总说明书、数据维护手册、schema、访问策略、`qdata.cmd list|describe|resolve`、目录校验器、中央共享根和存储规则。

完成含义仅是“数据资产可以被发现、区分实际与计划并受访问控制”，不表示实际数据质量已通过。

### L1-D1：旧日频数据退役与新事实基线准备

状态：`PARTIAL`；退役与物理清理 `DONE`，新事实字段合同 `DONE`，隔离发布底座 `DONE`，正式发布集成与新事实数据尚待 D12A 后续/D2 建设

范围：现有 `cn-a-share-qlib-daily@2026-07-14` 和 `@2026-07-18` 已完成研究阻断、逐文件清单、依赖审计和数据本体删除。新日频事实基线的字段级来源、许可、PIT 和 schema 合同已由 `L1-D3A` 完成；隔离发布基础已由 `L1-D12A-01` 验证，正式数据发布集成和新数据仍未建设。

已有产物：旧数据逐路径大小和引用清单；分批删除目标；删除前清单与哈希；`DELETION_RECEIPT.json`；活动目录移除与 tombstone；`SOURCE_POLICY.md`、`DATA_QUALITY_POLICY.md`；`g_data_core_v1` 字段/来源/许可/PIT 合同；隔离原子发布底座。待完成部分是正式 catalog 集成、真实来源发布链和实际新事实数据。

验收：旧 Qlib 无法以 `research` 解析；每个实际删除目标都有精确路径、清单、依赖、批准和回执；历史证据若因此不可复现必须明确登记；新数据不引用旧 Qlib，只有按新合同生成的数据才能进入 `OK` 候选。

### L1-D2：证券主数据、日历、可交易性与公司行动

状态：`ACTIVE_AUTHORIZED_BOUNDED_D2A04B_SOURCE_CLOSURE__STAGE_A_PRE_ADMISSION_0_OF_4__LEGACY_COMBINED_AUDIT_POST_ADMISSION__FORMAL_CORE_0_OF_20__D2B_BLOCKED__INSPECTION_ONLY`

范围：永久证券身份、代码历史、交易所/板块、上市退市、ST/风险警示、停牌、涨跌停、交易日历、股本变化、分红送转配股及复权链。

验收：研究日期只能看到当时存在且当时可交易的证券状态；退市、代码变化、停牌和公司行动不被当前状态倒灌。

此前执行子包：用户于 2026-07-28 明确启动的
`L1-D2A-02_CSMAR_PRIORITY_TABLE_CENSUS_AND_MULTIPACK_PILOT` 已完成五年
`TRD_Dalyr` 包、拆包、Parquet、主键、时间覆盖和容量测量。实际交易日期
2020-01-02 至 2024-12-31，共 5,852,287 行、5,659 个证券、60 个年月分区；
下载包和候选分别为 340,147,343 B 与 693,892,239 B，机械检查通过。涨跌停
三字段约 2.5 万行 NULL 的制度含义仍待解释，原值保留。

PIT 财务子项未完成：专门的财报公布日期库在当前权限下不可读；可访问财务事实表
虽有更正字段，却没有已核实的首次 `published_at/available_at`。本包已经按预先
声明的停止条件暂停，不能以报告期、更正日或下载时间替代首次可见时间。正式
catalog、质量状态和 CORE-20 完成计数均未改变；D2B 仍被阻断并需要新的明确启动。

最近执行的 `L1-D2A-03` 已按标准停止条件暂停。深交所 `publishTime` 保存秒级原值；上交所
只使用 `SSEDATE` 并拒绝把 `ADDDATE` 当公开时间；北交所仅保存 discovery，
未执行未闭合的业务请求。日期级或精度未知记录没有钉住 CORE-02 日历，因此
`available_at` 保持空值。官方 PIT v1 的巨潮逐行证券标签被 v2 纠正，v1 原始响应
继续保留但其归一化候选已被取代。CSMAR 微型财务样本因门户字段交互超时未生成。
CORE-01～06 的 16 证券工程切片只有 CORE-03 源观测可保留；其余为 quarantine 或
`BLOCKED_NO_OUTPUT`。D2A-04 Phase A 已在本地完成：引用五年日行情分区而不复制
全表，并生成 CORE-01/02/03/04/06 的观察、字段和缺口产物；完成门因永久身份、
官方日历、公司行动、历史规则与 Limit 字段语义缺口未通过，现已暂停。全部候选
继续 `INSPECTION_ONLY/NOT_ADMITTED`；D2B 与正式因子研究尚未启动。多因子
L2-P0 只完成 qdata 输入解析门与拒绝越权测试。

用户于 2026-07-28 明确启动、并在历史门户预检暂停后重新启动
`L1-D2A-04B_OFFICIAL_IDENTITY_CALENDAR_ACTION_STATUS_SOURCE_CLOSURE`。用户随后于
2026-07-29 明确要求扩大结构化低频候选收集并人工完成校内导出，又明确批准按
33 个 CSMAR 包执行、验证成功后删除 C/F 原件。该精确批次完成时的 04B 阶段
暂停状态为
`PAUSED_AFTER_33PKG_LOCAL_BATCH__33PKG_INTAKE_SEALED__16_FINANCIAL_AUDIT_CANDIDATES_BUILT__2_AUDIT_TEXT_TABLES_RAW_ONLY__CSMAR_PORTAL_CODE27_UNTIL_20260730_0000__OFFICIAL_PARTIAL__BSE_WAF_HARD_STOP__INSPECTION_ONLY`。
后续 04C 已闭合两张审计文本；该历史状态不得再作为当前候选计数。

`TRD_Dalyr` 八个真实请求包已经形成 1990-12-19—2026-07-28 的只读去重投影：
18,691,664 个唯一 `(Stkcd, Trddt)` 键、5,990 个证券、8,741 个交易日；2022—2024
重叠 3,785,013 键的 24 字段不一致为 0。八个分段候选物理合计 464 个分区、
2,629,656,346 B，但未创建物理合并快照。`TRD_Co` 完整当前截面为 5,991 行，
`TRD_Cale` 为 61,540 行。`TRD_Cptl`、`TRD_Capchg`、`TRD_NoLimit` 和
`TRD_AdjustFactor` 又分别形成 71,514、141,157、13,226 和 61,663 行的全历史
结构化候选；四张新增表的源 ZIP 合计 5,853,414 B，候选合计 16,033,830 B。
全部输出保持 `INSPECTION_ONLY/NOT_ADMITTED`，正式 catalog 哈希未变，
`qdata --intent research` 正确拒绝。

机械完整不等于完成门通过：北交所 `Markettype=64` 在 2020-07-27—2021-11-12
有 15,162 行行情但没有同市场 `TRD_Cale` 记录；行动/股本/无限制表存在未来生效
日期，`TRD_Cptl` 的公告/支付日期含缺失或哨兵，`TRD_Capchg` 有一行两个负股本值，
均已原样保留，等待公告首次可用时间、代码表、修订链和跨源裁决。永久身份、官方
市场级日历/会话、退市/历史状态、来源权利和财务 PIT 仍未闭合。普通公司资产负债表、
利润表和直接法现金流量表已经分别形成 691,546、693,242 和 668,828 行的隔离
候选，合计 2,053,616 行、684,875,634 B。新增的 12 张银行/证券/保险报表和
`FIN_AuditStats` 又形成 13 个机械 PASS 候选，共 80,109 行、1,303 分区、
33,113,057 B。两批基础业务键重复和数值转换失败均为 0。

33 个 CSMAR ZIP 已事务迁移到
`data/market/quarantine/csmar-manual-export/intake-20260729-33pkg-v1/`；33 个
SHA-256 与 122 个 ZIP 成员复核通过后，只删除 18 个 C 盘和 15 个 F 盘原件，
总计 2,038,345,395 B。

用户随后批准第一阶段本地闭合包
`L1-D2A-04C_33PKG_LOCAL_SCHEMA_PIT_AND_TEXT_CLOSURE`。该包未访问网络、未修改
封存原包、未写正式 catalog，也未运行因子；它完成 33/33 包、25/25 逻辑表、
1,197 个表—字段出现、559 个区分大小写字段名（大小写归并后 474 个）的结构、
关系和 PIT 缺口审计，并把批次闭合为 32 个候选和 1 个仅
用于重叠一致性校准的 `TRD_Dalyr` 原包。`FIN_Audit` 与 `FIN_AuditDetail` 通过
专用零丢失解析器分别保全 80,301 与 82,264 行；238 与 103 条需引号修复的记录
均有逐行 sidecar。v2 候选把源 ZIP/member、parser、repair manifest、sidecar、
修复 CSV 和 Parquet content hash 绑定为一条可重算链，CSV 与 Parquet 双向
`EXCEPT ALL` 均为 0，业务键重复为 0。旧 v1 候选不删除，但其权威性已由
provenance 完整的 v2 取代。`FIN_Audit` 仍有 1 条语义复核记录；
`FIN_AuditStats` 与 Detail 双向孤儿和 `AuditNo` 计数错配均为 0，但
`302132/2024-12-31` 有 1 个 Stats/Detail 期间没有对应 `FIN_Audit`，保留为
覆盖缺口，不填补、不删除。

因此本地财务/审计候选现为 18 张、2,296,290 行，按当前权威 v2 口径合计
803,059,261 B；它们只是 `INSPECTION_ONLY/NOT_ADMITTED` 候选，不是 18 张相加
即可研究的同口径事实。普通报表与银行/保险/证券专表的发行人—期间—范围键完全
重叠，必须按分行业 taxonomy 并存，禁止静默 union 或重复计数。

`Accper/EndDate` 只是报告期，`DeclareDate` 只记录更正披露日；15 张财务表中
`DeclareDate` 非空只出现在 `IfCorrect=1`，因此它只能证明更正披露，不能恢复
首次可见时间。包内仍没有初次披露、旧值或完整修订链，不能用于 PIT 财务因子。
普通公司间接法现金流量表、报表公布日期及正式审计报告 ID/版本链仍未闭合。本批
不运行因子，不形成有效性或投资结论。第一阶段闭合证据见
`reports/data-governance/csmar-33pkg-local-closure-20260729/`。

此前门户预检曾观察到“无权限”，该事实继续作为历史回执保留，但不再是当前阻断。
SSE 与 SZSE 官方源已分别封存部分检查包；BSE 在检查官方法律声明时遇到 HTTP 403
WAF，按硬停止门失败关闭。三所产物均为 `INSPECTION_ONLY/NOT_ADMITTED`，未写
正式 catalog，不能闭合 D2A-04 完成门。本轮 CSMAR 汇总见
`reports/data-governance/d2a-04b-csmar-structured-candidates-20260729/` 与
`reports/data-governance/csmar-33pkg-transactional-intake-20260729/`；33 包
本地结构、PIT 与审计文本闭合的当前权威证据见
`reports/data-governance/csmar-33pkg-local-closure-20260729/`。
清华图书馆其他数据库仍可访问；当前只完成资源详情 census，确认 RESSET 企业大数据、
中国微观经济企业库、CVSource、Capital IQ 和 Datago 高管/调研可能带来企业、
私募交易、信用、知识产权/司法/招投标及治理网络等规划外候选，同时纠正了
World Trade 与巨灵主要属于贸易政策/财经全文而非数值贸易流或结构化核心事实的
误判。没有修改 52 项计划或下载新库，初筛见
`reports/data-governance/library-economic-financial-source-census-20260729/`。
历史门户回执位于
`reports/data-governance/d2a-04b-source-closure-20260728/d2a04b-portal-preflight-20260728-v1/`；
本轮官方源检查包位于
`tmp/d2a04b-source-closure/d2a04b-official-track-a-20260728-v1/`。

此前 D2A-03 包采用 G 盘双闸门：批次最大 footprint 为 20 GiB，且任一时刻必须保留不少于
全盘 25% 的空闲空间。实测官方 PIT v2 为 58,971 B、CORE 切片为 85,514 B，
结束时 G 盘空闲 383.482 GiB（84.49%），闸门通过。禁止付费、整库或全历史回补、
新闻、高频、自动正式 catalog 登记、质量晋级/豁免和自动清理。机器授权、测量与
暂停回执见 `reports/data-governance/d2a-03-official-pit-min-core-slice-20260728/`。

### L1-D3：统一时间、修订、来源与许可合同

状态：`DONE`（`L1-D3A` 合同完成；采集阻断如实保留）

范围：统一 `event_time`、`published_at`、`available_at`、`ingested_at`、`effective_date`、`revision_id`、时区、迟到、更正、来源层级、许可和抓取证据。

完成子包 `L1-D3A`：已冻结 20 项 `G-DATA-CORE` 的“数据族—字段—主来源—校准来源—许可—时间/PIT—schema”机器合同和人类说明，共 27 个来源、45 张逻辑表、588 个含继承字段定义；19 个来源绑定因许可、历史可得性、凭据或上游依赖显式阻断。该结果只代表可实施合同完成，没有下载或登记新业务数据。验收回执见 `reports/data-governance/g-data-core-d3a-20260720/README.md`。

验收：任何研究输入都能回答“在决策时刻是否已合法可见”；无法回答的数据不能进入正式研究。

### L1-D4：PIT 基本面、估值与公司质量

状态：`PLANNED`

范围：三大财务报表、财务指标、单季度和 TTM 派生、业绩预告/快报、审计意见、分红、主营构成、总股本/流通股本、日频市值估值和换手；必须保留原始公告版本和重述链。

验收：报告期不能替代公告可用时间；派生指标能重建到原始报表、版本和公式；跨行业会计口径差异有明确字段和排除规则。

### L1-D5：指数、行业、基准与风险输入

状态：`PLANNED`

范围：指数主数据和日行情、PIT 成分/权重、行业分类历史、基准、无风险利率，以及行业、规模、Beta、波动、流动性、动量等基础风险暴露输入。行业分类采用证监会官方基线 + 申万研究主口径，多口径独立版本化并建立 PIT 映射；不得把某一口径覆盖为唯一事实。官方流通股本与派生自由流通口径分开保存。短端国债和银行间短期利率均进入 L1，具体研究口径由运行配置钉住。

指数覆盖采用“元数据尽量全、事实按可得性分层”的合同：优先登记上交所/中证、深交所/国证、北交所及其他官方或合法公开提供商的可发现指数，覆盖综合/宽基、规模与板块、行业和细分行业、主题与产业链、风格、策略、因子、红利、REITs，以及公开发布的价格、全收益和净全收益版本。强制覆盖示例包括半导体/芯片、芯片设计、半导体设备、半导体材料、半导体材料设备组合、中证REITs全收益和其他官方行业主题系列；这不是封闭白名单，新发布指数按同一合同持续追加。

验收：禁止当前成分和当前行业倒灌；指数身份至少包含 `provider_id + index_code + return_variant + currency + methodology_version`；所有发现的官方指数元数据都有覆盖状态，许可允许且官方可得的日行情、成分和权重按最大可验证历史落盘；历史不可得必须明确标记 `MISSING`，不得伪装完整。风险暴露是可重建的发布数据，不是无来源黑箱。

### L1-D6：消费者绑定与新项目接入模板

状态：`PARTIAL`

范围：所有新配置和项目通过 `qdata` 绑定精确实际数据身份，并在运行 manifest 记录解析回执；`planned_datasets.json` 不可解析，冻结旧配置只作历史证据。

验收：从任意项目目录都能精确解析同一快照；研究及以上用途没有 `latest`、路径猜测或未登记输入。

### L1-D7：基金、持仓、所有权与分析师扩展

状态：`PLANNED`

范围：ETF/公募基金主数据、NAV、份额、持仓和流量；股东、机构、限售解禁、董监高；分析师预测、评级和盈利修订。供应商、费用和披露可见时间单独审查。

验收：通过 `G-DATA-FUND`；未通过时不得进入相应 L2 因子，但不阻塞 `G-DATA-CORE`。

### L1-D8：新闻、公告与结构化事件基础快照

状态：`DEFERRED_BY_USER`

范围：本包当前暂停，不进入核心执行顺序。未来由用户重新启用后，从官方来源、News Signal Agent 可变工作库或批准来源形成 raw 只追加层、curated 文档层和冻结快照；本包不提前承诺情绪或事件因子有效。财务和公司行动所需的公告身份、公告时间及修订元数据由 D2/D4 保留，不等于启用新闻正文库。

验收：正文哈希、URL/公告身份、版本、发布时间、可用时间、去重、许可和证券映射可追踪；可变 SQLite 不被正式研究直接读取。

### L1-D9：资金流可观察账本与发布边界

状态：`PARTIAL`

范围：复用 Capital Flow 的 raw、normalized、Parquet、manifest 与缺失/冲突规则；融资融券、互联互通、ETF 等可观察事实和派生归因严格分开，不把供应商标签写成投资者身份。

验收：通过 `G-DATA-EVENT` 的限定字段才能进入 L2；v22 在 `G1 REVIEW` 期间仍为项目内 `RESEARCH_ONLY`，发布特征仍需 G3。

### L1-D10：全球环境、港股、全球关联个股与衍生品

状态：`PLANNED_REQUIRED`；范围已批准，合同、来源准入和实际数据均未完成

本包拆成四个不可互相冒充完成的子包：

1. `L1-D10A / G-DATA-GLOBAL-CONTEXT`：8 项必建全球环境数据，覆盖交易日历与时区、全球基准主数据和日行情、宏观发布与修订、利率/收益率/信用、汇率、商品期货、波动率/流动性/风险状态；与 CORE-20 一起构成 `R2/A_SHARE_GLOBAL` 研究基础；
2. `L1-D10B / G-DATA-HK-EQUITY`：4 项必建港股数据，覆盖永久证券身份与退市历史、未复权日行情、公司行动/可交易性、HKFRS/IFRS PIT 财务和披露；
3. `L1-D10C / G-DATA-GLOBAL-EQUITY`：3 项必建全球关联个股数据，覆盖版本化的中国/产业链关联研究宇宙、当地币未复权日行情、US GAAP/IFRS PIT 财务与披露；这里的“完整”指获准且版本化的定义宇宙，不等于全球所有股票；
4. `L1-D10D / G-DATA-XASSET`：2 项国内股指期货与期权数据，覆盖合约主数据、行情、基差、持仓、连续合约规则和隐含波动率。

验收：四个子门分别具备合同、来源权利、PIT/时区/币种、最大合法可验证历史、真实快照、质量与恢复证据。任何海外前收盘、同日盘中或夜盘输入都必须映射到实际可用于 A 股决策的 `available_at`；不得按自然日期直接连接。具体端点、付费许可、凭据和批量历史回补仍逐次批准。

### L1-D11：分钟、逐笔、L2 与执行数据

状态：`DEFERRED_PENDING_CONTRACT`

完整规划保留 1m/5m、tick、逐笔委托、L2、集合竞价和 Shadow/经纪商执行回执，不按“最小量”从计划中删除。实际建设前必须确定用途、市场/标的、来源许可、费用、年度体积、分区、保留期、质量抽样、安全边界和日频对账。

验收顺序：书面合同 → 容量估算 → 小样本试点 → 质量审计 → 人工批准 → 批量摄取。账户和凭据不得写入工作区文件。

### L1-D12：原子发布、恢复与持续审计

状态：`PARTIAL_FOR_FORMAL_DATA`（`L1-D12A-01` 隔离基础设施 `DONE`；真实来源、正式 catalog 集成、真实数据发布、调度器、正式 compaction 和持续审计尚未通过；独立备份不在当前范围）

范围分两段：核心数据采集前先实现 raw/curated/quarantine、候选 manifest、内容哈希和原子可见性的最小发布闭环；核心数据完成后再补主盘内逻辑恢复、保留策略、定期质量检查、跨快照差异和失效处理。日增量、月度 compaction、迟到修订和 schema 演化遵循 `data/catalog/MAINTENANCE_RUNBOOK.md`。独立物理备份、对象存储备份和异盘灾难恢复不属于当前范围。

`L1-D12A-01` 已完成的部分：标准库零第三方依赖的受控发布器在隔离合成工作区完整执行已支持且受白名单约束的请求 schema；严格 RFC 3339 日期时间；以 Windows kernel wait state 与 creation token 区分死亡进程和 PID 复用；锁、交易、准备计划、数据集、快照、请求哈希和重推导路径强绑定；恢复切换由 OS 自动释放的 transition guard 串行化；真正 `os._exit` 子进程、同 run 错绑、越界计划、双恢复竞争、owner 缺失及提交后清理失败均有回归。中央数据工程依赖已形成 CPython 3.11 / Windows AMD64 的 44 包精确版本锁，当前兼容见证环境验证通过；适配器门已覆盖 11 字段宽度、一对一 row lineage、明确单位、字段类型/日期、主键、关键 NULL、有限数、正/非负约束和 OHLC。以上全部只属于 `ISOLATED_INFRASTRUCTURE_ONLY/SMOKE_ONLY`，未登记正式业务数据。

`L1-D12A` 尚未完成的部分：真实端点 raw 字段到标准字段的正式语义映射、来源/许可准入、正式 catalog 集成、真实快照、统一更新器、调度器、正式 compaction 和持续审计。中央生产 venv 已安装并通过精确版本锁，但版本锁仍不是 wheel 哈希锁；row hash lineage 不证明转换公式正确。任何正式或无人值守发布仍被阻断。

容量审计子包 `L1-D2A_SOURCE_ADMISSION_AND_MEASURED_CORE_PILOT` 的证据与算术复核已完成：实际 catalog 中 CORE-20 为 `0/20`，19 个来源绑定仍阻断；旧报告的 6 TiB 高位按其自身分层和 35% 余量重算应至少为 6.371 TiB，且财务文档高位单项已超过旧 Raw 总高位，因此 1.5–6 TiB 不再作为采购或回补依据。退役 Qlib 的 0.667 GiB 实际投影已作为压缩投影锚点。报告见 `reports/data-governance/data-capacity-validation-20260724/README.md`。

`L1-D2A-01` 对正式来源仍为 `PAUSED__BLOCKING_CONFLICTS_REPORTED__NO_FORMAL_ADMISSION`；其 CSMAR 本地接入子包已经 `DONE__LOCAL_CANDIDATE_PIPELINE_ONLY`。CSMAR 当前真实样本解决了“能否取得并机械解析 A 股日频结构化数据”的问题，但没有关闭机构许可文本、字段/单位正式语义、PIT/修订链、全历史覆盖和多源冲突门。门户显示 `TRD_Dalyr` 18,686,062 行、1990-12-19 至 2026-07-27、单次日期最多 5 年；完整日表工程外推约 0.9–1.5 GB ZIP / 3.5–5.5 GB CSV，但并发、排队和日配额未测。D2A-01 的 P2 证据为早期元数据/HEAD 锚和 `v1/v2/v3` 三次相同的 10 条 CNINFO 元数据请求（1 个唯一响应哈希）；后续 D2A-03 又取得深交所 2 条、上交所 7 条和巨潮 5 条校准记录，共 14 条 PIT 候选，但仍未下载 PDF，也未闭合北交所、修订链和公司行动语义。P3 尚未达到多提供商目录 census 标准。只有来源权利、字段/PIT 合同和新的用户启动先通过，才允许正式扩样或回补；不得用合成数据、未知填 0 或未经许可端点代替真实测量。

上句的“新的用户启动”只保留为 D2A-01 历史门条件；2026-07-30 用户已经启动有界 D2A-04B 来源闭合，但没有授权正式扩样、整库回补、付费、新密钥或 D2B 快照发布。

`L1-D2A-02` 已把一个五年全字段包实测为 0.317 GiB ZIP、1.261 GiB 解压 CSV
和 0.646 GiB 密封候选（raw + Parquet + 证据），计划转换峰值 2.838 GiB；
G 盘复测空闲 383.634 GiB，容量闸门未触发。一次串行请求没有观察到 HTTP 429，
但不能据此推断并发或日配额。财务 PIT 权限缺口已经触发暂停，故本次仍没有正式
来源准入、正式快照或因子研究输入。

2026-07-26 容量与管道小样本报告给出十进制 TB 的条件场景：35 项必建库持久层为 0.73 / 1.98 / 5.62 TB，加入工作峰值并保留 25% 空闲后的单主盘需求为 1.17 / 3.31 / 9.49 TB；新闻与高频均关闭的 46 项为 1.63 / 5.28 / 17.09 TB；新闻启用但高频关闭的 48 项为 2.09 / 11.55 / 43.09 TB。它们是低/基准/压力场景点，不是 P50/P95 或上下界。完整 52 项因 tick/L2 的记录语义、日体积和保留年限未冻结，容量继续为 `UNDETERMINED`。报告见 `reports/data-governance/data-capacity-pipeline-pilot-20260726/README.md`。

验收：每个准备通过的子门都完成中断发布与恢复演练；旧快照仍可解析，新快照只在 manifest 完整后可见，删除继续遵守人工批准闸门。

## 5. L1 执行顺序与出口

### 5.1 执行波次

1. **旧数据退役**：`D1` 已完成旧 Qlib 阻断、逐文件审计、分批清理、tombstone 和删除回执；新基线合同已由 `D3A` 完成。
2. **维护底座**：`D3 DONE → D12A-01 DONE__ISOLATED_INFRASTRUCTURE_ONLY → D12A PARTIAL_FOR_FORMAL_DATA → D6 PARTIAL`；隔离原子基础已通过，但真实来源、正式数据和无人值守运维仍须后续独立验证。
3. **核心事实数据**：`D2A CAPACITY AUDIT DONE → D2A-01 PUBLIC PILOT PAUSED → D12A-01 REPAIR DONE → D2A-01B LOCAL CANDIDATE PIPELINE DONE → D2A-02 DAILY MULTIPACK PASS / FINANCIAL PIT PAUSED → D2A-03 SZSE/SSE PIT PASS + CORE03 SOURCE SLICE / BSE+FINANCIAL GAPS PAUSED → D2A-04 PHASE A LOCAL ENGINEERING DONE / COMPLETION GATE NOT MET / PAUSED → D2A-04B 33PKG LOCAL BATCH COMPLETE / INTAKE SEALED / PAUSED / SSE+SZSE PARTIAL / BSE WAF HARD STOP → D2A-04C 33PKG LOCAL CLOSURE COMPLETE / 32 CANDIDATES + 1 OVERLAP CALIBRATION PACKAGE / 18 FINANCIAL-AUDIT CANDIDATES / CORE TOUCHED 14 OF 20 / FORMAL CORE 0 OF 20 → RQDATA-01 OFFLINE RUNTIME PASS / LIVE CAPABILITY PENDING → D2B BLOCKED → D2/D4/D5`。当前正式来源准入仍为 0；旧 CSMAR“无权限”预检仅是历史事实。当前已经构建 1990—2026 日行情分段全历史、公司当前截面、日历、分配、股本变动、无涨跌停限制、日复权因子以及 18 张财务/审计候选，但它们仍缺永久身份、逐市场官方日历/会话、公司行动公告首次可用时间与修订链、历史状态规则、来源权利和财务 PIT。CORE-04 合格前禁止生成 CORE-05。RQData 已成为证据驱动的优先能力与缺口补源候选，Choice 和 iFinD 仍是历史备选；米筐 live entitlement/覆盖/PIT/权利探针、继续外部采集、正式 catalog 登记、质量晋级和因子研究仍未获批准。
   2026-07-31 覆盖更新：用户已批准有界 D2A-04B 来源闭合。旧 combined 审计的 0/4 和 BLOCK 门 `PASS=1 / FAIL=12 / UNKNOWN=3` 固定为 `POST_ADMISSION_AUDIT` 历史结果；当前非循环 Stage A 仍为 0/4 ready（CORE-01/02/03/06 分别 11/9/12/11 个阻断），Stage B blocked，正式状态仍为 0。CSMAR Python API 个人认证通过但数据权限被拒；本地每日增量候选引擎新旧 14/14 测试通过，并补上 manifest 外额外 payload、shell 元字符与链接路径失败关闭，但仅限 `INSPECTION_ONLY`，无真实来源下载器、生产发布器或调度器。CORE-02 沪深 2015—2026 候选已形成，最终失败关闭门状态为 Q1 FAIL、Q2 FAIL、Q3 UNKNOWN、Q4 PASS。官方来源与校内/供应商候选已覆盖映射 35/35，四源无密钥检查适配器 fixture 4/4、单次 live 3/4 结构通过，ECB 本机 TLS 失败关闭并隔离。所有 raw、哈希和检查回执只进入候选/报告根，正式可准入来源仍为 0。D2A-04B 当前可继续冻结范围内的官方/许可/PIT/身份/日历/行动/状态闭合和无密钥最小检查；付费、新密钥、批量历史回补、D2B 快照发布、质量晋级和因子研究仍未授权。
   2026-08-02 候选证据更新：RESSET 人工导出的 `COMNMHIS`、`DELINFO`、`STKSPCTRMT`、`SUSPNSNRESMPTN` 四包已封存在 `data/market/quarantine/resset-manual-export/intake-20260802-04pkg-v1/`，并分别形成 9,778、8,936、4,105、81,535 行隔离候选。新增证据只触及 CORE-01、CORE-03、CORE-06；CORE-02、CORE-04、CORE-05 没有闭合。绑定报告为 `reports/data-governance/resset-r0-core01-core06-arrival-intake-20260802-v2/`，供应商在线字典与使用权只读证据为 `reports/data-governance/resset-vendor-dictionary-evidence-20260802-v1/`；二者结论均为 `formal_effect=NONE`、`INSPECTION_ONLY/NOT_ADMITTED`。Stage A 仍为 0/4，CORE-01～06 正式准入 0/6，完整 CORE 0/20，R0/R1/R2 均为 `NOT_MET`；本次不改变 D2A-04B/D2B 顺序，也不授权正式 catalog、质量晋级、快照或研究读取。
   2026-08-04 候选证据更新：RESSET Downloads 已归并为 11 个数据包和 1 个说明文档包。`DIST` 已形成 57,287 行候选；新增 6 个公司行动包合计 557,690 行，完成不可变封存与 schema 审计但候选解析待建，29 行宽异常中 28 行可确定性修复、1 行 `APLA` 必须隔离或安全格式重下。C 盘 12 个 RESSET 原件已在哈希核对后移入未清空的 Windows 回收站。当前物化候选为 5 张，新增 6 张只触及 CORE-04/05/07/17 的候选证据而不闭合这些数据族；Stage A、CORE 正式计数、R0/R1/R2、D2B 和正式 catalog 均不变。回执见 `reports/data-governance/resset-downloads-consolidation-cleanup-20260804-v1/`，候选来源状态见 `data/catalog/contracts/source_admission_v1/current_source_registry.json`。
4. **全球环境必建波次**：在国内来源准入与核心合同推进的同时排队完成 `D10A` 字段/来源/许可/PIT/时区合同；CORE-20 通过后可按 `R1/CLASSIC_CN` 的限定范围进入国内经典因子候选研究，GCTX-8 再与 CORE-20 共同通过后开放 `R2/A_SHARE_GLOBAL`。
5. **港股与全球关联个股必建波次**：继续完成 `D10B` 和 `D10C`，先冻结港股全市场与定义范围 GLEQ 研究宇宙，再做容量 census/pilot、实际回补和质量验收；两层不能因费用或许可困难从目标删除，但未准入来源保持阻断。
6. **其他全域扩展波次**：继续完成 `D7`、`D9`、`D10D`，分别建设基金/ETF/持仓/所有权/分析师、公告与资金流可观察账本、国内衍生品；不能绕过自己的 PIT、许可和质量门，当前无付费数据授权。
7. **暂停但保留波次**：`D8` 新闻和 `D11` 分钟/tick/L2/券商执行继续保留在 52 项全域计划中；当前不执行，未来只有在来源、许可、费用、容量和明确授权通过后恢复。
8. **全域收口**：依次审查全部八个数据子门；未激活分支保持明确 `DEFERRED/BLOCKED`，不得被写成完成，也不得从 Roadmap 删除。

### 5.2 `G-DATA-CORE` 出口

1. 日频未复权事实、公司行动和研究复权投影的关系可重建，至少一个主线快照在限定字段/区间内达到 `OK`；
2. 证券主数据、完整历史股票池、交易日历、可交易性、退市和 PIT 公司行动已冻结；
3. 指数/行业历史、PIT 基本面、股本、估值、基准和风险输入达到各自限定 `OK`；
4. 所有输入具备身份、schema、hash、许可、质量、PIT 和消费者登记；
5. `qdata doctor` 与目录校验无结构错误，新消费者用精确 `snapshot_id` 通过读取测试；
6. 可变库、缓存、失败运行、计划项和 `DO_NOT_USE` 无法被正式研究误读；
7. 核心数据完成发布、主盘内中断恢复与投影重建演练并由用户批准；独立备份不属于出口条件。

`G-DATA-CORE` 通过后确认国内经典日频底座达到自身出口，并可在通过对应 L2 安全门后开放 `R1/CLASSIC_CN` 候选研究；`G-DATA-GLOBAL-CONTEXT` 再与 CORE-20 汇总为 `R2/A_SHARE_GLOBAL`。任一研究门通过都不宣称港股、全球关联个股、新闻、资金流、基金、分析师、衍生品或高频已经完成。

### 5.3 子门、聚合门与总状态

- `G-DATA-GLOBAL-CONTEXT`：8 项全球市场环境；与 CORE 共同组成 `R2/A_SHARE_GLOBAL` 研究基础；
- `G-DATA-HK-EQUITY`：4 项港股全市场事实；完整研究数据库必建；
- `G-DATA-GLOBAL-EQUITY`：3 项定义范围的全球关联个股事实；完整研究数据库必建；
- `G-DATA-FUND`：基金、持仓、所有权、分析师；
- `G-DATA-EVENT`：公告、新闻、结构化事件和资金流；
- `G-DATA-XASSET`：国内股指期货和期权；
- `G-DATA-HF`：分钟、tick、L2 和执行。

数据库聚合定义保持不变：`G-DATA-RESEARCH-BASE = CORE-20 + GCTX-8`，现在对应最高研究能力门 `R2/A_SHARE_GLOBAL`；`G-DATA-REQUIRED-35 = CORE-20 + GCTX-8 + HK-4 + GLEQ-3`。总 `G-DATA-FULL-52` 是全部八个子门的状态汇总。计划完整、合同完整和数据建成是三种不同状态：计划条目、空目录、接口代码、合成数据、smoke 或明确阻断都不能计作实际数据完成。

#### 5.3.1 分层研究能力门

研究能力门与 35/52 项数据库完成定义正交；它只规定在已正式准入的数据范围内允许开展哪一类 L2 候选研究，不降低单个数据集的来源权利、PIT、质量、不可变快照、精确 `qdata --intent research` 解析或恢复要求。

| 门 | 精确输入与附加安全门 | 允许的 L2 候选研究范围 | 当前状态 |
|---|---|---|---|
| `L0_INSPECTION` | `INSPECTION_ONLY/NOT_ADMITTED` 候选；只能经隔离入口读取 | 数据形状、schema、解析、公式和管道 smoke；不得评价因子有效性 | `AVAILABLE_ONLY`；不是研究门 |
| `R0/PV` | CORE-01～06 全部形成合格不可变快照；通过 `L2-PV-SAFETY`：永久身份、逐市场日历、未复权价量、PIT 公司行动、可重建复权、历史可交易性、`available_at`、防未来信息和可重建性 | 动量、反转、波动率、换手率、流动性等纯价量候选；不得形成基本面、行业或全球环境结论 | `NOT_MET` |
| `R1/CLASSIC_CN` | CORE-20 全部通过 `G-DATA-CORE`；通过 `L2-CLASSIC-CN-SAFETY`：国内 PIT 财务、股本估值、行业/指数历史、基准、研究宇宙和防泄漏检查 | 国内经典多因子候选，包括价值、质量、成长、规模、动量、波动率、流动性和行业中性；不得使用未准入的全球环境输入 | `NOT_MET` |
| `R2/A_SHARE_GLOBAL` | CORE-20 + GCTX-8 共同形成 `G-DATA-RESEARCH-BASE`；通过 `L2-A-SHARE-GLOBAL-SAFETY`：跨市场时区、币种、发布时点、修订链和决策时刻可见性 | 在 R1 基础上加入全球市场、利率、汇率、商品和宏观环境条件 | `NOT_MET` |

R0、R1、R2 只开放对应范围的 **L2 候选研究**。因子在滚动样本外、purge/gap、成本、容量、稳定性、多重检验、压力测试、真正 Holdout 和风险审查全部通过 `L3 / G-VAL` 前，不得称为可靠结论、正式投资结论或可发布特征。高层门通过不会追溯改写低层快照、运行或结论。`R2/A_SHARE_GLOBAL` 不是 `G-DATA-FULL-52`，也不是 `REQUIRED_DATABASE_COMPLETE_35`。

### 5.4 全域数据库完成定义

全域数据库目标固定为 `planned_datasets.json` 中的 52 项，不以任何研究能力门通过为终点，也不因新闻、高频或付费来源暂缓而删减。当前执行优先级细化为：CORE-01～06（解锁 R0）→ CORE-07～20（解锁 R1）→ GCTX-8（解锁 R2）→ HK-4 → GLEQ-3 → 4 项 `G-DATA-FUND` → 7 项 `G-DATA-EVENT` → 2 项 `G-DATA-XASSET` → 4 项 `G-DATA-HF`；这只改变分批顺序，不改变 20+8+4+3+4+7+2+4 的 52 项范围与完成定义。

完成状态分三级：`PLAN_COMPLETE_52` 只表示 52 项范围和门齐全；`REQUIRED_DATABASE_COMPLETE_35` 要求 CORE/GCTX/HK/GLEQ 全部有实际快照、PIT/schema/许可/质量/哈希/消费者合同和更新、主盘内逻辑恢复证据；`FULL_DATABASE_COMPLETE_52` 要求其余 17 项也通过各自数据门。明确 `DEFERRED/BLOCKED` 是诚实治理状态，不等于建成。独立备份不属于完成定义。新闻当前暂停，高频与券商当前暂缓，付费数据当前未授权，因此当前既不是 35 项必建数据库完成，也不是 52 项全域数据库完成。

## 6. L2—L5 轮廓（暂不细化）

### L2 特征与算法层

只消费 L1 已准入数据；按 `R0/PV`、`R1/CLASSIC_CN`、`R2/A_SHARE_GLOBAL` 的数据与安全上限形成候选特征，记录公式、上游快照、参数、代码版本和可重建证据。多因子负责组合候选研究。R0/R1/R2 可以分批开启，不要求先完成 35/52 项数据库；但每一批仍必须先把对应 L2 安全门细化为可执行工作包并通过验收。

D2A-02 的 `INSPECTION_ONLY` 候选只允许做数据形状、公式和管道 smoke，不得评价
因子有效性。D2B 以后只有单项正式准入、不可变且能以精确
`dataset_id + snapshot_id` 通过 `qdata --intent research` 解析的数据，才允许
运行标记为 `SMOKE/EXPLORATORY` 的提前因子切片；单项数据未达到任何完整研究门时，
该切片不计入正式 L2 或 `G-ALG`，不写入 `data/published/`，不形成投资结论。
达到 R0、R1 或 R2 的完整输入与安全门后，才可在该层限定范围内登记正式 L2 候选研究；
登记仍不表示因子可靠或允许发布。

### L3 验证与阶段门层

统一处理滚动样本外、purge/gap、成本、容量、稳定性、多重检验、压力测试、真正 Holdout 和风险审查。R0/R1/R2 只决定 L2 候选研究的输入上限，不替代本层；只有通过 `G-VAL` 的冻结候选才能称为可靠结果。项目内测试通过不等于本层通过；失败结果和 `NO_FACTOR` 继续作为正式证据保留。

### L4 发布与 Shadow 层

只有通过 L3 的冻结特征才能按合同写入 `data/published/`。随后才做组合、成本/容量/执行仿真和 `Shadow Only`，并保留人工批准；不连接自动下单。

### L5 报告、监控与反馈层

形成固定报告、数据质量和模型漂移监控、失败恢复、版本退役、复盘以及回流 L1/L2 的问题单。现有零散报告可复用，但不等于闭环已经建立。

## 7. CWOPT 外部协作旁路线

CWOPT 不占用上述五层的本地开发工作包。

- 开发、实验、测试、版本和发布：由合作者负责；
- 本工作区职责：人工审核后从 GitHub 拉取并验证，不主动改 CWOPT 业务代码；
- 本地仓库：`${QUANTIFICATION_ROOT}\CWOPT相关\CWOPT`；
- 远端与跟踪分支：`origin = https://github.com/jingyuanlm/CWOPT-v0.git`，`finally` 跟踪 `origin/finally`；
- 同步方式：仅人工触发，先 `fetch` 和审阅差异，再使用 fast-forward-only 拉取；禁止自动 merge、rebase、force reset；
- 拉取后：运行合作者提供的最小测试并记录 commit hash、测试结果和同步日期；
- 数据边界：CWOPT 缓存继续 `ENGINEERING_ONLY`，除非未来单独按 L1 数据合同导出不可变快照，否则不得进入主线。

截至 2026-07-20，本地 CWOPT 工作树存在已修改文件和未跟踪文件，状态为 `SYNC_BLOCKED_DIRTY_WORKTREE`。在所有者明确处理这些本地改动前，不执行 `git pull`，也不由本 Roadmap 推断哪些文件可以丢弃。

## 8. 最近完成与当前工作包状态

`L1-D12A-01_ATOMIC_GUARD_AND_RUNTIME_REPAIR` 状态为 `DONE__ISOLATED_INFRASTRUCTURE_ONLY`。完成证据包括：负向 schema/RFC 3339 测试；两个真正子进程 hard-crash 恢复；PID reuse 与未知状态失败关闭；同 `run_id` 错绑、越界计划和双 recovery 竞争回归；owner/清理结果语义；中央版本锁和兼容见证验证；适配器宽度、单位、lineage、类型、重复键、关键 NULL、数值边界和 OHLC 门；原有控制层回归；正式 catalog SHA-256 保持 `debdbea74d3597dab66efc850517d3ab8ed6062a58229a61303556351883be00`，且没有正式 raw/curated/quarantine/staging/transaction 落盘。验收回执位于 `reports/data-governance/d12a-01-atomic-guard-runtime-repair-20260727/README.md`，机器可读验证记录位于同目录 `VERIFICATION.json`。该完成状态不授予来源准入或正式发布权。

历史暂停工作包 `L1-D2A-01_SOURCE_ADMISSION_AND_PILOT_ADAPTERS` 的状态为 `PAUSED__BLOCKING_CONFLICTS_REPORTED__NO_FORMAL_ADMISSION`。最终公开源运行只在 `tmp/source-admission-pilot/d2a01-20260727-v3` 保存 SSE/SZSE 当前参考数据、2026 休市通知、CNINFO 10 条元数据和许可证据；11 个获准 capture 成功，3 类业务请求在 fetch 前阻断。随后受控 BaoStock v1—v4 证据均保存在 `tmp/source-admission-pilot/baostock-daily-coverage-*`；v4 登录成功、第一条全 A 压缩响应到达，但帧尾合同未闭合，业务结果接受数 `0/25`，正式目录指纹前后一致。付费、用户密钥、全历史回补、正式 catalog 登记和质量晋级均为 0。阻断包括：BaoStock 帧尾/实际行覆盖/单位/权利/PIT 未闭合；BSE 主档 raw schema/权利未验证；三所正式日行情权利未准入；Tencent 后六字段、单位和日期边界未闭合；CNINFO 精确可用时间、修订链、批量保存与内部共享权未闭合。回执位于 `reports/data-governance/d2a-01-source-admission-pilot-20260727/` 与 `reports/data-governance/baostock-daily-coverage-pilot-20260727/`。

此前完成的本地引导子包是 `L1-D2A-01B_CSMAR_LOCAL_RESEARCH_BOOTSTRAP`，状态 `DONE__LOCAL_CANDIDATE_PIPELINE_ONLY__FORMAL_ADMISSION_0`。中央 venv、下载包候选构建器、两份精确 current-pilot request、CSMAR intake manifest 和两份真实候选均已验证；78 项 control 测试通过，1 项真实 Windows symlink 测试因账户权限跳过。当前最近完成子包已更新为下述 `L1-D2A-04C_33PKG_LOCAL_SCHEMA_PIT_AND_TEXT_CLOSURE`；所有候选仍不在 `datasets.json` 中，不得由研究项目绕过 `qdata` 读取。

此前执行工作包 `L1-D2A-02_CSMAR_PRIORITY_TABLE_CENSUS_AND_MULTIPACK_PILOT` 的状态为 `PARTIAL__DAILY_MULTIPACK_PASS__FINANCIAL_PIT_PERMISSION_BLOCKED`。一个 2020—2024 五年全字段日行情包已完成全量机械审计和候选构建；5,852,287 行、5,659 个证券、60 个年月分区，主键、分区、内容哈希和 catalog guard 均通过。当前机构权限无法读取专门的财报公布日期库，可访问财务事实表也没有已验证的首次 `available_at`，因此本包按声明的 PIT 停止条件暂停。portal `tbId` 继续只作页面定位证据；RQData/Choice/iFinD 仅作缺口补充和多源校准候选。

此前暂停子包 `L1-D2A-03_OFFICIAL_DISCLOSURE_PIT_AND_MINIMUM_CORE_CANDIDATE_SLICE` 的状态为 `PAUSED__PARTIAL__BSE_BUSINESS_AND_CSMAR_FINANCIAL_PIT_BLOCKED__CORE03_SOURCE_SLICE_ONLY__FORMAL_ADMISSIONS_0`。深交所、上交所 PIT 小样本和 16 证券机械切片已完成；北交所业务 PIT、CSMAR 财务包、永久身份、官方日历、真实公司行动、可重建前复权与完整可交易性均未通过。其未闭合缺口已经并入后续来源闭合队列，不单独恢复该历史子包。

D2A-04 Phase A 已完成并暂停，状态 `COMPLETED__PHASE_A_LOCAL_ENGINEERING__D2A_04_COMPLETION_GATE_NOT_MET__PAUSED`。执行计划固定在 `data/catalog/contracts/source_admission_v1/execution_plans/d2a04_core01_to_core06_candidate_closure_v1.json`；成功候选位于 `tmp/d2a04-core01-to-core06-candidate/d2a04-phase-a-20260728-v1/`，共 16 个文件、568,246 B，content hash 为 `f228a3a8b55aec100291b21e32e6fa2b6aacf1c58866d89dc884c096a52ab1f3`。它建立了 CORE-01 临时代码事件、CORE-02 观察日期诊断、CORE-03 分区引用/字段/映射覆盖、CORE-04 `Capchgdt` 线索和缺口证据、CORE-06 原始状态分布；CORE-05 为 0 行且没有目录。正式 catalog 前中后 SHA-256 均为 `debdbea74d3597dab66efc850517d3ab8ed6062a58229a61303556351883be00`，`qdata --intent research` 正确拒绝。第一次 SQL 列歧义失败留下 214,961 B staging，因 `automatic_cleanup=false` 未删除；它不是成功候选。

用户于 2026-07-28 以“把这些数据收集过来，看看够不够”明确启动
`L1-D2A-04B_OFFICIAL_IDENTITY_CALENDAR_ACTION_STATUS_SOURCE_CLOSURE`，并在
历史门户预检暂停后重新启动有界执行；2026-07-29 又明确要求扩大结构化低频候选
收集，并批准按 33 个 CSMAR 包执行、验证后删除 C/F 原件。该本地批次已经完成，
并在该阶段以
`PAUSED_AFTER_33PKG_LOCAL_BATCH__33PKG_INTAKE_SEALED__16_FINANCIAL_AUDIT_CANDIDATES_BUILT__2_AUDIT_TEXT_TABLES_RAW_ONLY__CSMAR_PORTAL_CODE27_UNTIL_20260730_0000__OFFICIAL_PARTIAL__BSE_WAF_HARD_STOP__INSPECTION_ONLY`
暂停。该字符串只保留为 04B 阶段回执，不再表示当前本地候选计数。

用户随后批准的
`L1-D2A-04C_33PKG_LOCAL_SCHEMA_PIT_AND_TEXT_CLOSURE` 已完成，状态为
`COMPLETED__33PKG_INVENTORY__AUDIT_TEXT_ZERO_DROP_PROVENANCE_BOUND_V2__PIT_GAP_AUDIT_COMPLETE__PIT_NOT_CLOSED__CORE_TOUCHED_14_OF_20__FORMAL_CORE_0_OF_20__INSPECTION_ONLY`。
D2A-04 Phase A 的历史状态和证据保持不变。当前已经实际下载并机械构建
`TRD_Dalyr` 1990—2026 分段全历史、`TRD_Co`、`TRD_Cale`、`TRD_Cptl`、
`TRD_Capchg`、`TRD_NoLimit`、`TRD_AdjustFactor` 和 18 张财务/审计表；另有
1 个重叠 `TRD_Dalyr` 包只作校准、不重复构建。全部输出继续
`INSPECTION_ONLY/NOT_ADMITTED`。D2A-04C 本批本身不批准付费、新密钥、正式准入、
质量晋级/豁免或因子研究；2026-07-30 用户另行启动的有界 D2A-04B 来源闭合可以继续
官方/许可/PIT/身份/日历/行动/状态补证和无密钥最小预检，但财务首次披露/修订链和缺失
CORE 数据族仍必须逐包落盘、核对真实表头并通过隔离门。

历史门户预检曾观察到 `TRD_Dalyr`“无权限”、控件禁用和购买动作；该回执继续
保留，但不得再写成当前阻断。当前日行情只读去重投影为 18,691,664 行、
5,990 个证券和 8,741 个交易日，重叠字段冲突为 0；四张行动/约束/复权新增候选
共 287,560 行。SSE 检查包封存 21 份官方原始响应，SZSE 检查包封存 23 份官方
原始响应并在旧规则 URL 404 后停止；BSE 在官方 WAF HTTP 403 处按门硬停且业务
数据为 0。官方源包只形成部分身份、休市通知和规则/公司行动观察，仍未闭合永久
身份、逐市场日历与会话链、完整公司行动修订链、历史状态链或 PIT 财务。33 包迁移
回执位于 `reports/data-governance/csmar-33pkg-transactional-intake-20260729/`；
当前 32 个候选 + 1 个重叠校准包、两张审计文本 v2 哈希链和 CORE-01～20 缺口矩阵
位于 `reports/data-governance/csmar-33pkg-local-closure-20260729/`。所有输出
继续 `INSPECTION_ONLY/NOT_ADMITTED`，正式准入和正式 CORE 均为 0。

2026-07-30 先对 CORE-01/02/03/06 执行了 selected-core combined 审计，历史结果为 0/4，16 个 BLOCK 门为 1 PASS、12 FAIL、3 UNKNOWN。随后两阶段架构复核确认其中 `formal_closure`、`formal_catalog_identity`、`qdata_research_resolution` 是发布后才能成立的检查，若作为发布前条件会形成循环；因此旧 combined 审计固定重分类为 `POST_ADMISSION_AUDIT`，不得再解释为真正的发布前准入门。新的非循环 Stage A 只检查权利、语义、PIT、修订链、官方交叉证据、完成证据、开放合同阻断与质量门，当前仍为 0/4 ready，CORE-01/02/03/06 分别有 11/9/12/11 个阻断；Stage B 为 `BLOCKED__PHASE_A_NOT_READY`，没有用户哈希绑定授权回执、真实发布、catalog 写入或 qdata 研究解析。LimitDown/LimitUp/LimitStatus 已确认存在于 24 字段实包；本轮补充的标准 TRD 手册进一步确认了供应商层的单位、枚举和计算说明，但交易所官方历史制度版本、适用期、最小价位/舍入、IPO/重上市例外、NULL/sentinel、PIT/修订链和来源权利仍未闭合。证据位于 `reports/data-governance/core01-02-03-06-local-evidence-closure-20260730-v1/`、`reports/data-governance/d2b-selected-core-formal-admission-gate-20260730-v1/`、`reports/data-governance/selected-core-two-stage-admission-architecture-audit-20260730-v1/` 与 `reports/data-governance/csmar-intake-supplemental-evidence-20260730-v1/`。

同日完成四条标准化并行轨。第一，CSMAR Python API 为 `AUTHENTICATION_PASS__DATA_ENTITLEMENT_DENIED__NOT_A_DAILY_UPDATE_SOURCE`。第二，在旧 synthetic smoke 之上新增本地每日增量候选引擎：新旧 14/14 测试通过，已实现显式本地 CSV/JSONL 发现、严格 schema/主键/日期/PIT 修订检查、自包含分区合并、同卷原子封口、幂等与失败 quarantine，并拒绝 manifest 外额外 payload、shell 元字符和 reparse/symlink/hardlink 路径；状态固定为 `PASS_INSPECTION_ONLY_LOCAL_INCREMENT_ENGINE__NO_PRODUCTION_UPDATER__NO_SCHEDULER`，不是下载器、真实来源适配器、正式发布器或无人值守调度器。第三，35 项官方来源与校内/供应商候选均已映射；四源无密钥检查适配器 fixture 4/4 通过，单次 live 为 BIS、美国财政部、HKMA 3/4 结构通过，ECB 的一次本机 TLS EOF 进入 quarantine 且没有重试。第四，CORE-02 官方日历/时段候选形成沪深 2015-01-01—2026-07-28 共 8,454 条 venue-date、28 条公告/修订 lineage 和 12 条当前/近年时段记录，20/20 官方请求原文与哈希保留；随后又以 ISO 10383 官方页面及 CSV 闭合 XSHG、XSHE、BJSE 三所 MIC 映射子检查，原文、哈希和规范化 3 行证据位于 `reports/data-governance/stage-a-core02-iso-mic-evidence-20260730-v1/`。该子检查通过不改变 CORE-02 门；最终 v4 汇总按传输边界失败关闭为 `Q1=FAIL / Q2=FAIL / Q3=UNKNOWN / Q4=PASS`。北交所完整日历/时段链、三所完整临时/应急停市档案、沪深更早时段版本链和日内发布时间仍未闭合。以上成功响应和候选只保存 raw、SHA-256、标准化视图、rights/PIT/缺口回执；没有调度、分页、历史回补、凭据、正式 catalog 写入或质量晋级，均不是来源正式准入。报告分别位于 `reports/data-governance/csmar-python-api-entitlement-probe-20260730-v1/`、`reports/data-governance/local-daily-update-engine-20260730-v1/`、`reports/data-governance/official-automatable-source-inventory-20260730-v1/`、`reports/data-governance/library-structured-vendor-coverage-20260730-v1/`、`reports/data-governance/public-official-api-preflight-20260730-v1/`、`reports/data-governance/public-official-api-inspection-adapters-20260730-v1/`、`reports/data-governance/core02-official-calendar-session-closure-20260730-v1/` 和 `reports/data-governance/stage-a-core02-iso-mic-evidence-20260730-v1/`。

本轮四份上游报告已由 `reports/data-governance/nightly-standardization-and-blockers-20260730-v1/` 做统一哈希绑定；安全回归为每日更新 14/14、两阶段门 13/13，合计 27/27，凭据模式命中 0，正式 catalog 哈希保持 `debdbea74d3597dab66efc850517d3ab8ed6062a58229a61303556351883be00`。

同夜继续完成三项 `INSPECTION_ONLY` 维护骨架：本地日更编排状态层以 SQLite 账本、连续水位线、单写者租约、最早缺口 replay、断点恢复、幂等事件和重试耗尽失败关闭通过 10/10 新测试，并复跑既有日更 14/14；schema 演化分类器以 14/14 测试区分 `NO_CHANGE`、带 `field_coverage_start` 的兼容可空新增字段、破坏性变化和未知失败关闭；通用 revision/vintage 事件账本以 10/10 测试覆盖带时区 `available_at + observed_at`、迟到、撤回/恢复、supersedes、重复幂等和冲突失败关闭。证据分别位于 `reports/data-governance/local-daily-orchestration-state-layer-20260730-v1/`、`reports/data-governance/schema-evolution-classifier-20260730-v1/` 和 `reports/data-governance/revision-event-ledger-inspection-20260730-v1/`。三者均未联网、未创建调度任务、未运行真实 worker、未写正式 catalog/`data/market`、未质量晋级；它们不能替代来源权利、真实发布时间/修订语义、生产发布器或正式准入。对当前 Stage A 的 43 个整项阻断理由复核后，纯靠现有磁盘候选和标准化代码可完全闭合的仍为 0/43；下一步必须取得 CORE-01 官方永久身份历史、CORE-02 北交所/完整时段与临停档案、CORE-03/06 官方字段字典和规则版本及相应保存权证据。

2026-07-31 又完成独立、受限的本地日更 inspection worker，将已经落地并标准化的单日 CSV/JSONL 输入连接到上述编排状态层和候选合并内核。它绑定 dataset/source/scope、冻结日历、最早未完成日期、请求指纹、候选身份、raw 日期与 manifest 哈希；输入在 claim 后漂移、构建后校验失败或非预期执行异常均失败关闭并清除活动 claim，原始路径及父链 reparse/symlink/junction 也会被拒绝。相关链 34/34 回归通过，状态为 `PASS_INSPECTION_ONLY_BOUNDED_LOCAL_WORKER__34_OF_34_RELEVANT_REGRESSIONS__NO_DOWNLOADER_NO_NETWORK_NO_SCHEDULER_NO_PRODUCTION_PUBLISHER_NO_FORMAL_WRITE`，证据见 `reports/data-governance/local-daily-inspection-worker-20260731-v1/VERIFICATION.json`。这只闭合“已标准化单日输入 → `tmp/` 候选或 quarantine → 编排回执”，不提供真实来源下载器、正式 raw sealing、正式快照发布或 catalog 登记；Stage A 仍为 0/4，正式来源准入仍为 0，D2B 和研究继续阻断。

历史预检回执位于
`reports/data-governance/d2a-04b-source-closure-20260728/d2a04b-portal-preflight-20260728-v1/`；
官方源检查包位于
`tmp/d2a04b-source-closure/d2a04b-official-track-a-20260728-v1/`。

下一唯一工作包改为 `R0_UNLOCK_CORE01_TO_CORE06_FORMAL_SNAPSHOT_AND_L2_PV_SAFETY`，当前状态 `NEXT__R0_NOT_MET__STAGE_A_0_OF_4`。它把既有 D2A-04B、D2B 和最小 L2 价量安全门串成一个不跳步的解锁包：先闭合 CORE-01/02/03/04/06 的官方/许可/PIT/身份/日历/行动/状态缺口并从合格 CORE-03/04 生成 CORE-05；再由支持真实数据的原子发布器建立首批正式不可变 CORE-01～06 快照、登记 catalog 并核验 `qdata --intent research`；最后完成 `PanelData`、PIT/`available_at`、可交易性、复权、防未来信息和可重建性验收，重新计算 `R0/PV`。只有 D2A-04 完成门与非循环 Stage A 全部通过、另行取得与精确 Stage-A bundle 哈希绑定的发布授权，并由真实发布器产生可验证 receipt 后，才可执行 Stage B；当前 `atomic_snapshot_publish.py` 仅支持 synthetic fixture，不得用于真实发布。厂商联系、付费、新密钥、全历史回补、正式 catalog 登记、质量晋级和真实发布不因本 Roadmap 分层决定而自动获权。

并行的非晋级工程准备包 `R0_LOCAL_PREP_NO_PROMOTION_V1` 当前状态为 `IN_PROGRESS__LOCAL_CONTRACT_AND_FIXTURE_ONLY__FORMAL_EFFECT_NONE`。本包只允许建立 CORE-01～06 候选 payload 布局/manifest、排除/冲突/未知/修订账本、默认禁用且仅输出 dry-run 计划的真实工作区发布适配边界、formal-like fixture payload 到内存 `PanelData` 的接口测试，以及统一 verify-only 命令；不得读取真实候选作为研究输入，不得写 `datasets.json`、`data/market/snapshots` 或 `data/published`，不得生成正式快照、晋级质量、改变 Stage A/D2B/R0 状态或运行因子。它是上述唯一解锁包的机械前置件，不替代任何来源权利、PIT/修订链、正式发布授权或 L2 研究安全门；当前 Stage A 仍为 0/4、正式 CORE 仍为 0/20、`R0/PV` 仍为 `NOT_MET`。

从当前状态到分层研究按工作包推进：① 执行 `R0_UNLOCK_CORE01_TO_CORE06_FORMAL_SNAPSHOT_AND_L2_PV_SAFETY`，实际通过后只开放 `R0/PV` 价量候选研究；② 并行完成并正式准入 CORE-07～20 及 `L2-CLASSIC-CN-SAFETY`，实际通过后开放 `R1/CLASSIC_CN`；③ 完成并正式准入 GCTX-8 及 `L2-A-SHARE-GLOBAL-SAFETY`，实际通过后开放 `R2/A_SHARE_GLOBAL`；④ 每层候选继续进入 L3 的样本外、成本、稳定性、压力、Holdout 和风险审查，通过 `G-VAL` 后才能称为可靠结果。D2B 的单项探索性 smoke 不是任何研究门通过。Codex/Agent 应将步骤内可独立的数据族、来源适配器和机械校验并行执行；人工门户导出、来源权利和关键 PIT 冲突仍是外部阻断，不能由并行计算绕过。当前 R0、R1、R2 均为 `NOT_MET`，不再给出未经过批次吞吐实测的周数硬估计，完成每个工作包后按真实剩余缺口更新。

并行工程准备包 `L2-P0_QDATA_FACTOR_INPUT_ENGINEERING` 于 2026-07-28 完成统一解析和 12 项拒绝越权测试。2026-08-04 独立 `R0_SHSZ_PV` 正式快照发布后，真实 `qdata`、正式 `PanelData` 小样本和时间/防泄漏窄门也已通过，状态更新为 `R0_SHSZ_PV_INPUT_OPEN__CANONICAL_R0_R1_R2_BLOCKED`。2026-08-05 又完成 C-v1.1、14,385 条统一因子元数据目录、米筐 13 类小样本能力探针、11 族因子驱动数据解锁矩阵，以及 10 因子/140 日/5,221 证券的全市场工程冒烟；随后在同一精确快照上完成 96 因子校准、512 因子 Discovery 和 50 因子的多分割稳定性描述。该批只开放沪深普通 A 股价量 Discovery，未使用 CSMAR/RESSET 候选、基本面、行业、ETF 穿透或全球环境；50 个统计候选中只有 6 个保持四方案平均 30bps 成本后为正，且尚未相关性去重或独立 Validation。Validation/Holdout 均未消费；完整 CORE bundle 的 PanelData 与 canonical R0/R1/R2 仍等待相应正式快照和独立验收。

外盘必建队列已登记为：`L1-D10A_GLOBAL_CONTEXT_CONTRACT` → `L1-D10B_HK_EQUITY_CONTRACT` → `L1-D10C_GLOBAL_LINKED_EQUITY_CONTRACT`。该队列不改变当前 D2A 内部的“来源准入 → 真实 pilot → 容量报告”后继顺序，也不表示三个包已激活；每包先完成字段、宇宙、来源权利、PIT/时区/币种和容量试点合同，再申请真实回补。

当前容量判断按真实批次更新：完成 D2A-04C 后，`tmp/local-dataset-build/` 实测
3,750,325,676 B（约 3.493 GiB），其中审计文本两张当前权威 v2 Parquet 候选合计
85,070,570 B；`tmp/csmar-audit-text-candidate/` 因同时保留 v1、v2 和逐行修复证据
实测 721,763,921 B（约 0.672 GiB）。受控 33 包封存 raw 为 2,038,345,395 B
（约 1.898 GiB）；对应 C/F 临时原件已经删除，G 盘空闲约 373.801 GiB。旧 v1
候选按用户批准的“保留、不删除”边界继续占用空间，但不再作为权威候选。
八段日行情候选物理为 2.449 GiB，
四张新行动/约束/复权
候选仅 15.291 MiB。近期结构化 A 股日频核心的持久工程预算先按 20—60 GiB，
连同 raw、修订链和若干不可变快照按 50—120 GiB 工作区间逐批复测；这不是采购要求
或硬上限。2026-07-26 的 35 项 1.17 / 3.31 / 9.49 TB 继续只作含多来源、全球数据、
快照和工作峰值的条件场景，不再代表当前结构化日频库预测。新闻正文/PDF、分钟、
tick/L2 和大规模全球全股票历史启用前，不得用上述 TB 场景宣称当前空间不足。
用户没有要求固定保留 200 GiB，`user_fixed_free_gib=null`；用户也已决定不配置
独立备份。付费来源、新闻、高频、质量晋级和豁免仍须另行批准。

旧数据 A+B 已完成物理删除；活动 catalog 当前为 13 个身份，另保留 2 个退役
tombstone。已经下载并构建 CSMAR 日行情全历史分段、公司当前截面、日历、四张
行动/约束/复权及 18 张财务/审计 `INSPECTION_ONLY/NOT_ADMITTED` 候选；33 包
现为 32 个候选 + 1 个重叠校准包，但尚无任何新正式准入数据，未运行 V7，也未拉取
CWOPT。最新 CSMAR 汇总见
`reports/data-governance/d2a-04b-csmar-structured-candidates-20260729/` 与
`reports/data-governance/csmar-33pkg-local-closure-20260729/`；33 包迁移见
`reports/data-governance/csmar-33pkg-transactional-intake-20260729/`；旧数据删除
范围与回执见 `reports/storage-cleanup/legacy-data-retirement-20260720/README.md`。

2026-07-30 新增两个指数候选族：`IDX_Idxinfo` 为 5,574 行、12 字段；三批 `IDX_Chgsmp` 分别为 279,744、3,596,921 和 7,170,930 行，均通过各自 CSV↔Parquet/主键机械校验但保持 `INSPECTION_ONLY/NOT_ADMITTED`。`tmp/csmar-idx-chgsmp-candidate/` 与 `tmp/csmar-idx-zero-loss-candidate/` 合计约 2.064 GiB。G 盘当前总容量 453.853 GiB、空闲 368.793 GiB；空间足以继续结构化低频的窄批次，不等于授权整库或高频回补。

同日对后续 17 个 C 盘下载来源执行了非破坏性集中封存：内容去重后为 10 个唯一对象（7 个 ZIP、3 份 PDF 手册），7 个 ZIP 共 31 个成员均通过完整读取/CRC；17/17 源文件在 intake 封存时仍留在 `${USER_PROFILE}\Downloads` 且字节和 SHA-256 一致，随后已与新增指数批合计 35 个已验证来源一起移入 Windows 回收站，回收站未清空。本批绑定 `FS_Comscfi`、`BDT_CO`、`BDT_Trdchg`、`IDX_Idxinfo` 与三批 `IDX_Chgsmp` 共 7 个候选，`IDX_Idxinfo/IDX_Chgsmp` 使相对于冻结 33 包审计的候选触及投影由 14/20 增为 16/20；冻结 33 包的 14/20 历史事实不改写，正式 CORE 仍为 0/20、Stage A 仍为 0/4。集中封存、28/28 引用验哈希和手册页级语义证据分别见 `reports/data-governance/csmar-arrival-intake-20260730-v1/` 与 `reports/data-governance/csmar-intake-supplemental-evidence-20260730-v1/`；这些输出没有 catalog 写入、质量晋级、正式快照或研究授权。

随后新增指数日行情 intake `data/market/quarantine/csmar-manual-export/intake-20260730-18src-12blob-idx-idxtrd-v1/`：18 个来源别名共 435,572,489 B，按内容归并为 12 个唯一 ZIP、397,397,885 B，ZIP 完整读取/CRC、字节数和 SHA-256 均通过；`INTAKE_MANIFEST.json` SHA-256 为 `10439a5e5857fd9cf5c00c36aa2cd3d5fcfaac52bd11a67efff4e31f451eff23`，封存时点 retention receipt SHA-256 为 `36b8fc683cef53a460883813f4f21b3265db4c06833892dc550b53945299051c`。两个 2026-07-30 intake 合计 35 个来源、632,131,463 B，在 22 个唯一目标对象（19 个 ZIP + 3 个 PDF）全部通过哈希复核且 19 个 ZIP 通过完整读取/CRC 后送入 Windows 回收站，回收站未清空；可恢复清理回执为 `reports/data-governance/csmar-downloads-cleanup-20260730-v1/CLEANUP_RECEIPT.json`，SHA-256 `91e97ee9d188b4e5c52030009cb82191b672032404d6e7e698ed300700a3f731`。该操作只清理已进入不可变 quarantine 且通过目的端复核的来源别名，不改变候选或正式准入状态。

用户对三个精确 intake 的 68 个来源别名（别名字节合计 2,670,476,858 B）、55 个 canonical 唯一 blob（内容去重后 2,585,514,984 B）已形成逐资产权利声明回执：允许本机长期保存、校内非商业学术研究和内部派生因子/统计，禁止原始数据外部分发与商业使用；API/自动化批量采集、离校后处置、官方来源权利和独立合同核验不在声明覆盖内。`RIGHTS_RECEIPT.json` SHA-256 为 `1d07b07304fe767ce7edda2d2563d816c73e52fe7b1c21ef30bccb1db9498661`，报告 manifest SHA-256 为 `fcb22330aeec0731ee29562bfceb32312a37c1ab365b24d9c00bef20c3bdcded`。这关闭的是精确 CSMAR 本地资产的用户声明子范围，不是 GLOBAL 权利总门，故 `formal_admission=false`、`research_usable=false` 保持。

Stage-A auditor contract v3 的 2026-07-31 v4 packaging seal 对 9 个上游包及其递归绑定执行严格、只读审计；v4 封存时 auditor `--verify-only` PASS。五个 authority 文件于 2026-07-31 获准同步后，当前 live verify 预期 FAIL；其八项错误精确限于五个旧 authority 哈希绑定、CORE01/CORE03_CORE06 live guard 与 aggregate formal-guard 漂移，并由 `reports/data-governance/stage-a-authority-sync-post-verification-20260731-v1/` 核对，receipt `REPORT_MANIFEST.json` SHA-256 为 `46652312bb20fe021c013b7bc06581c359fd324d525b192b7d9e826a89a03f75`。当前 live 状态明确为预期 FAIL；strict upstream package integrity 仍为 9/9，网络请求 0、写入 0、队列状态变化 0。CORE-02 最终失败关闭状态为 Q1 FAIL、Q2 FAIL、Q3 UNKNOWN、Q4 PASS；ISO 10383 的 XSHG/XSHE/BJSE MIC 子检查 PASS 不触发 Stage-A 转换。绑定报告包路径为 `reports/data-governance/stage-a-evidence-consolidation-20260731-v4/`，`REPORT_MANIFEST.json` SHA-256 为 `751ecff9d6bb39cbb88c919c01a910b92c3e8e50fd65676d0472c3f868bfc4d3`。路径语义由 `reports/data-governance/stage-a-evidence-consolidation-20260731-v4-alias-receipt-v1/` 约束，其 `REPORT_MANIFEST.json` SHA-256 为 `587077cc0871960398bf4ec3ae4069c071410e81a3ced8212945f950f6dd4435`：内部 run ID `stage-a-evidence-consolidation-20260730-v3` 是固定 auditor contract 标识，v4 仅表示 2026-07-31 的第四次 packaging seal，不是正式证据根、门转换或准入晋级。该报告及 post-sync receipt 均为 `formal_effect=NONE`；正式 Stage A 仍为 0/4，正式来源准入为 0，全部候选仍为 `INSPECTION_ONLY/NOT_ADMITTED`，Stage B 与 D2B 仍为 `BLOCKED__PHASE_A_NOT_READY`，未写 `datasets.json`、正式快照或 `data/published/`。

`TRD_Dalyr` NULL/零值 v2 审计进一步逐分区核验 8 个候选、464 个 Parquet 分区：原始 22,476,677 行，去除 3,785,013 条精确重复后为 18,691,664 个干净键，主键冲突为 0，覆盖 5,990 个证券、8,741 个日期和 1990-12-19—2026-07-28；价格零值为 0，`Trdsta` 无空值或越界值，并识别、列出 5 条成交量/成交额一侧为零的异常记录。该结果只把“观测数据审计 + 手册显式零值”的证据子评估推进到 PARTIAL；当前 Stage-A CORE03-Q2 门仍为 UNKNOWN，且它不能证明供应商历史未知值从未编码为零，故 CORE03-Q3 也仍为 UNKNOWN、无正式门转换。证据见 `reports/data-governance/trd-dalyr-null-zero-semantics-audit-20260730-v2/`。
