from __future__ import annotations

from datetime import date, timedelta
import tempfile
import unittest
from pathlib import Path

import numpy as np

from factor_factory.pipeline import FactorFactory, FactorFactoryConfig
from tools.replay_discovery_under_v6 import (
    BASELINE_RETENTION_K,
    BASELINE_TOP_K,
    DiscoveryBoundaryError,
    DiscoveryOnlyQlibStore,
    DiscoveryReplayError,
    EXPECTED_DISCOVERY_DATES,
    EXPECTED_DISCOVERY_END,
    EXPECTED_DISCOVERY_START,
    EXPECTED_ENGINE,
    EXPECTED_PROTOCOL_ID,
    EXPECTED_SNAPSHOT_SHA256,
    FROZEN_CANDIDATES,
    REPLAY_RETENTION_K,
    REPLAY_TOP_K,
    _replay_config,
    validate_source_contract,
)


class FrozenReplayTests(unittest.TestCase):
    @staticmethod
    def _write_feature(root: Path, symbol: str, field: str, values: np.ndarray) -> None:
        directory = root / "features" / symbol.lower()
        directory.mkdir(parents=True, exist_ok=True)
        payload = np.concatenate(
            (np.array([0.0], dtype="<f4"), np.asarray(values, dtype="<f4"))
        )
        payload.tofile(directory / f"{field}.day.bin")

    @classmethod
    def _make_store(cls, directory: str) -> tuple[Path, tuple[str, ...]]:
        root = Path(directory) / "qlib"
        dates = tuple(str(date(2020, 1, 1) + timedelta(days=index)) for index in range(90))
        (root / "calendars").mkdir(parents=True)
        (root / "instruments").mkdir(parents=True)
        (root / "calendars" / "day.txt").write_text(
            "\n".join(dates) + "\n", encoding="utf-8"
        )
        symbols = ("SH600000", "SZ000001")
        (root / "instruments" / "all.txt").write_text(
            "".join(f"{symbol}\t{dates[0]}\t{dates[-1]}\n" for symbol in symbols),
            encoding="utf-8",
        )
        values = np.linspace(10.0, 12.0, len(dates), dtype=np.float32)
        for symbol in symbols:
            for field, field_values in {
                "open": values,
                "high": values * 1.01,
                "low": values * 0.99,
                "close": values,
                "volume": np.full(len(dates), 1_000_000.0, dtype=np.float32),
                "amount": values * 1_000_000.0,
                "change": np.zeros(len(dates), dtype=np.float32),
            }.items():
                cls._write_feature(root, symbol, field, field_values)
        return root, dates

    @staticmethod
    def _source_contract() -> tuple[dict, list[dict]]:
        manifest = {
            "engine": EXPECTED_ENGINE,
            "config": {
                "protocol_id": EXPECTED_PROTOCOL_ID,
                "protocol_version": 6,
                "discovery_first_selection": True,
                "top_k": BASELINE_TOP_K,
                "retention_k": BASELINE_RETENTION_K,
            },
            "splits": {
                "discovery": {
                    "start": EXPECTED_DISCOVERY_START,
                    "end": EXPECTED_DISCOVERY_END,
                    "count": EXPECTED_DISCOVERY_DATES,
                }
            },
            "panel": {"metadata": {"snapshot_sha256": EXPECTED_SNAPSHOT_SHA256}},
            "selection_protocol": {
                "discovery_event_backtest_count": 5,
                "validation_candidate_count": 0,
                "prevalidation_component_ids": [],
            },
            "ensemble": {"holdout_was_opened": False},
        }
        rows = [
            {
                "candidate_id": item.candidate_id,
                "formula": item.formula,
                "discovery_backtest": {"passed": False},
                "validation": None,
                "validation_backtest": None,
                "holdout": None,
                "holdout_backtest": None,
            }
            for item in FROZEN_CANDIDATES
        ]
        return manifest, rows

    def test_frozen_formula_ids_are_stable(self) -> None:
        self.assertEqual(len(FROZEN_CANDIDATES), 5)
        self.assertEqual(len({item.candidate_id for item in FROZEN_CANDIDATES}), 5)
        self.assertTrue(all(item.expression.canonical() == item.formula for item in FROZEN_CANDIDATES))

    def test_replay_changes_only_pre_registered_breadth(self) -> None:
        source = FactorFactoryConfig(
            protocol_version=6,
            protocol_id=EXPECTED_PROTOCOL_ID,
            discovery_first_selection=True,
            top_k=BASELINE_TOP_K,
            retention_k=BASELINE_RETENTION_K,
            horizon=5,
            rebalance_phase_count=5,
            initial_cash=100_000.0,
            require_no_unverified_terminal_events=True,
        )
        replay = _replay_config(source)
        self.assertEqual(replay.top_k, REPLAY_TOP_K)
        self.assertEqual(replay.retention_k, REPLAY_RETENTION_K)
        self.assertEqual(replay.cost_model, source.cost_model)
        self.assertEqual(replay.selection_gates, source.selection_gates)
        self.assertEqual(replay.maximum_backtest_drawdown, source.maximum_backtest_drawdown)

    def test_source_contract_rejects_validation_content(self) -> None:
        manifest, rows = self._source_contract()
        validate_source_contract(manifest, rows, "test", expected_manifest_sha256="test")
        rows[0]["validation"] = {"mean_ic": 1.0}
        with self.assertRaisesRegex(DiscoveryReplayError, "Validation/Holdout"):
            validate_source_contract(manifest, rows, "test", expected_manifest_sha256="test")

    def test_source_contract_rejects_discovery_boundary_drift(self) -> None:
        manifest, rows = self._source_contract()
        manifest["splits"]["discovery"]["end"] = "2024-10-30"
        with self.assertRaises(DiscoveryBoundaryError):
            validate_source_contract(manifest, rows, "test", expected_manifest_sha256="test")

    def test_feature_reader_rejects_any_index_after_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, dates = self._make_store(directory)
            store = DiscoveryOnlyQlibStore(root, discovery_end=dates[40])
            with self.assertRaises(DiscoveryBoundaryError):
                store._read_feature_slice("SH600000", "close", 0, 41)

    def test_post_discovery_feature_change_cannot_affect_bounded_panel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, dates = self._make_store(directory)
            first_store = DiscoveryOnlyQlibStore(root, discovery_end=dates[40])
            first = first_store.load_panel(
                start_date=dates[10],
                end_date=dates[40],
                minimum_history_dates=5,
            )
            first_hash = FactorFactory._panel_cache_fingerprint(first)
            path = root / "features" / "sh600000" / "close.day.bin"
            raw = np.fromfile(path, dtype="<f4")
            raw[56] = 999.0  # Header is element zero; this is global date index 55.
            raw.tofile(path)
            second_store = DiscoveryOnlyQlibStore(root, discovery_end=dates[40])
            second = second_store.load_panel(
                start_date=dates[10],
                end_date=dates[40],
                minimum_history_dates=5,
            )
            second_hash = FactorFactory._panel_cache_fingerprint(second)
            self.assertEqual(first_hash, second_hash)
            self.assertEqual(first_store.maximum_feature_end_index, 40)
            self.assertEqual(second_store.maximum_feature_end_index, 40)
            self.assertTrue(second_store.access_evidence()["boundary_enforced"])


if __name__ == "__main__":
    unittest.main()
