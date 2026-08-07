import { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  AlertTriangle,
  Bell,
  ExternalLink,
  GitBranch,
  ListFilter,
  Newspaper,
  RefreshCw,
  Search,
} from "lucide-react";
import "./styles.css";

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8766/api";

type Direction = "bullish" | "bearish" | "neutral" | "uncertain";
type Horizon = "short" | "mid" | "long";

type WatchSymbol = {
  symbol: string;
  name?: string;
  asset_type: string;
  market?: string;
  tags: string[];
};

type WorkflowNode = {
  node_name: string;
  status: string;
  output_ids: string[];
  decisions: Array<{ label: string; value: string | number | boolean }>;
  warnings: string[];
  started_at?: string;
  finished_at?: string;
};

type NewsItem = {
  id: string;
  title: string;
  summary?: string;
  source: string;
  source_type: string;
  url?: string;
  published_at?: string;
  metadata: Record<string, unknown>;
};

type ScoreBreakdown = {
  direction_confidence: number;
  relevance: number;
  source_credibility: number;
  impact: number;
  freshness: number;
  corroboration: number;
  market_validation: number;
  uncertainty_penalty: number;
};

type Signal = {
  id: string;
  event_id: string;
  symbol: string;
  name?: string;
  direction: Direction;
  primary_horizon: Horizon;
  final_score: number;
  horizon_scores: Record<string, number>;
  brief: string;
  score_breakdown: ScoreBreakdown;
  warnings: string[];
  metadata: Record<string, unknown>;
  evidence: NewsItem[];
};

type RunSnapshot = {
  run_id: string;
  stats: {
    news_items: number;
    events: number;
    signals: number;
    bullish: number;
    bearish: number;
  };
  watchlist: WatchSymbol[];
  nodes: WorkflowNode[];
  news_items: NewsItem[];
  signals: Signal[];
  bullish_top: Signal[];
  bearish_top: Signal[];
};

type SignalFilter = "all" | "bullish" | "bearish";

const componentLabels: Array<[keyof ScoreBreakdown, string]> = [
  ["direction_confidence", "方向置信"],
  ["relevance", "关联度"],
  ["source_credibility", "来源可信"],
  ["impact", "影响强度"],
  ["freshness", "新鲜度"],
  ["corroboration", "多源确认"],
  ["market_validation", "市场验证"],
  ["uncertainty_penalty", "不确定性"],
];

function App() {
  const [watchlist, setWatchlist] = useState<WatchSymbol[]>([]);
  const [snapshot, setSnapshot] = useState<RunSnapshot | null>(null);
  const [selectedSignalId, setSelectedSignalId] = useState<string | null>(null);
  const [selectedNodeName, setSelectedNodeName] = useState<string>("collect");
  const [filter, setFilter] = useState<SignalFilter>("all");
  const [query, setQuery] = useState("");
  const [isRunning, setIsRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${API_BASE}/watchlist`)
      .then((response) => response.json())
      .then((payload) => setWatchlist(payload.items ?? []))
      .catch(() => setWatchlist([]));
    runWorkflow();
  }, []);

  const signals = useMemo(() => {
    if (!snapshot) return [];
    const source =
      filter === "bullish"
        ? snapshot.bullish_top
        : filter === "bearish"
          ? snapshot.bearish_top
          : [...snapshot.bullish_top, ...snapshot.bearish_top];
    const needle = query.trim().toLowerCase();
    if (!needle) return source;
    return source.filter((signal) =>
      `${signal.symbol} ${signal.name ?? ""} ${signal.brief}`.toLowerCase().includes(needle),
    );
  }, [filter, query, snapshot]);

  const selectedSignal = useMemo(() => {
    if (!snapshot) return null;
    const all = [...snapshot.bullish_top, ...snapshot.bearish_top, ...snapshot.signals];
    return all.find((signal) => signal.id === selectedSignalId) ?? signals[0] ?? null;
  }, [selectedSignalId, signals, snapshot]);

  const selectedNode = useMemo(() => {
    return snapshot?.nodes.find((node) => node.node_name === selectedNodeName) ?? snapshot?.nodes[0] ?? null;
  }, [selectedNodeName, snapshot]);

  async function runWorkflow() {
    setIsRunning(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE}/workflow/run-news`, { method: "POST" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as RunSnapshot;
      setSnapshot(payload);
      setWatchlist(payload.watchlist ?? []);
      const firstSignal = payload.bullish_top[0] ?? payload.bearish_top[0] ?? null;
      setSelectedSignalId(firstSignal?.id ?? null);
      setSelectedNodeName(payload.nodes[0]?.node_name ?? "collect");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "刷新失败");
    } finally {
      setIsRunning(false);
    }
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand-block">
          <div className="brand-mark">
            <Newspaper size={22} />
          </div>
          <div>
            <div className="eyebrow">NEWS SIGNAL</div>
            <h1>盯盘 Agent</h1>
          </div>
        </div>

        <section className="watchlist-panel">
          <div className="panel-title">
            <ListFilter size={16} />
            <span>自选池</span>
          </div>
          <div className="watchlist">
            {watchlist.map((item) => (
              <button className="watch-item" key={item.symbol} title={item.tags.join(" / ")}>
                <span>
                  <strong>{item.symbol}</strong>
                  <em>{item.name}</em>
                </span>
                <small>{item.asset_type}</small>
              </button>
            ))}
          </div>
        </section>

        <section className="source-panel">
          <div className="panel-title">
            <Bell size={16} />
            <span>提醒</span>
          </div>
          <div className="notify-grid">
            <span>微信</span>
            <b>预留</b>
            <span>短信</span>
            <b>预留</b>
          </div>
        </section>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <div className="eyebrow">RUN {snapshot?.run_id ?? "READY"}</div>
            <h2>新闻信号工作台</h2>
          </div>
          <button className="primary-action" onClick={runWorkflow} disabled={isRunning} title="刷新新闻工作流">
            <RefreshCw size={18} className={isRunning ? "spin" : ""} />
            <span>{isRunning ? "刷新中" : "刷新"}</span>
          </button>
        </header>

        {error ? (
          <div className="error-banner">
            <AlertTriangle size={18} />
            <span>{error}</span>
          </div>
        ) : null}

        <section className="metrics-strip">
          <Metric label="新闻" value={snapshot?.stats.news_items ?? 0} />
          <Metric label="事件" value={snapshot?.stats.events ?? 0} />
          <Metric label="信号" value={snapshot?.stats.signals ?? 0} />
          <Metric label="利好榜" value={snapshot?.stats.bullish ?? 0} tone="positive" />
          <Metric label="利空榜" value={snapshot?.stats.bearish ?? 0} tone="negative" />
        </section>

        <section className="workflow-band">
          <div className="section-head">
            <GitBranch size={18} />
            <h3>工作流</h3>
          </div>
          <div className="node-row">
            {(snapshot?.nodes ?? []).map((node) => (
              <button
                className={`flow-node ${node.status} ${
                  selectedNodeName === node.node_name ? "selected" : ""
                }`}
                key={node.node_name}
                onClick={() => setSelectedNodeName(node.node_name)}
                title={node.node_name}
              >
                <span>{node.node_name}</span>
                <b>{node.output_ids.length}</b>
              </button>
            ))}
          </div>
        </section>

        <section className="main-grid">
          <div className="signal-board">
            <div className="board-toolbar">
              <div className="segmented" aria-label="signal-filter">
                <button className={filter === "all" ? "active" : ""} onClick={() => setFilter("all")}>
                  全部
                </button>
                <button
                  className={filter === "bullish" ? "active positive" : ""}
                  onClick={() => setFilter("bullish")}
                >
                  利好
                </button>
                <button
                  className={filter === "bearish" ? "active negative" : ""}
                  onClick={() => setFilter("bearish")}
                >
                  利空
                </button>
              </div>
              <label className="search-box">
                <Search size={15} />
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="代码 / 名称 / 判断"
                />
              </label>
            </div>

            <div className="signal-list">
              {signals.length ? (
                signals.map((signal, index) => (
                  <button
                    className={`signal-row ${signal.direction} ${
                      selectedSignal?.id === signal.id ? "selected" : ""
                    }`}
                    key={signal.id}
                    onClick={() => setSelectedSignalId(signal.id)}
                  >
                    <span className="rank">{index + 1}</span>
                    <span className="signal-main">
                      <strong>
                        {signal.symbol} {signal.name}
                      </strong>
                      <em>{signal.brief}</em>
                    </span>
                    <span className="horizon">{horizonText(signal.primary_horizon)}</span>
                    <span className={signal.final_score >= 0 ? "score positive" : "score negative"}>
                      {formatScore(signal.final_score)}
                    </span>
                  </button>
                ))
              ) : (
                <div className="empty-state">暂无入榜信号</div>
              )}
            </div>
          </div>

          <aside className="detail-panel">
            <NodeDetail node={selectedNode} />
            <SignalDetail signal={selectedSignal} />
          </aside>
        </section>
      </section>
    </main>
  );
}

function Metric({ label, value, tone }: { label: string; value: number; tone?: "positive" | "negative" }) {
  return (
    <div className={`metric ${tone ?? ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function NodeDetail({ node }: { node: WorkflowNode | null }) {
  if (!node) return null;
  return (
    <section className="detail-section">
      <div className="section-head">
        <Activity size={18} />
        <h3>节点</h3>
      </div>
      <div className="node-detail-title">
        <strong>{node.node_name}</strong>
        <span className={`status-pill ${node.status}`}>{node.status}</span>
      </div>
      <dl className="node-facts">
        <div>
          <dt>产出</dt>
          <dd>{node.output_ids.length}</dd>
        </div>
        <div>
          <dt>开始</dt>
          <dd>{formatDate(node.started_at)}</dd>
        </div>
        <div>
          <dt>结束</dt>
          <dd>{formatDate(node.finished_at)}</dd>
        </div>
      </dl>
      <div className="decision-list">
        {node.decisions.length ? (
          node.decisions.map((item) => (
            <div key={item.label}>
              <span>{item.label}</span>
              <b>{String(item.value)}</b>
            </div>
          ))
        ) : (
          <div>
            <span>decision</span>
            <b>none</b>
          </div>
        )}
      </div>
      {node.warnings.length ? (
        <div className="warnings">
          {node.warnings.map((warning) => (
            <span key={warning}>{warning}</span>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function SignalDetail({ signal }: { signal: Signal | null }) {
  if (!signal) {
    return (
      <section className="detail-section">
        <div className="empty-state">暂无信号详情</div>
      </section>
    );
  }
  return (
    <section className="detail-section">
      <div className="signal-detail-head">
        <div>
          <span className={`direction-dot ${signal.direction}`} />
          <strong>
            {signal.symbol} {signal.name}
          </strong>
          <em>{horizonText(signal.primary_horizon)}</em>
        </div>
        <b className={signal.final_score >= 0 ? "score positive" : "score negative"}>
          {formatScore(signal.final_score)}
        </b>
      </div>
      <p className="brief">{signal.brief}</p>
      <div className="component-bars">
        {componentLabels.map(([key, label]) => (
          <div className="component-row" key={key}>
            <span>{label}</span>
            <div className="bar-track">
              <i
                style={{ width: `${Math.min(100, Math.abs(signal.score_breakdown[key]))}%` }}
                className={key === "uncertainty_penalty" ? "penalty" : ""}
              />
            </div>
            <b>{key === "uncertainty_penalty" ? "-" : ""}{Math.round(signal.score_breakdown[key])}</b>
          </div>
        ))}
      </div>
      <div className="horizon-grid">
        <span>短 {formatScore(signal.horizon_scores.short ?? 0)}</span>
        <span>中 {formatScore(signal.horizon_scores.mid ?? 0)}</span>
        <span>长 {formatScore(signal.horizon_scores.long ?? 0)}</span>
      </div>
      <div className="evidence-list">
        {signal.evidence.map((item) => (
          <a href={item.url} target="_blank" rel="noreferrer" key={item.id}>
            <span>{item.title}</span>
            <small>
              {item.source} · {formatDate(item.published_at)}
            </small>
            <ExternalLink size={14} />
          </a>
        ))}
      </div>
      {signal.warnings.length ? (
        <div className="warnings">
          {signal.warnings.map((warning) => (
            <span key={warning}>{warning}</span>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function formatScore(value: number) {
  if (value > 0) return `+${value.toFixed(2)}`;
  return value.toFixed(2);
}

function horizonText(value: Horizon) {
  return { short: "短期", mid: "中期", long: "长期" }[value];
}

function formatDate(value?: string) {
  if (!value) return "-";
  return new Date(value).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

const root = createRoot(document.getElementById("root") as HTMLElement);
root.render(<App />);
