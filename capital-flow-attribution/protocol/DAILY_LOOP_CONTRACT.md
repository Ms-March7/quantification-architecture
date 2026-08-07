# 第一个日频闭环合同

协议版本：`0.1.0`  
状态：`FROZEN_FOR_G0_REVIEW`  
冻结日期：`2026-07-18`

## 1. 目的

S1第一闭环证明一条小型但完整的数据链可以被采集、标准化、按时间点冻结、会计核验、质量拦截并生成不过度归因的报告。

最小闭环是扩展基线，不是最终数据范围。未通过G1前不增加多年分钟数据或复杂身份模型。

## 2. 执行批次

1. 来源探针：固定最近5个交易日，仅验证来源、字段、发布时间、许可和舍入。
2. 正式试点：来源探针获批后，固定20个交易日，产生可复现快照和离线夹具。
3. 历史回填：不属于S1首闭环，必须在G1后另行批准。

具体5日和20日起止日期由S1工作包在运行前写入冻结配置；不得运行后按结果改窗口。

## 3. 市场与证券范围

- 沪、深、北交易所历史时点个股和ETF证券主表；
- 沪股通、深股通、港股通和ETF互联互通历史资格；
- 港股通合资格证券行情及必要行业、日历、汇率背景；
- 全部内地ETF按资产类别分组；
- 申万2021一级行业背景和市场广度。

纳入市场范围不等于每个市场都有相同公开字段。缺失必须显式报告，不能通过缩小市场定义掩盖。

## 4. S1输入

### 4.1 必需输入

- 历史证券主表、上市退市和交易状态；
- 分市场交易日历；
- 日行情和复权/公司行为背景；
- ETF主表、每日份额、NAV、可得AUM及发布时间；
- 各交易所公开可得的融资余额、买入、偿还及融券汇总；
- 互联互通历史资格和公开可得日频字段；
- 人民币/港币汇率和可用时间；
- 来源、许可、字段和manifest元数据。

### 4.2 现有只读背景

- Qlib快照可作为日行情结构和研究背景，保持 `RESEARCH_ONLY`；
- 旧东方财富缓存只作为 `SMOKE_ONLY`；
- 历史资金监控报告只用于缺失和过度归因反例。

任何现有数据若不满足许可和 `available_at` 合同，不能因“已经存在”而升级为正式输入。

## 5. 标准处理链

```text
source probe
  -> raw append-only evidence
  -> normalized tables
  -> point-in-time snapshot
  -> manifest and quality states
  -> ETF reconciliation
  -> margin reconciliation
  -> unified daily state ledger
  -> evidence-scoped report
  -> offline validation fixtures
```

## 6. 必须产物

S1实现时至少产生下列逻辑产物，物理路径由S1配置冻结：

```text
security_master_snapshot
trading_calendar_snapshot
daily_market_context
etf_shares_nav_ledger
etf_aum_reconciliation
margin_balance_ledger
margin_reconciliation
stock_connect_coverage
daily_state_ledger
coverage_report
conflict_report
dataset_manifests
daily_state_report
golden_fixtures
failure_fixtures
```

日频状态账本只合并可比较的事实和代理，不形成一个声称覆盖全部投资者身份的“总净流入”。

## 7. 状态语义

### 7.1 数据状态

`OK / MISSING / STALE / CONFLICT / UNLICENSED`

### 7.2 证据状态

`OBSERVED / PROXY / PARTIALLY_IDENTIFIED / NOT_IDENTIFIED`

### 7.3 测试状态

`PASS / FAIL / ERROR / SKIP`

### 7.4 工作流状态

`DONE / REVIEW / BLOCKED`

坏样本被正确识别时，对应测试为 `PASS`，但样本研究状态为 `REVIEW`。来源不可用但合同正确拦截时，不得把测试误报为数据正常。

## 8. 报告截止点

交易日T报告使用下一内地交易日的 `T+1 09:00 Asia/Shanghai` 截止点。每条输入必须满足 `available_at <= as_of`。晚到数据进入修订版，不静默覆盖首版。

## 9. S1不做

- 不回答精确机构、现金散户或外资净金额；
- 不把融资散户外推为全部散户；
- 不把ETF申赎外推为机构净买卖；
- 不建立行业资金迁移模型；
- 不计算正式1日/5日/20日可切换性；
- 不下载多年分钟、逐笔或L2；
- 不训练分类器；
- 不发布多因子资金特征；
- 不生成投资建议。

## 10. Harness接口

未来在现有 `quant-research-reliability` Harness中增加 `capital_flow_attribution` 目标，使用固定离线夹具和隔离运行目录。目标至少验证：

- 字段、代码、日期、单位和币种；
- `available_at` 和历史资格；
- ETF及融资会计；
- 缺失、过期、冲突、许可和修订；
- ETF/个股及互联互通防重复记账；
- 报告过度归因和输出路径安全。

本S0工作包只冻结接口，不修改现有Harness。

## 11. 存储与停止条件

S1硬上限10GB，运行前后都测量G盘空间。出现以下任一情况立即停止写入并进入 `REVIEW`：

- 本阶段累计写入预计超过10GB；
- G盘预计可用空间低于120GB；
- 来源许可不清；
- raw无法保持只追加；
- 时间点或会计合同无法实现；
- 输出需要跨越S1推断边界。

## 12. G1通过条件

来源、许可、字段、日期、单位、覆盖、时间点、会计恒等式、好坏样本和报告语言全部通过。任何核心市场或核心数据族缺失时，可以完成测试，但G1保持 `REVIEW`，不能声称闭环完整。
