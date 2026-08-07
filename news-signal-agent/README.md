# News Signal Agent

自选代码驱动的 A 股实时新闻事件雷达。

它不做持仓、不算盈亏、不输出买卖建议、不自动交易。它把新闻、公告正文、搜索源、海外/商品扰动和行情组织成可查看、可追踪、可人工修正的事件信号。

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
- 事件卡：标题、极简摘要、影响对象、原文链接。
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
