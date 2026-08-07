# 多因子研究工程

这是独立于 CWOPT 的 A 股多因子研究、短周期观察、不可变数据快照和券商只读接入工程。项目根目录固定为：

`${QUANTIFICATION_ROOT}\多因子`

## 2026-08-04 `R0_SHSZ_PV` 窄门

首个正式价量输入已经开放：`cn-a-share-r0-shsz-pv-panel@20050104-20260803-v1`，内容哈希 `26309d36b7c08096fff55df100f1d27df8412b106ea48f282ce0f7caad37084b`。它只覆盖沪深普通 A 股 2005-01-04 至 2026-08-03，允许未复权价量单因子 Discovery 和预注册验证准备；不开放基本面、行业中性、ETF 穿透、全球环境或自动交易，也不改变 canonical R0/R1/R2。

旧协议 `configs/r0_shsz_pv_single_factor_screen_v1.json` 保留为 `PREREGISTERED__NOT_RUN`，已由 2026-08-05 的 C-v1.1 执行配置替代但不覆盖。确定性目录 `configs/r0_shsz_pv_factor_catalog_512_v1.json` 冻结 512 个候选（48 seed + 464 grammar），候选语义内容哈希 `6216ca0af144998d97032de715467ba61cec46cdb877af9bbb419ed582e0cb64`；当前物理文件 SHA-256 为 `23c78fa9d25e15d22bed87134b3aa7c5e228fb0f539e5169149e5120b3093d81`。V6/V7 的历史状态不被改写。

## 2026-08-05 C-v1.1 价量 Discovery

经用户批准，C-v1.1 只在上述精确快照和 2005-07-01—2021-12-31 Discovery 窗口执行。GPU 运行 `r0-shsz-pv-c-v1-1-discovery-512-20260805-v1` 已完成 512/512 并 `COMMITTED`：68 个定义达到公司行动不敏感层，50 个同时达到 BY-FDR≤10%。随后 `r0-shsz-pv-c-v1-1-stability-20260805-v1` 对这 50 个候选完成 3/5/8 年滚动窗与扩展窗、年度测试、purge/embargo 和 15/30/50bps 成本描述；45 个在四种方案下保持正平均 IC，6 个又在四种方案下保持正的平均 30bps 成本后 Top 20% 超额。

这些都是 Discovery 内的稳定性描述，不是独立 Validation 或可交易结论。6 个候选尚未做相关性聚类、行业/规模中性、容量和账户映射；Validation 仍未消费，Holdout 仍未打开，自动交易仍为 false。收口证据见 `${QUANTIFICATION_ROOT}\reports\data-governance\r0-shsz-pv-c-v1-1-discovery-20260805-v1\`。

## 2026-08-06 外部经典因子 Discovery

新的外部经典因子目录在同一精确 `R0_SHSZ_PV` 快照上完成全量 Discovery。正式运行 `research_runs/factor_factory/r0-shsz-pv-classic-discovery-426-20260806-v1/` 已完成 426/426、失败 0 并 `COMMITTED`：376 个达到 Discovery BY-FDR≤10%，其中 281 个同时通过运行时公司行动尺度安全门；186 个在 30bps 成本描述下为正，其中 130 个同时通过公司行动尺度安全门，覆盖 43 个相关簇。进一步同时要求 12 种 walk-forward 方案的最差年度折 IC 为正、5/10/20/60 日方向一致和六类市场状态方向一致后，严格交集为 77 个规格、27 个相关簇。

这 426 个规格是本地透明公式重写，不是旧 512 目录的续跑或重跑。旧 512 仅用于指纹比较，语义重叠项在新目录构建时排除；供应商可见名称只承担来源与能力映射，名称相同不表示本地公式与供应商公式等价，也没有下载或保存供应商历史因子值。本次结果全部仍属于 Discovery：Validation 未消费，Holdout 未打开，自动交易为 false，可交易结论为 0。

权威证据：

- [A/B/C 收口报告](../reports/data-governance/abc-classic-factor-discovery-20260806-v1/README.md)
- [正式运行 manifest](research_runs/factor_factory/r0-shsz-pv-classic-discovery-426-20260806-v1/manifest.json)
- [正式运行 summary](research_runs/factor_factory/r0-shsz-pv-classic-discovery-426-20260806-v1/summary.json)
- [COMMITTED 提交标记](research_runs/factor_factory/r0-shsz-pv-classic-discovery-426-20260806-v1/COMMITTED.json)

## 当前权威状态

截至 2026-08-06，本项目仍没有已通过 Validation 或可打开 Holdout 的可靠因子，可交易结论数为 0。

| 项目 | 权威状态 | 结论 |
|---|---|---|
| C-v1.1 512 因子 Discovery | 2026-08-05 已 `COMMITTED` | 68 个公司行动不敏感候选，50 个同时达到 Discovery BY-FDR≤10%；不等于 50 个可靠因子 |
| C-v1.1 多分割稳定性 | 2026-08-05 已 `COMMITTED` | 45 个保持方向稳定，6 个保持四方案平均 30bps 成本后为正；相关性去重、Validation、Holdout 均未执行 |
| 外部经典因子 426 Discovery | 2026-08-06 已 `COMMITTED__DISCOVERY_ONLY` | 426/426 机械完成、失败 0；376 个 BY-FDR≤10%，281 个同时 action-safe；186 个通过 30bps 描述，130 个同时 action-safe，覆盖 43 簇 |
| 外部经典因子严格 Discovery 交集 | `77 SPECS__27 CLUSTERS__DISCOVERY_ONLY` | 130 个 action-safe+30bps 候选中，77 个同时通过 12 种 walk-forward 最差年度折、四预测期和六市场状态方向门；不是 Validation 或可交易结果 |
| 外部经典因子 Validation / Holdout | `NOT_CONSUMED` / `NOT_OPENED` | Holdout 内容未加载；自动交易 false；可交易结论 0 |
| V6 正式研究 | 2026-07-19 03:59（Asia/Shanghai）已 `COMMITTED`，终态 `NO_FACTOR` | 512 个候选经 Discovery 数值筛选后仅 5 个进入 Discovery 事件门，5 个全部失败；入选数为 0 |
| V6 Validation | `NOT_CONSUMED` | Validation 候选、指标、事件回测和组合指标均为 0；治理登记曾进入 `VALIDATION_CONSUMED`，但登记状态不能替代实际阶段证据 |
| V6 Holdout | `NOT_OPENED` | 没有冻结组合或 `freeze_hash`，不得打开 Holdout |
| Top-10/Retention-30 诊断 | `DIAGNOSTIC_NO_EVENT_GATE_PASS` | 固定 5 个候选在 Discovery 重放，测试 5 个、通过 0 个 |
| Equal-5 诊断 | `DIAGNOSTIC_ENSEMBLE_EVENT_GATE_FAILED` | 固定五因子等权 z-score 组合在 Discovery 的描述性指标为正，但唯一组合未通过事件门 |
| V7 ensemble-first | 代码与测试已实现，尚未注册正式研究尝试 | 不是研究结果，不代表已消费 Validation，更没有打开 Holdout |

V6 权威证据：

- [正式 manifest](research_runs/factor_factory/full-shsz-2018-2026-v6-discovery-first-gpu-research/manifest.json)
- [COMMITTED 提交标记](research_runs/factor_factory/full-shsz-2018-2026-v6-discovery-first-gpu-research/COMMITTED.json)
- [空 selected.json](research_runs/factor_factory/full-shsz-2018-2026-v6-discovery-first-gpu-research/selected.json)
- [中文研究报告](research_runs/reports/full-shsz-2018-2026-v6-discovery-first-gpu-research-research-report.md)
- [机器可读研究报告](research_runs/reports/full-shsz-2018-2026-v6-discovery-first-gpu-research-research-report.json)

Discovery-only 诊断证据：

- [Top-10/Retention-30 诊断 manifest](research_runs/factor_factory/full-shsz-2018-2026-v6-discovery-only-top10-ret30-diagnostic/manifest.json)
- [Equal-5 诊断 manifest](research_runs/factor_factory/full-shsz-2018-2026-v6-discovery-only-equal5-zscore-top10-ret30-diagnostic/manifest.json)

这两个诊断都明确记录 `validation_consumed=false`、`holdout_opened=false` 和 `promotion_eligible=false`，不能当成新的正式尝试或成功因子。

## 当前能力与 V7 边界

- 自动生成、去重和演化因子表达式。
- Discovery / Validation / Holdout 三段时间切分，段间带完整持有期 purge。
- 分块置换检验、Benjamini–Yekutieli 多重检验和跨折稳定性门槛。
- A 股五相位事件回测、常规/双倍成本、回撤、成交约束和数据质量审计。
- 研究尝试与 Holdout 双状态机，具备哈希审计链、外部锚点和崩溃恢复。
- QMT 账户、持仓和行情只读接入，以及人工复核用影子意图；代码中没有下单接口。
- 不可变数据 catalog、目录哈希、来源封印、用途上限与显式快照解析。

V6 的实际失败暴露出“先逐个要求组件通过事件门和 Validation，再构造组合”的结构性问题。V7 已实现 ensemble-first 分支：

1. Discovery 单因子只通过数值门进入选材池。
2. 按预注册固定组件数、确定性排序和低相关贪心规则，只构造一个等权组合；禁止子集枚举、权重搜索和多次组合试验。
3. 组合先在 Discovery 通过数值门和一次五相位事件回测，再写入带哈希的 `discovery-freeze.json`。
4. Validation 只检验这一个冻结组合；组件 Validation 指标数和组件事件回测数均固定为 0，也不允许重选。
5. Holdout 继续由独立命令显式开封。

设计与实现见：

- [V7 ensemble-first 协议](PROTOCOL_V7_ENSEMBLE_FIRST.md)
- [确定性组合选择器](factor_factory/ensemble_first.py)
- [V7 单元与合成面板测试](factor_factory/test_ensemble_first.py)

V7 目前只有代码和本地测试证据，没有预注册正式配置、正式尝试记录、正式 Validation 结果或 Holdout 结果。

## 旧数据退役边界

以下两个社区 Qlib 快照的数据本体已于 2026-07-20 删除，并从活动 catalog 移入退役 tombstone，不再作为过渡数据、新研究输入或新数据库来源：

原路径（已删除）：`data/market/snapshots/cn-a-share-qlib-daily/2026-07-18/qlib_bin`

- GitHub release tag 为 `2026-07-18`，Qlib 交易日历实际截止 `2026-07-17`。
- 快照已通过摄取时的 release metadata、精确字节数、发布者 SHA-256、安全解包和 Qlib 结构检查。
- 原项目 release catalog 曾将其固定为 `source_tier=RESEARCH_ONLY`、`promotion_ceiling=RESEARCH_ONLY`、`pit_verified=false`；该记录作为不可改写历史证据保留，但数据本体已不存在。
- [快照 PROVENANCE](data/market/snapshots/cn-a-share-qlib-daily/2026-07-18/PROVENANCE.json)
- [catalog release manifest](quant_data_core/releases/cn-a-share-qlib-daily/2026-07-18/manifest.json)
- [数据 catalog 说明](quant_data_core/README.md)

不再继续以“是否可晋级”为目标做第二次科学复核；依赖、删除清单和回执已完成。

V6 正式研究和两个 Discovery 诊断历史上使用的是：

原路径（已删除）：`data/market/snapshots/2026-07-14/qlib_bin`

新快照不会追溯改变已经提交的 V6 结果。

公共 Qlib 数据仍有以下硬边界：

- OHLC 是 Qlib 标准化复权价格，不是券商原始成交价。
- `all.txt` 的起止日是供应商价格可用区间，不是独立验证的官方上市退市主表。
- 当前没有官方历史 ST、完整停牌/涨跌停封单、完整公司行为链、永久证券 ID 或逐笔成交。
- 北交所数据在建立永久证券 ID 和代码切换映射前不进入正式沪深研究池。
- `pit_verified=false` 表示不能声称已具备完整 point-in-time 基本面、成分和公司行为证据。

因此这些限制只用于解释历史证据；当前两个身份不能再由 `qdata` 解析，也不能用于新的研究、Shadow 或实盘候选。

## 未来正式运行禁令

除上述精确 `R0_SHSZ_PV` 快照上的预注册 Discovery 外，在以下条件全部满足前不得启动其他新的正式因子研究进程，也不得消费新的 Validation：

1. 新中央事实数据按工作区 `SOURCE_POLICY.md` 和 `DATA_QUALITY_POLICY.md` 生成，且旧 Qlib 不作为输入。
2. V7 正式配置、固定组件数、候选上限、相关性阈值、唯一组合规则和 lineage hash 完成预注册与复核。
3. 新 protocol ID 和独立输出目录完成治理预检，且没有复用已提交 V6 尝试的身份。
4. 相关单元测试、主回归和数据边界测试在当前代码与当前快照上通过。
5. 用户明确批准开始正式研究尝试。

同时禁止：

- 重跑或覆盖已 `COMMITTED` 的 V6 输出目录；该目录是不可变失败证据。
- 把 Top-10、Equal-5 或任何 Discovery-only 诊断解释为 Validation、统计显著性或可交易结论。
- 为 V6、诊断运行或未注册的 V7 打开 Holdout。
- 仅因 IC、ICIR、GPU 利用率或单段收益好看就绕过成本、事件门、数据质量和治理状态。
- 连接自动下单或使用当前研究结果生成无人监督的实盘交易。

## L2-P0 中央数据输入合同

状态：`R0_SHSZ_PV_INPUT_OPEN__CANONICAL_R0_R1_R2_BLOCKED`。原 12 项合同测试和
未登记身份负向 smoke 继续通过；2026-08-04 又通过精确 qdata、正式 `PanelData`
小样本与 v2 输入验收。回执位于
`${QUANTIFICATION_ROOT}\reports\data-governance\r0-shsz-pv-v2-formal-acceptance-20260804-v1\`。

`factor_factory/qdata_input.py` 是正式研究的最小只读解析层。它只接受明确的 `dataset_id + snapshot_id`，并调用根工作区：

```text
${QUANTIFICATION_ROOT}\data\catalog\qdata.cmd resolve <dataset_id> <snapshot_id> --intent research --json
```

解析成功后仍会本地复核：身份完全一致、`requested_intent=research`、不可变、用途上限至少为 `RESEARCH_ONLY`、生命周期为 `active/frozen`、`quality_status=OK`、许可状态已明确、登记路径和权威文件存在。`latest`、空快照、路径形式的输入、`tmp/inbox/raw/curated/quarantine` 等候选区以及已知本地构建候选路径一律失败关闭。

该层只返回不可变的快照描述，不自动启动因子生成、IC、回测、Validation 或 Holdout。2026-08-04 `R0_SHSZ_PV` 精确正式快照已通过该解析层，并由独立适配器完成真实 `PanelData` smoke；其他 catalog 身份和 canonical CORE bundle 仍须逐项通过相同门槛。

### R0 CORE-01～06 输入束预检

2026-08-01 在 L2-P0 解析层上新增了
`R0PVInputBundlePreflight`。它只处理六个固定身份：

1. `cn-a-share-security-master-pit`；
2. `cn-trading-calendar`；
3. `cn-a-share-daily-raw`；
4. `cn-a-share-corporate-actions-pit`；
5. `cn-a-share-daily-adjusted`；
6. `cn-a-share-tradability-daily-pit`。

预检会对每个明确 `dataset_id + snapshot_id` 单独执行 `qdata --intent research`
解析，并在打开任何 payload 前失败关闭地检查：六项齐全且不重复、禁止 `latest`、
不可变/`OK`/研究用途/权利状态、五份 authority 文档完整、manifest 身份与哈希合同、
请求研究窗口覆盖，以及六快照之间的 bundle as-of、永久身份命名空间、日历、市场、
时区、NULL 策略、PIT 修订语义和原始/前复权口径一致性。通过后只返回带 SHA-256
的冻结 descriptor，固定记录 `payload_opened=false` 与
`production_paneldata_status=BLOCKED__PAYLOAD_NOT_OPENED`。

实现和 fixture 回归分别位于
`factor_factory/qdata_input.py` 与 `factor_factory/test_r0_input_bundle.py`。当前真实正式
catalog 的 canonical CORE-01～06 仍为 `0/6`；独立 `R0_SHSZ_PV` 面板不能代替该六项
输入束。因此完整 CORE `PanelData`、canonical R0/R1/R2、Validation、Holdout 和
相应因子有效性结论继续阻断；只有窄门范围内的预注册价量 Discovery 可另行启动。

## Holdout 边界

只有新的正式研究尝试在 Discovery 冻结出明确组合、完成唯一一次 Validation 确认、提交 `FROZEN_AWAITING_HOLDOUT`，并由人工核对精确 64 位 `freeze_hash` 后，才允许通过独立 `open-holdout` 命令开封。

`NO_FACTOR`、`ABORTED_TECHNICAL`、Discovery-only 诊断、未注册 V7、空 `selected.json` 或仅有因子缓存，都不是打开 Holdout 的条件。

## 目录

- `factor_factory/`：因子表达式、统计、V7 组合选择、事件回测、治理和 Qlib/QMT 适配。
- `quant_data_core/`：不可变数据 catalog、安全摄取、来源与用途上限。
- `configs/`：已冻结或候选研究配置；文件存在不代表已获准运行。
- `data/market/`：真实市场数据 staging、快照和来源记录。
- `research_runs/factor_factory/`：不可变正式研究结果和诊断结果。2026-07-19 已清理全部终止运行的 `factor_cache`；缓存不是正式 manifest artifact，后续不得因位于该目录而被误当成不可变证据。
- `research_runs/reports/`：面向人工复核的研究报告。
- `ml/`：短周期观察、券商只读网关和测试。
- `tools/`：只读诊断和 Discovery 回放脚本。
- `.tmp/`：临时日志与试运行目录，不是最终研究证据。2026-07-19 已审计清理三个旧试运行的 193 个因子矩阵（8.090690 GiB），保留旧 V1 的 `manifest/candidates/backtests/selected` 四个小型结果文件；回执位于 `${QUANTIFICATION_ROOT}\reports\storage-cleanup\multi-factor-tmp-cache-20260719\`。
- `.state/`：正式研究和 Holdout 的防篡改治理状态。

缓存清理的逐文件哈希清单、删除前验证和删除后复核保存在 `${QUANTIFICATION_ROOT}\reports\storage-cleanup\multi-factor-cache-20260719\`。审计工具为 `tools/audit_factor_cache_cleanup.py`；它只验证并生成回执，不执行删除。

## 本地验证

下列命令只运行测试，不注册正式研究尝试，也不打开 Holdout：

```powershell
py -B -m unittest factor_factory.test_ensemble_first factor_factory.test_cli factor_factory.test_factor_factory -v
py -B -m unittest discover -s quant_data_core -p "test_*.py" -v
py -m compileall -q factor_factory quant_data_core ml
```

更多边界与设计见 [FACTOR_FACTORY_ARCHITECTURE.md](FACTOR_FACTORY_ARCHITECTURE.md)、[DATA_LAKE.md](DATA_LAKE.md)、[BROKER_DATA_ACCESS.md](BROKER_DATA_ACCESS.md)、[PROTOCOL_V6.md](PROTOCOL_V6.md) 和 [SHORT_CYCLE_RUNBOOK_20260716.md](SHORT_CYCLE_RUNBOOK_20260716.md)。
