# 开源组件登记与边界

更新日期：2026-07-19  
适用运行：`20260719-g1-research-v22`

## 1. 采用原则

成熟开源组件负责通用能力；项目代码只保留中国市场数据合同、时间点、会计、证据等级和阶段门。开源库或聚合接口不能把 `PROXY` 升级为 `OBSERVED`，也不能替代来源许可审查。

| 组件 | 复用能力 | 本项目仍负责 |
|---|---|---|
| [DuckDB](https://duckdb.org/docs/) | Parquet转换、只读查询目录、行数往返核验 | 数据集版本、manifest、许可和PIT |
| [Pandera](https://pandera.readthedocs.io/) | DataFrame字段、状态和唯一键校验 | 业务会计与禁止推断 |
| [Tenacity](https://tenacity.readthedocs.io/) | 有界重试和退避 | raw不可覆盖、坏HTTP-200正文识别、来源降级 |
| [NetworkX](https://networkx.org/documentation/stable/) | 迁移图数据结构 | 重复边拒绝、联合守恒和未知资金节点 |
| [scikit-learn TimeSeriesSplit](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html) | 扩展窗口、固定测试窗和gap | PIT可用时间、执行滞后、成本与缺信号政策 |
| [ruptures](https://github.com/deepcharles/ruptures) | 事后变点诊断 | 变点不进入信号、不解释为投资者身份 |
| [AKShare](https://akshare.akfamily.xyz/) | 公开接口发现和二级适配 | 官方源优先、许可`UNVERIFIED`和不升格规则 |
| [Tushare Pro](https://tushare.pro/document/2) | 有授权时的ETF/PIT/行业补数接口 | token只读环境变量、无token明确SKIP、不得持久化凭据 |

## 2. 没有直接复用的部分

- 北交所JSONP安全解析、WAF兼容和逐日raw证据；
- 深交所仅对官方域名启用的无环境代理回退；
- 港交所HTTP 200全零正文的结构校验和官方交易所补源；
- ETF份额/NAV/AUM会计恒等式及许可上限；
- `OBSERVED / PROXY / PARTIALLY_IDENTIFIED / NOT_IDENTIFIED`；
- 未知资金联合守恒、回测缺信号日空仓和阶段门裁决。

## 3. 依赖组

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[china-sources,research]"
```

核心运行不强制安装中国市场和研究扩展；无可选依赖、无凭据时必须显式SKIP，不能静默换源。
