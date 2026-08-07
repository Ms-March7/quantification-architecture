# Quantification 统一数据目录

更新时间：2026-08-05  
模式：`FEDERATED_LOGICAL_CATALOG_WITH_CENTRAL_SHARED_ROOTS`

## 结论

`factors.json` 是 2026-08-05 建立的统一因子**元数据目录**，当前登记本地 512、聚宽 390、米筐 13,483，共 14,385 个带来源命名空间的因子记录；结构合同见 `factor_registry.schema.json`。该目录只登记名称、定义/定义哈希、依赖级别和本地计算状态，不是 `datasets.json` 中的事实数据库，不包含任何历史因子值，也不授予研究准入。米筐名称或单日样本可见只代表端点能力，仍须按依赖数据、PIT、许可和不可变快照分别闭合。

工作区统一登记全部已识别数据，并已建立 `data/market/` 与 `data/published/` 两个中央共享根。当前有 14 个可解析数据身份和 2 个退役 tombstone；其中 `cn-a-share-r0-shsz-pv-panel@20050104-20260803-v1` 是首个通过 `RESEARCH_ONLY/OK/immutable/frozen` 窄门的正式沪深价量面板。它不等于 CORE-01～06 完成，canonical R0/R1/R2 仍未达到。

`candidate_assets.json` 是候选层中央索引，当前按受限 evidence JSON 登记 26 个 quarantine 顶层候选根、92 个证据文件和 67 个 manifest。它不扫描计划目录、不读取业务 payload、不修改 `datasets.json`，用途上限固定为 `INSPECTION_ONLY`；候选被索引不等于来源准入、质量通过或研究授权。结构合同见 `candidate_assets.schema.json` 与 `contracts/candidate_asset_index_v1/`。

### 2026-08-04 `R0_SHSZ_PV` 正式窄门与日更

正式面板内容哈希为 `26309d36b7c08096fff55df100f1d27df8412b106ea48f282ce0f7caad37084b`，覆盖沪深普通 A 股 2005-01-04 至 2026-08-03；排除北交所、B 股、CDR，并失败关闭排除已知身份冲突记录 `600849.XSHG`。精确 `qdata --intent research`、五份 authority/payload 哈希、原子发布和正式 `PanelData` smoke 已通过，只允许未复权价量、已实现总收益和成交仿真掩码研究。

`../../control/tools/run_candidate_daily_maintenance.py` 与同名 `.cmd` 已成为候选型日更统一入口，Windows 任务 `Quantification-Candidate-Daily-Maintenance` 在工作日 19:20 顺序调用沪深价量、B 路径更新器并刷新 `candidate_assets.json`。旧的 18:30/19:10 分立任务已禁用但未删除。2026-08-05 计划任务 smoke 返回 0，水位已到当日且未发起网络请求；25 项回归通过，正式 `datasets.json` 前后哈希一致。统一入口只写 quarantine、候选交易回执和候选索引；新日数据仍须独立质量复核和原子发布才能进入研究快照。

### 2026-07-29 历史闭合口径

> 本节只记录截至 2026-07-29 的历史暂停状态；当前授权必须以下方“2026-07-30 当前覆盖口径”为准。

数据库总架构、现有物理资产、格式职责、PIT 合同和完整多因子目标见 `DATABASE_MANUAL.md`。日增量、月度小文件合并、修订、schema 演化和人工批准见 `MAINTENANCE_RUNBOOK.md`；来源准入见 `SOURCE_POLICY.md`，缺失、冲突和失败处理见 `DATA_QUALITY_POLICY.md`。未来 52 个数据家族的机器可读计划见 `planned_datasets.json`；20 项国内核心数据族的已冻结字段/来源/许可/PIT 合同见 `contracts/g_data_core_v1/`，D2A-01 的历史 runner 决策见 `contracts/source_admission_v1/source_matrix.json`，跨 D2A-01～04C 与 CSMAR 候选的当前状态见 `contracts/source_admission_v1/current_source_registry.json`；GCTX-8、HK-4 和 GLEQ-3 的字段级合同仍待建设。`L1-D12A-01` 已完成隔离原子发布基础设施修复；CPython 3.11 中央数据工程环境已安装并通过 44 个锁定包验证。`contracts/local_download_build_v1/`、`../../control/tools/qdata_build_downloaded.cmd` 和审计文本专用零丢失解析器已处理 CSMAR 日行情 1990—2026 分段全历史、公司当前截面、日历、分配、股本变动、无涨跌停限制、日复权因子以及 18 张财务/审计表真实人工下载包。33 包当前闭合为 32 个候选 + 1 个重叠校准包；两张审计文本 v2 候选完整绑定修复证据，旧 v1 保留但不再权威。外部采集授权已关闭，下一次外部采集需重新明确启动。全部执行候选均为 `INSPECTION_ONLY/NOT_ADMITTED`，没有修改正式 catalog。计划、合同、Pilot 和本地候选都不是正式研究数据。

### 2026-07-30 当前覆盖口径

2026-07-30 覆盖更新：有界 D2A-04B 来源闭合已由用户启动并处于活动状态。旧 selected-core combined 审计的 0/4、BLOCK 门 `PASS=1 / FAIL=12 / UNKNOWN=3` 只保留为 `POST_ADMISSION_AUDIT` 历史证据；非循环两阶段门的 Stage A 对 CORE-01/02/03/06 仍为 0/4 ready，Stage B 因 `PHASE_A_NOT_READY` 阻断。CORE-02 已形成沪深 2015—2026 的 8,454 条逐所逐日候选、28 条公告 lineage 和 12 条时段记录，但 Q1/Q2 仍失败。当前授权仅限官方/许可/PIT/身份/日历/行动/状态补证与无密钥最小预检；付费、新密钥、全历史批量回补、正式 catalog 写入、质量晋级和研究仍未授权。CSMAR Python API 认证通过但数据 entitlement 被拒绝；本地每日增量候选引擎已通过新旧 14/14 测试，并补上 manifest 外额外 payload、shell 元字符与链接路径的失败关闭回归；统一真实来源下载器、生产发布器和调度器尚未实现。

同夜又补齐三个离线维护合同：`local_daily_orchestration_v1` 的 SQLite 任务账本/水位线/单写者租约/缺口 replay 通过 10/10 新测试并复跑旧日更 14/14；`schema_evolution_v1` 的兼容新增、破坏性变化和未知失败关闭分类通过 14/14；`revision_event_ledger_v1` 的 PIT/vintage/迟到/撤回/恢复回放通过 10/10。三者都只写临时回执或显式输出，不能联网、启动 worker/调度、写正式 catalog/`data/market` 或晋级质量。Stage A 的 43 个整项阻断没有任何一项可只靠本地候选与代码完全闭合；这些工具只把未来真实来源接入变得可审计，不改变 CORE/GCTX 正式计数。

执行顺序现采用分层研究门：先建设 CORE-01～06 并在其正式准入及对应 L2 安全门通过后开放 `R0_PV`；继续完成 CORE-20 后开放 `R1_CLASSIC_CN`；再完成 GCTX-8 后开放 `R2_A_SHARE_GLOBAL`。HK-4 与定义范围内的 GLEQ-3 继续作为 35 项必建数据库的一部分，基金、事件、国内衍生品和高频子门继续保留在全域 52 项。CORE + GCTX + HK + GLEQ 共 35 项的完成定义没有缩减。当前核心事实以官方/公开来源为优先锚点，并允许使用既有校内学习研究权限下的 CSMAR 人工导出作为待校准候选；不购买新的付费数据。行业允许证监会基线、申万研究主口径和其他合法口径版本化共存；指数主数据按多提供商、行业/主题/REITs和收益变体尽量完整登记。新闻和高频继续保留计划但暂缓；具体外盘端点、许可、凭据和批量回补仍需逐次准入。

用户于 2026-07-26 决定不建设独立数据备份。中央数据采用单一权威主盘；备份许可和备份容量不作为当前准入或完成门。不可变快照、修订链、PIT 截断、manifest、内容哈希、原子发布、主盘内中断恢复和投影重建仍是强制要求。主盘物理损坏后可能无法精确恢复的风险已被接受。

当前实际 catalog 与上述 35 项的交集为 0：CORE `0/20`、GCTX `0/8`、HK `0/4`、GLEQ `0/3`。因此工作区正在建设完整数据库的治理、合同和采集能力，但真实数据库尚未建立完成。

### 2026-08-01 分层研究门

研究层与数据门分开管理，低层通过不缩减高层和 35/52 项总目标：

| 研究层 | 数据完成门 | 允许范围 | 当前状态 |
|---|---|---|---|
| `L0_INSPECTION` | 无研究数据完成门 | raw、quarantine、候选、smoke 和工程审计；禁止因子有效性和投资结论 | `NOT_MET`；仅有 inspection 候选 |
| `R0_SHSZ_PV` | 独立沪深普通 A 股价量面板 + L2 窄门 | 未复权价量单因子 Discovery 与预注册验证准备 | `OPEN`；不计入 CORE/canonical R0 |
| `R0_PV` | CORE-01～06 | 国内日频动量、反转、波动率、换手和流动性候选发现 | `NOT_MET`，正式 `0/6` |
| `R1_CLASSIC_CN` | CORE-20 | 国内经典价值、质量、成长、规模、动量和行业中性研究 | `NOT_MET`，正式 `0/20` |
| `R2_A_SHARE_GLOBAL` | CORE-20 + GCTX-8 | 在 R1 上加入经过时区、收盘、币种与 `available_at` 校准的全球环境 | `NOT_MET`，正式 `0/28` |

`G_DATA_RESEARCH_BASE` 保留为 `R2_A_SHARE_GLOBAL` 的兼容别名。`R0_PV` 不授权完整策略绩效结论；相对指数和无风险超额等正式绩效需要相应基准输入。每个研究层除数据完成门外，还必须通过 L2 的 PIT、防未来信息、幸存者偏差、复权、可交易性、训练/验证/盲测和可重建性验收。当前所有研究层均未达到；`datasets.json` 不因架构分层而新增任何虚假身份。

用户于 2026-07-28 启动的
`L1-D2A-02_CSMAR_PRIORITY_TABLE_CENSUS_AND_MULTIPACK_PILOT` 已完成一个
2020—2024 五年全字段 `TRD_Dalyr` 包：5,852,287 行、5,659 个证券、60 个
年月 Parquet 分区，主键和机械检查通过，候选共 693,892,239 B。专门的财报
公布日期库在当前机构账号下不可读，可访问财务事实表也没有已验证的首次
`available_at`；本包已按 PIT 停止条件暂停，D2B 未启动。候选仍固定为
`INSPECTION_ONLY/NOT_ADMITTED`，没有修改 `datasets.json`，不能用于因子
有效性评价。G 盘双容量闸门未触发。机器测量和暂停回执见
`../../reports/data-governance/d2a-02-csmar-priority-table-pilot-20260728/`。

后继
`L1-D2A-03_OFFICIAL_DISCLOSURE_PIT_AND_MINIMUM_CORE_CANDIDATE_SLICE` 已按停止条件
暂停。官方 PIT v2 保存深交所 2 条、上交所 7 条和巨潮 5 条校准记录；北交所仅有
discovery，CSMAR 财务微型包没有下载。16 证券切片生成 509 行，只有 CORE-03
源观测可保留，CORE-01/02/04/06 进入 quarantine，CORE-05 零输出。全部产物只在
`tmp/d2a03-official-pit-core-slice/` 与对应报告，仍是
`INSPECTION_ONLY/NOT_ADMITTED`；`datasets.json` 未修改，D2B 继续阻断。

`L1-D2A-04_CORE01_TO_CORE06_CANDIDATE_CLOSURE_AND_SELECTED_SOURCE_PILOT`
Phase A 已完成并按停止条件暂停。它用 568,246 B 的
轻量候选按哈希引用 60 个五年日行情分区，生成 CORE-01/02/03/04/06 的观察、
字段和缺口证据，没有复制 5,852,287 行；CORE-05 保持 0 行。永久身份、官方
日历、公司行动条款/修订链、历史状态规则和 Limit 字段语义仍未闭合。

用户于 2026-07-28 启动了受限
`L1-D2A-04B_OFFICIAL_IDENTITY_CALENDAR_ACTION_STATUS_SOURCE_CLOSURE`。早先一次门户
预检曾因 `TRD_Dalyr` 表页显示“无权限”而按硬停止暂停，历史状态为
`PAUSED__CSMAR_FULL_TABLE_PERMISSION_BLOCKED__NO_NEW_DATA__INSPECTION_ONLY`；该次
0 业务请求、0 下载的回执继续保留在
`../../reports/data-governance/d2a-04b-source-closure-20260728/d2a04b-portal-preflight-20260728-v1/`，
不得被后续观察覆盖或改写。

后续在清华机构会话中确认了表级权限。CSMAR `TRD_Dalyr` 八个真实分段包已经形成
1990-12-19—2026-07-28 的 18,691,664 个只读唯一键、5,990 个证券和 8,741 个
交易日；重叠区间 24 字段不一致为 0。另已构建 `TRD_Co`、`TRD_Cale`、
`TRD_Cptl`、`TRD_Capchg`、`TRD_NoLimit` 和 `TRD_AdjustFactor` 候选。四张
行动/约束/复权新增候选合计 287,560 行，仅 16,033,830 B。2026-07-29 收到的
资产负债表、利润表和直接法现金流量表三个真实 ZIP 已分别形成 691,546、693,242
和 668,828 行候选，合计 2,053,616 行、684,875,634 B；表头、类型、基础键、
分区与 payload 哈希机械检查通过。但三表只有报告期和差错更正披露日期，没有首次
公布时间、更正前值和完整修订链，仍不能作为 PIT 财务研究输入。以上事实不证明本地
留存、内部研究共享权或 CORE 已闭合。

同日又把 C/F 临时收件位置中的 33 个 CSMAR ZIP 事务迁移到
`../market/quarantine/csmar-manual-export/intake-20260729-33pkg-v1/`：33/33 个
SHA-256 和 122 个 ZIP 成员复核通过后，只删除了 18 个 C 盘和 15 个 F 盘原件，
总计 2,038,345,395 B。新增 12 张银行/证券/保险报表与 1 张关键审计事项统计候选，
合计 80,109 行、1,303 分区、33,113,057 B，13/13 机械 PASS。随后获批的
`L1-D2A-04C_33PKG_LOCAL_SCHEMA_PIT_AND_TEXT_CLOSURE` 用专用零丢失解析器保全
`FIN_Audit` 80,301 行和 `FIN_AuditDetail` 82,264 行；238/103 条修复记录均有
逐行 sidecar。两套 v2 候选把源 ZIP/member、parser、repair manifest、sidecar、
修复 CSV 与 Parquet content hash 绑定，CSV↔Parquet 双向差异和业务键重复均为 0。
`FIN_Audit` 仍有 1 条语义复核记录；旧 v1 不删除，但已被 v2 取代。财务/审计候选
因此为 18 张、2,296,290 行、当前权威候选 803,059,261 B；普通报表与金融专表
存在同键重叠，不得相加冒充独立事实。

完整 33 包现在是 32 个候选 + 1 个仅作重叠一致性校准的 `TRD_Dalyr` 原包，触及
CORE 14/20，但正式闭合仍为 0/20。`Accper/EndDate` 不是 `available_at`，
`DeclareDate` 只记录更正披露，首次披露、旧值和完整修订链仍缺失。迁移回执见
`../../reports/data-governance/csmar-33pkg-transactional-intake-20260729/`；
当前结构、关系、PIT 和哈希闭合证据见
`../../reports/data-governance/csmar-33pkg-local-closure-20260729/`。

上交所和深交所的部分官方身份、日历等证据已封存在
`../../tmp/d2a04b-source-closure/d2a04b-official-track-a-20260728-v1/SSE/` 与
`../../tmp/d2a04b-source-closure/d2a04b-official-track-a-20260728-v1/SZSE/`；
北交所采集在 WAF/HTTP 403 处按门停止，未接受业务数据。三者都只是
`INSPECTION_ONLY/NOT_ADMITTED` 来源证据，不能解释为完整历史身份、逐日交易日历、
公司行动修订链或历史状态已经闭合。

本次获准的有界 33 包批次已经完成并保持密封；2026-07-30 用户另行启动了有界
D2A-04B 来源闭合。每个实际取得的输入仍必须保留 raw、查询与下载 receipt、内容
哈希、PIT/修订和来源权利证据；未闭合项进入 quarantine。不得购买、持久化凭据或
自动扩展到付费、新密钥、批量历史、新闻、高频、正式 catalog、质量晋级或研究读取。正式准入仍为 0，
D2B 与因子 IC/回测继续阻断。最新实测与缺口见
`../../reports/data-governance/csmar-33pkg-local-closure-20260729/`；历史结构化
构建与迁移回执分别见
`../../reports/data-governance/d2a-04b-csmar-structured-candidates-20260729/`
和 `../../reports/data-governance/csmar-33pkg-transactional-intake-20260729/`。

清华图书馆其他经济金融数据库未受 CSMAR 代码 27 影响。当前只完成资源详情页
census：RESSET 企业大数据、中国微观经济企业库、CVSource、Capital IQ 和 Datago
高管/调研提供规划外企业、交易、信用、创新/绿色、司法/知识产权/招投标和治理网络
候选；World Trade 与巨灵的图书馆说明则显示其主要是贸易政策/财经全文，不是已确认
的数值贸易流或结构化证券核心源。该发现没有修改 52 项计划或触发下载，见
`../../reports/data-governance/library-economic-financial-source-census-20260729/`。

2026-08-02 后续人工导出已经把 RESSET `COMNMHIS`、`DELINFO`、`STKSPCTRMT`、
`SUSPNSNRESMPTN` 四包封存在
`../market/quarantine/resset-manual-export/intake-20260802-04pkg-v1/`，并形成
9,778、8,936、4,105、81,535 行的四个隔离候选。它们分别补充 CORE-01 的名称/
退市/代码历史证据、CORE-03 的停复牌事件证据和 CORE-06 的 ST/特殊处理及停复牌
状态证据；不闭合 CORE-02、CORE-04 或 CORE-05。绑定报告见
`../../reports/data-governance/resset-r0-core01-core06-arrival-intake-20260802-v2/`，
供应商在线字典与使用权只读证据见
`../../reports/data-governance/resset-vendor-dictionary-evidence-20260802-v1/`。
这些候选仍为 `INSPECTION_ONLY/NOT_ADMITTED`、`formal_effect=NONE`；Stage A 0/4、
CORE-01～06 正式准入 0/6、完整 CORE 0/20 和 R0/R1/R2 `NOT_MET` 均未改变，
`datasets.json`、`planned_datasets.json`、质量状态和正式快照均未修改。

2026-08-04 的 Downloads 归并进一步封存了 RESSET `DIST` 分配包、6 个公司行动包和
1 个高频说明文档包。本地 RESSET 因而共有 11 个数据包和 1 个说明包，其中
`COMNMHIS`、`DELINFO`、`STKSPCTRMT`、`SUSPNSNRESMPTN`、`DIST` 五张表已形成
隔离候选；`SISSUA_EXT`、`APLA`、`COMPDIVIDENDPROG_EXT`、`DIVIDEND_EXT`、
`DIVIDENDOBJECT`、`DISTDIVSHR` 六张表只完成原始封存和 schema 审计，仍待严格
解析。七张公司行动表合计 614,977 行，29 行宽异常中 1 行必须继续隔离。C 盘
Downloads 的 12 个已验哈希原件已移入未清空的 Windows 回收站。候选源登记见
`contracts/source_admission_v1/current_source_registry.json`，回执与缺口矩阵见
`../../reports/data-governance/resset-downloads-consolidation-cleanup-20260804-v1/`。
这些变化仍为 `INSPECTION_ONLY/NOT_ADMITTED`；`datasets.json` 未写入，正式 CORE、
R0/R1/R2 和研究读取状态均未改变。

2026-07-26 隔离容量 smoke 的 35 项单主盘 1.17 / 3.31 / 9.49 TB 仍只表示含
多来源、全球数据、快照和工作峰值的低/基准/压力场景，不是当前结构化日频库预测。
2026-07-30 实测 `tmp/local-dataset-build/` 约 3.546 GiB，审计文本候选与逐行
证据工作区约 0.672 GiB，33 包受控封存 raw 约 1.898 GiB；指数成份变动与指数
基本信息候选合计约 2.064 GiB。C/F 的 33 个临时原件已删除，G 盘总容量
453.853 GiB、空闲约 368.793 GiB。此前口径只保留为历史测量。
近期结构化 A 股日频核心按 20—60 GiB
持久、50—120 GiB 含 raw/修订/若干快照的工作区间逐批复测；新闻正文/PDF、分钟、
tick/L2 和全球全股票历史启用前，空间不是当前阻断。本轮未修改 `datasets.json`，
未创建正式 raw/curated/snapshot，也未取得任何来源准入。

`datasets.json` 是工作区级“目录的目录”，负责回答：数据在哪里、谁负责、是否可改、质量和许可是什么、最高可用于什么阶段、哪些项目可以消费。项目自己的 manifest、provenance、SQLite/DuckDB catalog 继续保存更细的行数、哈希、字段和运行证据。

Codex、Agent 和项目程序通过 `dataset_cli.py` 解析精确数据身份，不需要记忆物理路径。这样既能统一发现与治理，也不破坏不可变哈希链和历史运行。

## 统一读取入口

```powershell
.\data\catalog\qdata.cmd list
.\data\catalog\qdata.cmd describe cfa-g1-research 20260719-v22
.\data\catalog\qdata.cmd resolve cfa-g1-research 20260719-v22 --intent research --consumer capital-flow-attribution
```

完整用途规则见 `ACCESS_POLICY.md`。解析器只读，不修改目录、数据或 manifest。两个旧 Qlib 数据本体已删除并从活动目录移除，因此 `qdata` 对这两个身份返回“未登记”；其历史信息位于 `retired_datasets`、保留的 manifest/provenance 和删除回执。

## 权威顺序

同一字段出现冲突时，按以下顺序判断：

1. 冻结快照自己的 manifest / provenance；
2. 项目级数据目录或运行 manifest；
3. 本目录的 `datasets.json` 工作区索引。

工作区索引不得覆盖更底层的事实，只能引用和汇总。

## 文件

- `datasets.json`：数据域、数据集和发布边界的统一登记表；运行和研究前先查这里。
- `factors.json`、`factor_registry.schema.json`：统一因子元数据目录及其结构合同；按 `source/namespace/source_key` 防止同名覆盖，只描述定义、依赖和可计算状态，不保存因子历史值，不替代数据准入与研究运行 manifest。
- `DATABASE_MANUAL.md`：工作区数据库总说明书，解释现状、权威顺序、目标存储和完整多因子覆盖。
- `MAINTENANCE_RUNBOOK.md`：每日、事件、月度、季度维护流程，以及自动化与人工批准边界。
- `SOURCE_POLICY.md`：权威来源、开源适配器、多源校准、覆盖和许可边界。
- `DATA_QUALITY_POLICY.md`：不确定、缺失、冲突、异常和失败数据的标准处置。
- `planned_datasets.json`：计划数据集组合；只用于排期和决策，不代表数据存在，不可由 `qdata` 解析。
- `contracts/g_data_core_v1/`：`L1-D3A` 已冻结的 20 项核心数据字段、schema、来源、许可、PIT、修订和质量阻断合同；当前 27 个来源、45 张逻辑表、588 个含继承字段定义，仍不代表数据已下载。
- `contracts/source_admission_v1/current_source_registry.json`：当前候选来源状态覆盖表；保留 D2A-01 历史 runner 合同不变，也不授权下载、正式准入或研究读取。
- `contracts/atomic_publish_v1/`：`L1-D12A-01` 的五文档快照哈希、严格请求、staging、commit plan、catalog 原子可见性、绑定恢复和清理状态合同；2026-08-04 工作区受控发布器已用单次精确授权完成首个 `R0_SHSZ_PV` 正式发布，其他数据不得据此自动晋级。
- `contracts/adapter_gate_v1/`、`adapter_gate.py`：适配器样本宽度、一对一 raw hash lineage、单位、字段类型/日期、主键、NULL、有限数、正/非负约束和 OHLC 门；`source_admission=false`，不能证明转换语义。
- `snapshot_contract.py`：供发布器、目录校验器和 `qdata` 共用的严格 JSON、完整合同哈希和深度 payload 校验实现。
- `dataset.schema.json`：登记表的结构合同。
- `ACCESS_POLICY.md`：Agent/Codex 和项目程序的用途、消费者与只读边界。
- `dataset_cli.py`、`qdata.cmd`：统一只读发现和路径解析入口；`.cmd` 不受 PowerShell 脚本执行策略影响。
- `../../control/tools/validate_dataset_catalog.py`：零第三方依赖的目录校验器。
- `../../control/tools/validate_core_data_contract.py`：零第三方依赖的核心合同校验器。
- `../../control/tools/validate_planned_dataset_catalog.py`：零第三方依赖的 52 项计划、分组、唯一身份和完成门校验器。
- `../../control/tools/atomic_snapshot_publish.py`：隔离合成夹具的原子发布/恢复器；不支持真实来源采集。
- `../../control/tools/validate_adapter_sample.py`：只读适配器样本门 CLI；PASS 仅为 `SMOKE_PASS__NO_SOURCE_ADMISSION`。
- `contracts/local_download_build_v1/`：已下载 CSV/ZIP 的候选接入 schema、CSMAR intake manifest、真实 current-pilot request、D2A-02 五年日行情 request 和未验证的全字段参考模板；不含凭据、不联网、不写正式 catalog。
- `contracts/local_daily_update_v1/`：已经标准化的本地 CSV/JSONL 增量候选合同；默认 PLAN 不写，显式执行也只能写 `tmp/local-daily-update/`，不是来源下载器、正式发布器、catalog 写入器或调度器。
- `contracts/local_daily_orchestration_v1/`：本地候选日更的持久任务账本、水位线、单写者租约、最早缺口 replay 与失败关闭合同；不运行 worker、不创建调度。
- `contracts/schema_evolution_v1/`：schema diff、版本分类和 `field_coverage_start` 合同；只允许 inspection 判定，不自动迁移正式数据。
- `contracts/revision_event_ledger_v1/`：revision/vintage/迟到/撤回/恢复的严格 PIT 事件合同与 replay 夹具；不代表真实来源修订链已闭合。
- `contracts/ricequant_capability_probe_v1/`：米筐独立运行环境与未来最小 live 能力探针合同；当前只完成 RQSDK/RQData 离线安装和验证，禁止凭据落盘、批量数据、正式准入、因子、回测和交易。API 广度只有与 PIT、修订、动态股票池、ETF 边界、权利、稳定性和不可变导出证据共同通过后，才可提出来源晋级。
- `../../control/tools/run_rqdata_shsz_daily_update.py`、同名 `.cmd`：`R0_SHSZ_PV` 的真实 RQData 沪深日增量候选更新器；自动运行只写 quarantine，单次最多 5 个交易日/35 MiB，正式 catalog 晋级仍需独立原子发布。
- `../../control/tools/build_downloaded_dataset.py`、`qdata_build_downloaded.cmd`：默认无写入 PLAN，显式 `-Execute` 才在 `tmp/local-dataset-build/` 生成不可覆盖的 `INSPECTION_ONLY` 候选。
- `../../control/runtime/data-engineering/`：CPython 3.11 / Windows AMD64 的数据工程精确版本锁与中央 `.venv`；44 个锁定包已通过，仍不是 wheel 哈希锁。
- `../../control/runtime/ricequant-rqsdk/`：与中央数据工程环境隔离的米筐专用 CPython 3.11 运行时；当前 33 个包锁定并已用于受控 RQData 采集，但认证值不在工作区持久化，运行时存在不等于其他端点或数据族准入。
- `../../control/tools/run_data_capacity_pipeline_pilot.py`：只写 `tmp/data-capacity-pipeline-pilot/` 的可达性、编码和本地机制 smoke；不发布数据。

## 数据类别

| 类别 | 含义 | 当前例子 |
|---|---|---|
| `immutable_snapshot_store` | 冻结数据快照，禁止覆盖 | 中央 `data/market/snapshots`；当前无新合规行情快照 |
| `published_feature_store` | 通过发布合同的跨项目特征快照 | `data/published`；当前为空 |
| `shared_run_store` | 项目隔离的采集与研究运行 | Capital Flow Attribution |
| `immutable_research_evidence_store` | 研究运行及其证据，不等于可复用数据 | 多因子 `research_runs` |
| `application_state_store` | 应用持续写入的工作数据库 | News Signal Agent SQLite |
| `experimental_cache_store` | 实验线路的可变缓存 | CWOPT `data/history` |
| `historical_output_store` | 已退役流程的历史输出 | 旧资金流 Skill 运行 |
| `temporary_run_store` | 冒烟或临时运行 | `tmp/capital-flow-attribution` |

## 当前清单

| 数据身份 | 所有者 | 状态 | 用途上限 |
|---|---|---|---|
| `cfa-eastmoney-capital-flow-smoke@2026-07-08-20d-4-symbols` | 资金流 | 历史 / PARTIAL | `SMOKE_ONLY` |
| `cfa-legacy-skill-smoke-run@2026-07-08` | 资金流 | 历史 / PARTIAL | `DO_NOT_USE` |
| `cfa-s1-pilot-intermediate@20260718-v1-v9` | 资金流 | 失败证据 | `DO_NOT_USE` |
| `cfa-s1-pilot@20260718-v10` | 资金流 | G1 REVIEW | `RESEARCH_ONLY` |
| `cfa-s1r-pilot@20260719-v11` | 资金流 | G1 REVIEW | `RESEARCH_ONLY` |
| `cfa-s1r-akshare-adapter-smoke@20260719-v3` | 资金流 | 临时冒烟 | `SMOKE_ONLY` |
| `cfa-g1-research-failed@20260719-v12-v21` | 资金流 | 失败证据 | `DO_NOT_USE` |
| `cfa-g1-research@20260719-v22` | 资金流 | G1 REVIEW | `RESEARCH_ONLY` |
| `news-signal-agent-state@working-2026-06-27` | 新闻 | 可变工作库 | `ENGINEERING_ONLY` |
| `cwopt-history-cache@working-2026-07-14` | CWOPT | 未治理缓存 | `ENGINEERING_ONLY` |
| `cwopt-router-cache@working-2026-07-19` | CWOPT | 未治理路由/特征缓存 | `ENGINEERING_ONLY` |
| `stock-relative-dominance-cache@working-2026-07-19` | 相对强弱（禁用） | 临时缓存 | `DO_NOT_USE` |
| `news-release-mirror-db-copy@2026-06-28` | 工作区归档 | 非权威旧数据库副本 | `DO_NOT_USE` |

完整路径、manifest、质量、许可、消费者和限制以 `datasets.json` 为准。

退役但保留审计身份：`cn-a-share-qlib-daily@2026-07-14`、`@2026-07-18`。二者只在 `retired_datasets` 中登记，不能由 `qdata list|describe|resolve` 当作实际资产读取。

## 使用规则

1. 新数据在被任何项目读取前，先登记 `dataset_id + snapshot_id`、物理路径、所有者、许可、质量、可变性和用途上限。
2. `RESEARCH_ONLY`、`SHADOW_ONLY`、`LIVE_READY` 禁止使用模糊的 `latest`；调用方必须钉住具体 `snapshot_id`。
3. `DO_NOT_USE` 只能用于故障取证、回归测试或历史复盘。
4. `SMOKE_ONLY` 只能验证字段、缺失处理和管道安全，不能形成投资结论。
5. `mutable` 数据库或缓存不是冻结快照；研究需要使用时，应先导出带时间戳和 manifest 的不可变快照，再登记新记录。
6. Capital Flow Attribution 只有通过 G3 的派生特征才能发布给多因子；新闻事件库也必须先形成有 schema、`available_at`、许可和版本的冻结特征快照。
7. CWOPT 保持独立线路；其 `data/history` 当前只是实验缓存，不自动进入主线。
8. 不直接编辑既有冻结 manifest、provenance 或内容哈希；变更只能追加新快照。
9. 新共享市场快照进入 `data/market/snapshots/<dataset_id>/<snapshot_id>/`；跨项目发布特征进入 `data/published/<domain>/<dataset_id>/<snapshot_id>/`。
10. 统一 CLI 的解析成功只代表身份、用途与路径检查通过，不代表 `quality_status=OK`，也不替代项目阶段门。
11. `planned_datasets.json` 中的条目不得被手工拼路径或直接用于研究；只有完成落盘和准入、进入 `datasets.json` 的快照才是实际资产。
12. 每日更新先产生不可变 delta；月度 compaction 只合并小文件，不把日频聚合为月频。旧快照引用的分区不得因合并而自动删除。

## 常用命令

在工作区根目录执行：

```powershell
& '${USER_PROFILE}\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  -X utf8 .\control\tools\validate_dataset_catalog.py
```

校验通过只表示目录结构和引用自洽，不表示数据质量阶段门通过。

完整计划一致性校验：

```powershell
& '${USER_PROFILE}\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  -X utf8 .\control\tools\validate_planned_dataset_catalog.py
```

该命令只读，锁定研究基础 28 项、必建数据库 35 项和全域 52 项的分组关系；通过不表示实际数据已经落盘。

## 新数据最小登记项

追加记录时至少填写：`dataset_id`、明确的 `snapshot_id`、`store_id`、所有者、物理路径、生命周期、可变性、许可、质量、用途上限、authority、消费者和已知限制。若是冻结快照，同时记录内容哈希与算法；若来自可变库，先导出新目录并生成 manifest，不能把“工作库当前状态”冒充快照。
