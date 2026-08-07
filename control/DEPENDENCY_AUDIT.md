# 路径与依赖审计

审计日期：2026-07-19  
结论：统一读取层和中央共享根已建立；一份无调用方的非权威数据库副本已按审计归档。核心项目、冻结快照、历史证据和可变工作库仍不得机械搬动。

## 第一阶段不变量（历史记录）

第一阶段只新增根 `AGENTS.md` 与 `control/`，当时没有修改项目代码、配置、数据、报告、发布副本或 Git 元数据。随后用户于 2026-07-19 完成了一批非核心材料移动，见第 7 节。

## 已确认的迁移阻断项

### 1. Capital Flow Attribution：高风险

- `configs/g1_research.json` 和 `configs/s1_pilot.json` 硬编码 `${QUANTIFICATION_ROOT}/data`、`reports`、`tmp` 以及多因子 Qlib 快照路径。
- `DATA_CATALOG.md`、`STATUS.md`、验收文档记录了冻结运行和报告的绝对路径。
- `protocol/DATA_CONTRACT.md` 规定统一数据根和只读发布边界。

结论：项目根可以将来整理，但 `data/`、`reports/`、`tmp/` 和 Qlib 快照不能先搬；必须同步修改配置并验证冻结证据引用。

### 2. 多因子：高风险

- `README.md`、运行手册和工具命令把 `${QUANTIFICATION_ROOT}\多因子` 作为固定项目根。
- 数据 manifest 的 `root_path`、`provenance_path` 含绝对路径并参与可追溯链。
- `research_runs/` 中证据与大体积缓存混合，不能按目录名直接判断可删除性。

结论：当前保持整个项目原位。未来如迁移，先定义 manifest 重定位规则和哈希兼容策略，且不得原地改写冻结运行。

### 3. News Signal Agent：中高风险

- `scripts/sync-to-github.ps1` 默认把同级 `news-signal-agent-github` 作为发布目标。
- 若只把发布副本移入 `releases/`，同步脚本会继续写旧位置或失败。

结论：开发项目暂不动；发布副本移动必须与同步脚本修改、dry run 和输出差异检查放在同一批次。

### 4. CWOPT：独立仓库边界

- `${QUANTIFICATION_ROOT}\CWOPT相关\CWOPT` 拥有独立 Git 历史、虚拟环境和下载缓存。
- 多因子已经迁出成为独立工程，但仍保留迁移来源记录。
- 当前用户工作树必须保留。

结论：把 CWOPT 视为独立实验线路，不纳入主线目录的机械重排。

### 5. 根 `.git`：单独风险项

- 根仓库目前没有可用基线提交，却覆盖大量未跟踪/忽略内容。
- 它不能为本轮整理提供可靠回滚，且清理对象本身具有破坏风险。

结论：冻结根 `.git`；是否保留、重建或移除必须另开明确任务。

### 6. 历史材料与发布副本：较低但非零风险

- `资金流向skill` 的历史输出仍被 Capital Flow Attribution 的数据目录文档引用。
- `stock-relative-dominance-release` 和风险事件报告虽不在当前主线启用，仍可能承担发布或追溯用途。

结论：先查反向引用和 Git 边界，再归档；“不启用”不等于“可以立即删除”。

### 7. 非核心材料移动：已完成并修复

- 用户将 `汇丰相关`、`紫荆路口_AI分享_PPT`、`多因子概要.txt`、`设计方案.docx` 和 `问题整理.txt` 移入 `${QUANTIFICATION_ROOT}\杂`。
- 三份散落文件移动前后 SHA256 一致；没有发现项目代码或脚本引用其旧路径。
- `紫荆路口_AI分享_PPT` 移动后发现 14 个源文件/说明文件共 17 处旧绝对路径引用。
- Python 脚本已改为根据 `__file__` 推导项目位置；字幕 README 的示例命令已更新为当前路径。
- 根 `AGENTS.md` 已由用户恢复至 `${QUANTIFICATION_ROOT}\AGENTS.md`。

结论：本批不影响量化主线。`杂/` 默认不进入量化扫描或自动运行；媒体项目若再次移动，Python 脚本无需再改，但 README 示例命令仍需复核。

## 每次物理迁移的必要检查

1. 用全文搜索列出旧路径的代码、配置、文档、manifest 和脚本引用。
2. 判断哪些引用是运行时依赖，哪些是不可修改的历史证据。
3. 记录源路径、目标路径、体积、Git 边界和备份/回滚位置。
4. 修改调用方并先 dry run；不得先搬后找依赖。
5. 运行最小相关测试、同步差异检查和哈希/manifest 检查。
6. 保留迁移回执，并再次搜索旧路径；确认无意外新写入旧位置。

## 8. 统一数据访问与低风险数据库归档批次（2026-07-19）

用户已明确批准按统一数据规则整理、迁移允许移动的数据，并让 Codex/Agent 通过统一入口读取。本批在执行路径变化前完成如下审计。

### 8.1 保持原位的数据

- 多因子 Qlib `2026-07-14`、`2026-07-18` 的数据本体已按第 9 节删除；manifest、provenance、目录哈希和 Capital Flow 历史配置继续记录原绝对路径。不得改写这些历史证据，统一解析器不再返回旧身份。
- `${QUANTIFICATION_ROOT}\资金流向skill` 的历史输出被旧 `fetch_manifest.json`、`context_manifest.json` 和 Capital Flow 文档引用；保持原位并继续标记 `DO_NOT_USE`。
- `news-signal-agent/data/news_signal_agent.sqlite3` 是应用持续使用的可变工作库；项目代码以项目相对路径读取，保持原位并限制为 `ENGINEERING_ONLY`。
- CWOPT `data/history` 和 `data/router` 被独立实验代码以相对路径读写；保持原位，补齐目录登记，禁止自动进入量化主线。
- `tmp/stock-relative-dominance/cache` 是已禁用相对强弱流程的可重建临时缓存；保持在 `tmp/`，补齐登记并标记 `DO_NOT_USE`。

### 8.2 允许迁移：新闻发布镜像的旧数据库副本

- 源：`${QUANTIFICATION_ROOT}\news-signal-agent-github\data\news_signal_agent.sqlite3`
- 目标：`${QUANTIFICATION_ROOT}\archive\data-duplicates\news-signal-agent-github\20260628-news_signal_agent.sqlite3`
- 大小：`18,337,792` bytes。
- 迁移前 SHA256：`025D3114D068FACD9DCC9AF66847B67AD7F1295CEB526D5F394FD2872689273C`。
- 调用方：未发现代码、配置、manifest 或文档把该副本作为运行输入；发布镜像 `.gitignore` 排除 `data/*.sqlite3`，源项目 `scripts/sync-to-github.ps1` 同时排除 `data` 和 `*.sqlite3`。
- 替换方案：无运行时替换；权威工作库仍为 `${QUANTIFICATION_ROOT}\news-signal-agent\data\news_signal_agent.sqlite3`，统一目录继续只登记该权威工作库。
- 验证命令：比较移动前后文件长度与 SHA256；确认源文件不存在、目标文件存在；运行目录校验器和统一解析器；再次全文搜索发布镜像数据库路径。
- 回滚：把目标文件原样移回源路径，复核长度与 SHA256。归档文件不得作为研究或应用输入。

### 8.3 统一读取替换方案

- 新增只读 CLI：`${QUANTIFICATION_ROOT}\data\catalog\dataset_cli.py`，以精确的 `dataset_id + snapshot_id` 解析物理路径和用途边界。
- 根 `AGENTS.md` 规定 Codex/Agent 先通过统一目录发现数据；不得自行猜路径、使用 `latest` 或把可变缓存提升为研究输入。
- 历史 Capital Flow 配置中的 `qlib_store` 保留为冻结运行证据；新任务和新配置必须先解析目录并记录明确数据身份。既有运行代码仍可兼容旧配置，不因本批失效。
- 最小验证：目录结构校验；CLI `list`、`describe`、`resolve`；从工作区根和项目子目录读取；`RESEARCH_ONLY` 成功解析、`DO_NOT_USE` 默认拒绝、可变工作库用于 research 时拒绝。

### 8.4 执行结果

- 新闻发布镜像旧 SQLite 已移动到审计目标；源文件不存在，目标文件存在，目标长度为 `18,337,792` bytes，SHA256 与迁移前一致。
- 中央共享根 `data/market/`、`data/published/` 和只读解析器已经建立。
- 目录已补齐 CWOPT `data/router`、相对强弱临时缓存和归档数据库副本，未改变其用途上限。
- 冻结 Qlib、历史资金流输出、新闻工作库和 CWOPT 缓存均未移动或改写。
- 迁移回执：`${QUANTIFICATION_ROOT}\reports\storage-migration\data-consolidation-20260719\README.md`。

## 9. 旧日频数据退役与清理批次（2026-07-20）

本节覆盖并更新 8.1 中“冻结 Qlib 保持原位供研究读取”的旧决定。用户已明确不再把现有 Qlib 作为过渡数据，并希望清理后重新建设。

### 9.1 已执行的阻断与退役登记

- 两个旧身份先被降为 `DO_NOT_USE`，完成删除后已从活动 `datasets` 移入 `retired_datasets`；
- `multi-factor-market-snapshots` store 保持历史、`DO_NOT_USE`；
- `qdata` 不再列出或解析这两个旧身份；
- 未改写旧 manifest、provenance、项目 release catalog 或冻结运行配置。

### 9.2 精确执行范围

- 批次 A：删除 `.staging/2026-07-18`，3 文件，559,147,864 bytes；
- 批次 B1：删除 `2026-07-14/qlib_bin` 和 `qlib_bin.tar.gz`，61,189 文件，1,273,790,270 bytes；保留同级 `PROVENANCE.json`；
- 批次 B2：删除 `cn-a-share-qlib-daily/2026-07-18/qlib_bin`，61,198 文件，715,868,066 bytes；保留同级 `PROVENANCE.json`；
- 合计删除 122,390 文件、2,548,806,200 bytes（约 2.374 GiB）。

### 9.3 调用方和影响

批次 B 被多因子 release manifest、provenance、README、Capital Flow 两份项目配置和多个 v8—v22 冻结运行配置引用。删除不影响未来的新基线，因为活动 catalog 已移除旧身份；但这些历史运行已不能从原路径完整复现。历史配置保留原样，作为当时依赖事实。

### 9.4 替换、验证与回滚

- 替换：新中央 Parquet 事实快照和新生成的前复权投影；旧 Qlib 不作为输入；
- 删除前验证：解析绝对路径、确认不是链接/根目录、复核文件数和字节数、确认没有活跃进程、保存清单和必要哈希；
- 删除后验证：目标不存在；实际 catalog 使用 tombstone/退役记录而非活动路径；`qdata doctor` 无结构错误；旧身份只能显示已删除历史状态；
- 回滚：批次 A 可重新下载；批次 B 只能依赖外部旧发布重新取得，未来可用性不保证，因此属于不可可靠回滚的物理删除。

完整清单与回执：`${QUANTIFICATION_ROOT}\reports\storage-cleanup\legacy-data-retirement-20260720\README.md` 和 `DELETION_RECEIPT.json`。执行完成时间为 2026-07-20T21:26:49+08:00；四个精确目标复核均不存在。

## 当前允许与禁止

- 允许：按统一目录只读发现和解析；向中央共享根追加通过登记闸门的新快照；只读查阅第 9 节的 tombstone 和回执。
- 禁止：凭目录名批量移动或删除；继续扩大本批迁移范围；改写冻结 manifest；清理根 Git；让禁用 Skill 自动恢复。
