# 存储与保留策略

本策略先定义语义，不授权删除。任何现有内容在完成分类、依赖检查和回滚准备前都保持原位。

所有当前数据域和关键数据集统一登记在 `${QUANTIFICATION_ROOT}\data\catalog\datasets.json`。登记统一了发现、所有者、状态和用途边界；新共享快照进入中央数据根，既有冻结 manifest、provenance、哈希和必须保持的物理路径不被改写。

## 当前无独立备份决定

用户于 2026-07-26 明确决定不建设独立数据备份。当前存储合同因此采用“单一权威主盘 + 主盘内版本链与逻辑恢复”，不购买第二备份盘，不配置对象存储备份，不安排备份任务，也不把独立备份容量、备份许可或异盘恢复作为数据准入和 `G-DATA` 阶段门。

该决定不改变以下要求：raw 只追加、修订不覆盖、不可变 snapshot、manifest/provenance/内容哈希、精确 `dataset_id + snapshot_id`、PIT 时间截断、原子发布、主盘内中断恢复和可重建投影验证。`IMMUTABLE` 表示内容不得原地覆盖，不表示存在第二个物理副本。

已接受的残余风险是：主盘发生不可恢复的物理故障时，工作区可能只能从仍可访问的外部来源重新建设“当时可重新取得的版本”，无法保证精确恢复已下架、已修订或无法再次取得的 raw 与历史研究快照。除非用户以后明确重新批准备份，本风险不得被自动改写成备份采购或阶段门阻断。

## 当前容量场景

2026-07-26 容量与管道 smoke 没有建立全量库，只给出条件场景。统一按十进制 TB，主盘需求为 `(持久数据 + 工作峰值) / 0.75`，不含独立备份：

| 范围 | 单主盘低 / 基准 / 压力场景 |
|---|---:|
| 35 项必建库 | 1.17 / 3.31 / 9.49 TB |
| 新闻和高频均关闭的 46 项 | 1.63 / 5.28 / 17.09 TB |
| 新闻启用、高频关闭的 48 项 | 2.09 / 11.55 / 43.09 TB |
| 完整 52 项 | `UNDETERMINED`；tick/L2 未冻结 |

这些数字不是统计 P50/P95，也不是可信上下界。2026-07-24 报告中的 CORE 8 TiB 临时包络、12 TB 最低与 16 TB 推荐作为历史审计保留，不再冒充最新全域结论。当前可把 12 TB 视作 35 项的前置压力规格，但不得据此批准回补或声称 46/48/52 项一定装得下。

容量模型假设相同来源原件按内容哈希只保存一次，由多个数据集 manifest 引用；跨数据集 raw 对象去重尚未实现。正式采购和回补前还必须补齐 A 股跨年代 P1、全历史文档 P2、多提供商指数 P3、HK/GLEQ 实测和真实工作峰值。报告见 `reports/data-governance/data-capacity-pipeline-pilot-20260726/README.md`。

### G 盘当前 Pilot 双闸门

`L1-D2A-02` 于 2026-07-28 以 G 盘总容量 453.853 GiB、已用
69.167 GiB、空闲 384.686 GiB 为启动基线。本包新增持久数据、候选、
解压/转换峰值、spill 和活动投影的合计 footprint 不得超过 240 GiB；
任一时刻必须保留全盘至少 25%（按启动基线为 113.463 GiB）的空闲空间，
任一先触发即在写入前停止。使用完整 240 GiB 上限时按启动基线仍剩
144.686 GiB。

这只是精选 CORE Pilot 的运行护栏，不授权整库或全历史回补。每个下载、拆包、
转换和投影步骤前后都要重测实际空间；不允许通过自动删除既有数据、报告、
候选或缓存来满足闸门。

### D2A-04B 受限候选采集包络

用户于 2026-07-28 启动 D2A-04B 结构化 CORE 第一波候选采集，并明确不要求固定
保留 200 GiB；机器字段记为 `user_fixed_free_gib=null`。本批计划 footprint
包络为 150 GiB，只覆盖 `TRD_Dalyr` 缺口区间、完整当前公司截面、交易日历、
精选公司行动/状态表和官方闭合证据；不授权 CSMAR 48 库整库或其他全历史批量
回补。门户预检已因 `TRD_Dalyr` 全表“无权限”、控件禁用并只显示购买动作而
触发硬停止；购买动作未点击，本轮 0 下载、0 业务字节，因此上述 150 GiB 只是
保留的计划包络，不是已发生 footprint。当前状态为
`PAUSED__CSMAR_FULL_TABLE_PERMISSION_BLOCKED__NO_NEW_DATA__INSPECTION_ONLY`；
登录会话和元数据可见性不等于表授权。恢复需要已生效机构订阅会话，或用户以
既有权利人工导出并交付 2015—2019 包，不自动购买。预检回执位于
`reports/data-governance/d2a-04b-source-closure-20260728/d2a04b-portal-preflight-20260728-v1/`。

取消固定 200 GiB 预留不自动改写既有运行合同：240 GiB hard max 与全盘 25%
最低空闲仍生效，任一先触发即停止写入。本批不通过自动删除腾挪空间；下载和转换
前后继续测量实际 footprint。全部输出保持 `INSPECTION_ONLY/NOT_ADMITTED`，
容量验收只判断 price-volume candidate sufficiency，不运行因子。

## 五类存储

| 等级 | 含义 | 默认动作 | 典型内容 |
|---|---|---|---|
| `IMMUTABLE` | 冻结证据，路径与哈希属于研究合同 | 永不原地覆盖 | 研究运行、快照、manifest、provenance、验收证据 |
| `RETAINED` | 需要长期保留，可按版本演进 | 版本化、可追溯 | 正式数据、报告、发布产物 |
| `REBUILDABLE` | 可由锁定代码和输入重建 | 验证可重建后可清理 | 虚拟环境、下载缓存、构建缓存、因子缓存 |
| `TEMPORARY` | 单次运行临时物 | 有运行回执且无引用后可清理 | smoke 输出、中间文件、临时日志 |
| `UNCLASSIFIED` | 尚未确定归属和价值 | 保持原位，先检查 | 根目录散落文档和私人材料 |

## 当前目录解释

- `data/`：默认 `RETAINED`；带快照、manifest 或 hash 的内容升级为 `IMMUTABLE`；`data/catalog/` 只存目录和合同，不复制业务数据。
- `data/market/`：新共享研究输入的中央根；获批数据集按 `raw/curated/snapshots/quarantine` 分层，使用带域前缀的 `dataset_id` 区分。Raw 只追加，curated 分区和 snapshot manifest 不可变，quarantine 不得被研究读取。既有两个冻结 Qlib 数据本体已于 2026-07-20 删除；仅保留 tombstone、manifest、provenance 和删除回执。
- `data/published/`：跨项目不可变派生快照发布根；可变工作表、SQLite、缓存、失败运行不得写入。
- `reports/`：默认 `RETAINED`，尤其阶段验收、比较报告和人工决策依据。
- `tmp/`：目标语义是 `TEMPORARY`，但当前不据此自动删除。
- `tmp/local-dataset-build/`：人工下载包的不可覆盖候选运行；包含 raw 副本、Parquet、manifest、provenance、质量摘要和查询入口，统一为 `INSPECTION_ONLY/NOT_ADMITTED`。在正式准入、原子发布或用户批准清理前不得自动删除；也不得被项目当作共享研究数据读取。
- `多因子/research_runs/`：2026-07-19 已完成逐文件审计并清理 69.762775 GiB `factor_cache`；当前剩余约 10.632 MiB 正式研究证据，按 `IMMUTABLE` 保护。清理回执位于 `reports/storage-cleanup/multi-factor-cache-20260719/`。
- 多因子 `factor_cache/`：即使生成在研究运行目录内，也属于 `REBUILDABLE`，不因相邻 `COMMITTED.json` 自动升级为正式证据。终止运行的缓存只有在确认未列入 manifest artifacts、逐文件登记并通过正式证据复核后，才可由用户批准清理。
- `多因子/.tmp/`：属于 `TEMPORARY`。2026-07-19 已逐文件审计并清理旧试运行的 193 个因子矩阵，共 8.090690 GiB；保留旧 V1 的四个小型结果文件及运行日志，清理回执位于 `reports/storage-cleanup/multi-factor-tmp-cache-20260719/`。未来临时因子缓存仍必须按同一删除闸门处理。
- `control/runtime/data-engineering/.venv/`：由精确版本锁重建的中央数据工程运行环境，属于 `REBUILDABLE`，不是数据资产；删除或升级前必须重新验证 44 包锁和所有候选构建回归。
- `control/runtime/ricequant-rqsdk/.venv/`：米筐专用隔离运行环境，属于 `REBUILDABLE`；不得把 RQSDK 依赖安装进中央数据工程环境。当前 33 个包已锁定并离线验证，License 不得进入工作区，环境存在不表示任何数据权限或来源准入。
- `control/runtime/factor-research-gpu/.venv/`：独立的 GPU 因子计算环境，属于 `REBUILDABLE`；锁定 CuPy/CUDA 组件、NumPy 与 DuckDB，仅用于通过 CPU/GPU 等价门的研究计算，不与中央数据工程或米筐运行时合并，也不承载数据和凭据。
- `多因子/.venv/`：2026-08-05 创建后确认因中文路径导致 CuPy NVRTC 不可靠，已验证无进程、无代码引用，约 451 MiB；删除请求被当前运行安全策略阻断，继续标记为待清理的 `REBUILDABLE` 失败环境，不得作为执行入口。
- `data/market/quarantine/cn-industry-classification-pit/` 与 `data/market/quarantine/r1-market-reference/`：米筐 R1 参考数据候选根。候选可保留哈希和 manifest 供准入复核，但在 `available_at`、修订链、许可和正式 catalog 未闭合前统一为 `INSPECTION_ONLY`，项目不得直接研究读取。
- `tmp/ricequant-capability-probe/`：只允许保存环境盘点、明确获批的小样本 schema/哈希和失败回执，统一为 `INSPECTION_ONLY`；禁止 License、账户身份、批量历史、RQAlpha bundle、正式快照、因子和回测输入。
- `tmp/ricequant-rqalpha-bundle/`：若以后另行批准下载，属于可变 `REBUILDABLE` 缓存；不得直接登记为研究数据。任何正式回测必须另行绑定精确 bundle 版本/哈希和不可变 RQData 输入，或先导出正式快照。
- 各项目 `.venv/`、下载缓存和构建缓存：候选 `REBUILDABLE`；必须先确认锁文件、Python/系统依赖和重建命令。
- `archive/`：未来只放已退役但仍需追溯的材料，不代表垃圾箱。
- `releases/`：未来只放清洗后的发布镜像，不作为本地开发源。
- `references/`：未来只放外部参考材料，不进入运行依赖。
- `inbox/`：未来用于新进入但尚未分类的文件，不作为长期存储。
- `archive/data-duplicates/`：非权威旧数据副本的追溯区，统一标记 `DO_NOT_USE`，不得被项目自动读取。

月度 compaction 只新写合并分区和新 snapshot；任何仍被保留快照引用的日 delta 不得自动删除。无引用文件的清理仍需引用审计、保留期和用户批准。

旧数据退役与精确物理清理清单见 `reports/storage-cleanup/legacy-data-retirement-20260720/README.md`。批次 A+B 已完成；其余缓存、历史输出和证据不在该授权范围，仍需单独审计和批准。

## 统一登记闸门

新数据进入任何研究或运行前，必须先登记 `dataset_id`、`snapshot_id`、所有者、物理位置、可变性、许可、质量、用途上限、权威 manifest 和消费者。可变应用库及实验缓存默认不能作为研究输入；需要使用时先导出不可变快照并生成 manifest。

目录结构校验命令：

```powershell
& '${USER_PROFILE}\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  -X utf8 .\control\tools\validate_dataset_catalog.py
```

只读发现和用途检查：

```powershell
& '${USER_PROFILE}\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  -X utf8 .\data\catalog\dataset_cli.py doctor
```

## 删除闸门

删除候选必须同时满足：

1. 已确认类别为 `REBUILDABLE` 或 `TEMPORARY`；
2. 不在配置、脚本、manifest、报告或同步流程中被引用；
3. 有明确重建命令或不再需要的书面理由；
4. 有大小、路径、最后修改时间和内容摘要的回执；
5. 用户明确批准本批次删除范围。
