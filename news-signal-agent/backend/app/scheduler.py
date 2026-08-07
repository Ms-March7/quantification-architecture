from __future__ import annotations

from pathlib import Path

from app.config import load_yaml
from app.workflow.news import run_news_workflow_snapshot


def start_scheduler(project_root: Path):
    workflow_config = load_yaml(project_root / "config" / "workflow.yaml")
    scheduler_config = workflow_config.get("scheduler", {})
    if not scheduler_config.get("enabled", False):
        return None

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception:
        return None

    interval_minutes = int(scheduler_config.get("interval_minutes", 15))
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        lambda: run_news_workflow_snapshot(project_root, allow_mock_fallback=True),
        trigger="interval",
        minutes=interval_minutes,
        id="news_signal_workflow",
        max_instances=1,
        replace_existing=True,
    )
    if scheduler_config.get("run_on_startup", False):
        scheduler.add_job(
            lambda: run_news_workflow_snapshot(project_root, allow_mock_fallback=True),
            trigger="date",
            id="news_signal_startup_run",
            replace_existing=True,
        )
    scheduler.start()
    return scheduler
