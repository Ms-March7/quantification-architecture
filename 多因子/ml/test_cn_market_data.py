#!/usr/bin/env python3
"""Tests for public A-share bar normalization and quality gates."""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from cn_market_data import (
    SHANGHAI,
    compare_sources,
    completed_bars,
    parse_eastmoney_payload,
    parse_sina_payload,
)


EASTMONEY = json.dumps({
    "data": {
        "name": "ETF",
        "klines": [
            "2026-07-15 14:55,1.399,1.397,1.402,1.396,395289,55308505,0,0,0,0",
            "2026-07-15 15:00,1.397,1.397,1.398,1.395,307329,42911565,0,0,0,0",
        ],
    }
})

SINA = """var _data=([{"day":"2026-07-15 14:55:00","open":"1.399","high":"1.402","low":"1.396","close":"1.397","volume":"39400000","amount":"55200000"},{"day":"2026-07-15 15:00:00","open":"1.397","high":"1.398","low":"1.395","close":"1.397","volume":"29647800","amount":"41395634"}]);"""


class MarketDataTests(unittest.TestCase):
    def test_source_units_are_normalized_to_shares(self) -> None:
        east = parse_eastmoney_payload("159995", "5m", EASTMONEY, 1.0)
        sina = parse_sina_payload("159995", "5m", SINA, 1.0)
        self.assertEqual(east.bars[-1].volume, 30_732_900.0)
        self.assertEqual(sina.bars[-1].volume, 29_647_800.0)

    def test_dual_source_match_is_observation_only(self) -> None:
        east = parse_eastmoney_payload("159995", "5m", EASTMONEY, 1.0)
        sina = parse_sina_payload("159995", "5m", SINA, 1.0)
        bundle = compare_sources(east, sina, 100)
        self.assertEqual(bundle.quality.status, "OK_DUAL_SOURCE")
        self.assertFalse(bundle.quality.manual_review_eligible)
        self.assertLess(bundle.quality.last_close_difference_bps, 1e-9)

    def test_timestamp_mismatch_blocks(self) -> None:
        east = parse_eastmoney_payload("159995", "5m", EASTMONEY, 1.0)
        short_sina = parse_sina_payload(
            "159995",
            "5m",
            """var _data=([{"day":"2026-07-15 14:55:00","open":"1.399","high":"1.402","low":"1.396","close":"1.397","volume":"39400000","amount":"55200000"}]);""",
            1.0,
        )
        bundle = compare_sources(east, short_sina, 100)
        self.assertEqual(bundle.quality.status, "DATA_BLOCKED")

    def test_incomplete_bar_is_removed(self) -> None:
        east = parse_eastmoney_payload("159995", "5m", EASTMONEY, 1.0)
        now = datetime(2026, 7, 15, 15, 0, 5, tzinfo=SHANGHAI)
        values = completed_bars(east.bars, now=now, grace_seconds=10.0)
        self.assertEqual(values[-1].time, "2026-07-15 14:55:00")


if __name__ == "__main__":
    unittest.main()
