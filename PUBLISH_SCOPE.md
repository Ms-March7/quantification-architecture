# 发布范围

## 纳入

- 根级治理说明与 Skill/Harness 控制文件；
- `control/` 的治理文档、工具与测试；
- `data/catalog/` 的政策、schema、合同、元数据目录和解析代码；
- `多因子/` 的协议、配置、源代码与测试；
- `capital-flow-attribution/` 的源代码、配置、协议、文档与测试；
- `news-signal-agent/` 的源代码、配置、文档与测试，以及仅含占位符的 `.env.example`。

测试中保留的少量数据形状 fixture 已改为显式合成值，仅用于结构测试，不是供应商观测数据。
CSV、XML、JSONL 等 fixture 一律排除，因此依赖它们的少数测试只保留结构，不能在镜像内单独运行。
真实自选股配置已排除，只保留 `watchlist.example.yaml` 占位模板。

## 排除

- `data/market/`、`data/published/` 及任何 raw、curated、snapshot 或 quarantine 数据；
- `reports/`、`tmp/`、archive、research/engineering runs、检查点和缓存；
- `.venv`、runtime、`node_modules`、构建产物和日志；
- 真实 `.env`、凭据、License、Cookie、账户或持仓材料；
- CSV、Parquet、数据库、压缩包、PDF、Office 文档及其他数据/二进制载荷；
- 嵌套 Git 仓库及第三方源码副本。

## 权利边界

本仓库不授予任何第三方数据的访问、保存、派生、再分发或商业使用权。代码可见不代表其所调用的数据端点已获授权；元数据登记也不代表数据已经正式准入。

`COPY_AUDIT.json` 记录本次白名单复制结果；其存在只证明发布工程完成，不证明研究阶段门通过。
