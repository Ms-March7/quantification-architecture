#!/usr/bin/env python3
"""Run the hash-bound BaoStock daily-data coverage pilot.

This is a one-off, inspection-only probe.  It never writes formal data,
registers a dataset, performs a bulk backfill, or promotes quality.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import shutil
import socket
import sys
import time
import zlib
import zipfile
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable


EXPECTED_PLAN_SHA256 = (
    "51854629ac283dcad09d6b4653bf6b6db322ccc206076ff58c0f93a6319ae56b"
)


class FatalTransportError(RuntimeError):
    """A socket/framing/cap failure after which the session is not reusable."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolved_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def write_json(path: Path, value: Any) -> None:
    partial = path.with_suffix(path.suffix + ".part")
    if path.exists() or partial.exists():
        raise ValueError(f"refusing to overwrite evidence: {path}")
    partial.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    partial.replace(path)


def tree_fingerprint(root: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    if root.exists():
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            records.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    return {
        "exists": root.exists(),
        "file_count": len(records),
        "total_bytes": sum(item["bytes"] for item in records),
        "contract_sha256": sha256_bytes(canonical_json(records)),
    }


def fingerprint(path: Path) -> dict[str, Any]:
    if path.is_file():
        return {
            "exists": True,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    return tree_fingerprint(path)


def extract_verified_wheel(
    wheel_path: Path,
    destination: Path,
    *,
    maximum_files: int,
    maximum_unpacked_bytes: int,
) -> dict[str, Any]:
    with zipfile.ZipFile(wheel_path) as archive:
        members = archive.infolist()
        if len(members) > maximum_files:
            raise ValueError("wheel file count exceeded reviewed cap")
        total_unpacked = sum(member.file_size for member in members)
        if total_unpacked > maximum_unpacked_bytes:
            raise ValueError("wheel unpacked bytes exceeded reviewed cap")
        for member in members:
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(f"unsafe wheel member path: {member.filename}")
            resolved = (destination / member_path).resolve()
            if not resolved_under(resolved, destination):
                raise ValueError(f"wheel member escapes destination: {member.filename}")
        archive.extractall(destination)
    return {
        "file_count": len(members),
        "unpacked_bytes": total_unpacked,
        "tree": tree_fingerprint(destination),
    }


def limited_zlib_decompress(payload: bytes, cap: int) -> bytes:
    decoder = zlib.decompressobj()
    output = decoder.decompress(payload, cap + 1)
    if len(output) > cap or decoder.unconsumed_tail:
        raise ValueError("decompressed response exceeded configured cap")
    remaining = cap + 1 - len(output)
    output += decoder.flush(remaining)
    if len(output) > cap:
        raise ValueError("decompressed response exceeded configured cap")
    if not decoder.eof:
        raise ValueError("compressed response did not reach a complete stream")
    return output


def parse_decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if not math.isfinite(float(number)):
        return None
    return number


def summarize_rows(
    fields: list[str],
    rows: list[list[Any]],
    *,
    fixed_daily_fields: list[str],
    sample_limit: int,
) -> dict[str, Any]:
    row_widths = Counter(len(row) for row in rows)
    field_indexes = {name: index for index, name in enumerate(fields)}
    code_prefixes: Counter[str] = Counter()
    date_values: list[str] = []
    null_counts = Counter()
    daily_numeric = {
        "numeric_parse_failures": 0,
        "negative_prices": 0,
        "negative_volume": 0,
        "negative_amount": 0,
        "ohlc_violations": 0,
        "vwap_within_low_high": 0,
        "vwap_outside_low_high": 0,
        "vwap_unavailable": 0,
    }
    status_values: Counter[str] = Counter()
    adjustflag_values: Counter[str] = Counter()

    for row in rows:
        for index, field in enumerate(fields):
            if index >= len(row) or row[index] is None or str(row[index]).strip() == "":
                null_counts[field] += 1
        if "code" in field_indexes and field_indexes["code"] < len(row):
            code = str(row[field_indexes["code"]])
            code_prefixes[code.split(".", 1)[0].lower()] += 1
        if "date" in field_indexes and field_indexes["date"] < len(row):
            date_values.append(str(row[field_indexes["date"]]))
        if "tradestatus" in field_indexes and field_indexes["tradestatus"] < len(row):
            status_values[str(row[field_indexes["tradestatus"]])] += 1
        if "adjustflag" in field_indexes and field_indexes["adjustflag"] < len(row):
            adjustflag_values[str(row[field_indexes["adjustflag"]])] += 1

        if all(name in field_indexes for name in ("open", "high", "low", "close")):
            values = {
                name: parse_decimal(row[field_indexes[name]])
                for name in ("open", "high", "low", "close")
                if field_indexes[name] < len(row)
            }
            observed_prices = [value for value in values.values() if value is not None]
            daily_numeric["negative_prices"] += sum(
                value < 0 for value in observed_prices
            )
            if len(observed_prices) != 4 and any(
                str(row[field_indexes[name]]).strip()
                for name in ("open", "high", "low", "close")
                if field_indexes[name] < len(row)
            ):
                daily_numeric["numeric_parse_failures"] += 1
            if len(observed_prices) == 4:
                open_value = values["open"]
                high_value = values["high"]
                low_value = values["low"]
                close_value = values["close"]
                assert open_value is not None
                assert high_value is not None
                assert low_value is not None
                assert close_value is not None
                if high_value < max(open_value, close_value, low_value) or low_value > min(
                    open_value, close_value, high_value
                ):
                    daily_numeric["ohlc_violations"] += 1

        if "volume" in field_indexes and field_indexes["volume"] < len(row):
            volume = parse_decimal(row[field_indexes["volume"]])
            if volume is None and str(row[field_indexes["volume"]]).strip():
                daily_numeric["numeric_parse_failures"] += 1
            elif volume is not None and volume < 0:
                daily_numeric["negative_volume"] += 1
        else:
            volume = None
        if "amount" in field_indexes and field_indexes["amount"] < len(row):
            amount = parse_decimal(row[field_indexes["amount"]])
            if amount is None and str(row[field_indexes["amount"]]).strip():
                daily_numeric["numeric_parse_failures"] += 1
            elif amount is not None and amount < 0:
                daily_numeric["negative_amount"] += 1
        else:
            amount = None
        low_value = (
            parse_decimal(row[field_indexes["low"]])
            if "low" in field_indexes and field_indexes["low"] < len(row)
            else None
        )
        high_value = (
            parse_decimal(row[field_indexes["high"]])
            if "high" in field_indexes and field_indexes["high"] < len(row)
            else None
        )
        if (
            amount is not None
            and volume is not None
            and volume > 0
            and low_value is not None
            and high_value is not None
            and high_value > 0
        ):
            empirical_vwap = amount / volume
            if low_value <= empirical_vwap <= high_value:
                daily_numeric["vwap_within_low_high"] += 1
            else:
                daily_numeric["vwap_outside_low_high"] += 1
        else:
            daily_numeric["vwap_unavailable"] += 1

    samples: list[list[Any]] = []
    if rows and sample_limit:
        sample_indexes = sorted({0, len(rows) // 2, len(rows) - 1})[:sample_limit]
        samples = [rows[index] for index in sample_indexes]
    normalized_hash = sha256_bytes(canonical_json({"fields": fields, "rows": rows}))
    return {
        "fields": fields,
        "field_count": len(fields),
        "fields_exactly_match_fixed_daily_fields": fields == fixed_daily_fields,
        "row_count": len(rows),
        "row_width_counts": dict(sorted(row_widths.items())),
        "all_row_widths_match_fields": set(row_widths).issubset({len(fields)}),
        "normalized_rows_sha256": normalized_hash,
        "date_min": min(date_values) if date_values else None,
        "date_max": max(date_values) if date_values else None,
        "code_prefix_counts": dict(sorted(code_prefixes.items())),
        "null_counts": dict(sorted(null_counts.items())),
        "trading_status_counts": dict(sorted(status_values.items())),
        "adjustflag_counts": dict(sorted(adjustflag_values.items())),
        "numeric_checks": daily_numeric,
        "empirical_unit_observation": (
            "amount_divided_by_volume is compared with daily low/high; this is "
            "diagnostic evidence only and does not replace a first-party unit dictionary"
        ),
        "sample_rows": samples,
    }


class BoundedBaoStockTransport:
    def __init__(
        self,
        plan: dict[str, Any],
        cons: Any,
        context: Any,
        *,
        deadline_monotonic: float,
    ):
        self.plan = plan
        self.cons = cons
        self.context = context
        self.records: list[dict[str, Any]] = []
        self.socket_messages = 0
        self.total_on_wire = 0
        self.total_decompressed = 0
        self.last_business_at: float | None = None
        self.current_probe_id = "UNSET"
        self.session_id = "UNSET"
        self.deadline_monotonic = deadline_monotonic
        self._real_socket: socket.socket | None = None
        self.expected_responses = {
            "00": "01",
            "02": "03",
            "45": "46",
            "67": "68",
            "69": "70",
            "71": "72",
            "73": "74",
            "93": "94",
            "95": "96",
            "98": "99",
        }

    @property
    def cfg(self) -> dict[str, Any]:
        return self.plan["transport"]

    def remaining_seconds(self) -> float:
        remaining = self.deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise FatalTransportError("run wall-clock cap exceeded")
        return remaining

    def connect(self, _instance: Any) -> None:
        if self._real_socket is not None:
            raise FatalTransportError("session already has a socket")
        timeout = min(
            float(self.cfg["connect_timeout_seconds"]),
            self.remaining_seconds(),
        )
        try:
            sock = socket.create_connection(
                (self.cfg["host"], int(self.cfg["port"])),
                timeout=timeout,
            )
        except OSError as exc:
            raise FatalTransportError(f"provider connect failed: {exc}") from exc
        sock.settimeout(
            min(float(self.cfg["read_timeout_seconds"]), self.remaining_seconds())
        )
        self._real_socket = sock
        setattr(self.context, "default_socket", sock)

    def send_msg(self, message: str) -> str:
        if self._real_socket is None:
            raise FatalTransportError("no active bounded socket")
        self.remaining_seconds()
        self.socket_messages += 1
        if self.socket_messages > int(self.cfg["maximum_socket_messages"]):
            raise FatalTransportError("socket-message budget exceeded")
        header = message[: int(self.cfg["header_bytes"])]
        header_parts = header.split("\1")
        if len(header_parts) != 3:
            raise FatalTransportError("invalid provider request header")
        request_version, request_type, _request_body_length = header_parts
        if request_version != self.plan["package"]["client_version"]:
            raise FatalTransportError(
                "request client version differs from execution plan"
            )
        expected_response_type = self.expected_responses.get(request_type)
        if expected_response_type is None:
            raise FatalTransportError(
                f"unplanned request message type: {request_type}"
            )
        is_business = request_type not in {"00", "02"}
        if is_business and self.last_business_at is not None:
            required_wait = float(
                self.cfg["minimum_business_request_interval_seconds"]
            ) - (time.monotonic() - self.last_business_at)
            if required_wait > 0:
                if required_wait >= self.remaining_seconds():
                    raise FatalTransportError("request interval exceeds run deadline")
                time.sleep(required_wait)
        if is_business:
            self.last_business_at = time.monotonic()

        sent_at = dt.datetime.now(dt.timezone.utc)
        try:
            self._real_socket.sendall((message + "\n").encode("utf-8"))
        except OSError as exc:
            raise FatalTransportError(f"provider send failed: {exc}") from exc
        response = bytearray()
        terminator = self.cfg["terminator_utf8"].encode("utf-8")
        per_response_cap = (
            int(self.cfg["daily_cross_section_on_wire_response_cap_bytes"])
            if request_type == "98"
            else int(self.cfg["default_on_wire_response_cap_bytes"])
        )
        while not response.endswith(terminator):
            self._real_socket.settimeout(
                min(
                    float(self.cfg["read_timeout_seconds"]),
                    self.remaining_seconds(),
                )
            )
            try:
                chunk = self._real_socket.recv(8192)
            except OSError as exc:
                raise FatalTransportError(f"provider receive failed: {exc}") from exc
            if not chunk:
                raise FatalTransportError(
                    "provider closed connection before terminator"
                )
            response.extend(chunk)
            if len(response) > per_response_cap:
                raise FatalTransportError(
                    "on-wire response exceeded per-response cap"
                )
            if self.total_on_wire + len(response) > int(
                self.cfg["total_on_wire_cap_bytes"]
            ):
                raise FatalTransportError(
                    "run-level on-wire response cap exceeded"
                )
        received_at = dt.datetime.now(dt.timezone.utc)
        raw = bytes(response)
        self.total_on_wire += len(raw)
        if len(raw) < int(self.cfg["header_bytes"]):
            raise FatalTransportError("provider response shorter than header")
        response_header = raw[: int(self.cfg["header_bytes"])]
        try:
            response_header_text = response_header.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FatalTransportError("provider header is not UTF-8") from exc
        response_parts = response_header_text.split("\1")
        if len(response_parts) != 3:
            raise FatalTransportError("invalid provider response header")
        response_version, response_type, response_body_length_text = response_parts
        response_version_expected = self.plan["content_and_semantic_gates"][
            "header_version_must_equal"
        ]
        try:
            body_length = int(response_body_length_text)
        except ValueError as exc:
            raise FatalTransportError("invalid declared response body length") from exc
        payload_end = int(self.cfg["header_bytes"]) + body_length
        footer = raw[payload_end : -len(terminator)]
        footer_match = re.fullmatch(rb"\x01(-?[0-9]{1,10})", footer)
        expected_crc32 = zlib.crc32(raw[:payload_end])
        observed_crc32 = int(footer_match.group(1)) if footer_match else None
        observed_crc32_in_range = (
            observed_crc32 is not None
            and -(2**31) <= observed_crc32 <= (2**32 - 1)
        )
        normalized_observed_crc32 = (
            observed_crc32 & 0xFFFFFFFF
            if observed_crc32_in_range and observed_crc32 is not None
            else None
        )
        frame_payload_and_crc_valid = (
            len(raw) >= payload_end + 2 + len(terminator)
            and footer_match is not None
            and observed_crc32_in_range
            and normalized_observed_crc32 == expected_crc32
        )
        if not frame_payload_and_crc_valid:
            self.records.append(
                {
                    "session_id": self.session_id,
                    "probe_id": self.current_probe_id,
                    "request_type": request_type,
                    "expected_response_type": expected_response_type,
                    "response_version": response_version,
                    "response_type": response_type,
                    "sent_at_utc": sent_at.isoformat(),
                    "received_at_utc": received_at.isoformat(),
                    "on_wire_bytes": len(raw),
                    "on_wire_sha256": sha256_bytes(raw),
                    "declared_body_bytes": body_length,
                    "declared_payload_end_byte": payload_end,
                    "crc_footer_bytes": len(footer),
                    "crc_footer_format_valid": footer_match is not None
                    and observed_crc32_in_range,
                    "crc32_matches_declared_payload": normalized_observed_crc32
                    == expected_crc32,
                    "frame_payload_and_crc_valid": False,
                    "terminator_present": raw.endswith(terminator),
                    "status": "FATAL_FRAME_OR_CRC_MISMATCH",
                }
            )
            raise FatalTransportError(
                "on-wire response violates declared-payload and CRC framing"
            )
        if response_type in self.cons.COMPRESSED_MESSAGE_TYPE_TUPLE:
            compressed_body = raw[
                int(self.cfg["header_bytes"]) : int(self.cfg["header_bytes"])
                + body_length
            ]
            try:
                decoded_body = limited_zlib_decompress(
                    compressed_body,
                    int(self.cfg["per_response_decompressed_cap_bytes"]),
                )
                decoded = response_header_text + decoded_body.decode("utf-8")
            except (ValueError, UnicodeDecodeError, zlib.error) as exc:
                raise FatalTransportError(
                    f"compressed provider response is invalid: {exc}"
                ) from exc
            decompressed_bytes = len(response_header) + len(decoded_body)
            decompressed_hash = sha256_bytes(
                response_header + decoded_body
            )
        else:
            try:
                decoded = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise FatalTransportError(
                    "uncompressed provider response is not UTF-8"
                ) from exc
            decompressed_bytes = len(raw)
            decompressed_hash = sha256_bytes(raw)
        self.total_decompressed += decompressed_bytes
        if self.total_decompressed > int(self.cfg["total_decompressed_cap_bytes"]):
            raise FatalTransportError(
                "run-level decompressed response cap exceeded"
            )
        self.records.append(
            {
                "session_id": self.session_id,
                "probe_id": self.current_probe_id,
                "request_type": request_type,
                "expected_response_type": expected_response_type,
                "response_version": response_version,
                "response_version_matches_expected": (
                    response_version == response_version_expected
                ),
                "response_type": response_type,
                "response_type_matches_expected": (
                    response_type == expected_response_type
                ),
                "sent_at_utc": sent_at.isoformat(),
                "received_at_utc": received_at.isoformat(),
                "elapsed_seconds": round(
                    (received_at - sent_at).total_seconds(), 6
                ),
                "on_wire_bytes": len(raw),
                "on_wire_sha256": sha256_bytes(raw),
                "declared_body_bytes": body_length,
                "declared_payload_end_byte": payload_end,
                "crc_footer_bytes": len(footer),
                "crc_footer_format_valid": footer_match is not None
                and observed_crc32_in_range,
                "crc32_matches_declared_payload": normalized_observed_crc32
                == expected_crc32,
                "frame_payload_and_crc_valid": frame_payload_and_crc_valid,
                "decompressed_bytes": decompressed_bytes,
                "decompressed_sha256": decompressed_hash,
                "terminator_present": raw.endswith(terminator),
            }
        )
        if response_version != response_version_expected:
            raise FatalTransportError(
                f"unexpected provider version: {response_version}"
            )
        if response_type != expected_response_type:
            raise FatalTransportError(
                f"unexpected response type {response_type}; expected {expected_response_type}"
            )
        return decoded

    def close(self) -> None:
        if self._real_socket is not None:
            self._real_socket.close()
            self._real_socket = None
        setattr(self.context, "default_socket", None)


def result_summary(
    result: Any,
    plan: dict[str, Any],
    probe: dict[str, Any],
) -> dict[str, Any]:
    fields = list(getattr(result, "fields", []) or [])
    rows = list(getattr(result, "data", []) or [])
    row_cap = (
        int(plan["row_caps"]["daily_all_a"])
        if probe["method"] == "query_daily_history_k_AStock"
        else int(plan["row_caps"]["other_single_page"])
    )
    summary = {
        "probe_id": probe["probe_id"],
        "method": probe["method"],
        "parameters": probe["parameters"],
        "error_code": str(getattr(result, "error_code", "")),
        "error_msg": str(getattr(result, "error_msg", "")),
        "response_method": str(getattr(result, "method", "")),
        "response_page": str(getattr(result, "cur_page_num", "")),
        "response_page_size": str(getattr(result, "per_page_count", "")),
        "automatic_pagination_performed": False,
        "truncated_without_next_page": len(rows) == row_cap,
    }
    summary.update(
        summarize_rows(
            fields,
            rows,
            fixed_daily_fields=plan["fixed_daily_fields"],
            sample_limit=int(plan["capture_policy"]["retain_at_most_sample_rows_per_probe"]),
        )
    )
    summary["row_cap"] = row_cap
    summary["row_cap_passed"] = len(rows) <= row_cap
    field_indexes = {name: index for index, name in enumerate(fields)}
    observed_codes = [
        str(row[field_indexes["code"]])
        for row in rows
        if "code" in field_indexes and field_indexes["code"] < len(row)
    ]
    observed_dates = [
        str(row[field_indexes["date"]])
        for row in rows
        if "date" in field_indexes and field_indexes["date"] < len(row)
    ]
    observed_date_code_keys = list(zip(observed_dates, observed_codes))
    summary["duplicate_code_count"] = len(observed_codes) - len(set(observed_codes))
    summary["duplicate_date_code_count"] = len(observed_date_code_keys) - len(
        set(observed_date_code_keys)
    )
    code_pattern = re.compile(r"^(?:sh|sz|bj)\.\d{6}$")
    codes_are_well_formed = (
        len(observed_codes) == len(rows)
        and all(code_pattern.fullmatch(code) for code in observed_codes)
    )
    dates_are_strict_iso = len(observed_dates) == len(rows)
    parsed_dates: list[dt.date] = []
    if dates_are_strict_iso:
        try:
            parsed_dates = [dt.date.fromisoformat(value) for value in observed_dates]
            dates_are_strict_iso = all(
                value == parsed.isoformat()
                for value, parsed in zip(observed_dates, parsed_dates)
            )
        except ValueError:
            dates_are_strict_iso = False
    is_daily_bar_probe = probe["method"] in {
        "query_daily_history_k_AStock",
        "query_history_k_data_plus",
    }
    is_state_list_probe = probe["method"] in {
        "query_terminated_stocks",
        "query_suspended_stocks",
        "query_st_stocks",
        "query_starst_stocks",
        "query_stocks_in_risk",
    }
    numeric_checks = summary["numeric_checks"]
    numeric_gate_passed = all(
        int(numeric_checks[key]) == 0
        for key in (
            "numeric_parse_failures",
            "negative_prices",
            "negative_volume",
            "negative_amount",
            "ohlc_violations",
        )
    )
    adjustflag_gate_passed = True
    if is_daily_bar_probe and rows:
        adjustflag_gate_passed = (
            (
                probe["parameters"].get("adjustflag") == "3"
                if probe["method"] == "query_history_k_data_plus"
                else True
            )
            and set(summary["adjustflag_counts"]) == {"3"}
            and int(summary["null_counts"].get("adjustflag", 0)) == 0
        )
    daily_required_values_present = True
    if is_daily_bar_probe:
        required_identity_fields = ("date", "code", "adjustflag", "tradestatus")
        active_numeric_fields = (
            "open",
            "high",
            "low",
            "close",
            "preclose",
            "volume",
            "amount",
        )
        daily_required_values_present = bool(rows) and all(
            field in field_indexes for field in required_identity_fields
        )
        if daily_required_values_present:
            for row in rows:
                if any(
                    field_indexes[field] >= len(row)
                    or not str(row[field_indexes[field]]).strip()
                    for field in required_identity_fields
                ):
                    daily_required_values_present = False
                    break
                trading_status = str(row[field_indexes["tradestatus"]]).strip()
                if trading_status not in {"0", "1"}:
                    daily_required_values_present = False
                    break
                if trading_status == "1" and any(
                    field not in field_indexes
                    or field_indexes[field] >= len(row)
                    or parse_decimal(row[field_indexes[field]]) is None
                    for field in active_numeric_fields
                ):
                    daily_required_values_present = False
                    break
    requested_identity_and_window_passed = True
    unique_business_key_passed = True
    if probe["method"] == "query_history_k_data_plus":
        requested_code = str(probe["parameters"]["code"])
        start_date = dt.date.fromisoformat(str(probe["parameters"]["start_date"]))
        end_date = dt.date.fromisoformat(str(probe["parameters"]["end_date"]))
        requested_identity_and_window_passed = (
            bool(rows)
            and codes_are_well_formed
            and dates_are_strict_iso
            and all(code == requested_code for code in observed_codes)
            and all(start_date <= value <= end_date for value in parsed_dates)
        )
        unique_business_key_passed = (
            len(observed_date_code_keys) == len(rows)
            and summary["duplicate_date_code_count"] == 0
        )
    elif probe["method"] == "query_daily_history_k_AStock":
        requested_date = str(probe["parameters"]["date"])
        requested_identity_and_window_passed = (
            bool(rows)
            and codes_are_well_formed
            and dates_are_strict_iso
            and all(value == requested_date for value in observed_dates)
        )
        unique_business_key_passed = (
            len(observed_codes) == len(rows)
            and summary["duplicate_code_count"] == 0
        )
    elif probe["method"] == "query_stock_basic":
        requested_code = str(probe["parameters"]["code"])
        requested_identity_and_window_passed = (
            bool(rows)
            and codes_are_well_formed
            and all(code == requested_code for code in observed_codes)
        )
        unique_business_key_passed = (
            len(observed_codes) == len(rows)
            and summary["duplicate_code_count"] == 0
        )
    elif is_state_list_probe:
        requested_identity_and_window_passed = bool(rows) and codes_are_well_formed
        unique_business_key_passed = (
            len(observed_codes) == len(rows)
            and summary["duplicate_code_count"] == 0
        )
    daily_coverage_heuristic_passed = True
    if probe["method"] == "query_daily_history_k_AStock":
        required_prefixes = set(
            plan["content_and_semantic_gates"][
                "daily_cross_section_required_code_prefixes"
            ]
        )
        daily_coverage_heuristic_passed = (
            len(rows)
            >= int(
                plan["content_and_semantic_gates"][
                    "daily_cross_section_minimum_rows_for_coverage_heuristic"
                ]
            )
            and required_prefixes.issubset(set(summary["code_prefix_counts"]))
            and not summary["truncated_without_next_page"]
        )
    summary["semantic_gates"] = {
        "source_error_code_zero": summary["error_code"] == "0",
        "row_cap_passed": summary["row_cap_passed"],
        "row_widths_match_fields": summary["all_row_widths_match_fields"],
        "fixed_daily_fields_exact": (
            summary["fields_exactly_match_fixed_daily_fields"]
            if is_daily_bar_probe
            else True
        ),
        "history_adjustflag_three": adjustflag_gate_passed,
        "ohlc_and_nonnegative_checks_passed": (
            numeric_gate_passed if is_daily_bar_probe else True
        ),
        "daily_required_values_present": (
            daily_required_values_present if is_daily_bar_probe else True
        ),
        "strict_iso_dates": dates_are_strict_iso if is_daily_bar_probe else True,
        "well_formed_security_codes": (
            codes_are_well_formed
            if is_daily_bar_probe
            or probe["method"] == "query_stock_basic"
            or is_state_list_probe
            else True
        ),
        "requested_identity_and_date_window_passed": (
            requested_identity_and_window_passed
        ),
        "unique_business_key_passed": unique_business_key_passed,
        "response_not_truncated": not summary["truncated_without_next_page"],
        "daily_cross_section_coverage_heuristic_passed": (
            daily_coverage_heuristic_passed
        ),
    }
    summary["semantic_gate_passed"] = all(summary["semantic_gates"].values())
    return summary


def run_result_probe(
    callable_: Callable[..., Any],
    probe: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = callable_(**probe["parameters"])
        value = result_summary(result, plan, probe)
        if value["error_code"] != "0":
            value["status"] = "SOURCE_ERROR_FOR_INSPECTION"
        elif value["row_count"] == 0:
            value["status"] = "EMPTY_FOR_INSPECTION"
        elif value["semantic_gate_passed"]:
            value["status"] = "PASS_FOR_INSPECTION"
        else:
            value["status"] = "FAIL_FOR_INSPECTION"
    except FatalTransportError:
        raise
    except Exception as exc:
        value = {
            "probe_id": probe["probe_id"],
            "method": probe["method"],
            "parameters": probe["parameters"],
            "status": "ERROR",
            "exception_type": type(exc).__name__,
            "exception": str(exc),
        }
    value["wall_seconds"] = round(time.monotonic() - started, 6)
    return value


def validate_plan(plan: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "execution_plan_id",
        "status",
        "execution_authorization_active",
        "purpose_ceiling",
        "formal_admission",
        "bulk_backfill_authorized",
        "quality_promotion_authorized",
        "paid_source_authorized",
        "user_credentials_or_keys_authorized",
        "output_root",
        "package",
        "transport",
        "fixed_daily_fields",
        "probes",
        "business_probe_count",
    }
    missing = sorted(required - set(plan))
    if missing:
        raise ValueError(f"execution plan missing keys: {missing}")
    if plan["status"] != "ACTIVE__ONE_OFF_INSPECTION_ONLY":
        raise ValueError("execution plan is not active")
    if not plan["execution_authorization_active"]:
        raise ValueError("execution authorization is false")
    if plan["purpose_ceiling"] != "INSPECTION_ONLY":
        raise ValueError("purpose ceiling is not INSPECTION_ONLY")
    forbidden = (
        plan["formal_admission"],
        plan["bulk_backfill_authorized"],
        plan["quality_promotion_authorized"],
        plan["paid_source_authorized"],
        plan["user_credentials_or_keys_authorized"],
    )
    if any(forbidden):
        raise ValueError("execution plan grants a forbidden action")
    probes = plan["probes"]
    observed_count = (
        len(probes["daily_cross_section"])
        + len(probes["stock_basic"])
        + len(probes["history"])
        + len(probes["state_lists"])
        + int(probes["stability"]["independent_sessions"])
    )
    if observed_count != int(plan["business_probe_count"]):
        raise ValueError("declared business probe count is inconsistent")
    expected_socket_messages = observed_count + 2 * (
        1 + int(probes["stability"]["independent_sessions"])
    )
    if expected_socket_messages != int(
        plan["transport"]["maximum_socket_messages"]
    ):
        raise ValueError("declared socket-message budget is inconsistent")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument("--network", action="store_true")
    args = parser.parse_args()
    if not args.network:
        raise SystemExit("Refusing to run without --network")

    workspace = args.workspace_root.resolve()
    plan_path = (
        workspace
        / "data"
        / "catalog"
        / "contracts"
        / "source_admission_v1"
        / "execution_plans"
        / "baostock_daily_coverage_v1.json"
    )
    actual_plan_hash = sha256_file(plan_path)
    if actual_plan_hash != EXPECTED_PLAN_SHA256:
        raise SystemExit(
            "Execution-plan hash mismatch; reviewed plan is not the executable plan"
        )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    validate_plan(plan)
    source_matrix_path = (
        workspace
        / "data"
        / "catalog"
        / "contracts"
        / "source_admission_v1"
        / "source_matrix.json"
    )
    source_matrix = json.loads(source_matrix_path.read_text(encoding="utf-8"))
    if (
        source_matrix.get("status")
        != "PAUSED_GENERIC_RUNNER__SCOPED_PLAN_ACTIVE__NO_FORMAL_ADMISSION"
        or source_matrix.get("execution_authorization_active") is not True
        or source_matrix.get("resume_requires_user_start") is not False
        or source_matrix.get("global_purpose_ceiling") != "INSPECTION_ONLY"
        or source_matrix.get("formal_admission") is not False
        or source_matrix.get("bulk_backfill_authorized") is not False
        or source_matrix.get("quality_promotion_authorized") is not False
    ):
        raise SystemExit("Top-level source-matrix scoped execution gate is closed")
    authorized_plans = source_matrix.get("authorized_execution_plans") or []
    authorization = next(
        (
            item
            for item in authorized_plans
            if item.get("execution_plan_id") == plan["execution_plan_id"]
        ),
        None,
    )
    if authorization is None:
        raise SystemExit("Execution plan is not authorized by source_matrix.json")
    if (
        authorization.get("status") != "ACTIVE__ONE_OFF_INSPECTION_ONLY"
        or authorization.get("execution_plan_sha256") != actual_plan_hash
        or authorization.get("execution_plan_path")
        != "data/catalog/contracts/source_admission_v1/execution_plans/"
        "baostock_daily_coverage_v1.json"
        or authorization.get("runner_path")
        != "control/tools/run_baostock_daily_coverage_pilot.py"
        or authorization.get("runner_sha256")
        != sha256_file(Path(__file__).resolve())
        or authorization.get("formal_admission")
        or authorization.get("bulk_backfill_authorized")
        or authorization.get("quality_promotion_authorized")
        or authorization.get("paid_source_authorized")
        or authorization.get("user_credentials_or_keys_authorized")
    ):
        raise SystemExit("Source-matrix execution authorization is invalid")

    output = (workspace / plan["output_root"]).resolve()
    allowed_root = (workspace / "tmp" / "source-admission-pilot").resolve()
    if not resolved_under(output, allowed_root):
        raise SystemExit("Refusing output outside isolated pilot root")
    if output.exists():
        raise SystemExit(f"Refusing to overwrite run directory: {output}")
    output.mkdir(parents=True)
    shutil.copyfile(plan_path, output / "EXECUTED_PLAN.json")
    shutil.copyfile(source_matrix_path, output / "EXECUTED_SOURCE_MATRIX.json")

    wheel_path = (workspace / plan["package"]["local_wheel"]).resolve()
    if sha256_file(wheel_path) != plan["package"]["wheel_sha256"]:
        raise SystemExit("BaoStock wheel hash mismatch")
    if wheel_path.stat().st_size > int(plan["package"]["wheel_max_bytes"]):
        raise SystemExit("BaoStock wheel exceeded reviewed size cap")
    runtime_import_root = output / "verified-wheel-runtime"
    runtime_import_root.mkdir()
    runtime_extraction = extract_verified_wheel(
        wheel_path,
        runtime_import_root,
        maximum_files=int(plan["package"]["wheel_max_files"]),
        maximum_unpacked_bytes=int(plan["package"]["wheel_unpacked_max_bytes"]),
    )
    sys.path.insert(0, str(runtime_import_root))

    import baostock as bs  # type: ignore[import-not-found]
    import baostock.common.context as bs_context  # type: ignore[import-not-found]
    import baostock.common.contants as bs_cons  # type: ignore[import-not-found]
    import baostock.security.sectorinfo as bs_sector  # type: ignore[import-not-found]
    import baostock.util.socketutil as bs_socket  # type: ignore[import-not-found]

    if bs.__version__ != plan["package"]["client_version"]:
        raise SystemExit("Imported BaoStock client version mismatch")
    if bs_cons.BAOSTOCK_SERVER_IP != plan["transport"]["host"]:
        raise SystemExit("Imported BaoStock host differs from reviewed plan")
    if int(bs_cons.BAOSTOCK_SERVER_PORT) != int(plan["transport"]["port"]):
        raise SystemExit("Imported BaoStock port differs from reviewed plan")
    if hasattr(bs_context, "apiKey"):
        delattr(bs_context, "apiKey")

    formal_targets = {
        "data/catalog/datasets.json": workspace / "data" / "catalog" / "datasets.json",
        "data/catalog/planned_datasets.json": workspace
        / "data"
        / "catalog"
        / "planned_datasets.json",
        "data/market": workspace / "data" / "market",
        "data/published": workspace / "data" / "published",
    }
    before = {name: fingerprint(path) for name, path in formal_targets.items()}
    started_utc = dt.datetime.now(dt.timezone.utc)
    started_monotonic = time.monotonic()
    deadline_monotonic = started_monotonic + float(
        plan["transport"]["maximum_wall_clock_seconds"]
    )
    transport = BoundedBaoStockTransport(
        plan,
        bs_cons,
        bs_context,
        deadline_monotonic=deadline_monotonic,
    )
    original_connect = bs_socket.SocketUtil.connect
    original_send_msg = bs_socket.send_msg
    bs_socket.SocketUtil.connect = lambda socket_util_instance: transport.connect(
        socket_util_instance
    )
    bs_socket.send_msg = transport.send_msg

    results: dict[str, Any] = {
        "schema_version": 1,
        "work_package": plan["work_package"],
        "execution_plan_id": plan["execution_plan_id"],
        "execution_plan_sha256": actual_plan_hash,
        "source_matrix_sha256": sha256_file(source_matrix_path),
        "runner_path": Path(__file__).resolve().relative_to(workspace).as_posix(),
        "runner_sha256": sha256_file(Path(__file__).resolve()),
        "started_at_utc": started_utc.isoformat(),
        "scope": "BAOSTOCK_BOUNDED_DAILY_COVERAGE_INSPECTION_ONLY",
        "safety": {
            "paid_source_used": False,
            "user_credentials_or_keys_used": False,
            "provider_public_anonymous_default_used": True,
            "api_key_present": hasattr(bs_context, "apiKey"),
            "historical_bulk_backfill_performed": False,
            "formal_catalog_registration_performed": False,
            "formal_quality_promotion_performed": False,
            "full_market_rows_retained": False,
        },
        "package": {
            "name": plan["package"]["name"],
            "version": plan["package"]["version"],
            "client_version": bs.__version__,
            "wheel_bytes": wheel_path.stat().st_size,
            "wheel_sha256": sha256_file(wheel_path),
            "runtime_extraction": runtime_extraction,
            "server_host": bs_cons.BAOSTOCK_SERVER_IP,
            "server_port": bs_cons.BAOSTOCK_SERVER_PORT,
        },
        "integrity": {"before": before},
        "probes": {},
    }

    def login(session_id: str) -> dict[str, Any]:
        transport.session_id = session_id
        transport.current_probe_id = f"{session_id}_login"
        started = time.monotonic()
        try:
            value = bs.login(
                user_id=plan["transport"]["login_user"],
                password=plan["transport"]["login_password_value"],
            )
            return {
                "error_code": str(value.error_code),
                "error_msg": str(value.error_msg),
                "status": "PASS" if str(value.error_code) == "0" else "FAIL",
                "wall_seconds": round(time.monotonic() - started, 6),
            }
        except FatalTransportError:
            raise
        except Exception as exc:
            return {
                "status": "ERROR",
                "exception_type": type(exc).__name__,
                "exception": str(exc),
                "wall_seconds": round(time.monotonic() - started, 6),
            }

    def logout(session_id: str) -> dict[str, Any]:
        transport.session_id = session_id
        transport.current_probe_id = f"{session_id}_logout"
        started = time.monotonic()
        try:
            value = bs.logout(user_id=plan["transport"]["login_user"])
            return {
                "error_code": str(getattr(value, "error_code", "")),
                "error_msg": str(getattr(value, "error_msg", "")),
                "status": (
                    "PASS" if str(getattr(value, "error_code", "")) == "0" else "FAIL"
                ),
                "wall_seconds": round(time.monotonic() - started, 6),
            }
        except FatalTransportError:
            raise
        except Exception as exc:
            return {
                "status": "ERROR",
                "exception_type": type(exc).__name__,
                "exception": str(exc),
                "wall_seconds": round(time.monotonic() - started, 6),
            }
        finally:
            transport.close()

    sessions: dict[str, Any] = {}
    results["sessions"] = sessions
    fatal_error: dict[str, Any] | None = None
    try:
        main_session = "main"
        sessions[main_session] = {"login": login(main_session)}
        if sessions[main_session]["login"]["status"] != "PASS":
            raise FatalTransportError("main anonymous login did not pass")
        method_maps: list[tuple[str, Callable[..., Any], list[dict[str, Any]]]] = [
            (
                "daily_cross_section",
                bs.query_daily_history_k_AStock,
                plan["probes"]["daily_cross_section"],
            ),
            ("stock_basic", bs.query_stock_basic, plan["probes"]["stock_basic"]),
            ("history", bs.query_history_k_data_plus, plan["probes"]["history"]),
        ]
        state_methods = {
            "query_terminated_stocks": bs_sector.query_terminated_stocks,
            "query_suspended_stocks": bs_sector.query_suspended_stocks,
            "query_st_stocks": bs_sector.query_st_stocks,
            "query_starst_stocks": bs_sector.query_starst_stocks,
            "query_stocks_in_risk": bs_sector.query_stocks_in_risk,
        }
        for group, callable_, probes in method_maps:
            group_results: list[dict[str, Any]] = []
            results["probes"][group] = group_results
            for probe in probes:
                transport.remaining_seconds()
                transport.session_id = main_session
                transport.current_probe_id = probe["probe_id"]
                parameters = dict(probe["parameters"])
                if probe["method"] == "query_history_k_data_plus":
                    parameters["fields"] = ",".join(plan["fixed_daily_fields"])
                executed_probe = {
                    "probe_id": probe["probe_id"],
                    "method": probe["method"],
                    "parameters": parameters,
                }
                group_results.append(
                    run_result_probe(callable_, executed_probe, plan)
                )
        state_results: list[dict[str, Any]] = []
        results["probes"]["state_lists"] = state_results
        for probe in plan["probes"]["state_lists"]:
            transport.remaining_seconds()
            transport.session_id = main_session
            transport.current_probe_id = probe["probe_id"]
            state_results.append(
                run_result_probe(
                    state_methods[probe["method"]],
                    probe,
                    plan,
                )
            )
        sessions[main_session]["logout"] = logout(main_session)
        if sessions[main_session]["logout"]["status"] != "PASS":
            raise FatalTransportError("main logout did not pass")

        stability_results: list[dict[str, Any]] = []
        results["probes"]["stability"] = stability_results
        stability_spec = plan["probes"]["stability"]
        for index in range(int(stability_spec["independent_sessions"])):
            transport.remaining_seconds()
            session_id = f"stability_{index + 1}"
            sessions[session_id] = {"login": login(session_id)}
            if sessions[session_id]["login"]["status"] != "PASS":
                raise FatalTransportError(f"{session_id} login did not pass")
            probe = {
                "probe_id": f"{stability_spec['probe_id_prefix']}_{index + 1}",
                "method": stability_spec["method"],
                "parameters": {
                    **stability_spec["parameters"],
                    "fields": ",".join(plan["fixed_daily_fields"]),
                },
            }
            transport.session_id = session_id
            transport.current_probe_id = probe["probe_id"]
            stability_results.append(
                run_result_probe(bs.query_history_k_data_plus, probe, plan)
            )
            sessions[session_id]["logout"] = logout(session_id)
            if sessions[session_id]["logout"]["status"] != "PASS":
                raise FatalTransportError(f"{session_id} logout did not pass")
    except Exception as exc:
        fatal_error = {
            "status": "FATAL_RUN_ABORTED",
            "exception_type": type(exc).__name__,
            "exception": str(exc),
            "session_id": transport.session_id,
            "probe_id": transport.current_probe_id,
            "observed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        results["fatal_error"] = fatal_error
    finally:
        bs_socket.SocketUtil.connect = original_connect
        bs_socket.send_msg = original_send_msg
        transport.close()

    after = {name: fingerprint(path) for name, path in formal_targets.items()}
    results["integrity"].update(
        {"after": after, "formal_targets_unchanged": before == after}
    )
    results["transport_ledger"] = transport.records
    results["transport_totals"] = {
        "socket_messages": transport.socket_messages,
        "on_wire_bytes": transport.total_on_wire,
        "decompressed_bytes": transport.total_decompressed,
    }
    all_probe_results = [
        item
        for group in results["probes"].values()
        for item in group
    ]
    daily_result = next(
        (
            item
            for item in results["probes"].get("daily_cross_section", [])
            if item["probe_id"] == "daily_all_a_20260724"
        ),
        None,
    )
    explicit_bj_results = [
        item
        for item in results["probes"].get("history", [])
        if str(item.get("parameters", {}).get("code", "")).startswith("bj.")
    ]
    daily_bj_rows = (
        int(daily_result.get("code_prefix_counts", {}).get("bj", 0))
        if daily_result
        else 0
    )
    explicit_bj_with_rows = [
        item["probe_id"]
        for item in explicit_bj_results
        if item.get("status") == "PASS_FOR_INSPECTION"
        and int(item.get("row_count", 0)) > 0
    ]
    stability_hashes = [
        item.get("normalized_rows_sha256")
        for item in results["probes"].get("stability", [])
        if item.get("status") == "PASS_FOR_INSPECTION"
    ]
    same_day = next(
        (
            item
            for item in results["probes"].get("history", [])
            if item["probe_id"] == "history_sh_same_day_freshness"
        ),
        None,
    )
    session_errors = sum(
        stage.get("status") == "ERROR"
        for session in results.get("sessions", {}).values()
        for stage in session.values()
    )
    probe_errors = sum(
        item.get("status") == "ERROR" for item in all_probe_results
    )
    critical_probe_ids = {
        "daily_all_a_20260724",
        "basic_sh_main",
        "basic_sh_star",
        "basic_sz_main",
        "basic_sz_chinext",
        "basic_bj_legacy_1",
        "basic_bj_legacy_2",
        "basic_sz_delisted",
        "basic_bj_920_current",
        "history_sh_exchange_start",
        "history_sz_early",
        "history_sz_delisted",
        "history_bj_opening_window",
        "history_bj_recent_legacy_code",
        "history_bj_recent_920_code",
        *{
            f"{plan['probes']['stability']['probe_id_prefix']}_{index + 1}"
            for index in range(
                int(plan["probes"]["stability"]["independent_sessions"])
            )
        },
    }
    results_by_probe_id = {
        str(item.get("probe_id")): item for item in all_probe_results
    }
    critical_probe_failure_ids = sorted(
        probe_id
        for probe_id in critical_probe_ids
        if results_by_probe_id.get(probe_id, {}).get("status")
        != "PASS_FOR_INSPECTION"
    )
    business_probes_executed = sum(
        item.get("status") != "SKIPPED_LOGIN_FAILED" for item in all_probe_results
    )
    evidence_collection_completed = (
        fatal_error is None
        and business_probes_executed == int(plan["business_probe_count"])
    )
    coverage_gate_passed = (
        evidence_collection_completed
        and probe_errors == 0
        and session_errors == 0
        and not critical_probe_failure_ids
        and bool(
            daily_result
            and daily_result.get("status") == "PASS_FOR_INSPECTION"
        )
        and bool(daily_bj_rows)
        and bool(explicit_bj_with_rows)
        and len(stability_hashes)
        == int(plan["probes"]["stability"]["independent_sessions"])
        and len(set(stability_hashes)) == 1
    )
    results["assessment"] = {
        "transport_errors": probe_errors + session_errors + int(fatal_error is not None),
        "fatal_run_aborted": fatal_error is not None,
        "session_errors": session_errors,
        "probe_errors": probe_errors,
        "business_probes_planned": int(plan["business_probe_count"]),
        "business_probes_executed": business_probes_executed,
        "evidence_collection_completed": evidence_collection_completed,
        "critical_probe_failure_ids": critical_probe_failure_ids,
        "coverage_gate_passed": coverage_gate_passed,
        "formal_targets_unchanged": before == after,
        "fixed_daily_schema_pass_count": sum(
            bool(item.get("fields_exactly_match_fixed_daily_fields"))
            for item in all_probe_results
        ),
        "semantic_gate_failure_probe_ids": [
            item.get("probe_id")
            for item in all_probe_results
            if item.get("row_count", 0)
            and item.get("semantic_gate_passed") is False
        ],
        "bse_daily_cross_section_rows": daily_bj_rows,
        "bse_daily_cross_section_semantic_passed": bool(
            daily_result
            and daily_result.get("status") == "PASS_FOR_INSPECTION"
        ),
        "bse_explicit_history_probes_with_rows": explicit_bj_with_rows,
        "bse_coverage_capability_passed": bool(
            daily_bj_rows
            and daily_result
            and daily_result.get("status") == "PASS_FOR_INSPECTION"
            and explicit_bj_with_rows
        ),
        "stability_successful_runs": len(stability_hashes),
        "stability_hashes_identical": (
            len(stability_hashes)
            == int(plan["probes"]["stability"]["independent_sessions"])
            and len(set(stability_hashes)) == 1
        ),
        "same_day_20260727_row_observed_at_probe_time": bool(
            same_day and int(same_day.get("row_count", 0)) > 0
        ),
        "rights_status": "UNRESOLVED__INSPECTION_ONLY",
        "unit_status": (
            "UNVERIFIED__EMPIRICAL_AMOUNT_DIVIDED_BY_VOLUME_DIAGNOSTIC_ONLY__"
            "FIRST_PARTY_DICTIONARY_NOT_CAPTURED"
        ),
        "pit_status": (
            "DAILY_NEXT_SESSION_RULE_POSSIBLE__SOURCE_PUBLICATION_AND_REVISION_HISTORY_UNPROVEN"
        ),
        "formal_admission": False,
        "quality_promotion": False,
    }
    results["finished_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    results["wall_seconds"] = round(time.monotonic() - started_monotonic, 6)
    write_json(output / "RESULTS.json", results)
    inventory = {
        "schema_version": 1,
        "execution_plan_sha256": actual_plan_hash,
        "self_exclusion": "INVENTORY.json is not included in itself",
        "files": [
            {
                "path": path.relative_to(output).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in sorted(item for item in output.rglob("*") if item.is_file())
        ],
    }
    write_json(output / "INVENTORY.json", inventory)
    print(json.dumps(results["assessment"], ensure_ascii=False, indent=2))
    if not results["integrity"]["formal_targets_unchanged"]:
        return 3
    if fatal_error is not None:
        return 4
    if results["assessment"]["transport_errors"]:
        return 4
    if not results["assessment"]["coverage_gate_passed"]:
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
