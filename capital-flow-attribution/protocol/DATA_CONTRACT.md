# 数据合同

协议版本：`0.1.0`  
状态：`FROZEN_FOR_G0_REVIEW`  
冻结日期：`2026-07-18`

## 1. 数据范围

### 1.1 市场

- 上交所、深交所、北交所历史时点证券；
- 沪股通、深股通、港股通和ETF互联互通历史合资格范围；
- 港股通合资格证券的必要行情、行业、日历和汇率背景；
- 内地全部上市ETF，按资产类别分开。

首轮不以“完整香港市场”为目标。若研究需要港股基准，可登记必要指数或基准，但不得扩张为未经批准的全港股采集。

### 1.2 证券历史时点

证券主表必须保留上市、退市、名称、交易状态和资格变化的生效/失效时间。退市、ST、停牌和新股不得因当前不可交易而从历史样本静默删除。

行业采用申万2021一级主分类、二级下钻，并保存映射的有效期。无法映射时使用 `UNKNOWN`，不得使用未来行业归属回填历史。

### 1.3 ETF分类

ETF至少区分：股票宽基、股票行业/主题、策略、债券、商品、跨境、货币及其他。货币ETF可作为现金替代观察，但不得与股票风险资产汇总为同一净流。

## 2. 数据集身份

每个正式数据集必须有稳定的 `dataset_id`，每次冻结有新的 `dataset_version`，字段合同变化时提升 `schema_version`。

推荐格式：

```text
dataset_id: CFA-<FAMILY>-<MARKET>-<FREQUENCY>
dataset_version: YYYYMMDD.N
schema_version: MAJOR.MINOR.PATCH
```

版本不得由文件修改时间替代。

## 3. 证券代码

规范代码由原始代码和交易所后缀组成：

```text
<raw_code>.SH
<raw_code>.SZ
<raw_code>.BJ
<raw_code>.HK
```

原始供应商代码必须另存为 `source_symbol`，不得覆盖。名称只用于展示，不能作为连接主键。跨市场证券、基金、指数和通道资格必须由带有效期的映射表连接。

## 4. 必填元数据

清单级必填：

```text
dataset_id
dataset_version
schema_version
source_name
source_reference
license_status
physical_location
market_scope
date_range
published_at_rule
available_at_rule
timezone
currency
unit_contract
row_count
instrument_count
content_hash
quality_status
known_missingness
known_conflicts
created_at
owner
```

行级最低时间和身份字段：

```text
symbol
source_symbol
exchange
security_type
trade_date
published_at
available_at
ingested_at
source_name
currency
value_unit
quality_flags
```

若来源不提供可靠 `published_at`，必须记录推定规则和证据层级；不能直接用 `ingested_at` 冒充发布时间。

## 5. 日期、时间和时区

- 统一时区：`Asia/Shanghai`；
- `trade_date`：对应交易所业务日期，不含时区；
- `published_at`：来源首次发布该记录的时间；
- `available_at`：研究流程最早能够合法、稳定使用该记录的时间；
- `ingested_at`：本工程实际取得记录的时间；
- `as_of`：某次报告允许使用数据的截止时间。

详细规则见 `POINT_IN_TIME_CONTRACT.md`。

## 6. 币种、单位和精度

- 内地金额基础币种为人民币元，港股原始金额保留港币；
- 跨市场汇总必须同时保存原币值、汇率、汇率可用时间和人民币换算值；
- 份额和股数保存来源原始单位；
- 比例保存小数值并另存展示格式；
- 底层保存来源精度，不在入库时换算成亿元、万元或万份；
- 价格字段必须明确是否复权，NAV不得用复权价格替代。

## 7. 来源和许可

来源优先级：

1. 交易所、基金管理人等官方披露；
2. 监管机构或指数公司；
3. 已有明确授权的商业数据；
4. 聚合网站，仅作交叉核对和冒烟。

许可状态至少为 `VERIFIED / UNVERIFIED / UNLICENSED / RESTRICTED`。`UNVERIFIED` 和 `UNLICENSED` 数据不得进入正式账本、冻结快照或对外报告。

## 8. 质量状态

- `OK`：通过字段、时间、许可和会计检查；
- `MISSING`：预期字段、市场或日期缺失；
- `STALE`：超过合同允许的更新时间；
- `CONFLICT`：两个来源或同源版本冲突且未解决；
- `UNLICENSED`：许可不清或不允许正式保存/使用。

禁止静默前向填充、用零替代缺失、以单一来源覆盖冲突，或在未记录规则时删除异常记录。

## 9. 物理存储

统一规划根：`${QUANTIFICATION_ROOT}\data`。

```text
raw/          只追加的原始响应和证据
normalized/   统一代码、日期、单位后的标准表
snapshots/    不可静默修改的冻结版本
derived/      可由上游和配置重算的账本/特征
manifests/    版本、哈希、行数、缺失、冲突和许可
catalog/      数据集身份与位置登记
```

大表使用Parquet，小型交换表使用CSV，清单使用JSON，协议使用Markdown。大表按数据族、市场、频率、年份和必要时月份分区；禁止产生大量单证券小文件。

S0不创建这些目录。S1硬上限10GB，且G盘可用空间不得低于120GB。预计分钟数据写入超过50GB时必须通过新决策批准。

## 10. 现有数据关系

- 现有Qlib快照只读引用，保持 `RESEARCH_ONLY`，不移动、不复制；
- 历史东方财富资金流缓存只作为 `SMOKE_ONLY`；
- 历史资金监控报告只作为报告和反例产物；
- 多因子不得读取本工程raw、临时结果或未验证输出；
- 只有通过G3并冻结的派生特征才能写入共享 `derived` 供多因子按版本只读。

具体登记见 `DATA_CATALOG.md`。

## 11. 合同变更

市场范围、证券主键、时间字段、单位、来源优先级、许可规则、存储根或预算发生变化时，必须新增决策并提升合同版本。字段新增若不破坏兼容性提升次版本；改变语义或删除字段提升主版本。
