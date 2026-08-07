"""Atomic, append-friendly research artifact registry."""

from __future__ import annotations

import json
import hashlib
import os
import platform
import socket
import sys
import tempfile
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np


def _json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent, suffix=".tmp") as handle:
        json.dump(_json_safe(payload), handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def atomic_write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent, suffix=".tmp") as handle:
        for row in rows:
            handle.write(json.dumps(_json_safe(row), ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class ResearchRunLock:
    """Exclusive run-directory lease; a process crash leaves a fail-closed lock."""

    def __init__(self, output_directory: str | Path) -> None:
        self.output_directory = Path(output_directory)
        self.path = self.output_directory / ".run.lock"
        self._held = False

    def __enter__(self) -> "ResearchRunLock":
        self.output_directory.mkdir(parents=True, exist_ok=True)
        if (self.output_directory / "COMMITTED.json").exists():
            ResearchRegistry(self.output_directory).verify_completed()
            raise FileExistsError(
                f"completed research run is immutable: {self.output_directory}"
            )
        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        try:
            with self.path.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise RuntimeError(
                f"research run is locked; inspect before removing stale lock: {self.path}"
            ) from exc
        self._held = True
        if (self.output_directory / "COMMITTED.json").exists():
            self.__exit__(None, None, None)
            ResearchRegistry(self.output_directory).verify_completed()
            raise FileExistsError(
                f"completed research run is immutable: {self.output_directory}"
            )
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False

    @staticmethod
    def _pid_is_running(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes

            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information, False, pid
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return ctypes.windll.kernel32.GetLastError() == 5
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @classmethod
    def recover_stale(
        cls, output_directory: str | Path, *, confirm_lock_sha256: str
    ) -> dict[str, Any]:
        """Archive one dead local-process lock after exact hash confirmation."""

        output = Path(output_directory)
        path = output / ".run.lock"
        if not path.is_file():
            raise FileNotFoundError(f"run lock does not exist: {path}")
        actual_hash = file_sha256(path)
        if actual_hash != confirm_lock_sha256:
            raise RuntimeError("explicit stale-lock hash confirmation does not match")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            pid = int(payload["pid"])
            host = str(payload["host"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("run lock payload is invalid") from exc
        if host != socket.gethostname():
            raise RuntimeError("cannot recover a lock created on another host")
        if cls._pid_is_running(pid):
            raise RuntimeError(f"lock owner process is still running: pid={pid}")
        if (output / "COMMITTED.json").exists():
            ResearchRegistry(output).verify_completed()
            raise RuntimeError("completed run needs no lock recovery")
        archive = output / ".recovered-locks" / f"{actual_hash}.json"
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.exists():
            raise RuntimeError("this stale lock was already archived")
        os.replace(path, archive)
        return {
            "status": "STALE_LOCK_ARCHIVED",
            "output": str(output.resolve()),
            "lock_sha256": actual_hash,
            "owner_pid": pid,
            "owner_host": host,
            "archive": str(archive.resolve()),
        }


class ResearchRegistry:
    def __init__(self, output_directory: str | Path) -> None:
        self.output_directory = Path(output_directory)
        self.output_directory.mkdir(parents=True, exist_ok=True)

    def write_run(
        self,
        *,
        manifest: dict[str, Any],
        candidates: Iterable[dict[str, Any]],
        selected: Iterable[dict[str, Any]],
        backtests: dict[str, Any],
    ) -> None:
        candidate_rows = list(candidates)
        selected_rows = list(selected)
        artifact_paths = {
            "candidates.jsonl": self.output_directory / "candidates.jsonl",
            "selected.json": self.output_directory / "selected.json",
            "backtests.json": self.output_directory / "backtests.json",
        }
        # Every payload is committed before manifest.json.  The manifest is the
        # only completed-run marker, never a first-write placeholder.
        atomic_write_jsonl(artifact_paths["candidates.jsonl"], candidate_rows)
        atomic_write_json(artifact_paths["selected.json"], selected_rows)
        atomic_write_json(artifact_paths["backtests.json"], backtests)
        ensemble = manifest.get("ensemble", {}) if isinstance(manifest.get("ensemble"), dict) else {}
        selection_protocol = (
            manifest.get("selection_protocol", {})
            if isinstance(manifest.get("selection_protocol"), dict)
            else {}
        )
        optional_names: list[str] = []
        if selection_protocol.get("discovery_freeze_hash"):
            optional_names.append("discovery-freeze.json")
        if ensemble.get("freeze_hash"):
            optional_names.append("freeze.json")
            optional_names.append("engine-source.json")
        if ensemble.get("holdout_was_opened"):
            optional_names.append("holdout-result.json")
        for optional in optional_names:
            path = self.output_directory / optional
            if not path.is_file():
                raise RuntimeError(f"expected final research artifact is missing: {path}")
            artifact_paths[optional] = path
        artifacts = {
            name: {"sha256": file_sha256(path), "bytes": path.stat().st_size}
            for name, path in sorted(artifact_paths.items())
        }
        enriched = {
            **manifest,
            "registry_schema": 2,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "runtime": {
                "python": sys.version,
                "platform": platform.platform(),
                "numpy": np.__version__,
            },
            "artifacts": artifacts,
        }
        atomic_write_json(self.output_directory / "manifest.json", enriched)
        atomic_write_json(
            self.output_directory / "COMMITTED.json",
            {
                "registry_schema": 2,
                "manifest_sha256": file_sha256(self.output_directory / "manifest.json"),
                "committed_at_utc": datetime.now(timezone.utc).isoformat(),
            },
        )
        self.verify_completed()

    def verify_completed(self) -> dict[str, Any]:
        manifest_path = self.output_directory / "manifest.json"
        commit_path = self.output_directory / "COMMITTED.json"
        if not commit_path.is_file():
            raise RuntimeError(f"research run has no commit marker: {commit_path}")
        if not manifest_path.is_file():
            raise RuntimeError(f"committed research manifest is missing: {manifest_path}")
        commit = json.loads(commit_path.read_text(encoding="utf-8"))
        if commit.get("registry_schema") != 2:
            raise RuntimeError("research commit marker has an unsupported schema")
        if file_sha256(manifest_path) != commit.get("manifest_sha256"):
            raise RuntimeError("research manifest hash does not match its commit marker")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("registry_schema") != 2 or not isinstance(manifest.get("artifacts"), dict):
            raise RuntimeError("research manifest is not a registry-v2 commit")
        for name, evidence in manifest["artifacts"].items():
            path = self.output_directory / name
            if not path.is_file():
                raise RuntimeError(f"committed research artifact is missing: {path}")
            if path.stat().st_size != int(evidence["bytes"]):
                raise RuntimeError(f"committed research artifact size mismatch: {path}")
            if file_sha256(path) != evidence["sha256"]:
                raise RuntimeError(f"committed research artifact hash mismatch: {path}")
        return manifest
