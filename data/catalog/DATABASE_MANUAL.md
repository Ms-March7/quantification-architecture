# Quantification 数据库说明书

更新时间：2026-08-06  
状态：`L1_COMPREHENSIVE_DATA_ESTATE_DESIGN`  
目标：为完整 A 股多因子研究及其必需的全球环境、港股和全球关联个股建立统一、可追溯、PIT 安全的数据资产体系；不是只满足一次回测的最小数据集合。

## 1. 这里的“数据库”指什么

本工作区不是、也不应被改造成一个巨大的单体数据库。“数据库”是以下五类资产组成的受控体系：

1. **不可变数据湖**：Parquet、原始文件、manifest、provenance 和质量报告；它是未来新增研究数据的事实主体。
2. **研究读取投影**：Qlib 二进制等面向训练和回测的只读格式；它是可重建投影，不是完整来源事实。
3. **查询层**：DuckDB 文件或视图；用于跨 Parquet 查询，不拥有已经存在于 Parquet 的第二份事实。
4. **应用状态库**：SQLite；保存采集、工作流、缓存和人工操作状态，不能直接冒充研究快照。
5. **治理目录**：`datasets.json`、项目 manifest 和发布 catalog；记录数据身份、位置、版本、许可、PIT、质量和用途上限。

因此，“统一管理”不等于强行把所有文件塞进同一个 `.db`，而是统一身份、合同、入口和生命周期。

### 2026-08-06 B 线候选维护当前覆盖口径

统一候选维护已按“沪深价量 → B 路径 → RQData 财务 PIT → 本地 JoinQuant 股本 PIT 幂等验证 → 中央候选索引”五步完成真实 `--run`，总状态 PASS。财务队列权威交易状态为 14/14 完成，覆盖 5,202 个唯一证券、`revenue/net_profit` 两字段和 12 个季度，封存 131,777 条原始观察并机械生成 263,554 条本地修订事件；已完成队列在导入 RQData 客户端前返回 `NO_CHANGE__QUEUE_COMPLETE__NO_NETWORK`。状态文件为 `data/market/transactions/rqdata-financial-pit-daily/rqdata-financial-pit-20260805-v1/state.json`。

本地 JoinQuant `STK_CAPITAL_CHANGE` 已机械构建 196,693 个日期级股本 PIT 半开区间。源哈希和正式 R0 日历绑定未变且不可变候选已提交时，维护步骤只验证 manifest、`COMMITTED.json`、四项 artifact 与报告绑定并返回无网络 `NO_CHANGE`；本地源缺失明确 `SKIPPED`，源漂移或不可变输出不完整则失败关闭，既有输出不得覆盖。中央 `data/catalog/candidate_assets.json` 当前为 28 个 quarantine 顶层候选根、107 个 evidence JSON、82 个 manifest；正式 `datasets.json` 未变。上述财务与股本资产全部保持 `INSPECTION_ONLY/NOT_ADMITTED`，本地保守 `available_at`、本地修订/状态链和供应商原样单位均不等于来源级 PIT、完整修订、正式单位裁决或来源准入。

本次 B 线候选维护不改变 canonical R0/R1/R2、CORE-01～20 正式计数、Validation 或 Holdout；正式研究仍只能读取已明确发布并获准的精确 `dataset_id + snapshot_id`。任何下文仍出现的财务 0/14、索引 26/92/67 或三步维护描述，均是 2026-08-05 及以前的历史回执口径，由本节覆盖，不得解释为当前状态。

### 2026-08-04 当前正式入口

工作区已新增首个合格窄门面板：`cn-a-share-r0-shsz-pv-panel@20050104-20260803-v1`，内容哈希 `26309d36b7c08096fff55df100f1d27df8412b106ea48f282ce0f7caad37084b`。它位于 `data/market/snapshots/`，登记为 `RESEARCH_ONLY/OK/immutable/frozen`，覆盖沪深普通 A 股 2005-01-04 至 2026-08-03 的未复权价量、已实现总收益和成交仿真掩码。精确 qdata、正式 PanelData smoke 与 v2 深验收均通过。

这是独立 `R0_SHSZ_PV`，不是 CORE-01～06 的替代品：北交所、基本面、行业、ETF 穿透和全球环境不在范围内，canonical R0/R1/R2 继续未达到。RQData 价量与 B 路径更新器现由工作日 19:20 的统一候选维护任务顺序调用，随后刷新中央候选索引；旧分立任务已禁用。自动更新不等于自动正式发布，新日数据必须另行质量复核和原子晋级。

候选资产由 `data/catalog/candidate_assets.json` 集中索引：截至 2026-08-06 当前为 28 个 quarantine 顶层候选根、107 个 evidence JSON、82 个 manifest。该索引只承担发现、完整性指针和维护编排，不拥有业务事实，也不进入 `qdata` 正式解析；其 `formal_effect=NONE`。

2026-08-05 又在两个独立 quarantine 根中封存米筐 R1 参考候选：中信 2019 一级行业包含 30 个行业、9,222 条成员区间和 5,496 个唯一证券；指数/市场参考候选包含 7,385 条当前指数主档、8 个核心指数的 28,250 条 2005-01-04—2022-01-28 日行情，以及 4,274 条中债国债收益率曲线。2026-08-03 交叉核验中，窄门面板的 5,201 个证券全部具有一个候选一级行业连接。上述结果只关闭字段、覆盖、身份连接和保存机械问题：历史 `available_at`、修订链、申万研究主口径、完整指数家族、许可复核和正式原子快照仍未闭合，因此它们保持 `INSPECTION_ONLY/NOT_ADMITTED`，未进入 `datasets.json`，不能据此称 CORE-09/11/12/19 或 R1 完成。

同日又完成 RQData B 路径日增量候选更新器。首次真实运行已证明当前权限可取得沪深证券主档、ST/停牌/涨跌停、申万 `sws` 行业，以及沪深300、中证500、中证1000、科创50和创业板指的成分与权重；两日候选的行业覆盖率为 100%。该组件现由统一 19:20 任务调用，原 19:10 任务已禁用。该结果纠正“米筐不提供行业/指数历史”的过宽判断，但只证明所测端点、日期和配置指数可用，不证明全部分类版本、全部指数家族或全历史覆盖。更新器仍只写 quarantine；首次运行只形成财务队列，队列后续 14/14 完成状态由上方 2026-08-06 当前口径记录，但正式财务账本、catalog 晋级和不可变研究快照仍未执行。

财务 PIT worker 随后完成工程与极小真实诊断：`get_pit_financials_ex(statements="all")` 可返回多公告日记录，5 证券样本机械生成 38 条本地修订事件；该早期诊断完整进程消耗 3,327,550 B，且低流量的 `get_forecast_report_date` 在当前权限下被拒绝，因而当时没有直接调度 400 证券批次。后续采用 50,000 行硬上限已完成 14/14 队列，但历史财务 PIT 的正式主线仍是把米筐多版本及 `info_date`、聚宽财务报表的 `pub_date/report_date`、本地 CSMAR 数值和更正记录合并为逐字段事件账本；日级研究可把无时分秒的公告日保守映射到下一交易日 09:30，审计级原始时点与已覆盖旧版本仍需官方公告档案。

2026-07-28 执行的 `L1-D2A-02` 是正式建库前的受控来源 Pilot，不改变上述
对象模型。其 2020—2024 五年日行情候选后来在 D2A-04B 中扩展为 1990—2026
八个真实分段包的只读去重投影；所有候选仍保存在 `tmp/local-dataset-build/`，
不得被当成正式事实湖或研究输入。有限 D2A-04 Phase A 已完成轻量候选工程，
但完成门未通过并已暂停；D2A-04C 又完成 33 包本地结构/PIT 审计和两张审计文本
零丢失候选闭合，但它同样不是来源准入。后续仍必须关闭 CORE-01/02/03/04/06 的
真实来源、身份、日历、公司行动、修订和历史状态缺口。D2B 只有在该完成门通过并
重新明确启动后，才可申请把精选 CORE 数据原子发布到 `data/market/` 并登记
`datasets.json`。

用户于 2026-07-28 启动了
`L1-D2A-04B_OFFICIAL_IDENTITY_CALENDAR_ACTION_STATUS_SOURCE_CLOSURE`。早先一次 CSMAR
门户预检因 `TRD_Dalyr` 表页显示“无权限”而按硬停止暂停；其
`PAUSED__CSMAR_FULL_TABLE_PERMISSION_BLOCKED__NO_NEW_DATA__INSPECTION_ONLY` 和
0 业务请求、0 下载回执是历史预检事实，继续保留。后续在清华机构会话中确认表级
权限，用户于 2026-07-29 扩大结构化低频候选收集。当前已构建 `TRD_Dalyr`
1990—2026 分段全历史、`TRD_Co`、`TRD_Cale`、`TRD_Cptl`、`TRD_Capchg`、
`TRD_NoLimit` 和 `TRD_AdjustFactor` 候选；普通及银行/保险/证券财务表、审计统计
与审计文本现为 18 张、2,296,290 行、当前权威候选 803,059,261 B。33 包整体形成
32 个候选和 1 个重叠校准包。表头、类型、基础键、分区与 payload 哈希机械检查通过；
两张审计文本 v2 还把源 ZIP/member、parser、repair manifest、逐行 sidecar、修复
CSV 与 Parquet content hash 完整绑定，并通过双向行级等价检验。旧 v1 保留但已被
v2 取代。以上仍只有报告期和差错更正披露日期，没有初次披露时间、更正前值或完整
修订链。权限、文件到达和机械通过都不自动闭合 local retention、内部研究共享权、
财务 PIT 或任何 CORE 数据族。闭合证据见
`../../reports/data-governance/csmar-33pkg-local-closure-20260729/`。

2026-07-29 继续搜索财务表时，清华图书馆代理的 CSMAR 资源路由返回“访问违规”、
代码 `27`，并明确显示解除时间为 2026-07-30 00:00。该 CSMAR 路由的浏览器请求
随即停止；不得并发重试、换账号或绕过限制。用户随后确认其他图书馆数据库仍可正常
使用，因此不能把该事件扩大解释为整个电子资源代理停用。已经下载的 CSMAR ZIP 只做
离线处理；审计意见文本内容已候选化，但其首次公开时间、报告 ID 与修订链仍未闭合。
尚未取得的报表公布日期、普通公司间接法现金流量表和其他外部缺口现属于 2026-07-30
用户启动的有界 D2A-04B 来源闭合范围；每个来源仍须按权利、导出、PIT 和留存合同
单独核验，不授权付费、新密钥或全历史批量回补。

同日对图书馆其他经济金融数据库完成了只读详情 census。已确认 RESSET 企业大数据
覆盖工商、投融资、企业信用、司法、知识产权、招投标、标准、产品、土地、人才等
14 类；中国微观经济查询系统包含工业企业、海关企业、创新企业和绿色发展四库；
CVSource 覆盖私募股权、战略投资和并购；Capital IQ 的说明覆盖全球上市/非上市
公司、PE、资管、并购、固收和高管。它们可能形成企业、交易、信用、创新/绿色和
治理网络等 52 项之外的候选。相反，World Trade 当前说明主要是贸易政策新闻/法律/
文档，巨灵是 1998 年以来财经报刊和研报全文，不能冒充数值贸易流或结构化证券核心
源。本轮 census 没有下载、没有修改 52 项范围，见
`../../reports/data-governance/library-economic-financial-source-census-20260729/`。

2026-08-02，RESSET 的 `COMNMHIS`（公司名称变更历史）、`DELINFO`（证券/退市及
代码信息）、`STKSPCTRMT`（ST、*ST、退市整理等特殊处理）和
`SUSPNSNRESMPTN`（停复牌）四个人工导出包已封存到
`../market/quarantine/resset-manual-export/intake-20260802-04pkg-v1/`。四个隔离候选
分别为 9,778、8,936、4,105、81,535 行，候选 manifest 位于
`../../tmp/local-dataset-build/resset-comnmhis-candidate/`、
`resset-delinfo-candidate/`、`resset-stkspctrmt/` 和 `resset-suspnsnresmptn/`。
它们为 CORE-01、CORE-03、CORE-06 提供实质候选增量，但不替代永久 security_id、
三所完整官方历史、PIT/修订链和版本化规则；`STKSPCTRMT` 的 73 条歧义尾部与
`SUSPNSNRESMPTN` 的 339 个 `1900-01-01` 恢复日期 sentinel 继续保留为待解释事实，
不得自行改写或填 0。CORE-02、CORE-04、CORE-05 没有因此闭合。完整绑定和失败关闭
结论见 `../../reports/data-governance/resset-r0-core01-core06-arrival-intake-20260802-v2/`；
供应商使用权、字段含义和覆盖边界的只读证据见
`../../reports/data-governance/resset-vendor-dictionary-evidence-20260802-v1/`。
全部资产仍为 `INSPECTION_ONLY/NOT_ADMITTED`、`formal_effect=NONE`；Stage A 0/4、
CORE-01～06 正式准入 0/6、完整 CORE 0/20、R0/R1/R2 `NOT_MET` 均不变。本次没有
正式 catalog 写入、质量晋级、快照发布或研究读取。

同一执行包下，上交所和深交所部分官方证据已经封存；北交所请求在 WAF/HTTP 403 处
按门停止，没有接受业务数据。全部新证据仍为 `INSPECTION_ONLY/NOT_ADMITTED`，
正式准入仍为 0，D2B 继续阻断；购买、试用和付费端点仍不在授权内。

2026-07-30 对 CORE-01/02/03/06 执行的旧 selected-core combined 审计历史结果为
0/4，16 个 BLOCK 门为 `PASS=1 / FAIL=12 / UNKNOWN=3`。后续架构复核确认其
`formal_closure`、`formal_catalog_identity`、`qdata_research_resolution` 都是发布后
检查；旧审计因此重分类为 `POST_ADMISSION_AUDIT`。新的非循环 Stage A 直接检查权利、
语义、PIT、修订链、官方交叉证据、完成证据、开放合同阻断与质量门，当前仍为 0/4
ready，CORE-01/02/03/06 分别有 11/9/12/11 个阻断；Stage B 为
`BLOCKED__PHASE_A_NOT_READY`。没有写正式 catalog、晋级质量或发布快照。D2A-04B
有界来源闭合现已授权且活跃；真正的 D2B 正式快照构建仍被 Stage A 与 D2A-04 完成门
阻断，且需在二者通过后取得精确哈希绑定的独立发布授权。

CSMAR Python API 个人认证通过但数据 entitlement 被拒绝，不能用于日更。本地每日
增量候选引擎新旧 14/14 测试通过，可对已经标准化的本地 CSV/JSONL 做严格 schema、
主键、日期、PIT 修订、幂等、自包含分区合并、同卷原子封口和失败 quarantine；它仍是
`INSPECTION_ONLY` 内核，不是下载器、真实来源适配器、正式发布器或调度器。公开官方
API 预检 4/7 结构通过；四源无密钥检查适配器 fixture 4/4 通过，一次有界 live 中
BIS、美国财政部和 HKMA 3/4 结构通过，ECB 本机 TLS EOF 已失败关闭并隔离，未重试。
成功响应只保存 raw、哈希、标准化检查视图和未闭合的 rights/PIT 回执，全部仍未正式
准入。此前清华代理 code 27 事件只作历史回执保留。

同日 CORE-02 官方日历/时段闭合候选取得 20/20 官方响应，生成沪深
2015-01-01—2026-07-28 共 8,454 条 venue-date、28 条年度/修订公告 lineage 和
12 条 SSE/SZSE 时段记录；最终失败关闭门状态为 Q1 FAIL、Q2 FAIL、Q3 UNKNOWN、Q4 PASS；
早期机械子检查不关闭 Q3。北交所、三所完整
临时/应急停市与补充公告档案、沪深更早时段版本链、官方 MIC 和日内发布时间精度仍未
闭合，因此不能钉住正式 CORE-02。

当前有界范围包括官方永久身份/代码历史、官方日历/会话、公司行动条款与修订链、历史
交易状态与规则版本，以及经用户人工导出的 CSMAR 结构化低频核心：日行情、公司、
日历、公司行动、股本、复权、可交易性和 PIT 财务事实/公布日期/审计意见。任何恢复
批次仍须分层保存 raw、receipt、哈希、PIT/修订、来源权利和 quarantine 证据，且
不包括正式准入、catalog 写入、质量晋级、研究输入、付费、凭据、新闻或高频。

`L1-D2A-03` 已执行并以 `PARTIAL` 暂停：深交所/上交所 PIT 小样本和巨潮校准
成功，北交所业务请求合同未闭合；没有钉住 CORE-02，因此日期级记录没有生成
`available_at`。CSMAR 财务微型包因门户字段交互超时未生成。16 证券切片只保留
CORE-03 源观测，其余为 quarantine 或零输出。该包仍只写 `tmp/` 和报告，不是
D2B、正式准入或因子研究。

## 2. 截至 2026-07-30 的现有资产

### 2.1 总体情况

- 工作区目录登记了 **13 个 store、14 个可解析数据身份和 2 个退役 tombstone**；其中独立 `R0_SHSZ_PV` 面板 1 个达到 `quality_status=OK`，其余项目数据不因此晋级。
- 实际研究数据仍分布在中央项目隔离根和项目工作目录；`qdata` 已能用 `dataset_id + snapshot_id` 统一解析活动资产，退役 tombstone 不可解析。
- 中央 `data/market/snapshots/` 和 `data/published/` 已建立规则，但尚没有新正式共享快照。
- `data/` 当前约 **0.129 GiB、1,539 个文件**；其中 CSV/Parquet 约 **161 个、63.6 MiB**。大量内容是临时运行、退役 Skill 输出或项目内部证据，不应因扩展名相同就自动当成共享数据。

### 2.2 物理数据库与数据湖清单

| 资产 | 物理位置 | 实现 | 当前性质 | 结论 |
|---|---|---|---|---|
| A 股日频快照 2026-07-14 | 原 `多因子/data/market/snapshots/2026-07-14/qlib_bin` | Qlib 数据本体已删除 | tombstone + manifest/provenance | 原 6,118 个特征标的；不能按原路径复现 |
| A 股日频快照 2026-07-18 | 原 `多因子/data/market/snapshots/cn-a-share-qlib-daily/2026-07-18/qlib_bin` | Qlib 数据本体已删除 | tombstone + manifest/provenance | 原 6,119 个标的；不能按原路径复现 |
| 多因子发布目录 | `多因子/quant_data_core/catalog.sqlite3` | SQLite | 两条不可变 release 登记 | 只登记 release 元数据，不存放完整行情事实 |
| 资金流 v11/v20/v21/v22 | `data/capital-flow-attribution/...` | 原始文件 + Parquet + DuckDB | 项目隔离、不可变运行 | Parquet 是事实层；4 个 DuckDB 文件只是只读视图/查询入口 |
| 新闻工作库 | `news-signal-agent/data/news_signal_agent.sqlite3` | SQLite | 可变、`ENGINEERING_ONLY` | 保存消息、事件、证据、工作流等应用状态；研究前必须导出冻结快照 |
| CWOPT history/router | `CWOPT相关/CWOPT/data/...` | 可变缓存/实验数据 | `ENGINEERING_ONLY` | 外部协作旁线，不自动进入主线 |
| 新闻发布镜像旧副本 | `archive/data-duplicates/.../news_signal_agent.sqlite3` | SQLite | 历史副本、`DO_NOT_USE` | 仅用于检查和追溯 |
| 共享输入根 | `data/market/snapshots/` | 目录合同 | 当前为空 | 未来新合规来源快照的默认入口 |
| 跨项目发布根 | `data/published/` | 目录合同 | 当前为空 | 只接收通过发布合同的派生特征 |

### 2.3 已核对的库结构

`多因子/quant_data_core/catalog.sqlite3` 包含 `catalog_meta` 和 `releases`；release 记录含数据身份、来源层级、频率、覆盖、许可、发布时间、可用时间、哈希、PIT 状态、用途上限和 manifest 路径。

`news-signal-agent/data/news_signal_agent.sqlite3` 当前包含 `raw_messages`、`events`、`event_cards`、`event_evidence`、`retained_events`、`rank_history`、`workflow_artifacts`、`user_overrides`、`notification_log` 和 `llm_cache`。它通过 SQLite `quick_check`，但“文件可读”不等于数据已经可用于研究。

Capital Flow v22 的 DuckDB 暴露保证金、ETF 规模、互联互通、外汇参考、来源覆盖和回测证据等 Parquet 视图。DuckDB 文件不应被复制成新的权威库；权威事实仍由运行 manifest、原始证据和 Parquet 共同确定。

## 3. 权威顺序与统一读取

同一事实冲突时，按以下顺序处理：

1. 不可变快照自己的 manifest、provenance、内容哈希和原始来源证据；
2. 项目级数据合同、运行 manifest 或 release catalog；
3. `data/catalog/datasets.json` 的工作区汇总；
4. 人工说明、目录名和代码默认值只能作为线索，不能覆盖以上事实。

Codex、Agent 和项目程序只通过以下入口发现实际数据：

```powershell
.\data\catalog\qdata.cmd list
.\data\catalog\qdata.cmd describe <dataset_id> <snapshot_id>
.\data\catalog\qdata.cmd resolve <dataset_id> <snapshot_id> --intent research
```

`planned_datasets.json` 只表示计划，不可解析、不可读取、不可作为回测输入。新数据只有在实际落盘、生成 manifest、完成质量审查并登记进 `datasets.json` 后才存在。

## 4. 目标存储架构

```text
外部来源
  └─ raw：只追加原始响应、文件和抓取证据
       └─ curated：标准化、不可变 Parquet 事实分区
            └─ snapshot：引用分区哈希的 manifest + provenance + quality
                 ├─ Qlib：日频/分钟研究读取投影
                 ├─ DuckDB：跨 Parquet 只读查询视图
                 └─ published：通过阶段门的派生特征快照
```

数据维护的日增量、月度合并、修订、schema 演化、人工批准和恢复流程见 `MAINTENANCE_RUNBOOK.md`。

### 4.1 标准快照目录

未来新共享输入按以下四层组织；目录在首个获批数据集实施时创建，不提前预建空业务目录：

```text
data/market/
├─ raw/<source>/<dataset_id>/ingest_date=<date>/<run_id>/
├─ curated/<dataset_id>/schema=<version>/year=<YYYY>/month=<MM>/*.parquet
├─ snapshots/<dataset_id>/<snapshot_id>/
│  ├─ manifest.json
│  ├─ provenance.json
│  ├─ partition-manifest.json
│  └─ quality/
└─ quarantine/<dataset_id>/<run_id>/
```

Snapshot 默认只引用不可变分区及其哈希，不复制整个历史库。Raw 可以按日/运行保留；日频 curated 数据每天先写 delta，月末物理合并为月分区。合并只减少小文件，仍保留每一个交易日的行。

跨项目派生特征默认进入：

```text
data/published/<domain>/<dataset_id>/<snapshot_id>/
```

既有冻结 Qlib 和历史运行继续留在登记原路径；除非另有依赖审计、回滚方案和用户批准，不为“看起来整齐”而移动或复制。

### 4.2 推荐格式

| 数据形态 | 主格式 | 辅助格式 | 禁止做法 |
|---|---|---|---|
| 大型表格事实 | 分区 Parquet | Arrow/DuckDB 只读视图 | 把 CSV 或 DuckDB 副本同时当权威 |
| 原始文档/响应 | 原始文件 + 哈希 | JSONL 索引 | 覆盖旧响应或丢失 URL/时间 |
| 研究读取 | Qlib/Arrow 投影 | 内存缓存 | 把投影当成唯一 PIT 来源 |
| 应用状态 | SQLite | 周期性冻结导出 | 直接读取持续变化的 SQLite 做正式研究 |
| 元数据 | JSON manifest/catalog | SQLite release catalog | 使用 `latest` 或仅凭目录名猜版本 |

完整事实表按业务分开：行情、可交易性、公司行动、股本估值、财务、行业和指数各有独立主键及 PIT 合同。只有进入 L2 后，才形成股票 × 日期 × 因子版本的发布数据；不得把因子列写回来源事实表。

### 4.3 分区与键

- 日频行情：按 `trade_date` 或年份分区，主键至少为永久证券身份 + `trade_date`。
- 当前规模的日频 curated 数据优先按 `year/month` 物理分区；每日 delta 定期合并，避免数千个日期目录和小文件。
- 分钟/tick：按交易所、交易日、标的或桶分区；必须先做容量和查询基准。
- 财务与事件：使用稳定证券身份、报告期、公告身份、`revision_id` 和可用时间组成复合键。
- 成分、行业、状态：必须是带生效区间的 PIT 表，不能只保存“当前列表”。
- 新闻/公告：保存来源文档身份、正文哈希、版本关系、发布时间、抓取时间和证券映射证据。

### 4.4 无独立备份与复现边界

用户于 2026-07-26 决定不建设独立数据备份。当前权威数据只在主盘保留一个物理副本，不购买第二备份盘、不配置对象存储备份，也不把备份容量或备份权利作为数据建成条件。

研究复现继续依赖双时间/PIT、只追加 raw、修订链、不可变分区、精确 snapshot manifest、内容哈希以及锁定的代码和配置。历史回测必须按 `available_at`、`ingested_at`、修订版本和 `snapshot_id` 截断，而不是仅遮住较晚的 `trade_date`。Snapshot manifest 默认引用共享不可变分区，不每日复制完整数据库。

该结构可以在主盘完好时精确重建研究投影，但不提供磁盘级容灾。主盘不可恢复损坏后，外部来源重新抓取只能恢复当时仍可取得的内容，不能保证恢复已下架、已修订或来源不再提供的历史版本。此风险已被用户接受，且不得由 Agent 自动升级为备份采购要求。

## 5. 时间与 PIT 合同

完整多因子底座至少统一以下字段：

- `event_time`：事件实际发生时间；
- `effective_date`：规则、分类或成分开始生效的日期；
- `published_at`：来源首次公开的时间；
- `available_at`：研究系统最早可以合法观察到的时间；
- `ingested_at`：本地实际采集时间；
- `revised_at`、`revision_id`：更正或重述链；
- `snapshot_id`：不可变冻结版本。

任何财报、成分股、行业分类、ST/停牌、公司行动、分析师预测、新闻或资金流数据，如果不能回答“当时是否可见”，就不能进入正式多因子研究。只有报告期而没有公告/可用时间的财务表不合格。

## 6. 完整多因子目标数据域

详细机器清单见 `planned_datasets.json`。规划采用八个独立数据门，而不是“先随便凑够 OHLCV”。研究用途另采用四个分层入口；研究层不替代数据门，也不缩减 35/52 项完成目标：

| 研究层 | 必需数据 | 允许的研究范围 | 当前状态 |
|---|---|---|---|
| `L0_INSPECTION` | 无 | 候选审计、字段检查、smoke 和工程验证；不允许因子有效性、组合绩效或投资结论 | `NOT_MET` |
| `R0_PV` | CORE-01～06 | 国内日频价量候选发现；不含经典财务、行业中性、全球环境或完整策略绩效结论 | `NOT_MET`，正式 0/6 |
| `R1_CLASSIC_CN` | CORE-20 | 国内经典价值、质量、成长、规模、动量和行业中性研究 | `NOT_MET`，正式 0/20 |
| `R2_A_SHARE_GLOBAL` | CORE-20 + GCTX-8 | 在 R1 上加入经时区、收盘时点、币种和 `available_at` 校准的全球环境 | `NOT_MET`，正式 0/28 |

`G_DATA_RESEARCH_BASE` 保留为 `R2_A_SHARE_GLOBAL` 的兼容别名。每一层还必须通过对应 L2 的 PIT、防未来信息、幸存者偏差、复权、可交易性、训练/验证/盲测和可重建性门；低层通过不允许读取高层尚未准入的数据。`R0_PV` 若需要相对指数、无风险超额或完整组合绩效结论，必须再钉住相应正式基准输入或升级到覆盖这些输入的层级。

截至 2026-08-01，计划与实际 catalog 的关系是：

| 数据门 | 计划数 | 完整数据库角色 | 字段合同 | 实际合格快照 |
|---|---:|---|---|---:|
| `G-DATA-CORE` | 20 | 国内核心必建 | 20 项合同已冻结，19 个来源绑定仍阻断 | 0/20 |
| `G-DATA-GLOBAL-CONTEXT` | 8 | `R2_A_SHARE_GLOBAL` 及以上必建 | 待建 | 0/8 |
| `G-DATA-HK-EQUITY` | 4 | 完整研究数据库必建 | 待建 | 0/4 |
| `G-DATA-GLOBAL-EQUITY` | 3 | 定义范围全球关联个股必建 | 待建 | 0/3 |
| `G-DATA-FUND` | 4 | 扩展 | 待建 | 0/4 |
| `G-DATA-EVENT` | 7 | 扩展；其中新闻暂停 | 待建 | 0/7 |
| `G-DATA-XASSET` | 2 | 国内衍生品扩展 | 待建 | 0/2 |
| `G-DATA-HF` | 4 | 高频/执行；当前暂缓 | 待建 | 0/4 |

### 6.1 核心组：通过 `G-DATA-CORE` 前必须具备

1. 永久证券主键、代码历史、上市退市、交易所/板块和证券类型；
2. 交易日历、日频未复权 OHLCV/成交额、公司行动和复权因子；
3. ST/风险警示、停牌、涨跌停、退市阶段和每日可交易性；
4. 总股本/官方流通股本、独立可重建的自由流通口径、日频估值和换手；
5. 多提供商指数主数据、指数日行情、价格/全收益/净全收益变体、PIT 成分及权重；
6. PIT 多口径行业分类：证监会行业统计分类作为官方基线，申万行业作为研究主口径，其他合法可得口径独立版本化并保留映射；
7. 三大财务报表、财务指标、单季度派生、业绩预告/快报、审计意见、分红和主营构成；
8. 基准、无风险利率和基本风险暴露输入；
9. 退市证券和历史状态，防止幸存者偏差。

#### 6.1.1 指数完整覆盖合同

指数不是只保存沪深300、中证500等少量宽基。`cn-index-master` 目标是尽量完整登记上交所/中证、深交所/国证、北交所及其他官方或合法公开提供商的指数元数据，覆盖综合/宽基、规模与板块、行业和细分行业、主题与产业链、风格、策略、因子、红利、REITs，以及基金、债券、商品和衍生品中与研究相关的基准。公开发布的价格、全收益和净全收益版本必须区分身份，不得因简称相似合并。

强制覆盖示例包括半导体/芯片、芯片设计、半导体设备、半导体材料、半导体材料设备组合、中证REITs全收益及其他官方行业主题系列。示例不是封闭白名单；新发布指数按相同合同持续追加。指数身份至少使用 `provider_id + index_code + return_variant + currency + methodology_version`。

“尽量完整”分两层验收：第一层对官方可发现指数建立元数据与覆盖状态；第二层对许可允许且官方可得的数据保存最大可验证的日行情、收益变体、成分和权重历史。官方未公开历史成分、许可不允许保存或来源中断时必须标记 `MISSING/REVIEW`，不得用当前成分回填历史或声称完整。

### 6.2 全球环境组：`G-DATA-GLOBAL-CONTEXT`

GCTX-8 与 CORE-20 共同构成 `R2_A_SHARE_GLOBAL`，完整覆盖：

1. 全球主要市场交易日历、会话、时区和夏令时；
2. 全球基准、离岸中国资产和收益变体主数据；
3. 全球主要指数与离岸中国资产日行情；
4. 主要宏观数据的发布日期、初值和修订链；
5. 政策利率、隔夜利率、国债曲线和信用利差；
6. USD/CNY、USD/CNH、HKD、JPY、EUR 和获准美元状态指标；
7. 原油、黄金、铜等中国相关商品和期货合约事实；
8. 波动率、流动性和全球风险偏好指标。

所有序列必须保存当地时区、`published_at/available_at`、当地交易日和对应可用于 A 股决策的交易日；不能把美股当晚收盘按同一自然日期喂给当天 A 股。指数、波动率、商品和离岸市场网页可见不代表获准批量保存，字段级权利仍是准入门。

### 6.3 港股组：`G-DATA-HK-EQUITY`

HK-4 是完整研究数据库必建层：

1. 港股永久证券身份、代码历史、股份类别和退市记录；
2. 全市场及退市证券的港币未复权日行情；
3. 公司行动、交易状态、停牌、除牌、交易单位和可交易性；
4. HKFRS/IFRS PIT 财务、公告、重述和原始披露版本。

港股事实不直接复用 A 股身份、公司行动或会计规则；A/H/ADR 和互联互通关系使用带有效期的映射表。完整建立不绕过 HKEX/指数所有者的数据保存、非展示使用和内部共享许可。

### 6.4 全球关联个股组：`G-DATA-GLOBAL-EQUITY`

GLEQ-3 是完整研究数据库必建层：

1. 中国资产、A/H/ADR 及 A 股关键产业链全球公司的永久身份和版本化映射；
2. 获准研究宇宙的当地币未复权日行情、公司行动输入和正式可用时间；
3. US GAAP/IFRS PIT 财务、官方 filing、修订/重述和字段映射。

这里的“完整”是完整覆盖**已冻结、可审计、带有效期的定义研究宇宙**，不是默认收录全球所有上市公司。定义范围外的全球全股票横截面必须另做用途、来源许可、容量和用户批准。

### 6.5 其他扩展组：独立准入

1. ETF/公募基金主数据、NAV、份额、持仓和流量；
2. 融资融券、沪深港通资格/成交/持仓；
3. 大宗交易、龙虎榜、异常交易；
4. 股东、机构、基金、限售解禁、董监高和重要持有人变动；
5. 分析师一致预期、评级和盈利预测修订；
6. 公告原文、新闻原文、结构化事件和事件可用时间；
7. 资金流项目通过合同发布的可观察特征；
8. 其他获准替代数据和项目发布特征。

### 6.6 国内衍生品组

指数期货、期权、ETF 期权、基差、期限结构、持仓量、隐含波动率和到期/合约主数据。它们主要服务风险、对冲、择时和跨资产特征，不应假装成个股基本面数据。

### 6.7 高频与执行组

1 分钟/5 分钟行情、逐笔成交与委托、L2 盘口、集合竞价、经纪商/QMT 模拟或真实执行回执。该组进入完整总规划，但必须经过“用途合同 → 许可与费用 → 容量估算 → 小样本 → 日频对账 → 人工批准”，不能直接批量下载多年全市场数据。

用户已确认全域数据库不因研究分层或执行顺序而缩减：CORE-01～06 → CORE-20 → GCTX-8 → HK-4 → 定义范围 GLEQ-3 → 基金/事件/国内衍生品/高频，共 52 项。前四个数据门共 35 项必须以真实合格快照完成，不能用计划、合同、低层研究门通过或阻断状态代替；新闻和高频当前暂停、付费数据当前未授权，但均保留明确的重启门，不得从总计划删除。

## 7. 来源层级

1. **官方直接来源**：境内外交易所、证监会、巨潮资讯、中国结算、指数公司、央行/财政部/统计机构和法定披露系统等；优先作为身份、行情、公告、规则、利率、汇率和宏观事实。
2. **开源采集适配器**：AKShare 等用于连接公开端点和扩大覆盖；必须记录真正底层来源，工具开源不等于底层数据可任意传播。
3. **Tushare Pro 等聚合接口**：作为第二来源、历史回补和工程校准；积分、权限、频次、字段历史和再利用边界必须记录。
4. **有合同的供应商**：只在公开来源覆盖、稳定性或 PIT 历史不足时使用；许可、费用和字段口径必须明确。
5. **网页研究回补**：只能在许可和稳定性明确后进入限定研究，不能静默覆盖官方观测。
6. **Qlib 社区数据**：现有两个快照已由用户退役，不作为过渡数据、中央事实源或新的研究输入。

同一字段的来源冲突必须保留为 `CONFLICT/REVIEW`，不能用“优先级更高”作为静默覆盖的理由。

## 8. 哪些可标准化，哪些要用户批准

### 可直接按成熟合同设计

证券主数据、交易日历、日频行情、公司行动、交易状态、股本、日频估值、指数行情、财务报表、基础财务指标和公告身份都有成熟数据模型。这里的“可标准化”表示 schema、PIT、快照和质量规则可以先建，不表示 Codex 可以未经授权下载外部数据或使用用户凭据。

### 必须用户裁决

- 主行业分类采用申万、中信、证监会还是多口径并存；
- 付费供应商、Tushare 权限/Token、许可和预算；
- 分析师预测、基金持仓和高频/L2 的采购范围；
- 分钟、tick、L2 的市场范围、历史年限、保留期和空间上限；
- 新闻正文的许可、保存范围和可再发布边界；
- 经纪商/QMT 数据是否接入，以及账户与凭据的安全边界；
- GCTX/HK/GLEQ 各字段的精确端点、保存/内部共享许可和批量历史回补；
- 定义范围 GLEQ 之外是否扩张到全球全股票横截面，以及对应研究用途、预算和容量。

### 当前容量场景

2026-07-26 的隔离 smoke 使用真实窄日表和巨潮元数据/HEAD 作为初步锚点，并以条件假设补足尚未采样的 HK/GLEQ、文档尾部和工作峰值。单一权威主盘、保留 25% 空闲且不计独立备份时：

- 35 项必建库：1.17 / 3.31 / 9.49 TB；
- 新闻和高频均关闭的 46 项：1.63 / 5.28 / 17.09 TB；
- 新闻启用但高频关闭的 48 项：2.09 / 11.55 / 43.09 TB；
- 完整 52 项：tick/L2 总体未冻结，`UNDETERMINED`。

这些数字是低/基准/压力场景点，不是统计 P50/P95，也不是可信上下界。正式回补前仍需 P1 跨年代日频、P2 全历史文档 census/分层/尾部、P3 多提供商指数，以及 HK/GLEQ 实测。相同来源原件若被多个数据集使用，容量模型假设只保存一个按内容哈希识别的 raw 对象，再由不同 manifest 引用；该跨数据集 raw 去重尚未实现。

## 9. 生命周期与状态

```text
PLANNED
  → ACQUIRING
  → QUARANTINED
  → NORMALIZED
  → SNAPSHOT_CANDIDATE
  → REVIEW
  → OK / REJECTED
  → DEPRECATED
```

- `planned_datasets.json` 只收计划；`datasets.json` 只收实际存在的资产。
- `OK` 必须限定到字段、区间和用途，不能笼统表示整库绝对正确。
- 新快照只能追加，不能覆盖；修订形成新 `snapshot_id`。
- `RESEARCH_ONLY`、`SHADOW_ONLY` 和 `LIVE_READY` 读取必须钉住精确快照。
- `DO_NOT_USE` 只允许 `inspection`。
- 删除、移动、压缩、归档仍遵守 `STORAGE_POLICY.md` 和人工批准。

## 10. 每个数据集的最小说明书

虽然目标数据域很全，但每个实际数据集都必须至少回答：

1. 数据身份和不可变版本是什么；
2. 谁负责、从哪里来、许可允许什么；
3. 表、字段、单位、主键、频率和分区是什么；
4. 复权、停牌、涨跌停、退市、代码变化如何处理；
5. 发布时间、可用时间和修订史如何保存；
6. 覆盖区间、缺失、重复、异常、交叉核验结果是什么；
7. 哪些项目能读、用途上限是什么；
8. 如何重建、验证、恢复和退役。

## 11. 当前裁决与下一步

用户已于 2026-07-20 确认：官方直接来源优先，开源适配器负责采集，多源校准；允许工作区内部保存和跨项目共享，但不得绕过底层来源许可；覆盖沪深北、退市证券和可验证的最大历史；L1 保存未复权事实和公司行动，研究投影默认前复权；现有 Qlib 不作为过渡数据。两个旧 Qlib 数据本体已经删除，活动目录移除并保留 tombstone、manifest、provenance 和删除回执。用户于 2026-07-24 进一步确认 GCTX-8、HK-4 和定义范围内的 GLEQ-3 均需完整建立；这是范围批准，不是具体来源、付费、凭据或批量回补批准。

`L1-D3A` 已完成：`contracts/g_data_core_v1/` 已冻结 20 项核心数据族的字段级来源/许可矩阵、schema、PIT/修订与多源校准合同；许可和历史可得性不清的绑定继续阻断。`L1-D12A-01` 已于 2026-07-27 完成隔离基础设施修复：请求 schema 与 RFC 3339 严格执行、Windows 真子进程 hard-crash/PID-reuse 恢复、请求—锁—交易—准备计划绑定、请求重推导路径、双 recovery 互斥、owner 前置校验和提交后清理待办状态均通过回归；中央数据工程已形成 44 包精确版本锁，适配器 `SMOKE_ONLY` 门已形成。中央 CPython 3.11 `.venv` 已于 2026-07-28 安装并通过锁验证；该结果没有写正式 catalog，也不等于父项 D12A 对真实数据完成。

`L1-D2A-01` 早期 inspection 已取得证券组件、二级日历、12 只日频样本、巨潮 4 年 × 4 类元数据/48 个 HEAD 和 732 条二级指数目录；2026-07-27 的正式授权批次又以 `contracts/source_admission_v1/` 和隔离运行 `d2a01-20260727-v3` 核验 SSE/SZSE 当前参考数据、2026 休市通知、CNINFO 10 条权益分派元数据及许可页面。2026-07-28 完成的 `L1-D2A-01B_CSMAR_LOCAL_RESEARCH_BOOTSTRAP` 以两份人工下载 ZIP 验证本地候选管道，D2A-02 把 2020—2024 五年 `TRD_Dalyr` 建成隔离候选；D2A-03 又形成 14 条官方/CNINFO PIT 候选和 509 行 CORE 切片，D2A-04 Phase A 形成 568,246 B 缺口证据。2026-07-29 的 D2A-04B 进一步机械构建 1990—2026 日行情只读全历史、公司当前截面、日历、分配、股本变动、无涨跌停限制、日复权因子和 16 张财务/审计候选；D2A-04C 随后把两张审计文本闭合为 provenance 完整的 v2 候选，并完成 33 包、25 逻辑表、CORE-01～20 的本地缺口审计。当前为 32 个候选 + 1 个重叠校准包，18 张财务/审计候选；所有候选固定为 `INSPECTION_ONLY/NOT_ADMITTED`。机构许可文本、字段/单位正式语义、PIT/修订链、永久身份、市场级日历和多源冲突尚未通过，正式来源准入和 CORE-20 正式快照仍为 0。历史 runner 合同与当前候选覆盖状态分别见 `contracts/source_admission_v1/source_matrix.json` 和 `contracts/source_admission_v1/current_source_registry.json`。GCTX/HK/GLEQ 合同包已排入 D10A–D10C，但尚未开始。

候选补源父包 `L1-D2A-04_CORE01_TO_CORE06_CANDIDATE_CLOSURE_AND_SELECTED_SOURCE_PILOT` 的 Phase A 状态仍为 `COMPLETED__PHASE_A_LOCAL_ENGINEERING__D2A_04_COMPLETION_GATE_NOT_MET__PAUSED`；`L1-D2A-04C_33PKG_LOCAL_SCHEMA_PIT_AND_TEXT_CLOSURE` 已完成本地机械闭合，但不改变来源准入。2026-07-30 用户已经启动有界 `L1-D2A-04B_OFFICIAL_IDENTITY_CALENDAR_ACTION_STATUS_SOURCE_CLOSURE`。旧 combined 审计 0/4、BLOCK 门 1 PASS/12 FAIL/3 UNKNOWN 已重分类为 `POST_ADMISSION_AUDIT`；当前非循环 Stage A 对 CORE-01/02/03/06 仍为 0/4 ready，Stage B blocked。D2A-04B 当前只在冻结范围内补齐官方身份/日历/行动/状态/许可/PIT 证据并执行无密钥最小预检，不得自动进入 D2B。用户取消固定 200 GiB 预留不取消工程护栏：此前 D2A-04B 计划 footprint 为 150 GiB，240 GiB 是硬上限，且全盘空闲不得低于 25%。D2B 只能在 D2A-04 完成门和 Stage A 全部通过、取得精确 bundle 哈希绑定的独立 Stage-B 授权后建立 canonical CORE 快照并验证管道。2026-08-01 起研究入口改为分层门：CORE-01～06 加对应 L2 安全验收形成 `R0_PV`；CORE-20 加对应 L2 验收形成 `R1_CLASSIC_CN`；CORE-20 + GCTX-8 加跨市场时间对齐验收形成 `R2_A_SHARE_GLOBAL`。2026-08-04 另行开放的 `R0_SHSZ_PV` 只证明沪深价量窄门；真实 `PanelData` 适配在该精确快照上已通过，但 canonical CORE bundle 仍未通过。Codex/Agent 可并行处理独立数据族、适配器和机械校验；人工门户、来源权利与关键 PIT 冲突不能由并行计算绕过。付费、新密钥、批量历史回补、其他正式 catalog 晋级和质量豁免没有扩大授权。

2026-07-30 的维护工程又新增三个独立、可复现但仍为 `INSPECTION_ONLY` 的标准件：日更编排状态层负责 SQLite 账本、连续水位线、单写者租约、缺口 replay 和断点恢复；schema 演化门负责兼容新增字段与破坏性/未知变化分类，并强制 `field_coverage_start`；修订事件账本负责严格 PIT 的 revision/vintage/迟到/撤回/恢复回放。新测试分别为 10/10、14/14、10/10，日更既有 14/14 同时回归通过。它们没有真实来源连接、生产 worker、正式发布器或调度任务，也没有改变 catalog、质量或研究状态。对当前 Stage A 的 43 个整项阻断理由复核后，仅凭现有磁盘候选和标准化代码可完整闭合的是 0/43；官方永久身份、北交所与完整会话/临停历史、来源权利、字段字典、发布时间和制度版本仍必须由真实来源闭合。

2026-07-31 又完成一个受限本地 inspection worker：34/34 测试覆盖身份/日期/请求/结果哈希绑定、冻结日历、最早缺口、TOCTOU、异常 claim 清理、reparse 链和构建后验证失败关闭。它只把已落盘、已标准化、单日期输入机械连接到编排状态层与 `tmp/` 候选/quarantine，不联网、不下载、不保存凭据、不建调度、不写正式 catalog、不晋级质量，也不提供研究入口。证据见 `../../reports/data-governance/local-daily-inspection-worker-20260731-v1/VERIFICATION.json`；因此“生产 worker 尚未实现”仍指真实来源采集、正式发布与无人值守调度链，而不是否认该受限 caller 已通过工程验证。

同日对清华图书馆结构化供应商做了只读自动化预检，未调用业务 API、未下载数据、未持久化凭据，也未创建账号、密钥或购买。CCER/CSMAR 的已购 A 股日行情门户表可见（页面显示 18,203,014 行、1990-12-19 至 2026-07-03），但 API 按钮隐藏，机构 WebAPI entitlement 未确认；CEIC 与 Capital IQ 虽存在企业接口，清华订阅内是否包含机器接口也未证明，RESSET/CNRDS、EPS/巨灵同样没有形成可调用 API 证据。因此这些结果只缩小后续询证范围，不构成生产来源或正式准入。证据见 `../../reports/data-governance/tsinghua-vendor-automation-preflight-20260731-v1/VERIFICATION.json`。

2026-07-30 的后续补充 intake 把 17 个下载来源按内容归并为 7 个 ZIP 与 3 份 PDF 手册，封存在新的 quarantine 根；7 个 ZIP/31 个成员、28 个证据引用和 C 盘 17 个 intake 时点保留源均通过字节与 SHA-256/CRC 复核。它们随后已与新增指数批合计 35 个已验证来源一起移入 Windows 回收站且未清空。本批的 7 个隔离候选仅使 CORE 候选触及投影由冻结 33 包审计的 14/20 增为 16/20，正式 CORE 仍为 0/20、Stage A 仍为 0/4。标准 TRD 手册可以作为供应商层字段字典，确认 `Dnshrtrd/Dnvaltrd` 的显式零值含义、`LimitDown/LimitUp` 单位及 `LimitStatus` 枚举和计算说明；它不能替代交易所官方历史制度版本、PIT/修订链和来源权利。TRDNEW 的 `Filling` 只属于 `TRDNEW_Dalyr`，不得静默迁移到标准 `TRD_Dalyr`。该 intake、候选与手册证据仍是 `INSPECTION_ONLY/NOT_ADMITTED`，未登记正式 catalog、未生成正式快照、未晋级质量、未授权研究。

同日新增指数日行情 quarantine 根 `data/market/quarantine/csmar-manual-export/intake-20260730-18src-12blob-idx-idxtrd-v1/`，封存 18 个来源别名、435,572,489 B，内容去重为 12 个唯一 ZIP、397,397,885 B；manifest SHA-256 为 `10439a5e5857fd9cf5c00c36aa2cd3d5fcfaac52bd11a67efff4e31f451eff23`，封存时点 retention receipt SHA-256 为 `36b8fc683cef53a460883813f4f21b3265db4c06833892dc550b53945299051c`。两个当日 intake 合计 35 个来源、632,131,463 B，在 22 个唯一目标对象（19 个 ZIP + 3 个 PDF）全部通过哈希复核且 19 个 ZIP 通过完整读取/CRC 后送入回收站，回执 SHA-256 为 `91e97ee9d188b4e5c52030009cb82191b672032404d6e7e698ed300700a3f731`。这些数值是物理来源与去重对象统计，不代表指数历史覆盖、字段或研究门已经闭合。

CSMAR 精确资产的用户声明回执绑定 3 个 intake、68 个来源别名（别名字节合计 2,670,476,858 B）、55 个 canonical 唯一 blob（内容去重后 2,585,514,984 B）：允许本机长期保存、校内非商业学术研究和内部派生因子/统计，禁止原始数据外部分发与商业使用；API/自动化批量、离校后处置、官方来源权利和独立合同核验不在覆盖内。`RIGHTS_RECEIPT.json` SHA-256 为 `1d07b07304fe767ce7edda2d2563d816c73e52fe7b1c21ef30bccb1db9498661`。该回执关闭精确本地资产的声明子范围，但不把 GLOBAL 权利总门或任何正式数据身份转为 PASS。

Stage-A auditor contract v3 的 2026-07-31 v4 packaging seal 必须按四层理解：9 个上游包及递归绑定的严格审计通过，只证明证据文件、引用和失败关闭投影正确；单个子命题 PASS 只关闭该子检查；package 声明不能覆盖正式状态机；只有一个 CORE 的全部阻断命题闭合才发生 gate 转换。v4 封存时 auditor `--verify-only` PASS；五个 authority 文件于 2026-07-31 获准同步后，当前 live verify 预期 FAIL，八项错误集合精确限于五个旧 authority 哈希绑定、CORE01/CORE03_CORE06 live guard 与 aggregate formal-guard 漂移。该事实由 `reports/data-governance/stage-a-authority-sync-post-verification-20260731-v1/` 核对，receipt `REPORT_MANIFEST.json` SHA-256 为 `46652312bb20fe021c013b7bc06581c359fd324d525b192b7d9e826a89a03f75`；当前 live 状态明确为预期 FAIL，strict upstream package integrity 仍为 9/9。CORE-02 为 Q1 FAIL、Q2 FAIL、Q3 UNKNOWN、Q4 PASS；XSHG/XSHE/BJSE MIC 子检查 PASS 不改变正式门，CORE-03 Q2/Q3 UNKNOWN，CORE-06 Q3/Q4 FAIL。绑定报告包路径为 `reports/data-governance/stage-a-evidence-consolidation-20260731-v4/`，`REPORT_MANIFEST.json` SHA-256 为 `751ecff9d6bb39cbb88c919c01a910b92c3e8e50fd65676d0472c3f868bfc4d3`。路径语义由 `reports/data-governance/stage-a-evidence-consolidation-20260731-v4-alias-receipt-v1/` 约束，其 `REPORT_MANIFEST.json` SHA-256 为 `587077cc0871960398bf4ec3ae4069c071410e81a3ced8212945f950f6dd4435`；内部 run ID `stage-a-evidence-consolidation-20260730-v3` 是固定 auditor contract 标识，v4 只表示 2026-07-31 的第四次 packaging seal，不是正式证据根或门转换。v4 与 post-sync receipt 均为 `formal_effect=NONE`；正式 Stage A 仍为 0/4，正式来源准入为 0，D2B blocked，全部候选仍为 `INSPECTION_ONLY/NOT_ADMITTED`；没有正式 catalog、快照或 published 写入。

`TRD_Dalyr` v2 NULL/零值审计逐一读取 8 个候选、464 个 Parquet 分区，22,476,677 原始行去除 3,785,013 条精确重复后形成 18,691,664 个干净键，主键冲突为 0；价格零值为 0，`Trdsta` 无空值或越界值，另识别并列出 5 条成交量/成交额一侧为零的异常记录。它把观测/供应商语义证据子评估推进到 PARTIAL，但当前 Stage-A CORE03-Q2 门仍为 UNKNOWN，且不能证明供应商历史未知值从未编码为零，因此 CORE03-Q3 也保持 UNKNOWN，不得作为正式准入依据。

D2A-04B 的旧“无权限、0 下载”和 2026-07-29 清华代理 code 27 事件继续作为历史
回执保留，不再作为当前等待条件，也没有重试或绕过。CSMAR Python API 的当前结论是
个人认证通过但数据库数据 entitlement 被拒绝；它不能承担自动日更。SSE/SZSE 仅
封存了部分官方来源证据，BSE 在 WAF/HTTP 403 处停止。所有来源证据和候选仍为
`INSPECTION_ONLY/NOT_ADMITTED`；非循环 Stage A 0/4，Stage B 与 D2B 均未解锁。

D2A-04B 不设置固定 200 GiB 用户预留。2026-07-30 复测：
`tmp/local-dataset-build/` 为 3.546 GiB，审计文本中间候选因同时保留 v1/v2
和逐行证据为 0.672 GiB，33 个封存 ZIP 为 1.898 GiB；指数成份变动与指数基本信息
候选合计约 2.064 GiB。G 盘总容量 453.853 GiB、空闲 368.793 GiB，当前结构化
低频库空间不是阻断。旧 v1 占用继续保留但不再权威。近期容量按每批实际压缩率重估，
旧 TB 场景不再作为当前结构化日频库预测。

指数候选包括 `IDX_Idxinfo` 5,574 行、12 字段，以及三批 `IDX_Chgsmp` 279,744、
3,596,921 和 7,170,930 行；它们通过各自的 CSV↔Parquet/主键机械校验，但没有闭合
指数权重有效时点、修订链、来源许可或正式研究身份，继续保持
`INSPECTION_ONLY/NOT_ADMITTED`。

当前结论不是“数据库已经完成”，而是“52 项范围已登记、CORE-20 合同已完成、真实下载包候选管道已通过、正式数据仍待建设”。`tmp/local-dataset-build/` 下的 CSMAR 候选不能由项目当作正式共享数据绕过 catalog 使用。CORE-01～06、CORE-20、CORE-20 + GCTX-8 分别对应尚未达到的 R0、R1、R2；HK-4 与 GLEQ-3 也必须实际通过才能称 35 项必建研究数据库完成。新闻、资金流、高频和国内衍生品继续按独立门管理。

## 12. 外部参考

- Microsoft Qlib 官方仓库及数据说明：<https://github.com/microsoft/qlib>
- 上海证券交易所行情服务：<https://star.sse.com.cn/transparency/services/>
- 深圳证券交易所数据服务：<https://www.szse.cn/English/services/dataServices/index.html>
- AKShare 项目说明：<https://github.com/akfamily/akshare/blob/main/docs/introduction.md>
- Tushare Pro 官方数据分类：<https://tushare.pro/document/2?doc_id=17>
- Tushare Pro A 股日线行情：<https://tushare.pro/document/2?doc_id=27>
- Tushare Pro 财务数据说明：<https://tushare.pro/document/2?doc_id=44>
- 巨潮资讯官方信息披露入口：<https://www.cninfo.com.cn/new/index.jsp>
