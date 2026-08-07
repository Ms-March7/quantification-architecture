# News Signal Agent 架构

## 定位

News Signal Agent 是面向 A 股自选标的的实时新闻事件雷达。它不做账户持仓、不计算盈亏、不输出买卖建议、不自动交易。它负责把多源消息、公告正文、海外和商品扰动、行情与回测信息组织成可查看、可追踪、可人工修正的事件信号。

## V0.6 工作流

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

## 运行模式

网页刷新默认使用 `fast` 模式：

```text
fast
  -> 采集、聚合、规则判断、行情验证、榜单
  -> 禁用 Codex/LLM 事件卡
  -> 禁用 Codex/LLM 分类
  -> 禁用回测
```

需要更完整解释时，手动切到 `deep` 模式：

```text
deep
  -> 使用本地事件卡摘要
  -> 只对规则排序靠前的少量候选做 Codex 分类
  -> 禁用批量 Codex 事件卡
  -> 禁用回测
```

这样日常盯盘优先保证速度，深度模式只做少量 Codex 精判。完整深度解析应放到单条事件详情按钮里手动触发。

网页进度条会显示节点内部进度。深度模式下，`llm_classification`
会展示当前处理第几个信号、已用时间、预计剩余、按 LLM 剩余调用预算计算的最坏等待时间。
默认深度模式最多挑 3 个候选，每次 Codex 调用超时 25 秒。

## 从 V0.5 到 V0.6

```text
event_cluster
  -> update_event_store
```

```text
event_card
  -> generate_event_card
```

```text
classify
  -> rule_classification
  -> llm_classification
  -> classification_merge
```

```text
horizon
  -> 已并入 rule_classification / llm_classification 的独立输出字段
```

## Event Store

V0.5 的事件只属于本轮运行。V0.6 会把聚合事件写入长期 `events` 表，同一类事件后续运行会更新同一个长期事件。

事件状态：

```text
new -> updating -> confirmed -> cooling -> fading
```

事件证据写入 `event_evidence`，包含新闻 id、标题、来源、链接、发布时间和原始 JSON。

## 分类审计

分类不再是单个黑盒节点。

```text
rule_classification
  -> 关键词规则判断
  -> 输出方向、周期、置信度、影响强度、不确定性惩罚、命中词
```

```text
llm_classification
  -> 默认通过本机 Codex CLI 判断，也可切换到 DeepSeek/OpenAI-compatible HTTP API
  -> 输出方向、周期、置信度、影响强度、不确定性惩罚、短理由
```

```text
classification_merge
  -> 合并规则和 LLM
  -> LLM 成功时默认采纳
  -> 如果 LLM 不确定但规则置信较高，则保留规则
  -> 方向冲突时标记 classification_disagreement
```

前端信号详情会显示：

```text
规则方向 / 规则命中
LLM方向 / LLM缓存状态
最终来源 / 判断冲突
```

## LLM 缓存和预算

LLM 调用经过统一客户端：

```text
prompt + model + prompt_version + 参数
  -> cache_key
  -> llm_cache
```

命中缓存时不再请求模型。未命中才调用配置的 provider，并记录：

```text
provider
model
prompt_version
usage
cache_hit
created_at
```

默认配置：

```yaml
llm:
  budget:
    max_calls_per_run: 8
  event_card:
    provider: codex_cli
    prompt_version: event_card_v0.6
    cache: on
    max_calls_per_run: 3
    timeout_seconds: 45
  classify:
    provider: codex_cli
    prompt_version: classify_v0.6
    cache: on
    max_calls_per_run: 8
    timeout_seconds: 45
```

## 数据源

已接入：

- 东方财富新闻。
- 巨潮公告列表与 PDF/HTML 正文抽取。
- RSS/Atom 多源。
- Tavily / SerpAPI 搜索适配。
- Yahoo chart 海外市场和商品扰动。
- 东方财富 / 新浪实时行情。
- 东方财富分时和日线，新浪 1 分钟线作为分时 fallback。

## 评分

评分仍保留可解释分量：

- 方向置信
- 关联度
- 来源可信
- 影响强度
- 新鲜度
- 多源确认
- 市场验证
- 不确定性惩罚

`/api/scoring/calibration` 会用历史榜和后验异常收益生成权重建议。样本不足时返回 `not_enough_samples`。

## 回测验证

`backtest_validate` 使用事件研究框架：

```text
raw_return
benchmark_return
abnormal_return
CAR
```

默认基准是 `512050`。窗口：

```text
T+1 / T+3 / T+5 / T+10 / T+20
```

新发生的事件没有未来行情时，会等待后续运行积累样本。

## 局部重跑

每个节点都会写入 `workflow_artifacts`。  
`POST /api/workflow/rerun-from` 会从指定节点前一个 artifact 恢复上下文继续跑。

示例：从 `rank` 重跑时只执行：

```text
rank -> memory_store -> notify
```

## 存储

SQLite 表：

- `raw_messages`
- `events`
- `event_evidence`
- `event_cards`
- `rank_history`
- `retained_events`
- `user_overrides`
- `notification_log`
- `workflow_artifacts`
- `llm_cache`

清理规则：

```yaml
storage_retention:
  raw_message_days: 14
  rank_history_days: 180
  notification_log_days: 60
  workflow_artifact_days: 7
  llm_cache_days: 30
  max_event_cards: 5000
```

## 通知

支持：

- console
- 企业微信
- PushPlus
- Server 酱
- Email SMTP
- 通用短信 webhook

通知带冷却和单标的每日上限。

## 行情界面

API：

```text
GET /api/market/quote/{symbol}
GET /api/market/intraday/{symbol}
GET /api/market/daily/{symbol}
```

网页信号详情里有“查看分时”，会展示价格线、均价线和成交量柱。东财分时失败时自动尝试新浪 1 分钟线。

## 网页交互

当前网页以事件榜单为主视图：

```text
顶部：运行模式、刷新、进度
主区：利好/利空/全部事件榜单
右侧：事件详情、审计、留存、历史
左侧：自选池新增、编辑、删除
底部折叠：工作流节点审计
```

自选池保存后只更新列表，不自动触发完整刷新；需要重新计算时手动点击刷新。

## 剩余限制

- Event Store 的 stable key 仍是规则版，还不是完整语义事件归并。
- 事件状态衰减还是时间和证据规则，尚未结合后续行情动态衰减。
- `sentence-transformers` 是可选重依赖，未强制安装。
- 事件研究需要未来交易日数据，刚发生的事件无法立即形成后验样本。
- 默认 LLM 需要本机可运行 `codex exec` 且已登录 Codex；HTTP API fallback 才需要 API key。
- 巨潮正文抽取对扫描 PDF 或复杂 PDF 仍可能失败。
- 短信是通用 webhook，还不是阿里云/腾讯云 SDK 原生适配。
