from __future__ import annotations

import datetime as dt
import hashlib
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from .utils import now_iso


class RawCollector:
    """HTTP collector with immutable raw evidence, fallback cache and bounded retry."""

    def __init__(
        self,
        raw_root: Path,
        timeout: int,
        user_agent: str,
        fallback_roots: Iterable[str | Path] = (),
        offline_raw_only: bool = False,
        retry_attempts: int = 4,
        retry_wait_seconds: float = 1.0,
    ) -> None:
        self.raw_root = raw_root
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.manifest: list[dict[str, Any]] = []
        self.fallback_roots = tuple(Path(root) for root in fallback_roots)
        self.unavailable_hosts: set[str] = set()
        self.offline_raw_only = offline_raw_only
        self.retry_attempts = retry_attempts
        self.retry_wait_seconds = retry_wait_seconds

    def _cached_response(
        self,
        *,
        source_id: str,
        url: str,
        relative_path: str,
        params: dict[str, Any] | None,
        cached: Path,
    ) -> requests.Response:
        content = cached.read_bytes()
        prepared_url = requests.Request("GET", url, params=params).prepare().url or url
        path = self.raw_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.manifest.append(
            {
                "source_id": source_id,
                "url": prepared_url,
                "fetched_at": dt.datetime.fromtimestamp(cached.stat().st_mtime, tz=dt.timezone.utc).isoformat(),
                "http_status": None,
                "content_type": "application/octet-stream; copied from same-day failed run",
                "relative_path": path.relative_to(self.raw_root.parent).as_posix(),
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "retrieval_mode": "same_day_failed_run_cache",
                "cache_source": cached.as_posix(),
                "original_http_status_unknown": True,
            }
        )
        response = requests.Response()
        response.status_code = 200
        response._content = content
        response.url = prepared_url
        return response

    def get(
        self,
        *,
        source_id: str,
        url: str,
        relative_path: str,
        params: dict[str, Any] | None = None,
        referer: str | None = None,
        allow_http_error: bool = False,
        direct_no_proxy_fallback: bool = False,
    ) -> requests.Response:
        headers = {"Referer": referer} if referer else None
        fetched_at = now_iso()
        cached = next((root / relative_path for root in self.fallback_roots if (root / relative_path).is_file()), None)
        if cached is not None:
            return self._cached_response(
                source_id=source_id,
                url=url,
                relative_path=relative_path,
                params=params,
                cached=cached,
            )
        host = urlparse(url).netloc
        if self.offline_raw_only:
            raise requests.ConnectionError(f"offline raw-only rebuild has no cached file: {relative_path}")
        if host in self.unavailable_hosts:
            raise requests.ConnectionError(f"host circuit open after earlier failure: {host}")

        retrying = Retrying(
            stop=stop_after_attempt(self.retry_attempts),
            wait=wait_exponential(
                multiplier=self.retry_wait_seconds,
                min=self.retry_wait_seconds,
                max=max(self.retry_wait_seconds, self.retry_wait_seconds * 4),
            ),
            retry=retry_if_exception_type(requests.RequestException),
            reraise=True,
        )

        def request_once(session: requests.Session) -> requests.Response:
            current = session.get(
                url,
                params=params,
                headers=headers,
                timeout=self.timeout,
            )
            if not allow_http_error:
                current.raise_for_status()
            return current

        retrieval_mode = "online"
        try:
            response = retrying(request_once, self.session)
        except requests.RequestException:
            if not direct_no_proxy_fallback:
                self.unavailable_hosts.add(host)
                raise
            direct = requests.Session()
            direct.trust_env = False
            direct.headers.update({"User-Agent": self.session.headers.get("User-Agent", "")})
            try:
                response = retrying(request_once, direct)
                retrieval_mode = "online_direct_no_env_proxy"
            except requests.RequestException:
                self.unavailable_hosts.add(host)
                raise

        content = response.content
        path = self.raw_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.manifest.append(
            {
                "source_id": source_id,
                "url": response.url,
                "fetched_at": fetched_at,
                "http_status": response.status_code,
                "content_type": response.headers.get("Content-Type", ""),
                "relative_path": path.relative_to(self.raw_root.parent).as_posix(),
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "retrieval_mode": retrieval_mode,
                "retry_attempts_max": self.retry_attempts,
            }
        )
        if "www.szse.cn" in url:
            time.sleep(0.4)
        return response

    def record_bytes(
        self,
        *,
        source_id: str,
        url: str,
        relative_path: str,
        content: bytes,
        content_type: str = "application/octet-stream",
        retrieval_mode: str = "adapter_bundle",
    ) -> Path:
        """Persist one adapter-produced raw bundle with the normal evidence manifest."""

        path = self.raw_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"raw evidence is immutable: {path}")
        path.write_bytes(content)
        self.manifest.append(
            {
                "source_id": source_id,
                "url": url,
                "fetched_at": now_iso(),
                "http_status": None,
                "content_type": content_type,
                "relative_path": path.relative_to(self.raw_root.parent).as_posix(),
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "retrieval_mode": retrieval_mode,
            }
        )
        return path
