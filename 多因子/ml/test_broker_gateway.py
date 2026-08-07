#!/usr/bin/env python3
"""Tests for the fail-closed broker boundary."""

from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path

from broker_gateway import (
    AccountSnapshot,
    DayRiskState,
    OrderIntent,
    OrderRiskGate,
    Position,
    Quote,
    QmtGateway,
    RiskPolicy,
    SHANGHAI,
    mask_account,
    qmt_code,
)


NOW = datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI)


def account(cash: float = 50_000.0) -> AccountSnapshot:
    return AccountSnapshot(
        "TEST", "****7756", NOW.timestamp(), 100_000.0, cash, 50_000.0, connected=True
    )


def quote(age: float = 2.0) -> Quote:
    return Quote("159995.SZ", NOW.timestamp() - age, 1.20, 1.19, 1.199, 1.201, 1.30, 1.08)


def intent(side: str = "BUY", quantity: int = 1000, price: float = 1.201) -> OrderIntent:
    return OrderIntent(
        "159995", side, quantity, price, NOW.timestamp() - 5, NOW.timestamp() + 30, "test"
    )


def state(**changes) -> DayRiskState:
    values = dict(
        date="2026-07-16",
        start_equity=100_000.0,
        current_equity=100_000.0,
        new_exposure=0.0,
        submitted_orders=0,
        submitted_keys=(),
        pilot_pnl=0.0,
        kill_switch=False,
    )
    values.update(changes)
    return DayRiskState(**values)


class BrokerRiskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = RiskPolicy(
            allowed_symbols=("159995",), max_order_value=10_000.0
        )
        self.gate = OrderRiskGate(self.policy)

    def evaluate(self, order: OrderIntent, **kwargs):
        return self.gate.evaluate(
            order,
            kwargs.get("account", account()),
            kwargs.get("positions", []),
            kwargs.get("quote", quote()),
            kwargs.get("state", state()),
            now=kwargs.get("now", NOW),
        )

    def test_code_and_account_normalization(self) -> None:
        self.assertEqual(qmt_code("159995"), "159995.SZ")
        self.assertEqual(qmt_code("588170"), "588170.SH")
        self.assertEqual(qmt_code("832000"), "832000.BJ")
        self.assertEqual(mask_account("1234567756"), "******7756")

    def test_valid_limit_order_passes_shadow_risk_checks(self) -> None:
        decision = self.evaluate(intent())
        self.assertTrue(decision.approved, decision.reasons)
        self.assertEqual(decision.status, "RISK_CHECK_PASSED")

    def test_stale_quote_fails_closed(self) -> None:
        decision = self.evaluate(intent(), quote=quote(age=30.0))
        self.assertFalse(decision.approved)
        self.assertIn("quoteFresh", decision.reasons)

    def test_empty_symbol_whitelist_fails_closed(self) -> None:
        gate = OrderRiskGate(RiskPolicy(allowed_symbols=()))
        decision = gate.evaluate(intent(), account(), [], quote(), state(), now=NOW)
        self.assertFalse(decision.approved)
        self.assertIn("symbolAllowed", decision.reasons)

    def test_t_plus_one_sellable_quantity_is_enforced(self) -> None:
        positions = [Position("159995", "ETF", 10_000, 200, 1.1, 1.2, 12_000.0)]
        decision = self.evaluate(intent("SELL", quantity=300), positions=positions)
        self.assertFalse(decision.approved)
        self.assertIn("sellableQuantity", decision.reasons)

    def test_duplicate_and_daily_loss_are_blocked(self) -> None:
        order = intent().normalized()
        decision = self.evaluate(
            order,
            state=state(current_equity=98_900.0, submitted_keys=(order.idempotency_key,)),
        )
        self.assertFalse(decision.approved)
        self.assertIn("uniqueIntent", decision.reasons)
        self.assertIn("dailyLossClear", decision.reasons)

    def test_pilot_loss_stop_is_independent_from_whole_account(self) -> None:
        decision = self.evaluate(intent(), state=state(pilot_pnl=-1_000.0))
        self.assertFalse(decision.approved)
        self.assertIn("pilotLossClear", decision.reasons)

    def test_auction_lunch_and_close_are_blocked(self) -> None:
        for moment in (
            datetime(2026, 7, 16, 9, 25, tzinfo=SHANGHAI),
            datetime(2026, 7, 16, 12, 0, tzinfo=SHANGHAI),
            datetime(2026, 7, 16, 14, 58, tzinfo=SHANGHAI),
        ):
            current_quote = Quote("159995", moment.timestamp(), 1.2, 1.19, 1.199, 1.201)
            current_intent = OrderIntent(
                "159995", "BUY", 100, 1.201, moment.timestamp(), moment.timestamp() + 30, "test"
            )
            decision = self.evaluate(current_intent, quote=current_quote, now=moment)
            self.assertFalse(decision.approved)
            self.assertIn("continuousSession", decision.reasons)

    def test_qmt_gateway_exposes_no_order_submission_method(self) -> None:
        gateway = QmtGateway(Path("."), "test-account")
        self.assertFalse(hasattr(gateway, "submit"))
        self.assertFalse(hasattr(gateway, "arm"))


if __name__ == "__main__":
    unittest.main()
