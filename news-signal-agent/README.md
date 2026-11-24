# News Signal Agent

面向 A 股基本面研究的事件与证据工具。公告采集器提取披露正文，事件卡整理相关报道；分析者可按指定时点复盘一项投资判断。

## 动态基本面研判

研究配置指定标的、问题、材料范围和可验证假设。每份报告检查公司经营、上游供给、下游需求、竞争、政策与治理、市场定价，并标记缺口或陈旧材料。关系图列出每条推断的成立条件、支持材料、反证与下一项核验。示例为事后重建的信息窗口；材料引句及解释经过人工选取。

[2026 年上半年案例](docs/memory-cycle-case.md) · [配置文件](config/examples/inference_memory.yaml) · [GitHub Actions](https://github.com/Ms-March7/quantification-architecture/actions/workflows/fundamental-research.yml)。在 Actions 选择 **Run workflow**，填写带时区的截止时间及可选对照时间，运行结束后下载 `fundamental-research-brief`。留空截止时间会刷新配置中的订阅源。本途径直接使用 GitHub 运行器。

已有 Python 环境可在 `news-signal-agent/backend` 执行下列命令。基本面入口使用 PyYAML、Pydantic 2、httpx，默认无需模型凭据。

```shell
python -m app.fundamental ../config/examples/inference_memory.yaml --as-of 2026-05-21T18:00:00+08:00 --since 2026-03-26T18:00:00+08:00
python -m app.fundamental ../config/examples/inference_memory.yaml --refresh --cache-dir ../../tmp/feed-observations
```

`--refresh` 更新配置中的 RSS/Atom。`--cache-dir` 保存首次观测与修订历史，回放已有缓存时指定同一目录。刚抓取的条目只能进入本次或以后的研究窗口。GitHub 的临时运行器不会跨运行保留该缓存。覆盖范围取决于提供的原始材料与订阅；行情、客户订单等缺少证据的栏目会在报告中标出。需要用模型提出新的候选分支时，可以在已配置模型的环境中额外传入 `--draft`；默认报告验证配置中声明的分支和逐字摘录。

## 投研逻辑网络

以“存储涨价能否支撑更高估值”为例。现货涨价要经过公司合约重新定价，才可能体现在毛利中。客户提前备货可能同时推高本季出货、透支下一季需求。示例为每个推断记录成立条件、材料原句和研究者的解释；同一关系可以同时保留支持材料和反证。

下列命令在 Python 3.10+ 环境运行，复盘只依赖 PyYAML 和 Pydantic 2。示例中的公司、材料、时间与引句均为合成数据。

```shell
cd news-signal-agent/backend
python -m pip install "pydantic>=2.7,<3" PyYAML
python -m app.research_review ../config/examples/margin_review.yaml --as-of 2026-04-29T18:00:00+08:00
python -m app.research_review ../config/examples/margin_review.yaml --as-of 2026-05-15T12:00:00+08:00
```

命令输出 Markdown，内含 Mermaid 关系图和逐条依据。对比两次输出，留意第二次新增的下游采购反馈如何削弱“现货涨价已普遍传导到合约价”的判断。材料公开时间与研究记录时间分别控制可见性。引文核验检查文字是否确实来自指定片段，关系是否成立仍须结合口径判断。

如需由模型起草逻辑网络，可在装好后端依赖、配好项目模型后运行下列命令。输入只需研究问题和来源清单。使用完整示例作输入时，程序只向模型提供问题和截止时点可见的材料。每条候选连线须给出成立条件与进一步验证办法，生成时刻独立记录。

```shell
python -X utf8 -m app.research_draft ../config/examples/margin_review.yaml --as-of 2026-04-29T18:00:00+08:00 --output candidate.yaml
```

按命令输出的实际生成时刻运行 `app.research_review`，即可查看这份候选图并逐条审核。

数据治理说明：工作区统一入口是 `${QUANTIFICATION_ROOT}\data\catalog\datasets.json`，只读解析器是 `${QUANTIFICATION_ROOT}\data\catalog\dataset_cli.py`。当前 `data/news_signal_agent.sqlite3` 是可变应用工作库；它保持项目相对路径，只允许 `ENGINEERING_ONLY`。导出带 manifest、`available_at` 和内容 hash 的不可变事件特征快照并登记到 `data/published/news-events/` 前，不得作为多因子正式输入。发布镜像中的旧 SQLite 副本已移出发布目录并按 `DO_NOT_USE` 归档。

## 当前工作流

```text
collect
  -> normalize
  -> dedup
  -> update_event_store
  -> generate_event_card
  -> entity_link
  -> rule_classification
  -> llm_classification
  -> classification_merge
  -> feedback_override
  -> score
  -> market_validate
  -> backtest_validate
  -> rank
  -> memory_store
  -> notify
```

## 已实现

- 东方财富新闻。
- 巨潮公告列表和 PDF/HTML 正文抽取。
- RSS/Atom 多源适配。
- Tavily/SerpAPI 搜索适配。
- 海外市场和商品扰动源。
- Event Store：把本轮聚合事件写入长期事件表，维护 new/updating/confirmed/cooling/fading 状态。
- 事件聚合：本地向量相似度，可选语义向量。
- 事件卡包含标题、摘要、影响对象、来源链接。启用模型时可附逐字核验的来源摘录和一个待核实问题。
- LLM 事件卡和 LLM 分类 auto 模式，支持 SQLite 缓存、prompt version 和每轮调用上限。
- 分类审计：规则判断、LLM 判断、合并结果同时保留，冲突会标记 `classification_disagreement`。
- 人工修正。
- 可解释评分。
- 实时行情验证。
- 事件研究：raw return、benchmark return、abnormal return、CAR。
- 历史榜和评分权重校准 API。
- workflow artifact 和真正局部重跑。
- 高影响事件留存和存储清理。
- 邮件和短信 webhook 通知。
- 网页分时图：价格线、均价线、成交量柱，东财失败时切新浪 1 分钟线。
- 事件优先网页：默认展示事件榜单，工作流节点折叠为审计区。
- 自选池支持新增、编辑和删除。

## 启动

```powershell
cd ${QUANTIFICATION_ROOT}\news-signal-agent\backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8766
```

打开：

```text
http://127.0.0.1:8766/app
```

## 关键接口

```text
POST /api/workflow/run-news
POST /api/workflow/run-news/async
GET  /api/workflow/run-news/jobs/{job_id}
POST /api/workflow/rerun-from
GET  /api/event-cards
GET  /api/events
GET  /api/rank-history
GET  /api/scoring/calibration
GET  /api/market/intraday/{symbol}
GET  /api/market/daily/{symbol}
```

## 可选能力

运行模式：

```text
fast: 默认网页刷新模式，不跑 Codex/LLM，不跑回测，适合实时盯盘。
deep: 手动切换深度模式，只挑少量高优先级候选做 Codex 分类。
```

深度模式下进度条会显示当前 LLM 处理第几个事件/信号、已用时间、预计剩余和最坏等待时间。
默认最多挑 3 个候选，每次 Codex 调用超时 25 秒。

Codex CLI / LLM：

```text
LLM_PROVIDER
CODEX_CLI_COMMAND
```

默认配置已使用本机 Codex CLI：

```text
LLM_PROVIDER=codex_cli
CODEX_CLI_COMMAND=codex
```

不需要 DeepSeek key。确保你本机已登录 Codex，深度模式会通过 `codex exec` 做少量 LLM 分类。
批量事件卡默认不走 Codex；Codex 超时或输出不可解析时自动回退规则版。

如需改回 HTTP API，可在 `backend/.env` 中设置：

```env
LLM_PROVIDER=deepseek
LLM_API_KEY=your_api_key_here
LLM_API_BASE=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash
```

搜索：

```text
TAVILY_API_KEY
SERPAPI_API_KEY
```

通知：

```text
WECOM_WEBHOOK_URL
PUSHPLUS_TOKEN
SERVERCHAN_SENDKEY
EMAIL_*
SMS_WEBHOOK_URL
```

## 当前限制

- 海外/商品源受 Yahoo chart 可用性影响。
- 语义向量模型是可选重依赖。
- 新事件需要未来交易日后才有回测后验样本。
- 扫描版公告 PDF 可能无法抽正文。

详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
