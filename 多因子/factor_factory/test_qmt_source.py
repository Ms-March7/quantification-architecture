#!/usr/bin/env python3
"""Offline tests for the QMT read-only data boundary."""

from __future__ import annotations

import unittest

from factor_factory.qmt_source import QmtMarketDataSource, qmt_symbol


class QmtSourceTests(unittest.TestCase):
    def test_symbol_normalization(self) -> None:
        self.assertEqual(qmt_symbol("600000"), "600000.SH")
        self.assertEqual(qmt_symbol("SZ300750"), "300750.SZ")
        with self.assertRaises(ValueError):
            qmt_symbol("920001")

    def test_static_probe_never_claims_connection_or_permission(self) -> None:
        probe = QmtMarketDataSource().probe()
        self.assertEqual(probe.market_data_session, "NOT_TESTED")
        self.assertEqual(probe.history_permission, "NOT_TESTED")
        self.assertEqual(probe.live_order_permission, "NOT_TESTED")
        self.assertFalse(probe.connected)


if __name__ == "__main__":
    unittest.main()
