# 项目状态

更新时间：`2026-07-19`  
当前阶段：`G1修复 + S2/S3研究骨架`  
当前工作包：`WP-G1R-01 / WP-S2-SCAFFOLD-01 / WP-S3-PILOT-01`  
工作包状态：`DONE`  
阶段门：`G1 REVIEW`

> 上游退役通知（2026-07-20）：历史 v10/v22 配置引用的 Qlib 数据本体已经删除。这些配置继续作为不可改写证据保留，但不能按原路径完整复现，也不得据此启动新运行；新研究等待中央事实数据重建。

## 0. v22研究候选

- 数据：`${QUANTIFICATION_ROOT}\data\capital-flow-attribution\research\20260719-g1-research-v22`
- 报告：`${QUANTIFICATION_ROOT}\reports\capital-flow-attribution\research\20260719-g1-research-v22`
- 体积：27,532,669 bytes；131份raw哈希复核0不一致；10张Parquet与DuckDB行数一致。
- 测试：39/39 PASS；ETF重复键0；回测执行日期重复0；前视时序违规0。
- G1修复：北交所融资融券20/20；ETF官方份额31,105/31,105；二级NAV 31,066/31,105；互联互通79/80。
- 研究回测：2019-04-19至2026-07-14，6折、gap=5、1,754个样本外交易日、两交易日滞后、10bp单边成本。
- 结果：策略累计+11.06%、CAGR 1.52%、最大回撤-24.48%、年化波动11.72%；等权基准累计+34.84%、CAGR 4.39%、最大回撤-37.95%、年化波动23.66%。
- 判定：回撤和波动下降，但没有超额收益证据；bootstrap年化超额95%区间为[-18.63%, +5.54%]。
- G2/G3：`NOT_EVALUATED`；没有身份分类、投资建议或多因子发布。

## 1. S1R工作包结果

| 工作包 | 状态 | 主要结果 |
|---|---|---|
| WP-S1R-01 架构和依赖基线 | DONE | 冻结官方/Tushare/AKShare/Qlib优先级；引入DuckDB、Pandera、Tenacity和可选中国市场适配器；建立离线存储、校验和来源政策组件 |
| WP-S1R-02 模块拆分 | DONE | `pipeline.py`由903行降至267行；HTTP/raw、交易所适配器、账本、报告分别进入独立模块；旧CLI入口保持兼容 |
| WP-S1R-03 可选适配器和对账 | DONE | AKShare/Tushare证券主表适配器、凭据SKIP、双源冲突保留和在线探针完成；AKShare最终备用链路取得5527行，Tushare无token明确SKIP |
| WP-S1R-04 兼容重建 | DONE | 生成 `20260719-s1r-pilot-v11`；9张Parquet与DuckDB行数一致；9张核心CSV与v10逐字节一致；66份raw哈希一致 |

验证结果：

- 项目隔离环境 `.venv` 安装成功，约277.8 MiB；
- 新旧单元测试23/23 PASS；
- v10统一账本280行通过Pandera，并经Parquet/DuckDB往返后保持280行；
- v10 raw证据66/66 SHA-256一致；
- 好、坏固定夹具均按预期PASS；
- AKShare在线探针经Sina备用入口取得5527行当前证券主表并通过Pandera；该结果为`SMOKE_ONLY / partial / UNVERIFIED`；
- Tushare包可导入，但因没有token明确SKIP且没有发起API请求；
- `TUSHARE_TOKEN`未配置、未读取、未保存；
- Qlib、多因子、现有Harness和资金监控技能零修改。

## 2. S1工作包结果

| 工作包 | 状态 | 主要结果 |
|---|---|---|
| WP-S1-01 来源探针 | DONE | 官方来源、字段、单位、许可与发布时间边界已写入 `docs/S1_SOURCE_PROBE.md`；raw响应逐文件哈希 |
| WP-S1-02 基础主表与分层 | DONE | 只读引用Qlib；6118行研究型证券区间、60行20日市场背景；raw/normalized/snapshot/manifest齐备 |
| WP-S1-03 ETF账本 | DONE | 1277行当前规模/份额代理；缺NAV或历史份额时拒绝AUM/申赎分解；正式覆盖为 `MISSING` |
| WP-S1-04 融资融券 | DONE | 沪深北各20日占位/观测账本；沪市1日残差冲突、深市偿还为恒等式代理、北交所为缺失 |
| WP-S1-05 互联互通与统一账本 | DONE | 当前合资格证券4606行、成交60行（19行OK）、统一日频长表280行 |
| WP-S1-06 夹具、Harness与报告 | DONE | 6项单元测试PASS；好/坏Harness两案PASS；示范报告与G1审查清单已生成 |

工作包“完成”表示约定的采集、拒绝规则、账本、夹具和审计产物已经实现，不表示所有外部数据均已取得。

## 3. 规范运行

- 数据：`${QUANTIFICATION_ROOT}\data\capital-flow-attribution\s1\20260718-s1-pilot-v10`
- 报告：`${QUANTIFICATION_ROOT}\reports\capital-flow-attribution\s1\20260718-s1-pilot-v10`
- 固定窗口：2026-06-16至2026-07-14，共20个交易日
- raw证据：66份，哈希复核0不一致
- 数据体积：3,637,867 bytes，远低于10GB硬上限
- 来源质量汇总：3个数据族 `OK`、5个 `MISSING`、1个 `CONFLICT`
- Qlib：只读，`RESEARCH_ONLY`，未移动、复制、链接或修改

`v1` 至 `v9` 是同日失败/中间运行证据，不是规范输出；因raw不可覆盖合同暂未删除。`v10` 逐文件重新哈希并在manifest中记录同日缓存来源。

## 4. G1未通过项

G1维持 `REVIEW`，原因如下：

1. ETF份额已取得官方20日链路，但NAV仍来自许可未核验的二级接口，AUM分解只能 `RESEARCH_ONLY`；
2. 互联互通当前资格清单已取得，但历史PIT资格仍缺已授权来源；
3. 申万2021历史时点映射尚无已授权来源；
4. 沪市融资余额在2026-06-22出现31,346,725元恒等式残差，保留为 `CONFLICT`，可能存在未披露调整；
5. 港交所2026-07-03官方日表返回全零坏正文，交易所补源后仍缺沪股通北向1条。

## 5. S1验证结果

| 验证 | 状态 | 结果 |
|---|---|---|
| 单元测试 | PASS | 6/6 |
| 好样本 | PASS | 工作流状态DONE |
| 坏样本 | PASS | 冲突与缺NAV触发工作流REVIEW |
| Harness新目标 | PASS | 2/2，输出未越出隔离运行目录 |
| raw哈希 | PASS | 66/66一致 |
| 统一账本唯一键 | PASS | 280行，0重复键，20日期 |
| 存储边界 | PASS | 规范数据3,637,867 bytes；全部S1运行22,177,229 bytes；G盘剩余363,442,843,648 bytes |
| G1阶段门 | REVIEW | 外部覆盖与1项会计冲突未满足PASS条件 |

## 6. 下一步

`WP-G1R-01 / WP-S2-SCAFFOLD-01 / WP-S3-PILOT-01`已经完成并停止。下一步由主控制台选择：取得授权PIT/NAV来源继续冲G1，或接受明确降级范围；在裁决前不得把骨架测试PASS写成G2/G3通过，也不得向多因子发布。
