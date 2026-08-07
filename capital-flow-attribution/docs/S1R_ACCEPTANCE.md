# S1R工作包验收报告

日期：`2026-07-19`  
范围：`WP-S1R-02—04`  
工作包状态：`DONE`  
兼容性测试：`PASS`  
研究阶段门：`G1 REVIEW`

## 1. 结论

S1R剩余三个工作包已经一次性完成。通用工程能力由开源组件承担，旧S1研究合同、PIT规则、会计恒等式和未知资金边界保持不变。

S1R候选运行 `20260719-s1r-pilot-v11` 与冻结基准 `20260718-s1-pilot-v10` 的9张核心CSV逐字节一致，66份raw文件的相对路径与SHA-256一致。新架构额外生成9张Parquet和DuckDB目录，所有行数与CSV一致。

## 2. 模块拆分

原 `pipeline.py` 从903行降至267行。职责分布如下：

| 模块 | 职责 |
|---|---|
| `collector.py` | Tenacity重试、host熔断、raw只追加、缓存回建和manifest |
| `exchange_adapters.py` | 上交所、深交所、北交所、港交所来源解析 |
| `ledger.py` | 统一日频账本和来源覆盖状态 |
| `validation.py` | Pandera日频账本和证券主表合同 |
| `storage.py` | CSV转Parquet、SHA-256和DuckDB目录 |
| `open_source_adapters.py` | AKShare/Tushare可选二级适配器 |
| `reconciliation.py` | 双源MATCH/CONFLICT/缺失对账，不平均、不静默覆盖 |
| `comparison.py` | 冻结运行逐表和raw哈希比较 |
| `reporting.py` | manifest和人类可读报告 |

## 3. 开源适配器验收

最终在线探针：

```text
${QUANTIFICATION_ROOT}\tmp\capital-flow-attribution\20260719-s1r-adapter-smoke-v3
```

- AKShare 1.18.64已安装；
- `stock_info_a_code_name`因深交所代理断连失败；
- `stock_zh_a_spot_em`因东财代理断连失败；
- 自动回退到 `stock_zh_a_spot` 后成功取得5527行；
- 5527个规范证券代码唯一并通过Pandera；
- 该结果保持 `partial / UNVERIFIED / SMOKE_ONLY`，不进入正式PIT证券主表；
- Tushare 1.4.29已安装；没有 `TUSHARE_TOKEN`，明确记录SKIP且没有API请求；
- manifest只记录环境变量是否配置，不记录token值。

失败探针 `v1` 和中间成功探针 `v2` 保留在 `tmp` 作为来源回退证据；没有覆盖或删除。

## 4. S1R候选运行

数据：

```text
${QUANTIFICATION_ROOT}\data\capital-flow-attribution\s1\20260719-s1r-pilot-v11
```

报告：

```text
${QUANTIFICATION_ROOT}\reports\capital-flow-attribution\s1\20260719-s1r-pilot-v11
```

| 表 | 行数 |
|---|---:|
| universe_research_proxy | 6118 |
| market_context | 60 |
| margin_ledger | 60 |
| etf_scale_proxy | 1277 |
| stock_connect_eligible_current | 4606 |
| stock_connect_turnover | 60 |
| fx_reference | 20 |
| source_coverage | 9 |
| unified_daily_state | 280 |

以上每张表的Parquet行数与DuckDB视图计数相等。

## 5. v10/v11兼容性

正式对比：

```text
${QUANTIFICATION_ROOT}\reports\capital-flow-attribution\s1r\20260719-v10-v11-comparison
```

- 9/9核心CSV SHA-256一致；
- 66/66 raw相对路径与SHA-256一致；
- Pandera日频账本验证PASS；
- Parquet与DuckDB行数一致；
- 对比脚本的相同样本PASS、变更样本FAIL测试均通过。

## 6. 完整测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\probe_optional_sources.py --self-test
.\.venv\Scripts\python.exe scripts\compare_s1_runs.py `
  --baseline-root "${QUANTIFICATION_ROOT}\data\capital-flow-attribution\s1\20260718-s1-pilot-v10" `
  --candidate-root "${QUANTIFICATION_ROOT}\data\capital-flow-attribution\s1\20260719-s1r-pilot-v11" `
  --output-dir "<new-output-dir>"
.\.venv\Scripts\python.exe -m pip check
```

结果：23/23单元测试PASS；适配器离线自测PASS；好/坏夹具PASS；依赖检查无冲突。

## 7. 隔离与存储

- v11数据体积4,057,462 bytes，远低于10GB硬上限；
- 验收时G盘剩余约338.0 GiB，高于120GB底线；
- 未修改、移动、复制或链接Qlib；
- 未修改多因子、现有Harness或资金监控技能；
- 未保存token、cookie或商业账户凭据；
- 未执行Git暂存、提交、分支或推送。

## 8. G1仍为REVIEW的原因

架构优化没有伪造缺失数据，以下问题继续存在：

1. 北交所融资融券20日正式观测缺失；
2. 全部内地ETF的20日份额+NAV链路缺失；
3. 历史互联互通PIT资格未补齐；
4. 深交所互联互通部分日报缺失；
5. 申万2021历史PIT映射未获授权；
6. 沪市2026-06-22融资余额恒等式残差仍为CONFLICT。

因此S1R工作包状态可以是DONE、兼容性测试可以是PASS，但研究阶段门必须保持G1 REVIEW。
