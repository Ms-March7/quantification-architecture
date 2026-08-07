from __future__ import annotations

from datetime import date, timedelta
import tempfile
import unittest
from pathlib import Path

import numpy as np

from tools.replay_discovery_under_v6 import (
    DiscoveryBoundaryError,
    DiscoveryOnlyQlibStore,
    DiscoveryReplayError,
    FROZEN_CANDIDATES,
    validate_source_contract,
)
from tools.replay_discovery_v6_equal5_ensemble import (
    ENSEMBLE_ID,
    ENSEMBLE_POLICY,
    ENSEMBLE_POLICY_SHA256,
    cross_sectional_zscore,
    equal_weight_complete_case,
    policy_sha256,
)


class EqualFiveReplayTests(unittest.TestCase):
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
        dates = tuple(str(date(2020, 1, 1) + timedelta(days=index)) for index in range(50))
        (root / "calendars").mkdir(parents=True)
        (root / "instruments").mkdir(parents=True)
        (root / "calendars" / "day.txt").write_text(
            "\n".join(dates) + "\n", encoding="utf-8"
        )
        (root / "instruments" / "all.txt").write_text(
            f"SH600000\t{dates[0]}\t{dates[-1]}\n", encoding="utf-8"
        )
        values = np.linspace(10.0, 11.0, len(dates), dtype=np.float32)
        for field, field_values in {
            "open": values,
            "high": values * 1.01,
            "low": values * 0.99,
            "close": values,
            "volume": np.full(len(dates), 1_000_000.0, dtype=np.float32),
            "amount": values * 1_000_000.0,
            "change": np.zeros(len(dates), dtype=np.float32),
        }.items():
            cls._write_feature(root, "SH600000", field, field_values)
        return root, dates

    def test_cross_sectional_zscore_is_daily_sample_zscore(self) -> None:
        values = np.asarray([[1.0, 2.0, 3.0], [3.0, 3.0, 3.0]], dtype=np.float32)
        eligible = np.ones_like(values, dtype=bool)
        result = cross_sectional_zscore(values, eligible)
        self.assertAlmostEqual(float(np.mean(result[0])), 0.0, places=7)
        self.assertAlmostEqual(float(np.std(result[0], ddof=1)), 1.0, places=7)
        np.testing.assert_array_equal(result[1], np.zeros(3, dtype=np.float32))

    def test_equal_weight_requires_all_five_without_renormalization(self) -> None:
        eligible = np.ones((2, 3), dtype=bool)
        components = [np.full((2, 3), float(index), dtype=np.float32) for index in range(1, 6)]
        components[4][0, 1] = np.nan
        ensemble, complete = equal_weight_complete_case(components, eligible)
        self.assertEqual(float(ensemble[0, 0]), 3.0)
        self.assertTrue(np.isnan(ensemble[0, 1]))
        self.assertFalse(bool(complete[0, 1]))
        self.assertEqual(float(ensemble[1, 2]), 3.0)

    def test_policy_fixes_exact_five_equal_weights_and_no_search(self) -> None:
        self.assertEqual(set(ENSEMBLE_POLICY["component_ids"]), {
            item.candidate_id for item in FROZEN_CANDIDATES
        })
        self.assertEqual(set(ENSEMBLE_POLICY["weights"].values()), {0.2})
        self.assertAlmostEqual(sum(ENSEMBLE_POLICY["weights"].values()), 1.0)
        self.assertFalse(ENSEMBLE_POLICY["subset_enumeration"])
        self.assertFalse(ENSEMBLE_POLICY["weight_fitting"])
        self.assertFalse(ENSEMBLE_POLICY["result_based_selection"])

    def test_policy_hash_is_deterministic_and_tamper_sensitive(self) -> None:
        self.assertEqual(policy_sha256(ENSEMBLE_POLICY), ENSEMBLE_POLICY_SHA256)
        changed = {**ENSEMBLE_POLICY, "top_k": 11}
        self.assertNotEqual(policy_sha256(changed), ENSEMBLE_POLICY_SHA256)
        self.assertEqual(len(ENSEMBLE_ID), 16)

    def test_source_manifest_hash_mismatch_fails_before_content(self) -> None:
        with self.assertRaisesRegex(DiscoveryReplayError, "pre-registered"):
            validate_source_contract({}, [], "wrong", expected_manifest_sha256="expected")

    def test_shared_reader_hard_rejects_validation_feature_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, dates = self._make_store(directory)
            store = DiscoveryOnlyQlibStore(root, discovery_end=dates[25])
            with self.assertRaises(DiscoveryBoundaryError):
                store._read_feature_slice("SH600000", "close", 0, 26)


if __name__ == "__main__":
    unittest.main()
