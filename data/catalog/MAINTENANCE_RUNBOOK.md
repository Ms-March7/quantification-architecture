# Quantification 数据维护手册

更新时间：2026-08-06  
状态：`R0_SHSZ_PV_FORMAL_BASE_PASS__UNIFIED_CANDIDATE_DAILY_MAINTENANCE_WITH_FINANCIAL_AND_LOCAL_JQ_VERIFICATION__FORMAL_PROMOTION_MANUAL`  
适用范围：`${QUANTIFICATION_ROOT}` 的共享事实数据、不可变快照、Qlib/DuckDB 投影和跨项目发布数据。

## 1. 维护目标

本手册把用户确认的数据管理方式固化为可执行合同：

1. 全量规划覆盖完整多因子所需数据，不以 OHLCV 最小集代替完整目标；
2. 原始事实、标准事实、快照、读取投影和派生因子分层保存；
3. 每日增量写入，定期合并小文件，但不丢失日频粒度；
4. 旧快照不可改，新版本通过 manifest 引用不可变分区；
5. 研究只能读取明确 `dataset_id + snapshot_id`；
6. 自动化负责机械工作，来源、许可、破坏性 schema 变化、质量豁免和删除由人工批准。

研究读取采用与生产维护状态机分离的分层门：`L0_INSPECTION` 只允许候选审计；
CORE-01～06 正式快照及对应 L2 安全门全部通过后才可进入 `R0_PV`；CORE-20 全部
通过后才可进入 `R1_CLASSIC_CN`；CORE-20 + GCTX-8 及跨市场时间对齐门全部通过后
才可进入 `R2_A_SHARE_GLOBAL`。旧 `G_DATA_RESEARCH_BASE` 保留为 R2 兼容别名。
上述 canonical 四层状态均未改变；另有独立 `R0_SHSZ_PV` 沪深价量窄门于 2026-08-04 开放，只允许精确正式面板上的价量 Discovery 和预注册验证准备。该窄门不改变 35 项必建库和全域 52 项完成定义。

`L1-D12A-01` 已完成隔离合成原子 catalog 基础设施修复：严格 sentinel 与 catalog containment、严格请求 schema/RFC 3339、staging、完整合同哈希、quarantine、幂等、同目录 replace、Windows 真子进程 hard-crash/PID-reuse 恢复、锁/交易/准备计划绑定、路径重推导、双 recovery 互斥、owner 前置校验和 cleanup-pending 状态均有回归证据。中央数据工程已建立精确版本锁，CPython 3.11 中央 `.venv` 已安装并通过 44 个锁定包验证；适配器门已覆盖样本宽度、lineage、单位、类型、主键、NULL、数值边界和 OHLC。2026-08-04 工作区受控发布器在单次精确授权下完成 `cn-a-share-r0-shsz-pv-panel@20050104-20260803-v1` 的正式原子发布；这只证明该窄门，不授权其他来源或数据族晋级。统一 `qdata update`、多数据族生产适配、自动正式晋级、正式 compaction 和持续审计仍未实现。

### 候选层统一日维护 v1

`control/tools/run_candidate_daily_maintenance.py` 与同名 `.cmd` 是当前候选层唯一计划任务入口。Windows 任务 `Quantification-Candidate-Daily-Maintenance` 在工作日 19:20 依次运行沪深价量、B 路径、RQData 财务 PIT 队列续跑、本地 JoinQuant 股本 PIT 幂等验证，最后刷新中央候选索引；`StartWhenAvailable=true`、并发策略为 `IgnoreNew`、最长运行 3 小时。旧任务 `Quantification-RQData-SHSZ-Daily` 与 `Quantification-RQData-B-Path-Daily` 已禁用但未删除，避免重复请求并保留可逆回滚。财务与 JoinQuant 步骤没有独立计划任务。

统一入口及 RQData 组件使用 Windows 内核字节区间锁，进程硬崩后由操作系统自动释放。各候选步骤相互独立，即使某一步失败，后续独立步骤与最后索引刷新仍会尝试；任一步非零退出都会使总回执失败关闭，已封存候选不回滚、不覆盖。2026-08-05 的旧三步真实入口和任务计划 smoke 均 PASS，历史闭合回执见 `../../reports/data-governance/candidate-daily-maintenance-closure-20260805-v1/`。2026-08-06 五步顺序 PLAN、31/31 离线测试和一次真实统一 `--run --through 2026-08-05` 均 PASS：价量、B 路径、财务 PIT 和 JoinQuant 股本 PIT 四步都在本地验证后返回 `NO_CHANGE/NO_NETWORK`，随后索引刷新为 28 个根、107 个 evidence、82 个 manifest；正式 catalog 运行前后 SHA-256 均为 `a11059fd7de45ea13feeff3b999ea79a397f2d7a8707e42c6c443580580ac758`。精确回执为 `../market/transactions/candidate-daily-maintenance/candidate-maintenance-20260806-030308-a5bd09a9-v1.json`。该运行没有业务下载、没有修改正式 `datasets.json`，也不产生自动准入或正式快照。

### RQData 沪深日增量组件 v1

`control/tools/run_rqdata_shsz_daily_update.py` 每次最多补 5 个交易日、最多使用 35 MiB RQData 流量。程序从精确正式基线和已封存 delta manifest 推导水位线，使用共享内核锁、同卷 staging、原子封口、逐文件哈希和失败关闭回执。原 18:30 独立任务已禁用；日常由统一 19:20 入口调用。

自动更新的出口固定为 `data/market/quarantine/r0-shsz-pv-daily-delta/` 下的 `INSPECTION_ONLY` 候选，不修改正式 catalog、不晋级质量、不运行因子。2026-08-04 首批执行封存 5,202 行，流量 3,446,687 B；同日复跑返回 `NO_CHANGE__WATERMARK_CURRENT__NO_NETWORK`。研究仍只能读取精确已发布 snapshot；将 delta 合入新 snapshot 必须经过独立质量复核、精确哈希绑定和原子发布。回执见 `../../reports/data-governance/rqdata-shsz-daily-updater-20260804-v1/`。

### RQData B 路径日增量组件 v1

`control/tools/run_rqdata_b_path_daily_update.py` 与 `.cmd` 入口负责证券主档/代码转换、ST/停牌/涨跌停、公司行动 45 日滚动观察、申万 `sws` 行业归属以及五个核心指数成分权重的候选更新。每批最多补 5 个交易日、200 次业务调用、64 MiB 实测 RQData 流量。程序使用与价量更新器共享的内核锁、同卷 staging、原子封口、逐文件 SHA-256、额度前后测量和失败关闭。原 19:10 独立任务已禁用；日常由统一 19:20 入口调用。

### 中央候选资产索引 v1

`control/tools/build_candidate_asset_index.py` 只在 `data/market/quarantine/` 的受限证据 JSON 中构建 `data/catalog/candidate_assets.json`；拒绝 reparse/symlink 路径，不扫描 `planned_datasets.json`，不读取业务 payload。当前索引为 28 个顶层候选根、107 个证据文件、82 个 manifest。它只提供候选发现与哈希指针，固定为 `INSPECTION_ONLY/formal_effect=NONE`，不能被 `qdata --intent research` 解析。

2026-08-05 首次真实运行覆盖 2026-08-04—08-05，封存 5,202 行主档、10,404 行状态、10,404 行申万行业、2,282 行公司行动观察和 10 个指数—日期成分权重对，实测流量 3,740,354 B；机械检查全部通过。该次 B 路径运行只生成确定性可续跑财务队列，未调用财务 API；财务后继由独立 worker 和交易状态承担。出口固定为 `data/market/quarantine/rqdata-b-path-daily/` 的 `INSPECTION_ONLY/NOT_ADMITTED` 候选；不自动登记 catalog、发布正式快照、晋级质量或运行因子。公司行动滚动观察不能恢复供应商已覆盖的旧版本，财务队列完成也不表示正式财务 PIT 闭合。回执见 `../../reports/data-governance/rqdata-b-path-daily-updater-20260805-v1/`。

### RQData 财务 PIT 候选 worker v1

`control/tools/run_rqdata_financial_pit_daily_update.py` 消费上述封存候选中的 `financial/queue.json`，按 `get_pit_financials_ex(statements="all")` 返回的 `info_date` 构建“下一交易日 09:30”保守可用时间，并生成本地确定性修订链。`rice_create_tm` 只能作为供应商内部元数据，禁止作为公开可用时间。worker 可续跑、原子封存、哈希绑定上游队列且不修改上游队列、catalog 或 Roadmap。

2026-08-05 的 5 证券/2 字段/4 季度真实诊断得到 19 行和 38 条修订事件，完整进程流量 3,327,550 B 超过 2 MiB smoke 预算；该诊断仍是历史失败关闭证据。随后按修订后的 50,000 行硬上限完成 14/14 队列：覆盖 5,202 个唯一证券、`revenue/net_profit` 两字段和 12 个季度，共 131,777 条原始观察与 263,554 条本地修订事件，状态见 `data/market/transactions/rqdata-financial-pit-daily/rqdata-financial-pit-20260805-v1/state.json`。所有任务仍为 `INSPECTION_ONLY/NOT_ADMITTED`，本地修订链完整性和来源级首次可见时间均未证明。

统一 19:20 入口现以 `--discover-through` 在 `data/market/quarantine/rqdata-b-path-daily/` 的直接、哈希验证候选子目录中选择不晚于维护日的最早未完成队列；若没有未完成队列，则验证最新已完成队列并在导入 RQData 客户端前返回 `NO_CHANGE__QUEUE_COMPLETE__NO_NETWORK`。无合格本地队列时明确 `SKIPPED`。每次统一维护最多续跑一项，避免一次计划任务隐式扩张为全历史回补。完整财务 PIT 仍需聚宽 `pub_date/report_date`、米筐多版本和本地 CSMAR 数值/更正记录的跨源账本、来源权利与正式准入。

### JoinQuant 股本 PIT 本地候选验证 v1

`control/tools/build_joinquant_share_capital_pit_candidate.py` 只读取固定的本地封存 `STK_CAPITAL_CHANGE` 与精确 R0 日历，不含外部客户端。统一入口在本地源存在时调用：若源哈希和正式日历绑定未变、不可变候选已有有效 `COMMITTED.json`，则逐一复核 manifest、四项 artifact 和可选报告绑定后返回 `NO_CHANGE__SOURCE_UNCHANGED__COMMITTED_CANDIDATE_VERIFIED__NO_NETWORK`；本地源缺失时明确 `SKIPPED`；payload/manifest 只缺一侧、源漂移、commit 不完整、artifact 哈希异常或报告绑定冲突均失败关闭。程序不会覆盖既有输出；源变化必须先获得新的合法不可变版本策略。

当前候选 `joinquant-share-capital-pit-intervals-20260806-v1` 仍为 `INSPECTION_ONLY/NOT_ADMITTED`。日期级 `available_at`、本地状态链和供应商原样单位不能替代来源级时分秒、供应商完整修订链、正式单位裁决、来源权利或正式 catalog 准入。

2026-07-30 的每日更新本地验证已从纯 synthetic smoke 扩展为
`PASS_INSPECTION_ONLY_LOCAL_INCREMENT_ENGINE__NO_PRODUCTION_UPDATER__NO_SCHEDULER`。
新旧 14/14 测试证明：对已标准化并被显式指定的本地 CSV/JSONL，可以做受限发现、
精确 schema/类型/必填/日期/主键/严格 revision+available_at 校验、基候选 payload
哈希复核、幂等自包含合并、year/month Parquet、同卷 staging + `os.replace` 原子
封口和 quarantine 失败回执。默认 PLAN 不写，显式执行也只能写
`tmp/local-daily-update/`。它不证明任何真实来源可自动下载，不是正式发布器、catalog
写入器、compactor 或无人值守调度器。

同夜新增的三个标准件仍不改变上述边界。`local_daily_orchestration_v1` 用 SQLite
任务账本实现连续水位线、单写者租约、最早缺口 replay、断点恢复、幂等事件和重试
耗尽失败关闭，新测试 10/10 且既有日更 14/14 回归；该状态层自身不运行 worker，
也不创建调度任务；独立受限 caller 见下段。`schema_evolution_v1` 以 14/14 测试区分无变化、带
`field_coverage_start` 的兼容可空新增字段、破坏性变化和未知失败关闭；
`revision_event_ledger_v1` 以 10/10 测试实现严格带时区的 PIT、vintage、迟到、
supersedes、withdraw/reinstate 与冲突失败关闭。三者只为未来真实来源建立可审计的
机械合同；来源权利、真实发布时间/修订语义、生产发布器和正式准入仍是外部前置项。

2026-07-31 新增的 `run_local_daily_inspection_worker.py` 只把已落盘、已标准化、
单日期本地输入接入上述状态层与候选构建器。新测试 34/34 通过，覆盖
dataset/source/scope、冻结日历、最早未完成日期、request fingerprint 与 manifest hash
绑定，以及 TOCTOU、异常 claim 清理、reparse 链和构建后验证失败关闭。默认 PLAN 不写；
显式执行也只允许写 `tmp/` 候选、quarantine 和编排回执。它不包含网络、下载器、凭据、
调度器、生产发布器、正式 catalog 写入、质量晋级或研究读取；来源 PIT 与权利缺口继续继承。
证据见 `../../reports/data-governance/local-daily-inspection-worker-20260731-v1/VERIFICATION.json`。

同日完成 ECB、BIS、美国财政部和 HKMA 四个无密钥检查适配器：fixture 4/4 通过；
一次有界 live 中 BIS、美国财政部、HKMA 3/4 结构通过，ECB 的本机 TLS 失败进入
quarantine 且 0 retry。它们只允许写
`tmp/public-official-api-inspection/<run_id>/`，并强制保存 raw/normalized 哈希与
未闭合 rights/PIT 回执；不得加入任务计划程序。来源权利、发布时间、修订链、稳定
增量参数和正式准入合同未闭合前，这仍不是生产更新源。

用户于 2026-07-26 决定不建设独立数据备份。下文“恢复”仅指主盘内的中断恢复、原子发布回滚、不可变快照复现和投影重建，不包含主盘物理损坏后的异盘或对象存储恢复。备份检查不再属于维护计划。

来源、覆盖和许可遵循 `SOURCE_POLICY.md`；不确定、缺失、冲突、异常和失败数据遵循 `DATA_QUALITY_POLICY.md`。用户已批准官方直接来源优先、开源适配器采集、多源校准、尽可能完整股票池和历史，以及默认前复权研究投影；每个具体端点的底层许可仍须登记。

`L1-D2A-02` 已按 PIT 缺口暂停；后继 `L1-D2A-03` 也已按标准停止条件暂停。
D2A-03 的官方 PIT v2 和 16 证券切片均由显式路径与哈希钉住；只有 CORE-03
源观测可保留，北交所业务 PIT、CSMAR 财务微型包、CORE-01/02/04/05/06 数据候选完成门
仍未闭合。输出继续 `INSPECTION_ONLY/NOT_ADMITTED`，不得触发 compaction、
正式发布、调度或自动清理。

`L1-D2A-04_CORE01_TO_CORE06_CANDIDATE_CLOSURE_AND_SELECTED_SOURCE_PILOT`
Phase A 已完成有限本地候选闭合并暂停；覆盖状态以
`contracts/source_admission_v1/current_source_registry.json` 为准。D2A-04
产物仍只是 `INSPECTION_ONLY` 候选证据，没有执行正式 catalog 写入或质量晋级；
D2B 在 D2A-04 完成门通过且另行启动前保持阻断。

用户于 2026-07-28 启动了
`L1-D2A-04B_OFFICIAL_IDENTITY_CALENDAR_ACTION_STATUS_SOURCE_CLOSURE`。早先 CSMAR
门户预检因 `TRD_Dalyr` 显示“无权限”而停止，0 业务请求、0 下载回执继续作为历史
事实保留。后续在清华机构会话中确认表级权限，用户于 2026-07-29 又明确扩大
结构化低频候选收集。日行情八个分段包已形成 1990—2026 只读去重投影；公司当前
截面、日历、分配、股本变动、无涨跌停限制和日复权因子已经机械构建。资产负债表、
利润表和直接法现金流量表也已完成真实表头、主键、报告口径、类型、分区与 payload
机械审计，合计 2,053,616 行、684,875,634 B 隔离候选；它们缺首次公布时间和
完整修订链，不能用于 PIT 财务研究。候选构建不触发 compaction、正式发布或调度。

上交所和深交所部分官方证据已经封存在 D2A-04B Track A 临时包；北交所请求在
WAF/HTTP 403 处按硬停止门结束，没有接受业务数据。全部输出仍为
`INSPECTION_ONLY/NOT_ADMITTED`，不触发自动重试、catalog 写入、质量晋级或研究读取；
正式准入仍为 0，D2B 继续阻断。

2026-07-30 的 CORE-02 专项闭合在不绕过北交所 WAF 的前提下，把沪深年度休市公告与
已知修订转成 2015-01-01—2026-07-28 共 8,454 条逐所逐日候选，保留 28 条公告
lineage、12 条时段记录和 20/20 官方原文/哈希。`Q3/Q4` 机械门通过，`Q1/Q2`
仍失败；XSHG/XSHE/BJSE 的 ISO 10383 MIC 子检查已经 PASS，但北交所、三所完整临时/应急停市档案、沪深早期时段版本链和日内
发布时间仍不完整。该候选不得用于生产日更或研究日历。

同日的准入架构复核把旧 combined 门重分类为 `POST_ADMISSION_AUDIT`，因为
`formal_closure`、`formal_catalog_identity`、`qdata_research_resolution` 只能在发布
后成立。新的 Stage A 不依赖 catalog/qdata，但当前 CORE-01/02/03/06 仍为 0/4
ready；Stage B 必须等待 Stage A 全通过、精确 bundle 哈希绑定的独立用户授权和支持
真实数据的原子发布器。当前 synthetic-only 发布器不得用于正式数据。

当前允许范围是官方身份/日历/公司行动/状态闭合，以及经用户人工导出的 CSMAR
结构化低频核心表：日行情、公司/日历、公司行动、股本、复权、可交易性和 PIT 财务
事实/公布日期/审计意见。CSMAR 资源路由 2026-07-29 的代码 27 事件只作历史回执；
CSMAR Python API 当前为个人认证通过但数据 entitlement 被拒，不能作为日更源。
其他图书馆数据库仍可做独立、串行的来源 census，但不得把一个来源的许可扩大解释为
其他来源的许可，也不得把其他来源入口自动加入 52 项计划。
每个输入必须先保存 raw 与 receipt，再记录查询范围、下载
时间、内容哈希、来源权利、PIT/修订和 quarantine 处置。用户不要求固定保留
200 GiB；当前结构化候选实测远低于旧 TB 场景，按批次实测重估。不得自动保存凭据，
不得自动扩展到付费、新闻、高频、正式 catalog、质量晋级或研究输入；候选足够仅
允许提交 D2B 启动申请。

## 2. 数据对象模型

全量数据库不是一张超级表。各逻辑表按业务事实拆分：

| 数据集类型 | 一行代表 | 主要关联键 |
|---|---|---|
| 证券主数据 | 一只证券的一个身份/状态版本 | `security_id + effective_from` |
| 交易日历 | 一个交易所的一天或一个会话 | `exchange + trade_date` |
| 日频行情 | 一只证券的一个交易日 | `security_id + trade_date` |
| 每日可交易性 | 一只证券在一个交易日的状态 | `security_id + trade_date` |
| 公司行动 | 一只证券的一次行动或修订 | `security_id + action_id + revision_id` |
| 股本/估值 | 一只证券在一个交易日的已知值 | `security_id + trade_date` |
| 财务报表 | 一只证券的一份报告版本 | `security_id + report_period + published_at + revision_id` |
| 行业归属 | 一只证券的一段分类有效区间 | `security_id + taxonomy + effective_from` |
| 指数成分 | 一只证券的一段指数成员关系 | `index_id + security_id + effective_from` |
| 指数行情 | 一个指数的一个交易日 | `index_id + trade_date` |
| 全球交易会话 | 一个市场的一次会话或日历版本 | `market_mic + session_date + revision_id` |
| 全球宏观/利率/汇率 | 一个序列的一次观测或发布版本 | `series_id + observation_time + vintage_id` |
| 港股/全球关联个股行情 | 一只证券的一个当地交易日 | `security_id + local_trade_date` |
| 跨市场映射 | 一个身份或事件在一段有效期内的关系 | `mapping_id + effective_from` |
| 资金流可观察账本 | 一只证券/基金/市场的一天或事件 | 由合同定义，必须含时间和来源身份 |
| 发布因子 | 一只证券、一个日期、一个因子版本的值 | `security_id + trade_date + factor_id + factor_version` |

行情、估值和交易状态可以按 `security_id + trade_date` 关联，但不得因为连接方便就写成同一张无边界宽表。财务、行业、指数成分和公司行动必须保留事件时间、有效区间和修订链。

## 3. 物理分层

未来新数据使用以下逻辑布局；目录在第一个获批数据集实施时创建，不预建空业务目录：

```text
data/market/
├─ raw/<source>/<dataset_id>/ingest_date=YYYY-MM-DD/<run_id>/
├─ curated/<dataset_id>/schema=vN/year=YYYY/month=MM/*.parquet
├─ snapshots/<dataset_id>/<snapshot_id>/
│  ├─ manifest.json
│  ├─ provenance.json
│  ├─ partition-manifest.json
│  └─ quality/
└─ quarantine/<dataset_id>/<run_id>/

data/published/
├─ factors/<dataset_id>/<snapshot_id>/
├─ risk/<dataset_id>/<snapshot_id>/
├─ capital-flow/<dataset_id>/<snapshot_id>/
└─ events/<dataset_id>/<snapshot_id>/
```

- `raw`：外部响应、文件和抓取证据，只追加；
- `curated`：标准化 Parquet 事实分区；
- `snapshots`：引用分区及其哈希的版本合同，不复制整库；
- `quarantine`：失败、冲突、缺字段或未完成批次；
- `published`：L2/L3 通过合同的派生数据；
- Qlib：由已准入事实快照生成的研究读取投影；
- DuckDB：对 Parquet 的只读视图和查询层；
- SQLite：工作状态或 catalog，不作为大型事实表的唯一载体。

既有两个冻结 Qlib 数据本体已于 2026-07-20 删除，并从活动 catalog 移入 tombstone；manifest、provenance、项目 release catalog 和历史运行配置不改写。历史运行因此不能按原路径完整复现。

## 4. 分区规则

| 数据类型 | 推荐物理分区 | 说明 |
|---|---|---|
| Raw 采集批次 | `ingest_date/run_id` | 保留每次请求和失败证据 |
| 日频行情/状态/估值 | `year/month` | 每日先写 delta，月末合并；日频行仍全部保留 |
| 证券主数据/公司行动 | `effective_year` 或 `published_year/month` | 保存有效区间与修订 |
| 财务报表 | `published_year/month`，必要时辅以 `report_year` | 研究可见性以公告/可用时间为准 |
| 行业/指数成分 | `effective_year` | 禁止当前成员倒灌历史 |
| 公告/新闻 | `published_year/month` | 新闻当前暂停建设 |
| 分钟/tick/L2 | `trade_date/exchange` | 仅在容量和许可批准后启用 |

目标文件大小和 row group 在真实数据试点后确定。禁止为了目录整齐而把每一天、每只股票、每个字段都拆成大量小 Parquet 文件。

## 5. 每日维护流程

交易日收盘后的标准状态机：

```text
SCHEDULED
  → ACQUIRING
  → RAW_SEALED
  → NORMALIZING
  → VALIDATING
  → SNAPSHOT_CANDIDATE
  → PUBLISHED / QUARANTINED
```

当前受限 inspection worker 只覆盖“已落盘的标准化单日期输入 → VALIDATING →
`tmp/` candidate/quarantine → orchestration receipt”，不实现 `ACQUIRING`、真实来源 raw
采集、正式 `PUBLISHED`、catalog 登记或调度。因此它不能把上述生产状态机解释为已经上线。

执行顺序：

1. 核对交易日历和预期市场范围；
2. 先更新证券身份、上市退市、代码、板块和当日状态；
3. 采集当日原始行情及来源元数据；
4. 以唯一 `run_id` 写入 raw，只追加并计算哈希；
5. 标准化永久证券 ID、字段类型、单位、时区和缺失值；
6. 生成当日 Parquet delta；
7. 检查主键、覆盖、OHLC、数量级、停牌、涨跌停、新股、退市和跨来源差异；
8. 失败批次进入 quarantine，正式 catalog 不变；
9. 成功批次冻结分区，生成 manifest/provenance/quality；
10. 原子登记新 `snapshot_id`，旧快照继续可解析；
11. 记录运行回执、耗时、行数、差异和失败告警。

非交易日默认不生成空行情快照；来源更正、公司行动和财报仍可按事件流程写入。

### 5.1 跨市场时间、币种与可用性

GCTX、HK 和 GLEQ 每次维护还必须：

1. 保存来源当地日期、时区、UTC 偏移、夏令时规则和北京时间，不用无时区日期代替；
2. 区分当地正式收盘、盘中观测、夜盘、发布时间和本地实际采集时间；
3. 由 `available_at` 映射到最早可用于 A 股决策的交易日，禁止按相同自然日期直接连接；
4. Raw/curated 保存当地币种事实，人民币或统一基准币收益由钉住的 FX 快照独立生成；
5. 市场假期错位、临时停市、时区规则变化和来源修订都生成显式版本；
6. A/H/ADR、互联互通和产业链关系保存带有效期映射，禁止用当前关系倒灌历史。

## 6. 幂等、迟到与修订

- 同一日期、同一来源、相同内容哈希重复运行：返回 `NO_CHANGE`；
- 同一日期内容变化：保存新 raw，标记 `REVISION_CANDIDATE`；
- 修订发布新快照，例如 `2026-07-21-r2`，不得覆盖 `2026-07-21`；
- 迟到数据保存真实 `published_at / available_at / ingested_at`，不得伪造为早已可见；
- 只返回 HTTP 200 但正文错误、全零或字段缺失仍视为失败；
- 部分市场或部分证券失败时默认整批不晋级，除非合同事先允许并明确覆盖上限。

### 不确定与缺陷数据

- 未知保留为 `NULL/MISSING`，不填 0；
- HTTP 200 但错误页、全零、空正文或 schema 变化仍为失败；
- 关键主键、证券身份、单位、PIT、许可或覆盖缺陷使整批进入 quarantine；
- 非关键缺失可以保留字段级质量标记和覆盖率，但不能宣称整库完整；
- 多源冲突保留各来源原值并标记 `CONFLICT`，禁止静默覆盖或取平均；
- 插补、缩尾和中性化只在 L2 派生层进行，不修改 L1 原始事实。

完整矩阵见 `DATA_QUALITY_POLICY.md`。

## 7. 月度小文件合并

“月度合并”是物理 compaction，不是把日频聚合成月频。

```text
2026-07-01.delta.parquet
2026-07-02.delta.parquet
...
2026-07-31.delta.parquet
        ↓ 新写入
year=2026/month=07/part-000.parquet
```

合并文件仍包含每个交易日、每只证券的全部日频行。流程必须：

1. 读取当月已发布分区清单；
2. 验证主键和内容哈希；
3. 写入新临时文件并完成全量复核；
4. 原子重命名为不可变月度分区；
5. 发布新的月末 snapshot manifest；
6. 旧日增量仍由旧快照引用，不立即删除；
7. 只有完成引用审计、保留期和用户批准后，才可处理不再被任何保留快照引用的文件。

正式研究优先钉住月末或明确批准的日快照；日常监控可以读取最新已发布日快照，但仍必须记录精确身份，不能只记 `latest`。

## 8. 新证券、新字段与 Schema 演化

### 新证券

先写证券主数据和有效日期，再进入行情分区；上市前不生成零值或未来成员记录。Qlib 投影在下一版本中增加证券目录和字段序列，旧投影不改。

### 新字段

1. 同一事实表的兼容字段：升级 schema 小版本，发布新快照；
2. 单位、主键、PIT 或语义变化：升级主版本，必要时新建 `dataset_id`；
3. 仅从某日可得：记录 `field_coverage_start`，历史保持未知；
4. 估值、状态、行业、财务等不同业务事实：建立独立数据集；
5. 动量、价值、质量等派生因子：进入 `data/published/factors`，不得写回 raw/curated 行情事实。

任何历史回补必须保存来源可得性，不得因为今天取得数据就假装历史决策时已经可见。

## 9. 维护频率

| 周期 | 自动任务 | 人工检查/批准 |
|---|---|---|
| 每交易日 | 行情、状态、估值增量；质量门；候选快照 | 异常覆盖、来源冲突和质量豁免 |
| 事件触发 | 新股、退市、ST、停复牌、公司行动、指数调样 | 重大身份变化和异常更正 |
| 公告触发 | 财报、业绩预告、快报、审计意见和修订 | PIT、口径变化和跨来源冲突 |
| 每月 | Parquet compaction、月末快照、Qlib 投影候选、manifest/哈希与主盘可读性检查 | 月末研究快照晋级 |
| 每季度 | 财务完整性、修订链、行业/指数历史复核 | 数据源和许可复核 |
| 每年 | 容量、保留、主盘内恢复演练、schema 退役审计 | 删除、归档和重大架构升级 |

资金流在对应来源通过准入后按日/事件维护。新闻按用户决定暂停，不进入当前执行队列；计划条目保留但标记为暂停。高频维持合同、容量和许可闸门。

## 10. 自动与人工边界

### 可以自动执行

- 已批准来源的增量抓取；
- raw 哈希、schema 校验、重复键和基本质量检查；
- 当日 delta 写入、隔离、manifest 候选生成；
- 月度小文件合并和 Qlib 投影候选构建；
- 成功/失败回执、指标和告警。

上述自动化只适用于已经批准并登记许可的官方/公开来源。当前核心批次不得自动购买付费数据、注册订阅、使用新密钥或把开放来源缺口静默替换为付费来源。指数目录发现可以自动生成候选清单，但具体行情、成分和权重只有完成字段级许可登记后才能进入正式采集。

### 必须人工批准

- 新数据源、凭据、许可或付费；
- 新数据集和破坏性 schema 变化；
- PIT 规则、复权口径、永久证券 ID 合并/拆分；
- 质量门豁免、冲突来源裁决；
- 从 `REVIEW` 提升到 `OK` 或更高用途；
- 历史大规模回补；
- 删除、归档、覆盖、凭据或账户接入；
- 高频批量采集和新闻正文许可。

用户已预先批准 20 项 `G-DATA-CORE` 的标准合同和工程骨架，并批准 GCTX-8、HK-4、GLEQ-3 作为必建范围；这不替代以下逐次批准：具体外盘端点和字段权利、付费供应商/订阅、任何凭据或密钥、容量报告后的全历史批量回补、质量晋级或豁免、新闻重启、分钟/tick/L2/券商启用。关键冲突、许可不清或 PIT 缺失默认阻断，不得以“必建范围已批准”为由自动放行。

## 11. 失败恢复与监控

每次维护至少记录：运行 ID、数据身份、来源、开始/结束时间、预期/实际行数、证券数、日期范围、缺失率、重复数、冲突数、分区哈希、snapshot ID 和最终状态。

发布必须使用 staging + 原子切换：manifest、provenance、quality 或分区任一未完成时，`qdata` 不得看见新快照。恢复演练应覆盖下载中断、标准化失败、文件写完但 catalog 未提交、catalog 已提交但回执丢失四类故障。

上述演练不宣称能够从主盘物理损坏中恢复。独立备份和异盘灾难恢复已由用户从当前范围移除。

2026-07-27 隔离修复结论与剩余阻断：

- `request.schema.json` 的额外属性、数组去重、严格日期时间和未知关键字失败关闭已由实现执行；
- Windows hard-crash 已由真正 `os._exit` 子进程回归，恢复同时绑定进程 creation token、请求身份、计划哈希和请求重推导路径，并串行化 stale-lock 切换；
- 二级行情适配器样本门已校验 11 字段宽度、一对一 raw hash lineage、明确单位、类型/日期、关键 NULL、重复键、数值边界和 OHLC；但真实 11 字段位置、来源语义和转换公式仍须 D2A-01 准入冻结，声明一致不等于语义已证明；
- 版本锁和中央 `.venv` 已通过 44 个锁定包验证，但仍不是 wheel 哈希锁；本地候选构建通过也不等于正式来源适配器或调度器完成；
- `qdata resolve` 成功、隔离发布通过或 adapter smoke PASS 都不等于 `quality_status=OK`，研究消费者必须经过独立阶段门。

## 12. 实施顺序

1. 已完成旧 Qlib 的退役、依赖清单、精确物理清理、tombstone 和删除回执；
2. 已按 `SOURCE_POLICY.md` 完成 20 项核心数据的时间/PIT/schema、字段级来源和许可合同，并纳入多口径行业、最大指数覆盖、流通/自由流通分层及双利率输入；
3. `L1-D12A-01_ATOMIC_GUARD_AND_RUNTIME_REPAIR` 已完成隔离基础设施修复；没有登记真实业务快照，父项 D12A 对正式数据仍为 `PARTIAL_FOR_FORMAL_DATA`；
4. `L1-D2A-01`、D2A-02 和 D2A-03 已分别在公开源、CSMAR 五年日行情和官方 PIT/CORE 小切片上执行并按阻断条件暂停；历史 runner 合同保留在 `source_matrix.json`，当前候选覆盖状态集中登记在 `current_source_registry.json`，正式来源准入仍为 0；
5. `L1-D2A-04_CORE01_TO_CORE06_CANDIDATE_CLOSURE_AND_SELECTED_SOURCE_PILOT` Phase A 已按冻结计划完成本地证据闭合并暂停；它验证了上游分区引用、观察表、字段合同、缺口和容量闸门，但没有取得 CORE-01 永久身份、CORE-02 官方日历/会话、真实 CORE-04 公司行动/修订链或 CORE-06 历史状态规则，CORE-05 保持零输出；恢复需新计划和用户启动，不得自动登录、整库回补、付费、写正式 catalog、晋级质量或做因子评价；
6. `L1-D2A-04B_OFFICIAL_IDENTITY_CALENDAR_ACTION_STATUS_SOURCE_CLOSURE` 的旧“无权限、0 下载”门户预检保留为历史；当前已构建 CSMAR 1990—2026 日行情分段全历史、公司当前截面、日历、分配、股本变动、无涨跌停限制和日复权因子候选，财务核心表进入隔离构建；SSE/SZSE 仍只有部分官方证据，BSE 因 WAF/HTTP 403 停止，全部仍为 `INSPECTION_ONLY/NOT_ADMITTED`，正式准入为 0；
7. 2026-07-30 旧 selected-core combined 审计 0/4 已重分类为 `POST_ADMISSION_AUDIT`；非循环 Stage A 对 CORE-01/02/03/06 仍为 0/4 ready，Stage B blocked，这不是 D2B。D2A-04 候选完成门和 Stage A 全通过后，仍须取得精确 bundle 哈希绑定的独立授权并使用支持真实数据的原子发布器，才能启动 D2B 建立首批正式不可变 CORE 快照并核验 catalog、读取和管道；CORE-01～06 全部发布且对应 L2 PIT、防未来信息、幸存者偏差、复权、可交易性、训练/验证/盲测和可重建性门通过后，才开放 `R0_PV` 价量候选发现；
8. 完成其余 CORE-20 的交易状态、公司行动、股本估值、PIT 财务、行业和指数真实快照，并验证日增量、修订、月度 compaction、恢复和消费合同；CORE-20 与对应 L2 门全部通过后开放 `R1_CLASSIC_CN`，且不得读取未准入 GCTX；
9. 完成 GCTX-8 字段/来源/许可/PIT/时区合同、容量 pilot 和实际快照；CORE-20 + GCTX-8 与跨市场 decision-time 门共同通过后开放 `R2_A_SHARE_GLOBAL`；
10. 各研究层均以钉住快照的基线因子、未来信息防护、样本外、成本与可重建性验收为运行前置；对应层通过前不得宣称该层可靠因子挖掘已经开始；
11. 完成 HK-4 与定义范围 GLEQ-3 的宇宙冻结、合同、容量 pilot、实际快照和恢复验证，达到 35 项必建研究数据库；
12. 资金流、基金/所有权和国内衍生品按独立子门接入，继续推进全域 52 项；
13. 新闻保持暂停，高频等待另行批准，但二者保留在全域 Roadmap 和重启门中。
