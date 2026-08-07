from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from app.models import WatchSymbol


def load_local_env(project_root: Path) -> None:
    for path in (project_root / ".env", project_root / "backend" / ".env"):
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping in {path}")
    return data


def load_watchlist(project_root: Path) -> list[WatchSymbol]:
    config = load_yaml(project_root / "config" / "watchlist.yaml")
    return [WatchSymbol(**item) for item in config.get("watchlist", [])]


def save_watchlist(project_root: Path, watchlist: list[WatchSymbol]) -> None:
    path = project_root / "config" / "watchlist.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"watchlist": [item.model_dump(mode="json") for item in watchlist]}
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, allow_unicode=True, sort_keys=False)


def upsert_watch_symbol(project_root: Path, symbol: WatchSymbol) -> list[WatchSymbol]:
    watchlist = load_watchlist(project_root)
    normalized = symbol.model_copy(update={"symbol": symbol.symbol.strip()})
    updated = False
    result: list[WatchSymbol] = []
    for item in watchlist:
        if item.symbol == normalized.symbol:
            result.append(normalized)
            updated = True
        else:
            result.append(item)
    if not updated:
        result.append(normalized)
    save_watchlist(project_root, result)
    return result


def delete_watch_symbol(project_root: Path, symbol: str) -> tuple[bool, list[WatchSymbol]]:
    normalized = symbol.strip()
    watchlist = load_watchlist(project_root)
    result = [item for item in watchlist if item.symbol != normalized]
    deleted = len(result) != len(watchlist)
    if deleted:
        save_watchlist(project_root, result)
    return deleted, result


def load_scoring_weights(project_root: Path) -> dict[str, float]:
    config = load_yaml(project_root / "config" / "scoring.yaml")
    return {key: float(value) for key, value in config.get("weights", {}).items()}


def load_scoring_config(project_root: Path) -> dict[str, Any]:
    return load_yaml(project_root / "config" / "scoring.yaml")


def load_sources_config(project_root: Path) -> dict[str, Any]:
    return load_yaml(project_root / "config" / "sources.yaml")


def load_notify_config(project_root: Path) -> dict[str, Any]:
    return load_yaml(project_root / "config" / "notify.yaml")
