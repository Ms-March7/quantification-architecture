from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from capital_flow_attribution.collector import RawCollector  # noqa: E402


class FakeSession:
    def __init__(self) -> None:
        self.calls = 0
        self.headers: dict[str, str] = {}

    def get(self, url: str, **_: object) -> requests.Response:
        self.calls += 1
        if self.calls < 3:
            raise requests.ConnectionError("temporary")
        response = requests.Response()
        response.status_code = 200
        response._content = b"ok"
        response.url = url
        response.headers["Content-Type"] = "text/plain"
        return response


class FakeServerErrorSession:
    def __init__(self) -> None:
        self.calls = 0
        self.headers: dict[str, str] = {}

    def get(self, url: str, **_: object) -> requests.Response:
        self.calls += 1
        response = requests.Response()
        response.status_code = 502 if self.calls == 1 else 200
        response._content = b"bad" if self.calls == 1 else b"ok"
        response.url = url
        return response


class RawCollectorTests(unittest.TestCase):
    def test_tenacity_retry_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            collector = RawCollector(
                Path(temporary) / "raw",
                timeout=1,
                user_agent="test",
                retry_attempts=4,
                retry_wait_seconds=0,
            )
            session = FakeSession()
            collector.session = session  # type: ignore[assignment]
            response = collector.get(
                source_id="test",
                url="https://example.com/data",
                relative_path="example/data.txt",
            )
            self.assertEqual(response.content, b"ok")
            self.assertEqual(session.calls, 3)
            self.assertEqual(collector.manifest[0]["retrieval_mode"], "online")
            self.assertTrue((Path(temporary) / "raw" / "example" / "data.txt").is_file())

    def test_offline_without_cache_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            collector = RawCollector(Path(temporary) / "raw", 1, "test", offline_raw_only=True)
            with self.assertRaises(requests.ConnectionError):
                collector.get(source_id="test", url="https://example.com/data", relative_path="missing.txt")

    def test_server_error_is_retried_before_raw_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            collector = RawCollector(
                Path(temporary) / "raw", 1, "test", retry_attempts=2, retry_wait_seconds=0
            )
            session = FakeServerErrorSession()
            collector.session = session  # type: ignore[assignment]
            response = collector.get(
                source_id="test", url="https://example.com/data", relative_path="server/data.txt"
            )
            self.assertEqual(response.content, b"ok")
            self.assertEqual(session.calls, 2)
            self.assertEqual(len(collector.manifest), 1)


if __name__ == "__main__":
    unittest.main()
