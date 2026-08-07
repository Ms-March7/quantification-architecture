# 统一数据读取策略

更新时间：2026-08-05  

## 目标

Codex、Agent 和项目程序可以从任意项目目录发现同一份数据登记，不需要记忆物理路径；读取能力不等于研究或交易授权。

## 入口

- 机器目录：`${QUANTIFICATION_ROOT}\data\catalog\datasets.json`
- 候选索引：`${QUANTIFICATION_ROOT}\data\catalog\candidate_assets.json`（仅 `INSPECTION_ONLY`，不是 qdata 正式身份）
- 计划目录：`${QUANTIFICATION_ROOT}\data\catalog\planned_datasets.json`（规划用途，不可解析）
- 维护合同：`${QUANTIFICATION_ROOT}\data\catalog\MAINTENANCE_RUNBOOK.md`（不表示自动化已经实现）
- 来源政策：`${QUANTIFICATION_ROOT}\data\catalog\SOURCE_POLICY.md`
- 质量政策：`${QUANTIFICATION_ROOT}\data\catalog\DATA_QUALITY_POLICY.md`
- 只读解析器：`${QUANTIFICATION_ROOT}\data\catalog\dataset_cli.py`
- Windows 入口：`${QUANTIFICATION_ROOT}\data\catalog\qdata.cmd`

解析器不写业务数据，不修改 manifest，不自动选择 `latest`，也不下载外部数据。

`L1-D2A-03` 已暂停，其官方 PIT v2 和 16 证券切片全部位于
`tmp/d2a03-official-pit-core-slice/`，固定为 `INSPECTION_ONLY/NOT_ADMITTED`。
切片的 `PASS_WITH_GAPS` 只覆盖机械检查；它不得由项目直接读取、不得用于因子
IC/回测。`qdata --intent research` 必须继续拒绝；D2A-04 Phase A 已完成并
暂停。用户于 2026-07-28 启动了
`L1-D2A-04B_OFFICIAL_IDENTITY_CALENDAR_ACTION_STATUS_SOURCE_CLOSURE`。早先门户预检
因完整表显示“无权限”而按门停止，旧状态
`PAUSED__CSMAR_FULL_TABLE_PERMISSION_BLOCKED__NO_NEW_DATA__INSPECTION_ONLY`
及 0 业务请求、0 下载回执仅作为历史事实保留。2015—2019 包随后已经下载、密封和
候选化，与其余分段形成 1990—2026 日行情只读候选。SSE/SZSE 仅封存部分官方来源
证据，BSE 在 WAF/HTTP 403 处停止；
全部仍为 `INSPECTION_ONLY/NOT_ADMITTED`。任何后续输入仍只允许在冻结范围内保存
raw、receipt、hash、PIT/修订、权利和 quarantine 证据，不授权正式 catalog、质量
晋级或研究读取。2026-07-30 旧 selected-core combined 审计对 CORE-01/02/03/06
得到 0/4；因包含 3 项发布后条件，它已重分类为 `POST_ADMISSION_AUDIT`。非循环
Stage A 当前仍为 0/4 ready，正式准入仍为 0，D2B 继续阻断。只有 Stage A 全通过、
取得精确 bundle 哈希绑定的独立发布授权并完成真实原子发布后，才能在 Stage B 检查
catalog 身份与 `qdata --intent research`。D2A-04B 有界来源闭合处于活动状态，但
付费、新密钥、批量历史回补和研究未授权；`qdata --intent research` 必须继续拒绝
CORE-02 的 8,454 条沪深日历候选、本地每日增量候选和其他临时包。并行的
`L2-P0_QDATA_FACTOR_INPUT_ENGINEERING` 先使用 fixture/synthetic 数据完成
读取解析合同和拒绝越权测试，2026-08-04 又对下述精确正式面板通过真实 qdata 与
`PanelData` smoke；仍不得直接读取候选目录或把输入验收写成因子有效性结论。

2026-08-04 独立 `R0_SHSZ_PV` 窄门已通过：`cn-a-share-r0-shsz-pv-panel@20050104-20260803-v1` 可由消费者 `multi-factor` 以精确身份和 `--intent research` 解析。它只开放沪深普通 A 股价量单因子 Discovery 和预注册验证准备；不授权读取其他候选，不改变 CORE/canonical R0/R1/R2，也不授权 Shadow、Live 或自动交易。2026-08-05 已完成 512 因子 Discovery 与 50 候选稳定性描述，但 Validation 未消费、Holdout 未打开。自动日增量和 `candidate_assets.json` 仍是候选层，不能以研究意图解析。

## 意图与用途上限

| `--intent` | 最低用途上限 | 说明 |
|---|---|---|
| `inspection` | `DO_NOT_USE` | 只读盘点、审计、故障取证；不得形成研究结论 |
| `engineering` | `ENGINEERING_ONLY` | 应用开发、字段和接口检查 |
| `smoke` | `SMOKE_ONLY` | 管道冒烟、缺失和安全测试 |
| `research` | `RESEARCH_ONLY` | 研究输入；必须是不可变快照 |
| `shadow` | `SHADOW_ONLY` | Shadow 组合输入；必须有阶段门 |
| `live` | `LIVE_READY` | 真实运行候选；仍需人工和执行系统批准 |

`DO_NOT_USE` 只允许 `inspection`。可变数据库和缓存只能用于 `inspection` 或其用途上限允许的工程任务；要进入研究，必须先导出新的不可变快照并登记。

## 分层研究读取

`--intent research` 只检查单个数据身份的用途上限，不单独证明一个研究层已经闭合。
正式因子运行还必须在 run manifest 中记录 `research_tier`、精确输入
`dataset_id + snapshot_id` 集合和对应 gate receipt：

| `research_tier` | 必需正式输入 | 允许范围 | 当前状态 |
|---|---|---|---|
| `L0_INSPECTION` | 无 | 仅候选审计和工程验证，不允许研究 | `NOT_MET` |
| `R0_SHSZ_PV` | `cn-a-share-r0-shsz-pv-panel@20050104-20260803-v1` | 沪深普通 A 股价量单因子 Discovery 与预注册验证准备 | `OPEN`；512 因子 Discovery 与 50 候选稳定性描述已完成，未消费 Validation/Holdout |
| `R0_PV` | CORE-01～06 | 国内日频动量、反转、波动率、换手和流动性候选发现 | `NOT_MET` |
| `R1_CLASSIC_CN` | CORE-20 | 国内经典多因子；不得读取未准入 GCTX | `NOT_MET` |
| `R2_A_SHARE_GLOBAL` | CORE-20 + GCTX-8 | 加入经时区、收盘、币种和 `available_at` 对齐的全球环境 | `NOT_MET` |

旧 `G_DATA_RESEARCH_BASE` 仅作为 `R2_A_SHARE_GLOBAL` 的兼容别名。层级不会由目录
数量、候选覆盖或一次成功的 `qdata resolve` 自动推断；必须逐项解析所有必需正式快照，
并验证对应 L2 PIT、防未来信息、幸存者偏差、复权、可交易性、训练/验证/盲测和
可重建性回执。低层运行不得读取高层尚未准入的数据。当前 `datasets.json` 与 52 项
canonical 计划身份交集仍为 0，因此只有上述独立 `R0_SHSZ_PV` 精确窄门可运行；其他研究层必须拒绝。

## Agent/Codex 规则

1. 先 `list` 或 `describe`，再以精确身份 `resolve`；不得通过目录名猜测数据。
2. 研究、Shadow 和 Live 必须显式传入对应 `--intent`，并记录解析得到的 `dataset_id + snapshot_id`。
3. 如果提供 `--consumer`，解析器会检查该项目是否为所有者或登记消费者。
4. 未登记数据默认不能作为项目输入；先补目录、所有者、许可、质量和用途上限。
5. 返回路径默认只读。写入只能进入项目工作区、新的临时运行或经批准的新快照目录。
6. `quality_status=REVIEW` 不是质量通过；解析成功也不等于阶段门通过。
7. `杂/`、密钥、账户数据和券商凭据永不进入自动数据扫描。
8. `planned_datasets.json` 中的条目不是实际资产；不得据此拼接路径、自动下载或运行研究。完成接入和准入、进入 `datasets.json` 后才能由 `qdata` 读取。
9. 日常更新、合并或候选 manifest 的生成不自动授予研究权限；新快照只有原子进入 `datasets.json` 并通过相应用途检查后才能解析。
10. `tmp/local-dataset-build/` 是人工下载包的机械候选区；即使质量摘要为 `PASS`，其 `purpose_ceiling` 仍是 `INSPECTION_ONLY`。Codex、Agent 和项目不得扫描该目录猜测身份、绕过 `qdata` 读取或把候选直接作为因子/回测输入。
11. `L1-D2A-02` 的元数据和下载候选仍受第 10 条约束；本包只允许数据形状、公式和管道 smoke。只有 D2B 以后正式准入、不可变、登记且能以精确 `dataset_id + snapshot_id` 通过 `--intent research` 的快照，并且对应分层 gate receipt 通过，才可进入该层因子运行；CORE-01～06 对应 `R0_PV`，CORE-20 对应 `R1_CLASSIC_CN`，CORE-20 + GCTX-8 对应 `R2_A_SHARE_GLOBAL`。
12. D2A-04B 只允许读取其精确计划和 receipts 指向的候选做覆盖、字段、PIT、修订、权利和容量检查；不得目录扫描扩展到 48 库、新闻或高频。容量字段为 `user_fixed_free_gib=null`、`planned_batch_footprint_gib=150`、`hard_max_batch_footprint_gib=240` 和 `minimum_free_fraction=0.25`：不设置固定 200 GiB 用户预留，但超过 150 GiB 必须暂停重规划，且仍执行 240 GiB/25% 双停止门。D2A-04B 的充分性结论只能是“可提交 D2B”或“缺口仍阻断”，不能触发因子运行。
13. `candidate_assets.json` 只允许候选盘点、完整性检查和维护编排；索引出现、计数增加或 manifest 机械 PASS 均不得解释为正式 catalog 登记、研究准入或质量晋级。

## 示例

```powershell
# 所有登记项
${QUANTIFICATION_ROOT}\data\catalog\qdata.cmd list

# 已删除 Qlib 已移出活动目录；任何 intent 都返回“未登记”
${QUANTIFICATION_ROOT}\data\catalog\qdata.cmd resolve `
  cn-a-share-qlib-daily 2026-07-14 `
  --consumer capital-flow-attribution --intent inspection

# 新闻工作库允许工程检查，但 research 会被拒绝
${QUANTIFICATION_ROOT}\data\catalog\qdata.cmd resolve `
  news-signal-agent-state working-2026-06-27 --intent engineering
```
