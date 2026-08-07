# S1R架构重整基线

状态：`WP-S1R-01—04 DONE / G1 REVIEW`  
批准日期：`2026-07-19`  
适用范围：S1到S2之间的采集、存储和校验重整

## 1. 为什么增加S1R

S0冻结的研究问题、推断边界、时间点合同和会计合同保持有效。S1的20日规范运行 `20260718-s1-pilot-v10` 继续作为不可覆盖的对照基准。

需要重整的是实现层：当前 `pipeline.py` 同时承担HTTP、重试、来源解析、标准化、会计账本、覆盖率、落盘和报告。继续向其中增加ETF、北交所、互联互通和申万数据源会放大维护风险，也会让来源失败与领域规则失败难以区分。

## 2. 目标结构

```text
官方来源 ───────────────┐
Tushare Pro（可选二级） ├─> adapters ─> raw + manifest
AKShare（可选二级） ────┤                    │
Qlib（只读行情背景） ────┘                    v
                                      normalize + Pandera
                                              │
                                              v
                                      Parquet + DuckDB
                                              │
                                              v
                                自研PIT、会计、归因和报告规则
```

通用组件与自研边界：

| 层 | 采用方式 | 组件或合同 |
|---|---|---|
| HTTP重试、退避 | 复用 | `requests + tenacity` |
| 表式转换 | 复用 | `pandas`，大规模分钟数据前不引入Polars |
| 字段和状态校验 | 复用 | `Pandera + pytest/unittest` |
| 大表存储与本地查询 | 复用 | `Parquet + DuckDB` |
| A股公开接口包装 | 可选适配器 | `AKShare` |
| 历史补数和交叉验证 | 可选适配器 | `Tushare Pro` |
| 证券行情背景 | 保持只读 | 已登记Qlib快照 |
| PIT、会计、未知残差和身份边界 | 自研 | 现有protocol与`accounting.py` |
| 研究质量门禁 | 保持现状 | 现有Harness，不建立第二套 |

## 3. 来源裁决

来源优先级由 `configs/source_policy.json` 机器可读地冻结：

1. 交易所、基金管理人、港交所等官方披露是规范观测；
2. Tushare Pro只做历史补数和交叉验证，需要的凭据仅从 `TUSHARE_TOKEN` 环境变量读取；
3. AKShare只做免费交叉验证、来源发现和官方链路失效时的候选替代；
4. 现有Qlib只提供研究型行情与日历背景，不升级为资金身份数据。

不同来源发生实质冲突时保留全部观测，标记 `CONFLICT` 并使工作流进入 `REVIEW`。不得求平均、静默覆盖或把二级来源伪装成官方来源。

## 4. 数据流与目录

现有存储根和不可覆盖规则不变。S1R新增的Parquet按数据族、市场、年份和必要时月份分区，DuckDB只保存目录、视图和小型质量统计；原始响应仍按文件只追加并逐文件哈希。

```text
raw/          官方或适配器原始响应，只追加
normalized/   统一代码、日期、单位和来源元数据
parquet/      规范列式数据，不产生单证券小文件
catalog/      DuckDB目录或视图
snapshots/    阶段门冻结快照
manifests/    来源、时间、行数、哈希、缺失和冲突
```

## 5. S1R工作包

| 工作包 | 状态 | 内容 | 完成证据 |
|---|---|---|---|
| `WP-S1R-01` | DONE | 冻结架构、来源和依赖政策；建立DuckDB/Pandera离线组件 | 配置可验证；组件离线测试通过；不改v10 |
| `WP-S1R-02` | DONE | 将HTTP、重试、raw证据和交易所适配器从`pipeline.py`拆出 | 旧入口兼容；`pipeline.py`由903行降至267行 |
| `WP-S1R-03` | DONE | 加入AKShare/Tushare可选适配器及双源对账 | AKShare在线取得5527行；无token的Tushare明确SKIP；冲突测试PASS |
| `WP-S1R-04` | DONE | 从冻结raw重建同一20日窗口并比较v10 | 9张核心CSV逐字节一致；66份raw哈希一致；Parquet/DuckDB计数一致 |

只有 `WP-S1R-04` 完成并重新审查G1后，才进入S2。

## 6. 当前明确延后

- 不引入Airflow、Dagster、Prefect、dbt或dlt；
- 不因使用AKShare/Tushare降低官方源优先级；
- 不索取或写入Tushare token；
- 不修改Qlib、多因子、现有Harness或资金监控技能；
- 不在本工作包下载正式市场数据；
- 不改变现有PIT、会计和证据语言合同。

## 7. WP-S1R-01验收

- `source_policy.json` 的tier唯一且递增；
- 官方源必须是tier 1且角色为`canonical`；
- Tushare只登记环境变量名，不保存token值；
- 冲突策略禁止平均和静默覆盖；
- Pandera能够拦截重复键和非法质量状态；
- DuckDB能够将小型CSV转换为Parquet并核对行数；
- 原S1六项单元测试继续通过；
- `v10`及所有正式数据、报告、Qlib、Harness和技能零修改。

## 8. 验收结果

2026-07-19在项目隔离 `.venv` 中验证：

| 项目 | 结果 |
|---|---|
| 依赖导入 | DuckDB 1.5.4、Pandera 0.32.1、Tenacity 9.1.4、AKShare 1.18.64、Tushare 1.4.29均成功 |
| 单元测试 | 最终23/23 PASS，其中原S1测试6项及S1R模块、适配器、重试、对账、存储和运行比较测试17项 |
| v10统一账本 | 280行通过Pandera；CSV转Parquet后DuckDB计数仍为280 |
| v10 raw证据 | 66/66 SHA-256一致 |
| 固定夹具 | 好样本PASS；坏样本正确进入REVIEW且测试PASS |
| 凭据 | `TUSHARE_TOKEN`未配置；未访问Tushare接口，未保存任何凭据 |
| 环境体积 | `.venv`约277.8 MiB；G盘验证时剩余约338.1 GiB |

`WP-S1R-01`当时没有修改或生成正式市场数据。随后经用户授权，`WP-S1R-02—04`已完成；完整证据见 `S1R_ACCEPTANCE.md`。
