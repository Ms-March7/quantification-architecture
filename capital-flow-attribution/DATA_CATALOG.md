# 数据目录

状态：`V22 REGISTERED / RESEARCH_ONLY / G1 REVIEW`  
更新时间：`2026-07-19`

本目录登记数据身份、来源、许可、物理位置和可用状态。登记不等于批准用于身份归因或生产发布；S1规范运行只用于研究审计。

工作区统一发现入口为 `${QUANTIFICATION_ROOT}\data\catalog\datasets.json`；本文件和各运行 manifest 仍是 Capital Flow Attribution 数据事实的项目级权威。

## 1. 目录状态

- `PROPOSED`：合同中需要但尚未采集；
- `REGISTERED`：已确认物理存在并有基本元数据；
- `RESEARCH_ONLY`：只能用于研究背景或结构探查；
- `SMOKE_ONLY`：只能用于管道冒烟和反例测试；
- `HISTORICAL_REPORT`：历史报告产物，不是规范数据集；
- `BLOCKED`：因许可、质量或时间点问题不得读取。

质量状态与许可状态单独记录，不能用“存在”代替“可用”。

## 2. 已存在数据和产物

### CFA-EXT-MKT-QLIB-20260714

| 字段 | 值 |
|---|---|
| 类型 | 日行情快照 |
| 状态 | `RETIRED / PAYLOAD_DELETED_METADATA_RETAINED`；2026-07-20 用户退役并删除数据本体 |
| 原物理位置 | `${QUANTIFICATION_ROOT}\多因子\data\market\snapshots\2026-07-14\qlib_bin`（已删除） |
| 快照日期 | 2026-07-14 |
| 日历范围 | 2000-01-04 至 2026-07-14 |
| 标的数 | 6118 |
| 字段 | adjclose, amount, change, close, factor, high, low, open, volume, vwap |
| 来源 | GitHub `chenditc/investment_data` release 2026-07-14 |
| 许可 | Apache-2.0（按现有PROVENANCE登记，S1仍需复核适用边界） |
| 归档字节数 | 558586188 |
| SHA-256 | `ac965ce44b0b27163f6b775c04b1a61c95bb79b62c8915e397a7b33feb81f62e` |
| 限制 | 仅保留 tombstone、manifest/provenance 和历史依赖审计；不能由 `qdata` 解析，不得用于新研究 |

### CFA-SMOKE-EASTMONEY-20260708

| 字段 | 值 |
|---|---|
| 类型 | 历史资金流冒烟缓存 |
| 状态 | `REGISTERED / SMOKE_ONLY` |
| 物理位置 | `${QUANTIFICATION_ROOT}\data\capital-flow\eastmoney\000001-159995-399001-588170-20d-c9ba783e8c99a833` |
| 标的 | 000001.SH、399001.SZ、588170.SH、159995.SZ |
| 请求窗口 | 20日；现有manifest记录每个标的5行 |
| 来源 | 东方财富资金流和行情接口 |
| 抓取时间 | 2026-07-08T01:54:47+08:00；背景增强至02:34:51+08:00 |
| 许可 | `UNVERIFIED`，禁止进入正式账本 |
| 限制 | “主力/大单/超大单”等为供应商代理标签，不代表真实投资者身份；仅用于字段、缺失和报告安全测试 |

### CFA-LEGACY-RUN-20260708-SMOKE

| 字段 | 值 |
|---|---|
| 类型 | 旧资金监控技能冒烟运行 |
| 状态 | `REGISTERED / SMOKE_ONLY` |
| 物理位置 | `${QUANTIFICATION_ROOT}\资金流向skill\20260708-smoke-000001-399001-588170-159995` |
| 来源 | 东方财富接口；许可状态 `UNVERIFIED` |
| 限制 | 历史技能产物；不迁入项目根，不转换为正式数据集 |

### CFA-LEGACY-REPORT-20260708-V2

| 字段 | 值 |
|---|---|
| 类型 | 历史资金监控报告 |
| 状态 | `REGISTERED / HISTORICAL_REPORT` |
| 物理位置 | `${QUANTIFICATION_ROOT}\资金流向skill\20260708-v2-verified-000001-399001-588170-159995` |
| 最新交易日 | 2026-07-07 |
| 规范化行数 | 26 |
| 覆盖评分 | 50/100 |
| 已知缺失 | 板块/主题、北向、融资融券；ETF与市场流动性仅部分覆盖 |
| 限制 | 只用于报告结构、缺失状态和过度归因反例；不作为S1会计账本输入 |

### CFA-S1-PILOT-20260718-V10

| 字段 | 值 |
|---|---|
| 类型 | S1固定20交易日日频状态试点 |
| 状态 | `REGISTERED / RESEARCH_ONLY / G1_REVIEW` |
| 物理位置 | `${QUANTIFICATION_ROOT}\data\capital-flow-attribution\s1\20260718-s1-pilot-v10` |
| 报告位置 | `${QUANTIFICATION_ROOT}\reports\capital-flow-attribution\s1\20260718-s1-pilot-v10` |
| 日期范围 | 2026-06-16至2026-07-14，20个Qlib交易日 |
| raw | 66份，逐文件SHA-256，0不一致 |
| normalized | 6118行研究型证券区间；60行行情背景；60行两融；1277行ETF规模代理；4606行当前互联互通资格；60行互联互通成交；20行参考汇率 |
| snapshot | 统一状态长表280行、20日期、0重复键 |
| 来源 | 上交所、深交所、北交所探针、港交所、已登记Qlib快照 |
| 许可 | 官方公开披露仅作内部研究；再利用条款未逐项法律确认；Qlib按登记Apache-2.0且只读 |
| 质量 | 3个数据族OK、5个MISSING、1个CONFLICT |
| 限制 | ETF连续份额+NAV、北交所两融、历史互联互通资格、申万PIT缺失；不做身份归因或投资建议 |
| S1R只读复核 | 2026-07-19：280行统一账本通过Pandera及Parquet/DuckDB往返；66份raw哈希全部一致；物理数据未修改 |

### CFA-S1-INTERMEDIATE-20260718-V1-V9

| 字段 | 值 |
|---|---|
| 类型 | 失败与中间运行证据 |
| 状态 | `AUDIT_ONLY / NON_CANONICAL` |
| 物理位置 | `${QUANTIFICATION_ROOT}\data\capital-flow-attribution\s1\20260718-s1-pilot-v1` 至 `v9` |
| 用途 | 记录HTTP 502、代理断连、空响应和解析修复过程；为v10同日raw重建提供逐文件来源 |
| 限制 | 不作为下游规范数据集；不得与v10拼接或重复计数 |

### CFA-S1R-PILOT-20260719-V11

| 字段 | 值 |
|---|---|
| 类型 | S1R模块化兼容运行 |
| 状态 | `REGISTERED / RESEARCH_ONLY / G1_REVIEW` |
| 物理位置 | `${QUANTIFICATION_ROOT}\data\capital-flow-attribution\s1\20260719-s1r-pilot-v11` |
| 报告位置 | `${QUANTIFICATION_ROOT}\reports\capital-flow-attribution\s1\20260719-s1r-pilot-v11` |
| 对比报告 | `${QUANTIFICATION_ROOT}\reports\capital-flow-attribution\s1r\20260719-v10-v11-comparison` |
| 日期范围 | 2026-06-16至2026-07-14，20个交易日 |
| raw | 66份；与v10相同相对路径和SHA-256 |
| CSV兼容性 | 9张核心表与v10逐字节一致 |
| 列式存储 | 9张Parquet；DuckDB视图行数全部一致 |
| 校验 | Pandera PASS；单元测试23/23 PASS；好/坏夹具PASS |
| 质量 | 与v10相同：3个数据族OK、5个MISSING、1个CONFLICT |
| 限制 | 架构兼容性PASS不代表G1覆盖PASS；不得进入身份归因或投资建议 |

### CFA-S1R-AKSHARE-SMOKE-20260719-V3

| 字段 | 值 |
|---|---|
| 类型 | 可选开源适配器在线冒烟 |
| 状态 | `SMOKE_ONLY / partial / UNVERIFIED` |
| 物理位置 | `${QUANTIFICATION_ROOT}\tmp\capital-flow-attribution\20260719-s1r-adapter-smoke-v3` |
| 抓取时间 | 2026-07-19T03:17:28+08:00 |
| 来源 | AKShare 1.18.64；`stock_zh_a_spot`新浪备用入口 |
| 行数 | raw 5527；normalized 5527；证券代码唯一且通过Pandera |
| 回退记录 | `stock_info_a_code_name`和`stock_zh_a_spot_em`因本机代理失败后回退成功 |
| Tushare | 包已安装；`TUSHARE_TOKEN`未配置，状态SKIP，未发起API请求 |
| 限制 | 当前截面、聚合来源、许可未核验；只用于适配器可用性和交叉验证，不进入正式账本 |

### CFA-RESEARCH-20260719-V22

| 字段 | 值 |
|---|---|
| 类型 | G1修复、S2骨架与S3研究回测的不可覆盖候选运行 |
| 状态 | `REGISTERED / RESEARCH_ONLY / G1 REVIEW` |
| 物理位置 | `${QUANTIFICATION_ROOT}\data\capital-flow-attribution\research\20260719-g1-research-v22` |
| 报告 | `${QUANTIFICATION_ROOT}\reports\capital-flow-attribution\research\20260719-g1-research-v22` |
| 20日窗口 | 2026-06-16 至 2026-07-14 |
| raw | 131份；SHA-256复核0不一致 |
| 存储 | 27,532,669 bytes；10张Parquet；只读DuckDB目录 |
| 融资融券 | 沪深北60行；北交所余额/买入为官方观察、偿还为恒等式代理；沪市2026-06-22保留1项冲突 |
| ETF | 官方份额31,105行；二级NAV覆盖31,066行；可分解29,494行；NAV许可`UNVERIFIED` |
| 互联互通 | 79/80通道日通过；2026-07-03沪股通北向因HKEX全零坏文件为`MISSING` |
| 当前资格 | 5,250行当前清单；不是历史PIT资格 |
| 历史融资代理 | 3,956日期；3,752个沪深双源完整日，204个不完整日置缺；来源Jin10，许可`UNVERIFIED` |
| 回测 | 1,754个样本外交易日；6折；gap=5；两交易日滞后；缺信号日空仓；10bp单边成本 |
| 发布上限 | 不得进入身份归因事实、投资建议或多因子；G2/G3未评估 |

### CFA-RESEARCH-FAILED-20260719-V12-V21

| 字段 | 值 |
|---|---|
| 类型 | 不可覆盖的失败/被否决中间运行证据 |
| 状态 | `REGISTERED / FAILED_OR_REJECTED / DO_NOT_USE` |
| 物理位置 | `${QUANTIFICATION_ROOT}\data\capital-flow-attribution\research\20260719-g1-research-v12` 至 `v21`（实际存在版本） |
| 原因 | 上游502、非标准JSONP、HKEX全零HTTP-200、跨市场休市、历史单交易所缺失、回测缺日或单位审计发现 |
| 限制 | 仅用于故障取证和回归测试；不得作为研究结论或规范输入 |

## 3. S1计划数据族

以下是S0冻结的数据族蓝图。S1实际覆盖以 `CFA-S1-PILOT-20260718-V10` 的manifest和来源覆盖表为准；未满足项继续保持 `MISSING`，不因工作包完成而自动升级：

| dataset_id前缀 | 数据族 | 市场 | S1角色 |
|---|---|---|---|
| `CFA-SECURITY-MASTER` | 历史证券主表、上市退市、交易状态 | 沪、深、北、互联互通合资格证券 | 必需 |
| `CFA-TRADING-CALENDAR` | 交易日历和跨市场日历 | 沪、深、北、港 | 必需 |
| `CFA-MARKET-DAILY` | 个股/ETF日行情、成交和复权背景 | 沪、深、北、港股通范围 | 必需背景 |
| `CFA-ETF-MASTER` | ETF类型、跟踪指数、管理人、互联互通资格 | 内地ETF及ETF互联互通 | 必需 |
| `CFA-ETF-SHARES-NAV` | 每日份额、NAV、AUM、披露时间 | 内地ETF | 核心会计 |
| `CFA-MARGIN-AGG` | 融资余额、买入、偿还、融券汇总 | 各交易所公开可得范围 | 核心会计 |
| `CFA-STOCK-CONNECT-UNIVERSE` | 历史资格、生效失效时间 | 双向互联互通 | 必需 |
| `CFA-STOCK-CONNECT-DAILY` | 公开可得的成交、持股、额度等字段 | 双向互联互通 | 有则观察，无则MISSING |
| `CFA-INDUSTRY-SW2021` | 申万2021一级/二级历史映射 | A股及可映射港股通证券 | 必需背景 |
| `CFA-FX-CNY-HKD` | 人民币/港币换算及可用时间 | 内地与香港 | 必需背景 |

基金持仓、公司资金、证券级融资、订单规模代理和分钟数据属于S2候选，不进入S1首闭环。

## 4. 数据集登记必填项

每个正式数据集必须登记：

```text
dataset_id
dataset_version
schema_version
source_name
source_url_or_reference
license_status
physical_location
market_scope
security_scope
date_range
published_at_rule
available_at_rule
timezone
currency_and_units
row_count
instrument_count
content_hash_or_manifest
quality_status
known_missingness
known_conflicts
owner
```

未填写许可、`available_at` 或版本字段的数据不能从 `PROPOSED/SMOKE_ONLY` 升级为正式输入。

## 5. 存储合同摘要

共享根为 `${QUANTIFICATION_ROOT}\data`，S1已按以下结构创建项目隔离子目录：

```text
catalog/      数据集登记
raw/          不可覆盖的原始证据
normalized/   标准化表
snapshots/    冻结时点快照
derived/      可重算特征和账本
manifests/    哈希、行数、版本、缺失、冲突和许可
```

S1写入前必须再次测量可用空间；预计写入超过10GB或使G盘可用空间低于120GB时停止并进入 `REVIEW`。
