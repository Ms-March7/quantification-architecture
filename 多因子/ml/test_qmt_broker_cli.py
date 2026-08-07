#!/usr/bin/env python3
"""Offline tests for QMT CLI state and approved-order serialization."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from broker_gateway import AccountSnapshot
from qmt_broker_cli import load_state, new_state, save_state


class QmtCliTests(unittest.TestCase):
    def test_state_round_trip_never_needs_account_credentials(self) -> None:
        account = AccountSnapshot("QMT", "******7756", 1.0, 4_100_000.0, 20_000.0, 4_080_000.0)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            with self.assertRaises(RuntimeError):
                load_state(target, account, pilot_pnl=-123.0)
            state = new_state(account, pilot_pnl=-123.0)
            save_state(target, state)
            loaded = load_state(target, account)
            self.assertEqual(loaded.pilot_pnl, -123.0)
            self.assertNotIn("7756", target.read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()
