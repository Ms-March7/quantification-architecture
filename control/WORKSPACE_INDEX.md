# Quantification 工作区总索引

更新时间：2026-08-06  
当前阶段：`L1_DATA_ACTIVE__L2_R0_SHSZ_PV_DISCOVERY_COMPLETE__B_CANDIDATE_MAINTENANCE_5STEP_PASS__G_DATA_REVIEW`  
物理迁移：`LOW_RISK_DATA_DUPLICATE_ARCHIVED`

2026-08-06 B 路径候选维护已由统一 19:20 入口按“沪深价量 → B 路径 → RQData 财务 PIT → 本地 JoinQuant 股本 PIT 幂等验证 → 中央候选索引”五步真实 `--run`，总状态 PASS；各数据步骤独立继续、索引最后，任何非零步骤仍使总回执失败关闭。原 18:30/19:10 分立任务继续禁用。该自动化只生成或验证 `INSPECTION_ONLY/NOT_ADMITTED` 候选，不自动写正式 catalog、发布研究快照、晋级质量或运行因子。

RQData 财务 PIT 队列当前已完成 14/14：覆盖 5,202 个唯一证券、`revenue/net_profit` 两字段和 12 个季度，共封存 131,777 条原始观察并机械生成 263,554 条本地修订事件；统一维护对已完成队列返回 `NO_CHANGE__QUEUE_COMPLETE__NO_NETWORK`。权威状态为 `data/market/transactions/rqdata-financial-pit-daily/rqdata-financial-pit-20260805-v1/state.json`。这些 `available_at` 与修订链仍是本地保守推导，来源级首次可见时间、完整更正/撤回链和正式准入未闭合。

本地 JoinQuant `STK_CAPITAL_CHANGE` 已机械构建 196,693 个日期级股本 PIT 半开区间；现有不可变候选在源哈希未变时由五步入口只做 manifest、`COMMITTED.json`、四项 artifact 与报告绑定复核并返回无网络 `NO_CHANGE`，源缺失则 `SKIPPED`，源漂移或不可变输出不完整则失败关闭。中央 `data/catalog/candidate_assets.json` 当前登记 28 个 quarantine 顶层候选根、107 个 evidence JSON 和 82 个 manifest；正式 `datasets.json` 哈希未变，全部新增资产仍为 `INSPECTION_ONLY/NOT_ADMITTED`。因此 canonical R0/R1/R2、CORE-01～20 正式计数、Validation 与 Holdout 状态均不变。股本候选证据见 `reports/data-governance/joinquant-share-capital-pit-intervals-20260806-v1/`；统一维护合同与边界见 `data/catalog/contracts/candidate_daily_maintenance_v1/`。

## 一句话结论

### 2026-07-29 历史闭合口径

> 本节只记录截至 2026-07-29 的历史暂停状态；当前授权必须以下方“2026-07-30 当前覆盖口径”为准。

当前已采用五层总 Roadmap，并只详细推进第一层“数据”。统一数据目录、中央共享根、只读解析器、数据库总说明书和维护手册已经存在；`L1-D3A` 已完成 20 项国内核心数据族的字段/来源/许可/PIT 合同，覆盖 27 个来源、45 张逻辑表和 588 个含继承字段定义，同时保留 19 个采集阻断。`L1-D12A-01_ATOMIC_GUARD_AND_RUNTIME_REPAIR` 已完成隔离基础设施修复；CPython 3.11 中央数据工程环境于 2026-07-28 安装并通过 44 个锁定包验证。`L1-D2A-01B_CSMAR_LOCAL_RESEARCH_BOOTSTRAP` 已用两份真实 CSMAR ZIP 验证本地候选接入。`L1-D2A-03` 完成深交所/上交所官方 PIT 小样本、巨潮校准和 16 证券机械切片；`L1-D2A-04` Phase A 生成 568,246 B 的轻量候选和 CORE-01/02/03/04/06 缺口证据，但完成门未通过。`L1-D2A-04B` 于 2026-07-29 完成精确 33 包本地批次；随后用户批准的 `L1-D2A-04C_33PKG_LOCAL_SCHEMA_PIT_AND_TEXT_CLOSURE` 已完成本地闭合：33/33 封存包、25/25 逻辑表、1,197 个表—字段出现、559 个区分大小写字段名（大小写归并后 474 个），形成 32 个候选和 1 个只作重叠一致性校准的 `TRD_Dalyr` 原包。八个日行情候选形成 1990-12-19—2026-07-28 的 18,691,664 个只读唯一键、5,990 个证券和 8,741 个交易日，重叠 24 字段冲突为 0；另已构建 `TRD_Co`、`TRD_Cale`、`TRD_Cptl`、`TRD_Capchg`、`TRD_NoLimit`、`TRD_AdjustFactor` 和 18 张财务/审计候选。两张审计文本表通过 provenance 完整的 v2 零丢失解析器分别保全 80,301 和 82,264 行，CSV↔Parquet 双向差异为 0；旧 v1 保留但不再权威。33 个原包共 2,038,345,395 B，经 SHA-256 和 122 个 ZIP 成员复核后集中封存在 `data/market/quarantine/csmar-manual-export/intake-20260729-33pkg-v1/`，18 个 C 盘和 15 个 F 盘原件已精确删除。该批触及 CORE 14/20，但正式闭合仍为 0/20；财务首次 `available_at`、旧值与修订链、永久身份、市场级日历/会话、公司行动公告链、历史状态和来源权利仍未闭合。上交所和深交所官方源分别形成部分封存包，北交所在官方 WAF 403 处失败关闭；清华代理的 CSMAR 资源路由曾触发代码 27。当前状态为 `PAUSED_AFTER_D2A04C_LOCAL_CLOSURE__33PKG_INTAKE_SEALED__18_FINANCIAL_AUDIT_CANDIDATES_BUILT__1_OVERLAP_CALIBRATION_PACKAGE_RAW_ONLY__CORE_TOUCHED_14_OF_20__FORMAL_CORE_0_OF_20__OFFICIAL_PARTIAL__BSE_WAF_HARD_STOP__INSPECTION_ONLY`。执行授权已经关闭，任何后续外部采集需重新明确启动。并行的 `L2-P0_QDATA_FACTOR_INPUT_ENGINEERING` 已完成统一读取解析门和 12 项拒绝越权测试，但真实面板适配与研究仍被 L1 阶段门阻断。D2B 正式快照后继继续阻断。所有候选仍是 `INSPECTION_ONLY/NOT_ADMITTED`，正式 catalog 哈希未变。实际活动目录仍只有原有 13 个数据身份，均未达到 `quality_status=OK`；CORE-20、GCTX-8、HK-4、GLEQ-3 的正式快照仍为 0。计划目录现有 52 个数据家族；计划、合同、Pilot 和本地候选都不代表正式数据库已经建成。当前闭合证据见 `reports/data-governance/csmar-33pkg-local-closure-20260729/`，33 包事务回执见 `reports/data-governance/csmar-33pkg-transactional-intake-20260729/`，图书馆其他数据库初筛见 `reports/data-governance/library-economic-financial-source-census-20260729/`。

2026-08-03 `L1-RQDATA-01/02/03` 已完成离线环境、最小 entitlement、R0 endpoint/schema 与 coverage/PIT/volume 有界探针。米筐隔离环境位于 `control/runtime/ricequant-rqsdk/`，锁定 `rqsdk 1.7.4`、`rqdatac 3.6.1`、`rqdatac_fund 1.0.44` 及 33 个依赖；认证值只由进程环境读取，未写入工作区。RQData 已证实 5,901 条最新属性股票主档、2000 年以来 6,441 个交易日、沪深未复权日行情和公司行动正例及有界 `as_of` 行为；北交所日行情、基金主档和 ETF 成分权限被拒。全历史原始日行情样本外推约 3.48 GiB，约为当前 200 MiB 日配额的 17.84 倍。RQData 提升为“优先 R0 结构化源候选”，但仍限 `INSPECTION_ONLY`、正式准入 false；历史主档 PIT、完整修订链、保存/派生权、正式快照、RQFactor/RQOptimizer 完整权限和稳定性未闭合。正式 CORE/qdata 仍为 0/6，没有运行因子、回测或交易。回执见 `../reports/data-governance/ricequant-r0-coverage-probe-20260803-v1/README.md`。

2026-08-04 独立窄门 `R0_SHSZ_PV` 已通过。正式面板 `cn-a-share-r0-shsz-pv-panel@20050104-20260803-v1` 已原子发布并登记为 `RESEARCH_ONLY/OK/immutable/frozen`，精确 qdata、五份 authority/payload 哈希、真实 `PanelData` smoke 和至少一交易日滞后验收均 PASS。它只覆盖沪深普通 A 股未复权价量，不计入 canonical CORE-01～06，因此 R0/R1/R2 总门仍为 `NOT_MET`。活动 catalog 现为 14 个身份，其中该面板 1 个质量为 OK。RQData 日增量更新器及工作日 18:30 任务已经建立：首批 2026-08-04 delta 为 5,202 行、3,446,687 B，只进入 quarantine，正式晋级仍须独立原子发布。多因子侧已冻结 512 个确定性价量候选与预注册协议，尚未运行因子、IC 或回测，Validation/Holdout 均未消费。证据见 `../reports/data-governance/r0-shsz-pv-v2-formal-acceptance-20260804-v1/` 与 `../reports/data-governance/rqdata-shsz-daily-updater-20260804-v1/`。

2026-08-05 C-v1.1 已在该精确窄门快照上完成 GPU Discovery：512/512 因子运行及产物哈希通过，68 个定义属于公司行动不敏感层，50 个同时达到 Discovery BY-FDR≤10%。这 50 个候选又完成 3/5/8 年滚动窗与扩展窗、2009—2021 年年度测试、20 日 purge、5 日测试起点 embargo 和 15/30/50bps 成本描述；45 个在四方案下保持正平均 IC，6 个同时保持正平均 30bps 成本后 Top 20% 超额。6 个仍明显集中于成交额活跃度与日内价格位置家族，尚未做相关性去重、行业/规模中性、独立 Validation、Holdout、容量或账户映射，因此不是可靠因子或交易结论。运行由 RTX 3060/CuPy 完成；因子工厂 154/154、米筐候选构建 7/7 回归通过，`qdata doctor` 0 errors。同期米筐中信一级行业历史和指数/中债利率候选已封存于 quarantine，但仍为 `INSPECTION_ONLY/NOT_ADMITTED`，不改变 canonical R0/R1/R2。证据见 `../reports/data-governance/r0-shsz-pv-c-v1-1-discovery-20260805-v1/`。

### 2026-07-30 当前覆盖口径

2026-07-30 状态覆盖上段末尾的旧暂停口径：用户已经启动有界 `L1-D2A-04B` 来源闭合。最初的 selected-core combined 审计历史结果为 0/4、16 个 BLOCK 门 `PASS=1 / FAIL=12 / UNKNOWN=3`；两阶段架构复核随后确认其中 3 项是发布后检查并会造成前置循环，故旧审计重分类为 `POST_ADMISSION_AUDIT`。新的非循环 Stage A 对 CORE-01/02/03/06 仍为 0/4 ready，阻断数分别为 11/9/12/11；Stage B 因 `PHASE_A_NOT_READY` 阻断，正式 CORE 仍为 0/20，`datasets.json`、质量状态、`data/market/snapshots/` 和 `data/published/` 均未被提升。CSMAR Python API 仅通过个人账号认证，数据库枚举返回无数据权限，不能作为每日更新源；本地每日增量候选引擎新旧 14/14 测试通过，并已拒绝 manifest 外额外 payload、shell 元字符与链接路径，但仍是 `INSPECTION_ONLY` 合并内核，没有真实下载器、生产发布器或调度器。35/35 必建身份已完成来源映射；四源无密钥检查适配器 fixture 4/4 通过，单次 live 为 BIS、美国财政部、HKMA 3/4 结构通过，ECB 的本机 TLS 失败已隔离且未重试。CORE-02 形成沪深 2015—2026 的 8,454 条逐所逐日候选、28 条公告 lineage 和 12 条时段记录；ISO 10383 官方页面及 CSV 已闭合 XSHG、XSHE、BJSE 三所 MIC 映射子检查，但 `Q1/Q2` 仍 FAIL，北交所完整日历/时段链、完整临时停市档案、早期时段版本链与日内发布时间仍未闭合。所有输出仍是 `INSPECTION_ONLY/NOT_ADMITTED`，正式可准入来源为 0。D2A-04B 当前只允许官方/许可/PIT/身份/日历/行动/状态的有界闭合和无密钥最小检查；付费、新密钥、全历史批量回补、正式 catalog 写入、质量晋级和因子研究仍未获授权。四份上游报告的统一哈希回执与 27/27 安全回归见 `reports/data-governance/nightly-standardization-and-blockers-20260730-v1/`；分项证据见 `reports/data-governance/selected-core-two-stage-admission-architecture-audit-20260730-v1/`、`reports/data-governance/core02-official-calendar-session-closure-20260730-v1/`、`reports/data-governance/stage-a-core02-iso-mic-evidence-20260730-v1/`、`reports/data-governance/local-daily-update-engine-20260730-v1/`、`reports/data-governance/csmar-python-api-entitlement-probe-20260730-v1/`、`reports/data-governance/official-automatable-source-inventory-20260730-v1/`、`reports/data-governance/library-structured-vendor-coverage-20260730-v1/`、`reports/data-governance/public-official-api-preflight-20260730-v1/` 与 `reports/data-governance/public-official-api-inspection-adapters-20260730-v1/`。

同日新增的 CSMAR 补充 intake 已把 17 个 C 盘来源非破坏性地集中到 `data/market/quarantine/csmar-manual-export/intake-20260730-08pkg-03manual-v1/`，内容去重后为 10 个唯一 blob（7 ZIP + 3 PDF）；7 个 ZIP 的 31 个成员与 28/28 个证据引用均通过完整性复核。17 个原件在 intake 时点继续保留，随后已与新增指数批合计 35 个已验证来源一起移入 Windows 回收站且未清空。补充批绑定 7 个隔离候选，新增 `CORE-09/10` 候选触及，使非正式投影达到 16/20；冻结 33 包的历史口径仍是 14/20，正式 CORE 仍是 0/20，Stage A 仍是 0/4。标准 TRD 手册补清供应商层成交量/成交额零值、涨跌停价单位和 `LimitStatus` 枚举/计算说明；TRDNEW 的 `Filling` 语义只属于升级表，不转用于标准 `TRD_Dalyr`。本批没有修改 `datasets.json`、质量状态、正式快照或研究权限，证据见 `reports/data-governance/csmar-arrival-intake-20260730-v1/`、`reports/data-governance/csmar-intake-supplemental-evidence-20260730-v1/` 与 `reports/data-governance/csmar-downloads-cleanup-20260730-v1/CLEANUP_RECEIPT.json`。

第二个 2026-07-30 指数日行情 intake 封存 18 个来源别名、435,572,489 B，内容去重为 12 个唯一 ZIP、397,397,885 B；manifest SHA-256 为 `10439a5e5857fd9cf5c00c36aa2cd3d5fcfaac52bd11a67efff4e31f451eff23`，封存时点 retention receipt SHA-256 为 `36b8fc683cef53a460883813f4f21b3265db4c06833892dc550b53945299051c`。两个当日 intake 合计 35 个来源、632,131,463 B 的回收站操作绑定 22 个唯一目标对象（19 个 ZIP + 3 个 PDF）；全部对象哈希通过，19 个 ZIP 完整读取/CRC 通过，回收站未清空。用户声明回执另精确绑定 3 个 intake、68 个来源别名（别名字节合计 2,670,476,858 B）、55 个 canonical 唯一 blob（内容去重后 2,585,514,984 B），只覆盖本机长期保存、校内非商业研究及内部派生，禁止原始数据外分发和商业使用；API/自动批量、离校后处置、官方来源权利与独立合同核验未覆盖，故不产生正式准入。

Stage-A auditor contract v3 的 2026-07-31 v4 packaging seal 对 9 个上游包及递归绑定完成严格只读审计；v4 封存时 auditor `--verify-only` PASS。五个 authority 文件于 2026-07-31 获准同步后，当前 live verify 预期 FAIL；八项错误集合精确匹配五个旧 authority 哈希绑定、CORE01/CORE03_CORE06 live guard 与 aggregate formal-guard 漂移，post-sync receipt 位于 `reports/data-governance/stage-a-authority-sync-post-verification-20260731-v1/`，其 `REPORT_MANIFEST.json` SHA-256 为 `46652312bb20fe021c013b7bc06581c359fd324d525b192b7d9e826a89a03f75`。当前 live 状态明确为预期 FAIL；strict upstream package integrity 仍为 9/9，网络请求 0、写入 0、队列状态变化 0。CORE-02 最终失败关闭状态为 Q1 FAIL、Q2 FAIL、Q3 UNKNOWN、Q4 PASS（XSHG/XSHE/BJSE MIC 子检查 PASS 不触发门转换），CORE-03 Q2/Q3 UNKNOWN，CORE-06 Q3/Q4 FAIL。`TRD_Dalyr` 全量观测审计覆盖 8 个候选、464 个分区和 18,691,664 个去重键，主键冲突为 0，并识别、列出 5 条成交量/成交额零值不一致；其观测/供应商语义子评估为 PARTIAL，未发生正式门状态转换。绑定报告包路径为 `reports/data-governance/stage-a-evidence-consolidation-20260731-v4/`，`REPORT_MANIFEST.json` SHA-256 为 `751ecff9d6bb39cbb88c919c01a910b92c3e8e50fd65676d0472c3f868bfc4d3`。alias receipt 位于 `reports/data-governance/stage-a-evidence-consolidation-20260731-v4-alias-receipt-v1/`，其 `REPORT_MANIFEST.json` SHA-256 为 `587077cc0871960398bf4ec3ae4069c071410e81a3ced8212945f950f6dd4435`；它明确内部 run ID `stage-a-evidence-consolidation-20260730-v3` 是固定 auditor contract 标识，v4 仅为 2026-07-31 的第四次 packaging seal，不是正式证据根或门转换。v4 与 post-sync receipt 均为 `formal_effect=NONE`；Stage A 仍为 0/4，正式来源准入为 0，全部候选仍为 `INSPECTION_ONLY/NOT_ADMITTED`；Stage B 与 D2B blocked，`datasets.json`、正式快照及 `data/published/` 均未变化。

同夜新增的三个维护骨架继续严格限于 `INSPECTION_ONLY`：日更编排状态层 10/10 新测试（另复跑既有日更 14/14）、schema 演化分类器 14/14、revision/vintage 事件账本 10/10；正式 catalog 哈希未变，也没有真实下载、worker、调度、正式发布或研究。当前 Stage A 的 43 个整项阻断中，纯靠现有磁盘候选和标准化代码可完全闭合的为 0/43；本地机械工程已经能够记录、拒绝和重放，但不能自行补造官方身份、北交所与完整时段/临停历史、来源权利、真实字段字典、发布时间或规则版本。

2026-07-31 新增受限本地日更 inspection worker：它把已经落地、已经标准化的单日 CSV/JSONL 与日更编排状态层和候选合并内核连接起来，绑定 dataset/source/scope、冻结日历、最早未完成日期、请求指纹和候选 manifest 哈希；构建后校验失败、输入漂移或执行异常均记录失败并清除活动 claim，路径父链 reparse 也会失败关闭。相关链 34/34 回归通过。默认 PLAN 不写入，显式执行也只写 `tmp/`；它没有真实来源下载器、凭据、调度器、生产 worker、正式 catalog、质量晋级或研究入口。同期清华资源只读预检确认 CCER 已购 A 股表覆盖存在，但 WebAPI entitlement 尚未确认，其他校内资源也未被推定为生产 API。正式 Stage A、来源准入和 CORE/GCTX 计数均不变。

### 2026-08-02 RESSET 四包候选到达

用户人工导出的 `COMNMHIS`、`DELINFO`、`STKSPCTRMT`、`SUSPNSNRESMPTN` 四包已按原始 ZIP 哈希封存到 `data/market/quarantine/resset-manual-export/intake-20260802-04pkg-v1/`；`INTAKE_MANIFEST.json` SHA-256 为 `d7a8bdacbe8ed8d4876179a0e0c0be293b2a15a0fab39daf8ccb2ffea7d0c969`，四包合计 4,524,042 B。隔离候选分别形成 9,778、8,936、4,105、81,535 行，候选 manifest 位于 `tmp/local-dataset-build/resset-comnmhis-candidate/`、`resset-delinfo-candidate/`、`resset-stkspctrmt/`、`resset-suspnsnresmptn/`。它们只为 CORE-01 的名称/退市/代码历史、CORE-03 的停复牌事件、CORE-06 的 ST/特殊处理与停复牌状态提供新增候选证据；CORE-02、CORE-04、CORE-05 没有因此闭合。报告包 `reports/data-governance/resset-r0-core01-core06-arrival-intake-20260802-v2/` 已绑定封存 intake 与四个候选，`VERIFICATION.json` SHA-256 为 `3aa133b526a360e963862bafd7105c0c29fc1cc080fcfc98673b232717a1bd95`；供应商在线字典与使用权只读证据见 `reports/data-governance/resset-vendor-dictionary-evidence-20260802-v1/`。全部资产仍为 `INSPECTION_ONLY/NOT_ADMITTED`、`formal_effect=NONE`；Stage A 保持 0/4，CORE-01～06 正式准入 0/6，完整 CORE 0/20，R0/R1/R2 均为 `NOT_MET`。本次没有写入正式 catalog、晋级质量、发布快照或开放研究读取。

### 2026-08-04 RESSET Downloads 汇总与可恢复清理

后续盘点把本地 RESSET 资产归并为 11 个数据包和 1 个说明文档包：除上述四包外，`DIST` 分配包已封存并形成 57,287 行候选；新增 6 个公司行动包和 1 个高频说明 RAR 已封存到 `data/market/quarantine/resset-manual-export/intake-20260804-06pkg-company-actions-01doc-v1/`，其 manifest SHA-256 为 `0c6d86fcac9f36e48a1c35e6cf6eb020d6e71464c26ae6e99f5e753c53b5bac8`。六张新表合计 557,690 行，已完成 schema 审计但尚未构建候选；七张公司行动表共发现 29 行宽异常，28 行可确定性修复，1 行 `APLA` 必须隔离或重下安全格式。C 盘 Downloads 的 12 个 RESSET 原件在逐文件哈希核对后移入 Windows 回收站，回收站未清空，未触及其他文件。当前 5 张表已物化、6 张待建候选；正式 catalog、Stage A、CORE 和 R0/R1/R2 状态均未改变。统一回执和来源缺口矩阵见 `reports/data-governance/resset-downloads-consolidation-cleanup-20260804-v1/`，候选源状态登记于 `data/catalog/contracts/source_admission_v1/current_source_registry.json`。

存储采用单一权威主盘；用户于 2026-07-26 明确决定不建设独立数据备份并接受主盘不可恢复故障风险。快照、PIT 版本链、哈希、原子发布和主盘内逻辑恢复继续保留，独立备份不再是来源准入、容量报告或 `G-DATA` 阻断项。

2026-07-26 初步容量场景为：35 项必建库单主盘 1.17 / 3.31 / 9.49 TB，新闻与高频均关闭的 46 项为 1.63 / 5.28 / 17.09 TB，新闻启用但高频关闭的 48 项为 2.09 / 11.55 / 43.09 TB。以上仅为低/基准/压力场景点，不是 P50/P95 或上界；完整 52 项因 tick/L2 未冻结继续 `UNDETERMINED`。报告位于 `reports/data-governance/data-capacity-pipeline-pilot-20260726/`。

2026-07-28 的五年 CSMAR 日行情实测为 0.317 GiB ZIP、1.261 GiB 解压 CSV、0.646 GiB 密封候选（raw + Parquet + 证据）和 2.838 GiB 计划转换峰值；这说明单张日行情表是 GB 级而不是 TB 级。精选 CORE 源表暂按 20 / 40 / 60 GB 持久场景，CSMAR 48 个结构化子库按 0.5 / 1.0 / 1.8 TB 工程场景，清华多来源结构化研究仓主盘按 1.12 / 2.47 / 4.80 TB 场景。它们不替代严格 35 项对公告/财报原件、PIT 修订链和全球/HK/GLEQ 的容量模型。D2A-02 启动时 G 盘总容量 453.853 GiB、空闲 384.686 GiB；暂停时复测空闲 383.634 GiB。D2A-04C 完成后，`tmp/local-dataset-build/` 实测 3.493 GiB，`tmp/csmar-audit-text-candidate/` 因保留 v1/v2 与逐行证据实测 0.672 GiB，封存 raw 约 1.898 GiB，G 盘空闲 373.801 GiB。D2A-04B 历史计划批次包络为 150 GiB；用户明确不要求固定保留 200 GiB，因此 `user_fixed_free_gib=null`，但既有 240 GiB hard max 与 25% 最低空闲运行护栏没有被自动删除。该盘当前足以继续精选 CORE 与日频结构化建设，但不适合未经容量实测的整库回补。报告位于 `reports/data-governance/csmar-local-research-bootstrap-20260728/`、`reports/data-governance/d2a-02-csmar-priority-table-pilot-20260728/` 与 `reports/data-governance/csmar-33pkg-local-closure-20260729/`。

2026-07-30 复测：G 盘总容量 453.853 GiB、空闲 368.793 GiB；33 包封存 raw 为 1.898 GiB，`tmp/local-dataset-build/` 为 3.546 GiB，审计文本候选为 0.672 GiB，新指数候选 `tmp/csmar-idx-chgsmp-candidate/` 与 `tmp/csmar-idx-zero-loss-candidate/` 合计约 2.064 GiB。结构化低频继续不是空间阻断；这不授权未经容量预检的整库或高频回补。

## 目标主线

```text
L1 数据：行情 / 主数据 / 公告 / 新闻 / 资金流基础数据
        ↓ G-DATA
L2 特征与算法：市场 + 资金流 + 新闻事件 + 多因子候选
        ↓ G-ALG
L3 验证与阶段门：走样本外 / 成本 / 压力 / Holdout / 风险
        ↓ G-VAL
L4 发布与 Shadow：合同化特征 / 组合 / 容量 / 执行仿真
        ↓ G-SHADOW
L5 报告、监控、复盘与数据质量反馈
```

详细执行顺序、当前暂停包、拟议下一包和被阻断的正式后继见 `ROADMAP.md`。这条主线覆盖研究生产链，但不是自动交易系统。任何进入组合层的特征都必须有版本、可用时间、许可和阶段门证据。

## 当前核心项目

| 项目 | 当前物理路径 | 架构角色 | 当前判断 |
|---|---|---|---|
| Capital Flow Attribution | `${QUANTIFICATION_ROOT}\capital-flow-attribution` | 资金行为特征生产者 | G1 仍为 `REVIEW`；尚未向多因子正式发布 |
| 多因子 | `${QUANTIFICATION_ROOT}\多因子` | 数据目录、因子研究与阶段门 | V6 为 `NO_FACTOR`；C-v1.1 的 512 因子 Discovery 与 50 因子稳定性描述已完成，6 个成本描述幸存者待去重与独立 Validation；canonical R0/R1/R2 未开放 |
| News Signal Agent | `${QUANTIFICATION_ROOT}\news-signal-agent` | 新闻采集、结构化与事件特征候选 | 本地工作副本；发布镜像另存于同级目录 |
| CWOPT | `${QUANTIFICATION_ROOT}\CWOPT相关\CWOPT` | 外部协作独立实验线路 | 合作者负责开发；本地仅人工审核后从 GitHub 拉取，不自动并入主线 |

## 支撑层

- `.agents/harnesses/quant-research-reliability`：研究可靠性与安全性验证。
- `.agents/skills-disabled/`：已退役或待升级 Skill，只保留历史与回归用途。
- `data/`：统一数据入口；未来获批的新事实数据在 `data/market/` 内按 `raw/curated/snapshots/quarantine` 分层；`data/published/` 是通过合同的派生快照根；`data/catalog/` 区分实际资产与计划组合，并登记用途上限、维护合同和发布边界。
- `data/catalog/dataset_cli.py`：Codex、Agent 和项目程序共用的只读精确身份解析器。
- `reports/`：跨项目持久报告根。
- `tmp/`：临时运行根，不等于可立即删除；多因子旧试运行的 8.090690 GiB 临时因子矩阵已于 2026-07-19 通过逐文件审计后清理。
- `.codex/`：当前为空的项目级 Codex 配置位置。
- `杂/`：用户选定的非量化主线材料区，包含汇丰参考、紫荆路口媒体项目和三份已分类散落文档；默认不参与量化流程。

## 管理文件

- `ROADMAP.md`：工作区五层总路线、当前活动层、阶段门和下一唯一工作包。
- `projects.yaml`：机器可读的项目、状态和依赖登记。
- `FILE_DISPOSITION.yaml`：每个顶层条目的保留、归档或迁移建议。
- `DEPENDENCY_AUDIT.md`：路径耦合与迁移阻断项。
- `STORAGE_POLICY.md`：数据、证据、缓存和临时文件的保存规则。
- `MIGRATION_PLAN.md`：未来物理整理的阶段和验收条件。
- `../data/catalog/README.md`：统一数据管理规则、权威顺序和登记流程。
- `../data/catalog/DATABASE_MANUAL.md`：数据库现状、物理实现、PIT、存储合同和完整多因子目标说明书。
- `../data/catalog/MAINTENANCE_RUNBOOK.md`：日增量、月度合并、修订、schema 演化、监控和人工批准制度。
- `../data/catalog/datasets.json`：机器可读的数据域、数据集与跨项目发布合同。
- `../data/catalog/candidate_assets.json`：quarantine 候选的中央只读证据索引；不是正式数据目录，不授予研究读取。
- `../data/catalog/planned_datasets.json`：机器可读的未来数据家族清单；仅规划，不可读取。
- `../data/catalog/contracts/g_data_core_v1/`：20 项核心数据族的字段、来源、许可、PIT、schema 与质量阻断合同；不是可解析数据资产。
- `../data/catalog/contracts/source_admission_v1/current_source_registry.json`：跨 D2A-01～04C 与 CSMAR 人工导出的当前候选来源状态覆盖表；它记录 33 包迁移、候选闭合和剩余缺口，但不修改历史 runner 合同，也不授予新的网络执行、正式准入或研究读取权。
- `../data/catalog/contracts/source_admission_v1/current_source_registry.json` 还记录 2026-07-30 用户授权的有界 D2A-04B 来源闭合、两个新增 intake 与回收站清理、精确资产权利声明、Stage-A v2 五包审计、CORE-01/02/03/06 当前门和本地增量候选引擎状态；授权来自用户而不是登记文件本身，登记仍不能授予正式 catalog 写入、质量晋级或研究读取权。
- `../data/catalog/contracts/atomic_publish_v1/`：只允许隔离合成工作区的原子发布、请求、恢复与锁绑定合同；不授予正式数据写入权。
- `../data/catalog/contracts/adapter_gate_v1/`：`SMOKE_ONLY` 适配器样本门；验证宽度、lineage、单位、类型、主键、NULL、数值边界和 OHLC，但不证明真实映射语义。
- `../data/catalog/ACCESS_POLICY.md`：统一读取意图、用途上限和 Agent/Codex 边界。
- `tools/validate_dataset_catalog.py`：统一目录的路径、身份和状态校验器。
- `tools/validate_core_data_contract.py`：核心合同的结构、字段继承、来源引用和计划一致性校验器。
- `tools/validate_planned_dataset_catalog.py`：锁定 52 项分组、唯一身份、来源 profile 和 28/35/52 完成定义的只读校验器。
- `tools/validate_adapter_sample.py`：适配器样本门 CLI；只返回 `SMOKE_ONLY` 判定，不修改 catalog。
- `runtime/data-engineering/`：CPython 3.11 / Windows AMD64 数据工程精确版本锁、中央 `.venv` 与验证器；44 个锁定包已通过，仍不是 wheel 哈希仓。
- `runtime/ricequant-rqsdk/`：米筐专用隔离 CPython 3.11 环境；33 个包锁定并离线验证，当前含 RQSDK/RQData/基金扩展。用户已在自己的会话中交互配置认证，工作区不保存或读取其值；该运行时为 `REBUILDABLE`，不得替代正式 qdata 快照。
- `tools/run_candidate_daily_maintenance.py`、同名 `.cmd`：工作日 19:20 统一维护价量、B 路径候选并刷新中央候选索引；只写 quarantine/交易回执/候选索引，不自动晋级正式 catalog。旧分立任务保留但已禁用。
- `tools/verify_r0_shsz_pv_v2_acceptance.py`：精确验证正式窄门身份、qdata、原子发布、授权消费、五份 authority/payload 哈希和 PanelData smoke。
- `../tmp/ricequant-capability-probe/`：米筐离线、最小 live、R0 schema 与 coverage/PIT/volume 有界探针的 `INSPECTION_ONLY` 输出根；禁止凭据、无边界批量数据、bundle、正式快照和研究读取。
- `../data/catalog/contracts/local_download_build_v1/`：人工下载 CSV/ZIP 的候选接入合同、CSMAR current-pilot request、D2A-02 五年日行情 request、33 包财务/审计候选 request、全字段参考模板和 intake manifest；只允许写 `tmp/`，不下载、不登记 catalog。
- `tools/qdata_build_downloaded.cmd`：已下载包的一条命令计划/构建入口；默认只计划，显式 `-Execute` 才生成 `INSPECTION_ONLY` 候选。
- `../reports/data-governance/csmar-local-research-bootstrap-20260728/`：CSMAR 样本测量、8 包日行情下载计划、覆盖与容量边界。
- `../reports/data-governance/d2a-02-csmar-priority-table-pilot-20260728/`：五年全字段日行情候选、全量机械审计、表级 census、容量测量和财务 PIT 权限阻断回执。
- `tools/run_data_capacity_pipeline_pilot.py`：只允许写入 `tmp/data-capacity-pipeline-pilot/` 的容量与机制 smoke 运行器；不登记正式数据。
- `../reports/data-governance/data-capacity-pipeline-pilot-20260726/`：35/46/48 项容量场景、HF 条件公式、来源可达性和管道缺陷证据。
- `../reports/data-governance/d12a-01-atomic-guard-runtime-repair-20260727/`：D12A-01 隔离原子防护、运行时锁和适配器门的验收回执与机器可读验证记录。

## 当前约束

1. 本索引不改变任何运行路径。
2. `releases/`、`references/`、`archive/`、`inbox/` 仍是可选目标分类；用户已选择以现有 `杂/` 承载本批非核心材料。
3. 根 `.git`、多因子剩余不可变运行、CWOPT 独立 Git 和发布同步脚本都必须单独处理；旧 Qlib 删除不授权处理这些范围。
4. 三份 Loose files 已完成内容检查并由用户移入 `杂/`；原文件哈希保持不变。
5. 总 Roadmap 不替代物理迁移计划；路线推进不自动授权移动数据、运行正式研究或拉取外部仓库。
6. 每一批 L2—L5 工作都必须同步删改总 Roadmap；过期轮廓和冲突方案要被替换，不允许仅追加新版本后继续推进。
7. 新闻原文和新闻事件特征当前由用户暂停；资金流仍是独立扩展分支。维护手册不授权自动下载、创建定时任务或删除旧分区。
8. 全域数据库目标固定保留 52 项；20 项 CORE、8 项 GCTX、4 项 HK 和 3 项定义范围内 GLEQ 构成 35 项必建研究数据库范围。新闻、高频、券商和未来付费数据的暂缓只改变执行顺序，不得从总 Roadmap 或计划目录删除。
9. 当前不建设独立备份、不购买第二备份盘、不配置对象存储备份；历史报告中的备份建议不再代表当前计划。任何未来备份建设必须由用户重新明确批准。
10. `L1-D2A-02` 保持“五年日行情候选通过、财务 PIT 暂停”的历史状态；`L1-D2A-03` 已以 `PARTIAL` 暂停。D2A-04 Phase A 已完成轻量本地候选工程但完成门未通过；D2A-04B 的精确 33 包本地批次已完成并暂停；D2A-04C 已完成只读结构/PIT审计和审计文本零丢失 v2 闭合。当前已实际构建 CSMAR 日行情 1990—2026 分段全历史、公司当前截面、日历、分配、股本变动、无涨跌停限制、日复权因子以及 18 张财务/审计候选，批次为 32 个候选 + 1 个重叠校准包；旧 v1 候选保留但不再权威。旧“无权限”和“2015—2019 仍生成、0 文件”只保留为历史回执，不再代表当前状态。SSE/SZSE 官方部分包已封存，BSE 在官方 WAF 403 处硬停；全部仍为 `INSPECTION_ONLY/NOT_ADMITTED`，正式准入和正式 CORE 都为 0。D2B 仍阻断；继续外部采集、付费、新密钥、正式 catalog 登记、质量晋级和因子研究未获批准。历史门户回执位于 `reports/data-governance/d2a-04b-source-closure-20260728/d2a04b-portal-preflight-20260728-v1/`，当前 CSMAR 闭合证据位于 `reports/data-governance/csmar-33pkg-local-closure-20260729/`。
    2026-07-30 覆盖更新：有界 D2A-04B 来源闭合已经获批并执行；旧 combined 审计的 0/4 固定为 `POST_ADMISSION_AUDIT` 历史结果，当前非循环 Stage A 仍为 0/4 ready，Stage B blocked，因而没有正式 catalog 写入、质量晋级或快照发布。当前可以继续同一冻结范围内的来源闭合与最小无密钥预检，不代表批准付费、新密钥、批量历史回补、D2B 快照发布或因子研究。
11. 研究采用四层准入门；四层都不缩减 `CORE20 + GCTX8 + HK4 + GLEQ3` 的 35 项必建数据库范围或 52 项总目标，通过低层也不代表通过高层。当前正式 CORE 仍为 `0/20`、GCTX 仍为 `0/8`，因此四层正式状态均为 `NOT_MET`：
    - `L0_INSPECTION`：只允许候选数据、字段、来源与管道检查；`INSPECTION_ONLY/NOT_ADMITTED` 数据不得进入因子 IC、回测或研究结论。
    - `R0_PV`：要求 `CORE-01～06` 正式合格不可变快照、精确 `qdata --intent research` 读取及价量用途的 PIT、质量、恢复、防泄漏、样本外和可复现验收；只允许动量、反转、波动率、换手率、流动性等价量研究，禁止价值、质量、成长、行业和宏观结论。
    - `R1_CLASSIC_CN`：要求 `CORE20` 正式合格不可变快照及相应 L2 验收；允许经典 A 股横截面研究，包括价值、质量、成长、规模、行业中性及 `R0_PV` 因子，但不允许依赖全球环境的结论。
    - `R2_A_SHARE_GLOBAL`：要求 `CORE20 + GCTX8` 正式合格不可变快照及相应 L2 验收；允许加入利率、汇率、商品和全球市场环境的完整 A 股研究。
