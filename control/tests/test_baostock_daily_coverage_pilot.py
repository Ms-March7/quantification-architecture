from __future__ import annotations

import importlib.util
import copy
import json
import time
import types
import unittest
import zlib
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = (
    WORKSPACE_ROOT / "control" / "tools" / "run_baostock_daily_coverage_pilot.py"
)
PLAN_PATH = (
    WORKSPACE_ROOT
    / "data"
    / "catalog"
    / "contracts"
    / "source_admission_v1"
    / "execution_plans"
    / "baostock_daily_coverage_v1.json"
)
SPEC = importlib.util.spec_from_file_location("baostock_daily_coverage_pilot", RUNNER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load pilot runner: {RUNNER_PATH}")
PILOT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PILOT)
PLAN = json.loads(PLAN_PATH.read_text(encoding="utf-8"))


class FakeResult:
    def __init__(self, fields: list[str], data: list[list[str]]):
        self.fields = fields
        self.data = data
        self.error_code = "0"
        self.error_msg = "success"
        self.method = "fake"
        self.cur_page_num = "1"
        self.per_page_count = str(len(data))


class FakeSocket:
    def __init__(self, response: bytes):
        self.response = response

    def sendall(self, value: bytes) -> None:
        self.sent = value

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def recv(self, size: int) -> bytes:
        del size
        value, self.response = self.response, b""
        return value

    def close(self) -> None:
        return None


def daily_row(date: str, code: str) -> list[str]:
    return [
        date,
        code,
        "10",
        "11",
        "9",
        "10.5",
        "10",
        "100",
        "1050",
        "3",
        "1",
        "1",
        "5",
        "10",
        "1",
        "1",
        "1",
        "0",
    ]


class BaoStockDailyCoveragePilotTests(unittest.TestCase):
    def summarize(self, method: str, parameters: dict[str, str], rows: list[list[str]]):
        plan = copy.deepcopy(PLAN)
        plan["content_and_semantic_gates"][
            "daily_cross_section_minimum_rows_for_coverage_heuristic"
        ] = 1
        plan["content_and_semantic_gates"][
            "daily_cross_section_required_code_prefixes"
        ] = ["sh", "sz"]
        return PILOT.result_summary(
            FakeResult(plan["fixed_daily_fields"], rows),
            plan,
            {
                "probe_id": "test",
                "method": method,
                "parameters": parameters,
            },
        )

    def test_history_requires_requested_code_date_window_and_unique_key(self) -> None:
        parameters = {
            "code": "sh.600000",
            "start_date": "2026-07-20",
            "end_date": "2026-07-24",
            "adjustflag": "3",
        }
        valid = self.summarize(
            "query_history_k_data_plus",
            parameters,
            [daily_row("2026-07-20", "sh.600000")],
        )
        self.assertTrue(valid["semantic_gate_passed"])

        wrong_code = self.summarize(
            "query_history_k_data_plus",
            parameters,
            [daily_row("2026-07-20", "sz.000001")],
        )
        self.assertFalse(
            wrong_code["semantic_gates"][
                "requested_identity_and_date_window_passed"
            ]
        )

        wrong_date = self.summarize(
            "query_history_k_data_plus",
            parameters,
            [daily_row("2026-07-25", "sh.600000")],
        )
        self.assertFalse(
            wrong_date["semantic_gates"][
                "requested_identity_and_date_window_passed"
            ]
        )

        malformed_date = self.summarize(
            "query_history_k_data_plus",
            parameters,
            [daily_row("2026-07-20x", "sh.600000")],
        )
        self.assertFalse(malformed_date["semantic_gates"]["strict_iso_dates"])

        duplicate = self.summarize(
            "query_history_k_data_plus",
            parameters,
            [
                daily_row("2026-07-20", "sh.600000"),
                daily_row("2026-07-20", "sh.600000"),
            ],
        )
        self.assertFalse(
            duplicate["semantic_gates"]["unique_business_key_passed"]
        )

    def test_daily_cross_section_requires_exact_date_and_unique_code(self) -> None:
        parameters = {"date": "2026-07-24"}
        valid = self.summarize(
            "query_daily_history_k_AStock",
            parameters,
            [
                daily_row("2026-07-24", "sh.600000"),
                daily_row("2026-07-24", "sz.000001"),
            ],
        )
        self.assertTrue(valid["semantic_gate_passed"])

        duplicate_code = self.summarize(
            "query_daily_history_k_AStock",
            parameters,
            [
                daily_row("2026-07-24", "sh.600000"),
                daily_row("2026-07-24", "sh.600000"),
            ],
        )
        self.assertFalse(
            duplicate_code["semantic_gates"]["unique_business_key_passed"]
        )

        blank_values = daily_row("2026-07-24", "sh.600000")
        for field in ("open", "high", "low", "close", "volume", "amount"):
            blank_values[PLAN["fixed_daily_fields"].index(field)] = ""
        blank = self.summarize(
            "query_daily_history_k_AStock",
            parameters,
            [blank_values],
        )
        self.assertFalse(
            blank["semantic_gates"]["daily_required_values_present"]
        )

    def test_stock_basic_requires_requested_code(self) -> None:
        result = PILOT.result_summary(
            FakeResult(
                ["code", "code_name", "ipoDate", "outDate", "type", "status"],
                [["sz.000001", "平安银行", "1991-04-03", "", "1", "1"]],
            ),
            PLAN,
            {
                "probe_id": "test_basic",
                "method": "query_stock_basic",
                "parameters": {"code": "sh.600000", "code_name": ""},
            },
        )
        self.assertFalse(
            result["semantic_gates"]["requested_identity_and_date_window_passed"]
        )

    def test_state_list_requires_code_schema(self) -> None:
        result = PILOT.result_summary(
            FakeResult([], [[]]),
            PLAN,
            {
                "probe_id": "test_state",
                "method": "query_suspended_stocks",
                "parameters": {"date": "2026-07-24"},
            },
        )
        self.assertFalse(result["semantic_gate_passed"])

    def test_transport_accepts_exact_payload_crc_footer_and_rejects_bad_crc(
        self,
    ) -> None:
        body = b"0\x01success\x01login\x01anonymous"
        header = b"00.9.00\x0101\x01" + f"{len(body):010d}".encode("ascii")
        terminator = PLAN["transport"]["terminator_utf8"].encode("utf-8")
        good_frame = (
            header
            + body
            + b"\x01"
            + str(zlib.crc32(header + body)).encode("ascii")
            + terminator
        )
        cons = types.SimpleNamespace(COMPRESSED_MESSAGE_TYPE_TUPLE=())
        context = types.SimpleNamespace()
        transport = PILOT.BoundedBaoStockTransport(
            PLAN,
            cons,
            context,
            deadline_monotonic=time.monotonic() + 10,
        )
        transport._real_socket = FakeSocket(good_frame)
        request = "00.9.30\x0100\x010000000001x"
        decoded = transport.send_msg(request)
        self.assertEqual(decoded.encode("utf-8"), good_frame)
        self.assertTrue(transport.records[-1]["frame_payload_and_crc_valid"])

        bad_frame = good_frame.replace(
            str(zlib.crc32(header + body)).encode("ascii"),
            b"0",
            1,
        )
        transport = PILOT.BoundedBaoStockTransport(
            PLAN,
            cons,
            context,
            deadline_monotonic=time.monotonic() + 10,
        )
        transport._real_socket = FakeSocket(bad_frame)
        with self.assertRaises(PILOT.FatalTransportError):
            transport.send_msg(request)


if __name__ == "__main__":
    unittest.main()
