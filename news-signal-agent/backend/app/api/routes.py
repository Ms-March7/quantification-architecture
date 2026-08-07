from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import threading
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.collectors.quotes import fetch_daily_bars, fetch_eastmoney_quotes, fetch_intraday_trends
from app.config import delete_watch_symbol, load_watchlist, upsert_watch_symbol
from app.models import AssetType, WatchSymbol
from app.scoring.calibration import calibrate_scoring_weights
from app.storage.sqlite import (
    create_user_override,
    list_active_retained_events,
    list_events,
    list_event_cards,
    list_rank_history,
    list_user_overrides,
)
from app.workflow.engine import WorkflowEngine
from app.workflow.news import run_news_workflow_snapshot

router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parents[3]
_RUN_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="news-workflow")
_RUN_JOBS: dict[str, dict] = {}
_RUN_JOBS_LOCK = threading.Lock()
_MAX_RUN_JOBS = 20


class WatchlistUpsertRequest(BaseModel):
    symbol: str
    name: str | None = None
    asset_type: AssetType | None = None
    market: str | None = None
    tags: list[str] = Field(default_factory=list)


class FeedbackOverrideRequest(BaseModel):
    target_type: str = "signal"
    target_id: str
    field: str
    old_value: object | None = None
    new_value: object
    reason: str = "manual correction"
    apply_scope: str = "this_event_only"


class RerunRequest(BaseModel):
    from_node: str = "feedback_override"
    override: FeedbackOverrideRequest | None = None


class RunNewsRequest(BaseModel):
    mode: str = "fast"


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/workflow/preview")
def workflow_preview() -> dict:
    engine = WorkflowEngine.default()
    return engine.preview()


@router.get("/watchlist")
def watchlist() -> dict:
    return {"items": [item.model_dump(mode="json") for item in load_watchlist(PROJECT_ROOT)]}


@router.post("/watchlist")
def upsert_watchlist_item(request: WatchlistUpsertRequest) -> dict:
    symbol = _normalize_symbol(request.symbol)
    market = (request.market or _infer_market(symbol)).upper()
    asset_type = request.asset_type or _infer_asset_type(symbol)
    candidate = WatchSymbol(
        symbol=symbol,
        name=request.name,
        asset_type=asset_type,
        market=market,
        tags=_clean_tags(request.tags),
    )
    if not candidate.name:
        candidate = candidate.model_copy(update={"name": _quote_name(candidate)})
    items = upsert_watch_symbol(PROJECT_ROOT, candidate)
    return {
        "item": candidate.model_dump(mode="json"),
        "items": [item.model_dump(mode="json") for item in items],
    }


@router.delete("/watchlist/{symbol}")
def delete_watchlist_item(symbol: str) -> dict:
    normalized = _normalize_symbol(symbol)
    deleted, items = delete_watch_symbol(PROJECT_ROOT, normalized)
    if not deleted:
        raise HTTPException(status_code=404, detail="watch symbol not found")
    return {"deleted": normalized, "items": [item.model_dump(mode="json") for item in items]}


@router.post("/workflow/run-news")
def workflow_run_news(request: RunNewsRequest | None = None) -> dict:
    mode = request.mode if request else "fast"
    return run_news_workflow_snapshot(PROJECT_ROOT, allow_mock_fallback=True, run_mode=mode)


@router.post("/workflow/run-news/async")
def workflow_run_news_async(request: RunNewsRequest | None = None) -> dict:
    mode = _normalize_run_mode(request.mode if request else "fast")
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "mode": mode,
        "status": "queued",
        "stage_status": "queued",
        "progress": 0,
        "current_node": None,
        "completed_nodes": 0,
        "total_nodes": 0,
        "detail": None,
        "item_index": None,
        "item_total": None,
        "elapsed_seconds": 0,
        "eta_seconds": None,
        "max_wait_seconds": None,
        "llm_calls": 0,
        "llm_cache_hits": 0,
        "llm_budget_skips": 0,
        "nodes": [],
        "result": None,
        "error": None,
        "created_at": _now_iso(),
        "started_at": None,
        "finished_at": None,
        "updated_at": _now_iso(),
    }
    with _RUN_JOBS_LOCK:
        _RUN_JOBS[job_id] = job
        _prune_run_jobs_locked()
    _RUN_EXECUTOR.submit(_run_news_job, job_id, mode)
    return _public_job(job)


@router.get("/workflow/run-news/jobs/{job_id}")
def workflow_run_news_job(job_id: str) -> dict:
    with _RUN_JOBS_LOCK:
        job = _RUN_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="workflow job not found")
        return _public_job(job)


@router.post("/workflow/rerun-from")
def workflow_rerun_from(request: RerunRequest) -> dict:
    created_override = None
    if request.override:
        created_override = create_user_override(
            project_root=PROJECT_ROOT,
            target_type=request.override.target_type,
            target_id=request.override.target_id,
            field=request.override.field,
            old_value=request.override.old_value,
            new_value=request.override.new_value,
            reason=request.override.reason,
            apply_scope=request.override.apply_scope,
        )
    snapshot = run_news_workflow_snapshot(
        PROJECT_ROOT,
        allow_mock_fallback=True,
        start_from=request.from_node,
    )
    snapshot["rerun"] = {
        "requested_from_node": request.from_node,
        "mode": "artifact_partial_rerun_with_fallback",
        "override": created_override,
    }
    return snapshot


@router.get("/retained-events")
def retained_events() -> dict:
    return {"items": list_active_retained_events(PROJECT_ROOT)}


@router.get("/event-cards")
def event_cards(limit: int = 100) -> dict:
    return {"items": list_event_cards(PROJECT_ROOT, limit=limit)}


@router.get("/events")
def events(limit: int = 100) -> dict:
    return {"items": list_events(PROJECT_ROOT, limit=limit)}


@router.get("/rank-history")
def rank_history(limit: int = 100) -> dict:
    return {"items": list_rank_history(PROJECT_ROOT, limit=limit)}


@router.get("/market/quote/{symbol}")
def market_quote(symbol: str) -> dict:
    item = _watch_symbol_from_code(symbol)
    return {"item": fetch_eastmoney_quotes([item]).get(item.symbol)}


@router.get("/market/intraday/{symbol}")
def market_intraday(symbol: str) -> dict:
    item = _watch_symbol_from_code(symbol)
    return {"item": fetch_intraday_trends(item)}


@router.get("/market/daily/{symbol}")
def market_daily(symbol: str, days: int = 120) -> dict:
    item = _watch_symbol_from_code(symbol)
    return {"items": fetch_daily_bars(item, days=max(1, min(days, 300)))}


@router.get("/scoring/calibration")
def scoring_calibration(min_samples: int = 20) -> dict:
    return calibrate_scoring_weights(PROJECT_ROOT, min_samples=min_samples)


@router.get("/feedback/overrides")
def feedback_overrides() -> dict:
    return {"items": list_user_overrides(PROJECT_ROOT)}


@router.post("/feedback/override")
def feedback_override(request: FeedbackOverrideRequest) -> dict:
    override = create_user_override(
        project_root=PROJECT_ROOT,
        target_type=request.target_type,
        target_id=request.target_id,
        field=request.field,
        old_value=request.old_value,
        new_value=request.new_value,
        reason=request.reason,
        apply_scope=request.apply_scope,
    )
    return {"item": override, "items": list_user_overrides(PROJECT_ROOT)}


def _run_news_job(job_id: str, mode: str) -> None:
    _update_run_job(job_id, status="running", stage_status="running", started_at=_now_iso())

    def on_progress(update: dict) -> None:
        stage_status = str(update.get("status") or "running")
        status = "failed" if stage_status == "failed" else "running"
        _update_run_job(
            job_id,
            status=status,
            stage_status=stage_status,
            run_id=update.get("run_id"),
            progress=update.get("progress"),
            current_node=update.get("current_node"),
            completed_nodes=update.get("completed_nodes"),
            total_nodes=update.get("total_nodes"),
            detail=update.get("detail"),
            item_index=update.get("item_index"),
            item_total=update.get("item_total"),
            elapsed_seconds=update.get("elapsed_seconds"),
            eta_seconds=update.get("eta_seconds"),
            max_wait_seconds=update.get("max_wait_seconds"),
            llm_calls=update.get("llm_calls"),
            llm_cache_hits=update.get("llm_cache_hits"),
            llm_budget_skips=update.get("llm_budget_skips"),
            nodes=update.get("nodes") or [],
        )

    try:
        snapshot = run_news_workflow_snapshot(
            PROJECT_ROOT,
            allow_mock_fallback=True,
            progress_callback=on_progress,
            run_mode=mode,
        )
        failed_node = next((node for node in snapshot.get("nodes", []) if node.get("status") == "failed"), None)
        if failed_node:
            error = "; ".join(failed_node.get("warnings") or []) or f"{failed_node.get('node_name')} failed"
            _update_run_job(
                job_id,
                status="failed",
                stage_status="failed",
                result=snapshot,
                error=error,
                finished_at=_now_iso(),
                current_node=failed_node.get("node_name"),
            )
            return
        _update_run_job(
            job_id,
            status="complete",
            stage_status="complete",
            progress=100,
            current_node=None,
            result=snapshot,
            finished_at=_now_iso(),
        )
    except Exception as exc:  # pragma: no cover - defensive API boundary.
        _update_run_job(
            job_id,
            status="failed",
            stage_status="failed",
            error=f"{type(exc).__name__}: {exc}",
            finished_at=_now_iso(),
        )


def _update_run_job(job_id: str, **updates: object) -> None:
    updates["updated_at"] = _now_iso()
    with _RUN_JOBS_LOCK:
        job = _RUN_JOBS.get(job_id)
        if not job:
            return
        job.update(updates)


def _public_job(job: dict) -> dict:
    return deepcopy(job)


def _prune_run_jobs_locked() -> None:
    if len(_RUN_JOBS) <= _MAX_RUN_JOBS:
        return
    old_ids = sorted(_RUN_JOBS, key=lambda key: str(_RUN_JOBS[key].get("created_at") or ""))[:-_MAX_RUN_JOBS]
    for job_id in old_ids:
        _RUN_JOBS.pop(job_id, None)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_run_mode(mode: str | None) -> str:
    value = str(mode or "fast").strip().lower()
    if value in {"deep", "full", "llm", "深度"}:
        return "deep"
    if value in {"balanced", "normal", "平衡"}:
        return "balanced"
    return "fast"


def _normalize_symbol(symbol: str) -> str:
    normalized = "".join(ch for ch in symbol.strip() if ch.isalnum()).upper()
    if not normalized.isdigit() or len(normalized) != 6:
        raise HTTPException(status_code=400, detail="symbol must be a 6-digit A-share code")
    return normalized


def _infer_market(symbol: str) -> str:
    if symbol.startswith(("5", "6", "9")):
        return "SH"
    if symbol.startswith(("0", "1", "2", "3")):
        return "SZ"
    raise HTTPException(status_code=400, detail="cannot infer market from symbol")


def _infer_asset_type(symbol: str) -> AssetType:
    if symbol.startswith(("15", "51", "56", "58")):
        return AssetType.etf
    if symbol.startswith(("16", "18")):
        return AssetType.fund
    if symbol.startswith(("0", "3", "6")):
        return AssetType.stock
    return AssetType.unknown


def _quote_name(symbol: WatchSymbol) -> str | None:
    try:
        quote = fetch_eastmoney_quotes([symbol]).get(symbol.symbol)
    except Exception:
        return None
    name = quote.get("name") if quote else None
    return str(name) if name else None


def _clean_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        clean = " ".join(tag.strip().split())
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result


def _watch_symbol_from_code(symbol: str) -> WatchSymbol:
    normalized = _normalize_symbol(symbol)
    for item in load_watchlist(PROJECT_ROOT):
        if item.symbol == normalized:
            return item
    return WatchSymbol(
        symbol=normalized,
        market=_infer_market(normalized),
        asset_type=_infer_asset_type(normalized),
    )
