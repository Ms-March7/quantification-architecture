#!/usr/bin/env python3
"""Unit tests for the next-session defensive monitor."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
import json
import tempfile
from pathlib import Path

import numpy as np

from broker_gateway import Position, SHANGHAI
from cn_market_data import DataBundle, MarketBar, QualityReport
from short_cycle_live_monitor import build_report, compute_factors, load_manual_snapshot, risk_score


def bars(negative: bool = False) -> tuple[MarketBar, ...]:
    start = datetime(2026, 7, 14, 9, 35, tzinfo=SHANGHAI)
    result = []
    price = 1.2
    timestamp = start
    for index in range(100):
        if index == 48:
            timestamp = datetime(2026, 7, 15, 9, 35, tzinfo=SHANGHAI)
        elif index:
            timestamp += timedelta(minutes=5)
            if timestamp.time().hour == 11 and timestamp.time().minute > 30:
                timestamp = timestamp.replace(hour=13, minute=5)
            if timestamp.time().hour >= 15:
                timestamp = (timestamp + timedelta(days=1)).replace(hour=9, minute=35)
        change = -0.006 if negative and index >= 94 else 0.0002 * np.sin(index / 4)
        open_price = price
        price *= 1.0 + change
        volume = 1_000_000.0 * (4.0 if negative and index == 99 else 1.0)
        result.append(MarketBar(
            timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            open_price,
            max(open_price, price) * 1.0005,
            min(open_price, price) * 0.9995,
            price,
            volume,
            price * volume,
        ))
    return tuple(result)


def bundle(symbol: str, values: tuple[MarketBar, ...], eligible: bool = False) -> DataBundle:
    quality = QualityReport(
        "OK_QMT" if eligible else "OK_DUAL_SOURCE",
        eligible,
        (),
        "QMT" if eligible else "eastmoney",
        "" if eligible else "sina",
        values[-1].time,
        values[-1].time,
        0.0,
        0.0,
        len(values),
    )
    return DataBundle(symbol, "5m", values, quality)


def shift_to(values: tuple[MarketBar, ...], target: datetime) -> tuple[MarketBar, ...]:
    latest = datetime.fromisoformat(values[-1].time).replace(tzinfo=SHANGHAI)
    offset = target - latest
    return tuple(MarketBar(
        (datetime.fromisoformat(value.time).replace(tzinfo=SHANGHAI) + offset).strftime("%Y-%m-%d %H:%M:%S"),
        value.open,
        value.high,
        value.low,
        value.close,
        value.volume,
        value.amount,
    ) for value in values)


class LiveMonitorTests(unittest.TestCase):
    def test_manual_snapshot_loads_without_credentials(self) -> None:
        payload = {
            "asOf": "2026-07-15 16:21:00",
            "totalAsset": 100000.0,
            "cash": 50000.0,
            "positions": [{
                "symbol": "159995", "totalQuantity": 1000, "sellableQuantity": 1000,
                "averagePrice": 1.1, "lastPrice": 1.2, "marketValue": 1200.0,
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "snapshot.json"
            target.write_text(json.dumps(payload), encoding="utf-8")
            account, positions = load_manual_snapshot(target)
        self.assertEqual(account["source"], "MANUAL_SNAPSHOT")
        self.assertEqual(positions[0].sellable_quantity, 1000)

    def test_factor_snapshot_is_finite(self) -> None:
        value = compute_factors(bars())
        self.assertTrue(all(np.isfinite(item) for item in value.__dict__.values()))

    def test_negative_break_gets_high_risk_score(self) -> None:
        value = compute_factors(bars(negative=True))
        score, reasons = risk_score(value)
        self.assertGreaterEqual(score, 45)
        self.assertIn("short_momentum_break", reasons)

    def test_public_data_never_allows_execution(self) -> None:
        values = bars()
        now = datetime.fromisoformat(values[-1].time).replace(tzinfo=SHANGHAI) + timedelta(seconds=20)
        report = build_report({"159995": bundle("159995", values)}, source_mode="public", now=now)
        self.assertEqual(report["workflowStatus"], "OBSERVATION_ONLY")
        self.assertIn("public_data_is_observation_only", report["globalBlocks"])

    def test_qmt_account_and_bars_allow_manual_review_only(self) -> None:
        values = shift_to(bars(), datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI))
        now = datetime(2026, 7, 16, 10, 0, 20, tzinfo=SHANGHAI)
        position = Position("159995", "ETF", 1000, 1000, 1.1, 1.2, 1200.0)
        report = build_report(
            {"159995": bundle("159995", values, eligible=True)},
            positions=(position,),
            account={"connected": True, "timestamp": now.timestamp()},
            source_mode="qmt",
            now=now,
            decision_cadence_minutes=5,
        )
        self.assertEqual(report["workflowStatus"], "ALLOW_MANUAL_ORDER_REVIEW")

    def test_after_hours_never_allows_order_review(self) -> None:
        values = bars()
        now = datetime(2026, 7, 15, 16, 0, tzinfo=SHANGHAI)
        report = build_report(
            {"159995": bundle("159995", values, eligible=True)},
            account={"connected": True, "timestamp": now.timestamp()},
            source_mode="qmt",
            now=now,
            decision_cadence_minutes=5,
        )
        self.assertEqual(report["workflowStatus"], "OBSERVATION_ONLY")
        self.assertIn("outside_continuous_session", report["globalBlocks"])

    def test_reit_is_not_routed_through_stock_etf_action(self) -> None:
        values = bars()
        now = datetime.fromisoformat(values[-1].time).replace(tzinfo=SHANGHAI) + timedelta(seconds=20)
        report = build_report({"508033": bundle("508033", values)}, source_mode="public", now=now)
        self.assertEqual(report["cards"][0]["action"], "SEPARATE_REIT_REVIEW")


if __name__ == "__main__":
    unittest.main()
