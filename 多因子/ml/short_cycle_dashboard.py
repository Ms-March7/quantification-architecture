#!/usr/bin/env python3
"""Localhost-only browser dashboard for the short-cycle defensive monitor."""

from __future__ import annotations

import argparse
import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Optional

from cn_market_data import json_safe
from short_cycle_live_monitor import one_cycle


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>多因子短周期防守台</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#151d31;--line:#2a3654;--text:#e8edf8;--muted:#9eabc5;--ok:#62d89f;--warn:#ffd166;--bad:#ff6b6b;--blue:#79a7ff}*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#090e1b,#111a2c);color:var(--text);font:14px/1.45 system-ui,"Microsoft YaHei",sans-serif}.wrap{max-width:1500px;margin:auto;padding:22px}.top{display:flex;gap:16px;align-items:flex-start;justify-content:space-between}.title{font-size:25px;font-weight:750}.sub{color:var(--muted);margin-top:5px}.badge{padding:9px 14px;border:1px solid var(--line);border-radius:10px;font-weight:700;background:var(--panel)}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:18px 0}.metric,.panel{background:rgba(21,29,49,.92);border:1px solid var(--line);border-radius:12px;padding:14px}.metric b{display:block;font-size:22px;margin-top:5px}.label{color:var(--muted);font-size:12px}.blocks{display:flex;gap:8px;flex-wrap:wrap}.block{background:#382838;border:1px solid #66405d;color:#ffd6e4;padding:5px 8px;border-radius:7px}table{width:100%;border-collapse:collapse;margin-top:12px}th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);font-weight:600;position:sticky;top:0;background:var(--panel)}.scroll{overflow:auto;max-height:65vh}.risk{font-weight:750}.action{font-weight:750}.good{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--muted)}.error{background:#421f2b;border-color:#7e3248}.foot{color:var(--muted);margin-top:12px;font-size:12px}@media(max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}.top{display:block}.badge{margin-top:12px;display:inline-block}}</style>
</head>
<body><div class="wrap">
<div class="top"><div><div class="title">多因子短周期防守台</div><div class="sub">完成K线 · 多因子风险 · 账户闸门 · 只读页面</div></div><div id="status" class="badge">等待数据</div></div>
<div id="error" class="panel error" style="display:none"></div>
<div class="grid"><div class="metric"><span class="label">生成时间</span><b id="generated">--</b></div><div class="metric"><span class="label">3根K线负向宽度</span><b id="breadth">--</b></div><div class="metric"><span class="label">持仓市值</span><b id="marketValue">--</b></div><div class="metric"><span class="label">可卖市值估算</span><b id="sellable">--</b></div></div>
<div class="panel"><div class="label">全局阻断</div><div id="blocks" class="blocks" style="margin-top:10px"></div></div>
<div class="panel" style="margin-top:12px"><div class="scroll"><table><thead><tr><th>代码</th><th>类别</th><th>末根完成K线</th><th>价格</th><th>风险</th><th>动作</th><th>数据</th><th>3根收益</th><th>相对6根</th><th>量能Z</th><th>原因</th></tr></thead><tbody id="rows"></tbody></table></div></div>
<div class="foot">本页不发送委托。公共行情仅供观察；QMT 模式最多进入人工复核，仓库不存在实盘提交路径。</div>
</div><script>
const n=(v,d=2)=>v===null||v===undefined||!Number.isFinite(Number(v))?'--':Number(v).toFixed(d);
const pct=v=>v===null||v===undefined?'--':n(Number(v)*100,2)+'%';
const clsAction=a=>a.includes('BLOCK')||a.includes('NO_NEW')||a.includes('T1_')?'bad':a.includes('REDUCE')||a.includes('DEFENSIVE')||a.includes('SEPARATE')?'warn':'good';
async function refresh(){try{const r=await fetch('/api/latest',{cache:'no-store'});const p=await r.json();if(p.error){throw new Error(p.error)}document.getElementById('error').style.display='none';const s=document.getElementById('status');s.textContent=p.workflowStatus;s.className='badge '+(p.workflowStatus==='ALLOW_MANUAL_ORDER_REVIEW'?'good':p.workflowStatus==='DATA_BLOCKED'?'bad':'warn');document.getElementById('generated').textContent=new Date(p.generatedAt).toLocaleTimeString();document.getElementById('breadth').textContent=pct(p.portfolio.negativeBreadth3Bars);document.getElementById('marketValue').textContent='¥'+n(p.portfolio.totalMarketValue,0);document.getElementById('sellable').textContent='¥'+n(p.portfolio.sellableMarketValueEstimate,0);document.getElementById('blocks').innerHTML=(p.globalBlocks.length?p.globalBlocks:['none']).map(x=>`<span class="block">${x}</span>`).join('');document.getElementById('rows').innerHTML=p.cards.map(c=>`<tr><td><b>${c.symbol}</b></td><td>${c.instrumentClass}</td><td>${c.lastBarTime||'--'}</td><td>${n(c.lastPrice,3)}</td><td class="risk ${c.riskScore>=70?'bad':c.riskScore>=40?'warn':'good'}">${c.riskScore}</td><td class="action ${clsAction(c.action)}">${c.action}</td><td>${c.dataQuality.status}</td><td>${pct(c.factors?.return_3)}</td><td>${pct(c.factors?.relative_return_6)}</td><td>${n(c.factors?.volume_z_20,2)}</td><td class="muted">${[...c.riskReasons,...c.actionReasons].join(', ')||'--'}</td></tr>`).join('')}catch(e){const box=document.getElementById('error');box.style.display='block';box.textContent='监控刷新失败：'+e.message}}
refresh();setInterval(refresh,5000);
</script></body></html>"""


class SharedState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.report: Optional[Mapping[str, Any]] = None
        self.error: str = ""

    def update(self, report: Optional[Mapping[str, Any]], error: str = "") -> None:
        with self.lock:
            self.report = report
            self.error = error

    def payload(self) -> Mapping[str, Any]:
        with self.lock:
            if self.report is None:
                return {"error": self.error or "monitor is warming up"}
            value = dict(self.report)
            if self.error:
                value["lastRefreshError"] = self.error
            return value


def make_handler(state: SharedState):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            if self.path.split("?", 1)[0] == "/api/latest":
                body = json.dumps(json_safe(state.payload()), ensure_ascii=False, allow_nan=False).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            elif self.path.split("?", 1)[0] == "/":
                body = HTML.encode("utf-8")
                content_type = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: Any) -> None:
            return

    return Handler


def monitor_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        broker=args.broker,
        symbols=args.symbols,
        period=args.period,
        limit=args.limit,
        workers=args.workers,
        qmt_userdata="",
        account_id="",
        account_type="STOCK",
        output=args.output,
        cache_dir=args.cache_dir,
        position_file=args.position_file,
        watch_seconds=0.0,
        max_cycles=1,
    )


def worker_loop(state: SharedState, args: argparse.Namespace, stop: threading.Event) -> None:
    options = monitor_args(args)
    while not stop.is_set():
        try:
            state.update(one_cycle(options))
        except Exception as exc:
            state.update(state.report, type(exc).__name__ + ": " + str(exc))
        stop.wait(max(10.0, args.refresh_seconds))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", choices=("public", "qmt"), default="public")
    parser.add_argument("--symbols", default="159995,159516,603986,588170,159338,159819,588330,512050,600487,508033")
    parser.add_argument("--period", choices=("1m", "5m", "15m"), default="5m")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--refresh-seconds", type=float, default=30.0)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / ".tmp" / "short-cycle" / "latest-monitor.json")
    parser.add_argument("--cache-dir", type=Path, default=Path(__file__).resolve().parents[1] / ".tmp" / "short-cycle" / "market")
    parser.add_argument("--position-file", type=Path, default=Path(__file__).resolve().parents[1] / ".tmp" / "short-cycle" / "manual-account.json")
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.once:
        print(json.dumps(json_safe(one_cycle(monitor_args(args))), ensure_ascii=False, allow_nan=False))
        return
    state = SharedState()
    stop = threading.Event()
    worker = threading.Thread(target=worker_loop, args=(state, args, stop), daemon=True)
    worker.start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    url = f"http://{args.host}:{args.port}/"
    print(json.dumps({"dashboard": url, "broker": args.broker}, ensure_ascii=False))
    if args.open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
