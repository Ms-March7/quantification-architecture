# Quantification Architecture

这是本地 Quantification 工作区的**脱敏架构镜像**，用于私有 GitHub 仓库中的版本管理、代码审阅与协作。

本仓库包含：

- 工作区治理、数据准入与质量控制文档；
- 数据目录 schema、合同、解析与维护工具；
- 多因子研究的协议、配置、计算代码与测试；
- 资金流归因和新闻信号项目的源代码与工程配置；
- 可复核的发布范围及文件复制审计。

本仓库不包含：

- 行情、财务、公司行动、基金持仓等数据载荷；
- RiceQuant、JoinQuant、校内数据库或其他供应商数据；
- 密钥、令牌、密码、License、账户信息或真实 `.env`；
- Python/Node 运行环境、缓存、日志、实验运行、检查点或研究报告；
- 独立 Git 仓库的历史或第三方源码副本。

## 状态边界

仓库中的协议、候选因子目录、工程测试和 Discovery 代码不等同于正式数据准入、独立 Validation、Holdout 通过或可交易结论。研究输入仍必须通过不可变的 `dataset_id + snapshot_id` 解析，并遵守 `data/catalog/ACCESS_POLICY.md`。

## 入口

- 工作区导航：`control/WORKSPACE_INDEX.md`
- 路线图：`control/ROADMAP.md`
- 数据目录说明：`data/catalog/README.md`
- 数据库手册：`data/catalog/DATABASE_MANUAL.md`
- 数据质量：`data/catalog/DATA_QUALITY_POLICY.md`
- 数据访问：`data/catalog/ACCESS_POLICY.md`
- 多因子研究：`多因子/README.md`
- 发布边界：`PUBLISH_SCOPE.md`
- 复制审计：`COPY_AUDIT.json`（记录脱敏前白名单复制）；最终内容与哈希以 `FILE_MANIFEST.json` 为准。

## 本地重建

数据与运行环境不随仓库分发。克隆后应按各项目的 README 和锁定依赖文件创建本地环境；正式数据只能从获授权来源进入本地 catalog，再由不可变快照供研究读取。

为坚持零供应商载荷，CSV、XML、JSONL 等离线 fixture 未随镜像分发；依赖这些 fixture 的少数测试仅保留测试源码，不能在本仓库中独立复现。唯一保留的 JSON 数据形状 fixture 已替换为合成值。
