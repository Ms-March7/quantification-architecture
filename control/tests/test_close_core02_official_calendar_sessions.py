from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "close_core02_official_calendar_sessions.py"
)
SPEC = importlib.util.spec_from_file_location(
    "core02_official_closure", MODULE_PATH
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class Core02OfficialClosureTests(unittest.TestCase):
    def test_parse_annual_ranges_and_single_days(self) -> None:
        text = (
            "一、休市安排 （一）元旦：1月1日至1月3日休市，"
            "1月4日起照常开市。（二）清明节：4月5日休市。"
            "二、清算交收"
        )
        self.assertEqual(
            MODULE.parse_closure_dates(text, 2025),
            {
                date(2025, 1, 1),
                date(2025, 1, 2),
                date(2025, 1, 3),
                date(2025, 4, 5),
            },
        )

    def test_parse_cross_year_explicit_range(self) -> None:
        text = (
            "一、休市安排 2018年12月30日至2019年1月1日休市。"
            "二、清算交收"
        )
        self.assertEqual(
            MODULE.parse_closure_dates(text, 2019),
            {
                date(2018, 12, 30),
                date(2018, 12, 31),
                date(2019, 1, 1),
            },
        )

    def test_parse_standard_sessions(self) -> None:
        text = (
            "9:15至9:25为开盘集合竞价时间；"
            "9:30至11:30、13:00至14:57为连续竞价时间；"
            "14:57至15:00为收盘集合竞价时间。"
        )
        sessions = MODULE.parse_standard_equity_sessions(text)
        self.assertEqual(len(sessions), 4)
        self.assertEqual(sessions[0]["session_name"], "OPENING_AUCTION")
        self.assertEqual(sessions[-1]["end_time"], "15:00")

    def test_sse_publication_date_fallback(self) -> None:
        self.assertEqual(
            MODULE.extract_publication_date(
                "关于调整休市安排的公告 2019-04-18 上证公告"
            ),
            date(2019, 4, 18),
        )

    def test_incomplete_sessions_fail_closed(self) -> None:
        self.assertEqual(
            MODULE.parse_standard_equity_sessions(
                "9:30至11:30为连续竞价时间"
            ),
            [],
        )

    def test_overlap_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "overlapping"):
            MODULE.validate_non_overlapping_sessions(
                [
                    {
                        "start_time": "09:15",
                        "end_time": "09:31",
                    },
                    {
                        "start_time": "09:30",
                        "end_time": "11:30",
                    },
                ]
            )

    def test_gate_direct_failure_dominates_unknown(self) -> None:
        self.assertEqual(
            MODULE.aggregate_gate_status(["PASS", "UNKNOWN", "FAIL"]),
            "FAIL",
        )

    def test_gate_unknown_without_failure(self) -> None:
        self.assertEqual(
            MODULE.aggregate_gate_status(["PASS", "UNKNOWN"]),
            "UNKNOWN",
        )

    def test_output_must_be_strict_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "tmp"
            parent.mkdir()
            MODULE.require_child_path(parent / "child", parent, "test")
            with self.assertRaisesRegex(ValueError, "must be a child"):
                MODULE.require_child_path(parent, parent, "test")

    def test_sanitise_headers_drops_cookie_and_auth(self) -> None:
        headers = MODULE.requests.structures.CaseInsensitiveDict(
            {
                "Content-Type": "text/html",
                "Set-Cookie": "secret=value",
                "Authorization": "secret",
                "ETag": "abc",
            }
        )
        cleaned = MODULE.sanitise_headers(headers)
        self.assertEqual(cleaned["content-type"], "text/html")
        self.assertEqual(cleaned["etag"], "abc")
        self.assertNotIn("set-cookie", cleaned)
        self.assertNotIn("authorization", cleaned)


if __name__ == "__main__":
    unittest.main()
