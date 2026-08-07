# Quantification 工作区导航

本文件适用于 `${QUANTIFICATION_ROOT}` 及其全部子目录。默认使用中文沟通。

## 每次工作前

涉及工作区架构、清理、迁移、项目选择或跨项目修改时，依次读取：

1. `control/WORKSPACE_INDEX.md`
2. `control/ROADMAP.md`
3. `control/projects.yaml`
4. `control/FILE_DISPOSITION.yaml`
5. `control/DEPENDENCY_AUDIT.md`
6. `control/STORAGE_POLICY.md`
7. `control/MIGRATION_PLAN.md`

涉及数据采集、读取、快照、因子输入或跨项目数据交换时，还必须读取：

8. `data/catalog/README.md`
9. `data/catalog/DATABASE_MANUAL.md`
10. `data/catalog/MAINTENANCE_RUNBOOK.md`
11. `data/catalog/SOURCE_POLICY.md`
12. `data/catalog/DATA_QUALITY_POLICY.md`
13. `data/catalog/datasets.json`
14. `data/catalog/planned_datasets.json`
15. `data/catalog/ACCESS_POLICY.md`

进入具体项目后，再读取该项目自己的 `README.md`、`STATUS.md`、协议和局部 `AGENTS.md`（若存在）。项目自身状态文件优先于根目录概述；实际目录和运行配置优先于人工台账。

## 强制边界

- 未经用户明确批准，不移动、重命名或删除现有项目、数据、报告、发布副本和研究运行。
- 路径变更前必须更新依赖审计，列出调用方、替换方案、验证命令和回滚位置。
- 不修改多因子冻结运行、快照、manifest、provenance 或其哈希链。
- 多因子 `factor_cache/` 属于可重建缓存，不是正式 manifest artifact；清理前仍必须确认运行已终止、缓存未列入 artifacts、完成逐文件清单与正式证据复核，并取得用户明确批准。
- 不处理根 `.git` 的垃圾对象、初始化、提交或清理；这是一项需要单独批准的任务。
- `.agents/skills-disabled/` 中的 Skill 不得自动调用。`NeedsUpgrade` 和 `PendingReplacement` 都视为禁用。
- Harness 测试通过不等于研究阶段门通过；不得把骨架、测试或候选信号写成正式投资结论。
- 不把密钥、令牌、券商凭据或账户信息写入项目文件。
- 新数据在被项目读取前必须登记到 `data/catalog/datasets.json`；项目 manifest / provenance 是数据事实权威，工作区目录只做汇总引用。
- `data/catalog/planned_datasets.json` 只表示未来计划，不是实际目录；Agent/Codex 不得解析、扫描、下载或将其中条目作为研究输入，除非用户明确批准相应接入批次且资产已进入 `datasets.json`。
- 修改计划数据范围、分组、必建/暂缓状态或完成定义时，必须同步 `control/ROADMAP.md`、`control/projects.yaml`、`control/WORKSPACE_INDEX.md` 和数据库说明文件，并运行 `control/tools/validate_planned_dataset_catalog.py`；计划校验通过仍不表示真实数据存在。
- `data/catalog/MAINTENANCE_RUNBOOK.md` 是已确认的维护合同，但当前统一更新器、调度器和 compaction 尚未实现；不得因手册存在就自动采集、修改 catalog、创建定时任务或删除旧分区。
- `L1-D12A-01` 已于 2026-07-27 完成隔离基础设施修复：严格请求 schema、Windows 真子进程 hard-crash/PID-reuse 恢复、恢复身份与路径绑定、并发恢复互斥、中央版本锁和 `SMOKE_ONLY` 适配器门均有回归证据。父项 `L1-D12A` 对正式数据仍为 `PARTIAL`；该证据不得解释为正式来源准入、正式 catalog 集成、调度器、正式 compaction、持续审计或无人值守更新已通过。
- CPython 3.11 中央数据工程 `.venv` 已于 2026-07-28 安装并通过 44 个锁定包验证。`control/tools/qdata_build_downloaded.cmd` 和 `contracts/local_download_build_v1/` 只处理用户已下载且明确授权的 CSV/ZIP，默认无写入 PLAN，显式 `-Execute` 也只能在 `tmp/local-dataset-build/` 生成 `INSPECTION_ONLY/NOT_ADMITTED` 候选；不得把本地候选解析为正式数据、绕过 `qdata` 用于研究、自动登记 catalog 或晋级质量。
- 官方优先、开源适配器、多源校准、全股票池、最大可验证历史和默认前复权投影按 `SOURCE_POLICY.md` 执行；工具开源不代表底层数据可任意再分发。
- 不确定、缺失、冲突、异常和失败数据按 `DATA_QUALITY_POLICY.md` 处理；禁止把未知填 0、静默覆盖来源冲突或在 L1 对关键事实做统计插补。
- 研究及以上用途必须钉住明确 `snapshot_id`，禁止依赖 `latest`；可变数据库或缓存必须先导出不可变快照再进入研究。
- 用户于 2026-07-26 明确决定不建设独立数据备份：不购买第二备份盘、不配置对象存储备份、不把备份权利或备份容量作为数据准入和阶段门。主盘内的 raw、修订链、不可变快照、manifest、哈希、PIT 截断、原子发布和逻辑恢复仍必须保留；主盘物理损坏后可能无法精确恢复的风险已被接受。除非用户以后明确重启备份议题，Agent/Codex 不得将独立备份重新列为当前阻断项或自动创建备份。
- Codex/Agent 查找数据时必须优先使用 `data/catalog/qdata.cmd list|describe|resolve`（底层为 `dataset_cli.py`）；不得靠目录名猜测、自动扫描未登记目录或绕过 `purpose_ceiling`。
- 统一解析器返回的是只读入口，不授予写权限，也不表示质量或研究阶段门通过。`DO_NOT_USE` 只能用于 `inspection`；研究必须用 `--intent research` 完成策略检查。
- 每一批涉及 L2—L5 的设计、实现、验证、发布或运维工作，都必须在执行前和执行后同步删改 `control/ROADMAP.md`：把目标层展开为当前计划，删除或改写过期占位与冲突方案，并按真实证据更新状态和下一工作包。Roadmap 未同步时，不得把该批工作视为完成或继续进入下一层。

## 目录语义

- `control/`：管理台账和迁移约束，不承载运行数据。
- `.agents/`：项目 Harness、Skill 状态和相关控制文件。
- `.codex/`：仅放本工作区的 Codex 配置；为空也可保留。
- `data/`：统一数据入口；`data/market/` 接收未来共享研究输入快照（行情、参考、基本面、事件等由 `dataset_id` 区分），`data/published/` 接收通过合同的跨项目派生快照，`data/catalog/` 只存实际目录、计划目录、说明书、读取策略和解析器。
- 未来获批的新共享事实数据在 `data/market/` 内按 `raw/curated/snapshots/quarantine` 分层；每日增量和月度 compaction 必须遵守维护手册，不得改写旧快照或把日频聚合成月频。
- `reports/`：持久的人类可读报告和验收证据。
- `tmp/`：可重建的临时产物；删除前仍须审计。
- `杂/`：非量化主线的混合材料区；其中内容仅在用户明确点名时进入，不得自动纳入量化项目、数据扫描或运行流程。
- 其余顶层项目和材料在物理迁移完成前继续使用现有路径。

## 当前整理策略

当前已进入“统一目录 + 中央共享根 + 受控分批迁移”阶段。既有两个冻结 Qlib 数据本体已于 2026-07-20 删除，活动 catalog 不再解析这些身份；tombstone、manifest、provenance、冻结配置和删除回执仅供历史审计。新共享快照进入 `data/market/` 或 `data/published/`。任何未完成的 proposed destination 仍只是规划。

工作区总路线以 `control/ROADMAP.md` 为准：当前只详细推进 `L1_DATA`，L2—L5 保持轮廓。CWOPT 由外部合作者开发，本地只在人工审核、工作树安全和明确授权后执行 GitHub 同步，不自动拉取或并入量化主线。

L1 当前执行顺序已获用户批准：先完成 20 项 `G-DATA-CORE`，再完成正式 A 股研究必需的 8 项 `G-DATA-GLOBAL-CONTEXT`；4 项 `G-DATA-HK-EQUITY` 与定义范围内的 3 项 `G-DATA-GLOBAL-EQUITY` 也属于完整数据库必建层，随后继续全域 52 项。扩展、新闻和高频计划不得因暂缓从 Roadmap 删除。核心事实优先使用官方/公开来源；既有校内学习研究权限下的 CSMAR 人工导出只作为结构化候选源，须与官方身份、日历、披露、公司行动和 PIT 证据校准，不自动购买或接入新的付费数据。行业采用证监会基线 + 申万研究主口径并允许多口径共存；指数按多提供商、行业/主题/REITs及收益变体尽可能完整登记。付费供应商、密钥、容量报告后的批量回补、质量晋级/豁免、新闻重启和高频/券商启用仍须逐次批准。完整建立 GCTX/HK/GLEQ 是范围决定，不等于已批准具体付费端点、凭据或全历史批量回补；定义范围外的全球全股票横截面仍需另行批准。

2026-07-26 容量 smoke 只产生低/基准/压力场景点：35 项单主盘为 1.17 / 3.31 / 9.49 TB；完整 52 项因 tick/L2 未冻结继续 `UNDETERMINED`。该 smoke 没有来源准入、正式快照或统计 P1–P3。`L1-D12A-01_ATOMIC_GUARD_AND_RUNTIME_REPAIR` 已于 2026-07-27 完成且仅限隔离基础设施。`L1-D2A-01_SOURCE_ADMISSION_AND_PILOT_ADAPTERS` 的正式来源部分仍因许可、关键字段语义和 PIT 冲突处于 `PAUSED__BLOCKING_CONFLICTS_REPORTED__NO_FORMAL_ADMISSION`；其 2026-07-28 子包 `L1-D2A-01B_CSMAR_LOCAL_RESEARCH_BOOTSTRAP` 已用两份人工下载的真实 CSMAR ZIP 完成本地候选验证。`L1-D2A-02` 随后把 2020—2024 五年日行情构建为 5,852,287 行、60 分区的隔离候选，但财务 PIT 未闭合。`L1-D2A-03` 已完成深交所/上交所 PIT 小样本、巨潮校准和 16 证券机械切片；官方 PIT v2 共 14 条，CORE 切片 509 行。`L1-D2A-04_CORE01_TO_CORE06_CANDIDATE_CLOSURE_AND_SELECTED_SOURCE_PILOT` Phase A 已用 568,246 B 轻量候选按哈希引用全部 60 个上游分区，形成 CORE-01/02/03/04/06 观察、字段和缺口证据，CORE-05 为 0 行；永久身份、官方日历/时段、公司行动条款与修订链、历史状态规则、Limit 字段语义和独立保存权未闭合，因此状态为 `COMPLETED__PHASE_A_LOCAL_ENGINEERING__D2A_04_COMPLETION_GATE_NOT_MET__PAUSED`。当前没有活动执行授权，D2B 继续阻断。下一包 `L1-D2A-04B_OFFICIAL_IDENTITY_CALENDAR_ACTION_STATUS_SOURCE_CLOSURE` 仅为待启动提案。`L2-P0_QDATA_FACTOR_INPUT_ENGINEERING` 已完成 12 项 qdata 输入解析与拒绝越权测试，但未读取候选、运行因子或消费 Validation；真实 `PanelData` 适配等待首个正式可研究快照。D2B 只验证首批正式快照与管道，不等于正式因子研究；可靠因子挖掘必须等 `CORE-20 + GCTX-8` 真实合格快照及 L2 PIT/防泄漏/样本外门通过。当前候选来源状态以 `data/catalog/contracts/source_admission_v1/current_source_registry.json` 为准；启动 D2A-04B、正式扩样、全历史回补、来源购买/合同、正式 catalog 登记、质量晋级和 D10A–D10C 均须用户重新明确启动。
