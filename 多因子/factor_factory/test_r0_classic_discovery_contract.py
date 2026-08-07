"""Contract and crash-recovery tests for the classic R0 Discovery runner."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np


PROJECT = Path(__file__).resolve().parents[1]
RUNNER_PATH = PROJECT / "tools/run_r0_classic_discovery.py"
SPEC = importlib.util.spec_from_file_location("run_r0_classic_discovery", RUNNER_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import infrastructure
    raise RuntimeError("cannot load classic Discovery runner")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class R0ClassicDiscoveryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads(runner.CONFIG.read_text(encoding="utf-8"))
        self.catalog = json.loads(runner.CATALOG.read_text(encoding="utf-8"))

    def test_live_catalog_and_execution_contract_are_fail_closed(self) -> None:
        self.assertEqual(runner.sha256_file(runner.CONFIG), runner.EXPECTED_CONFIG_SHA256)
        records = runner.validate_contract(self.config, self.catalog)
        self.assertEqual(len(records), self.catalog["record_count"])
        self.assertEqual(self.config["factor_catalog"]["record_count"], 426)
        self.assertEqual(
            self.config["factor_catalog"]["content_sha256"],
            self.catalog["catalog_content_sha256"],
        )
        self.assertGreaterEqual(len(records), 300)
        self.assertLessEqual(len(records), 600)
        self.assertEqual(
            self.config["dataset"]["content_hash"], runner.EXPECTED_CONTENT_HASH
        )
        self.assertEqual(
            self.config["discovery_window"]["protocol_holdout"],
            ["2022-02-01", "2026-08-03"],
        )
        self.assertTrue(
            all(row["legacy_512_overlap"] == {"exact": False, "semantic": False} for row in records)
        )

    def test_contract_only_cli_reads_no_market_data(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = runner.main(
                [
                    "--run-id",
                    "classic-contract-smoke",
                    "--factor-limit",
                    "2",
                    "--permutations",
                    "0",
                    "--contract-only",
                ]
            )
        self.assertEqual(status, 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["status"], "CONTRACT_PASS__NO_DATA_READ")
        self.assertEqual(receipt["factor_count"], 2)
        self.assertFalse(receipt["validation_consumed"])
        self.assertFalse(receipt["holdout_opened"])

    def test_partial_execution_requires_explicit_smoke_only(self) -> None:
        with self.assertRaisesRegex(SystemExit, "smoke-only"):
            runner.main(
                [
                    "--run-id",
                    "classic-partial-forbidden",
                    "--factor-limit",
                    "2",
                    "--permutations",
                    "0",
                    "--accelerator",
                    "cpu",
                ]
            )

    def test_full_discovery_cannot_override_frozen_permutations(self) -> None:
        with self.assertRaisesRegex(SystemExit, "permutation override"):
            runner.main(
                [
                    "--run-id",
                    "classic-permutation-override-forbidden",
                    "--permutations",
                    "19",
                    "--accelerator",
                    "cpu",
                ]
            )

    def test_horizon_decisions_never_escape_loaded_cutoff(self) -> None:
        dates = tuple(f"2021-01-{day:02d}" for day in range(1, 29)) + tuple(
            f"2021-02-{day:02d}" for day in range(1, 21)
        )
        decisions = runner.effective_decision_indices(
            dates, start="2021-01-01", end="2021-02-19", horizon=10
        )
        self.assertGreater(len(decisions), 0)
        self.assertLessEqual(int(decisions[-1]) + 11, len(dates) - 1)

    def test_walk_forward_has_exact_prefix_purge_and_gap(self) -> None:
        dates = tuple(f"{year:04d}-{month:02d}-{day:02d}" for year in range(2005, 2013) for month in range(1, 13) for day in range(1, 21))
        decisions = np.arange(len(dates), dtype=np.int64)
        folds = runner.build_walk_forward_splits(
            dates,
            decisions,
            train_years=3,
            first_test_year=2009,
            last_test_year=2012,
            purge_sessions=20,
            gap_sessions=10,
            minimum_train_sessions=200,
            minimum_test_sessions=100,
        )
        self.assertGreater(len(folds), 0)
        position = {int(value): index for index, value in enumerate(decisions)}
        for fold in folds:
            self.assertGreater(
                position[int(fold["test"][0])] - position[int(fold["train"][-1])],
                30,
            )
            self.assertTrue(all(dates[int(value)].startswith(str(fold["test_year"])) for value in fold["test"]))

    def test_walk_forward_evaluable_thresholds_are_explicit(self) -> None:
        indices = np.arange(20, dtype=np.int64)
        daily = {
            "indices": indices,
            "ic": np.linspace(-0.1, 0.1, 20),
            "top_excess": np.full(20, 0.01),
            "bottom_excess": np.full(20, -0.01),
            "spread": np.full(20, 0.02),
        }
        schemes = {
            "one": [
                {
                    "test_year": 2020,
                    "train": indices[:12],
                    "test": indices[12:],
                    "train_start": "2019-01-01",
                    "train_end": "2019-12-31",
                    "test_start": "2020-01-01",
                    "test_end": "2020-12-31",
                }
            ]
        }
        accepted = runner.summarize_walk_forward(
            daily,
            schemes,
            turnover_proxy=0.5,
            costs=[30.0],
            minimum_train_sessions=12,
            minimum_test_sessions=8,
        )
        rejected = runner.summarize_walk_forward(
            daily,
            schemes,
            turnover_proxy=0.5,
            costs=[30.0],
            minimum_train_sessions=13,
            minimum_test_sessions=8,
        )
        self.assertEqual(accepted["schemes"]["one"]["summary"]["fold_count"], 1)
        self.assertEqual(rejected["schemes"]["one"]["summary"]["fold_count"], 0)

    def test_checkpoint_resume_is_atomic_exact_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary) / "stage"
            fingerprint = {
                "config_sha256": "a" * 64,
                "factor_count": 2,
                "code_sha256": {
                    "engine": "c" * 64,
                    "evaluation": "d" * 64,
                    "dense_loader": "e" * 64,
                    "total_return_projection": "f" * 64,
                },
            }
            store = runner.CheckpointStore(stage, ("factor-a", "factor-b"), fingerprint)
            store.initialize()
            store.commit_factor(
                0,
                {
                    "factor_index": 0,
                    "factor_id": "factor-a",
                    "mechanical": {"completed": True},
                },
                np.asarray([0.1, np.nan, 0.3], dtype=np.float32),
            )
            resumed = runner.CheckpointStore(stage, ("factor-a", "factor-b"), fingerprint)
            resumed.initialize()
            records = resumed.load_prefix()
            self.assertEqual([row["factor_id"] for row in records], ["factor-a"])
            self.assertRegex(records[0]["correlation_sketch"]["sha256"], r"^[0-9a-f]{64}$")
            changed_fingerprint = json.loads(json.dumps(fingerprint))
            changed_fingerprint["code_sha256"]["engine"] = "b" * 64
            changed = runner.CheckpointStore(stage, ("factor-a", "factor-b"), changed_fingerprint)
            with self.assertRaisesRegex(RuntimeError, "fingerprint"):
                changed.initialize()

    def test_run_lock_and_orphan_stage_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with runner.RunLock(root / "run.lock"):
                with self.assertRaisesRegex(RuntimeError, "already active"):
                    with runner.RunLock(root / "run.lock"):
                        pass
            stage = root / "orphan"
            stage.mkdir()
            (stage / "partial.bin").write_bytes(b"partial")
            store = runner.CheckpointStore(stage, ("factor-a",), {"run_id": "orphan"})
            with self.assertRaisesRegex(RuntimeError, "no valid RUNNING or COMMITTED"):
                store.initialize()

    def test_tail_membership_respects_orientation(self) -> None:
        ranked = np.asarray([[0.1, 0.2, 0.8, 0.9]], dtype=np.float32)
        dates = np.asarray([0], dtype=np.int64)
        high = runner._tail_membership(ranked, dates, 0.5, top=True)
        low = runner._tail_membership(ranked, dates, 0.5, top=False)
        self.assertEqual(high.tolist(), [[False, False, True, True]])
        self.assertEqual(low.tolist(), [[True, True, False, False]])

    def test_tail_weights_split_boundary_ties_to_exact_mass(self) -> None:
        ranked = np.asarray([[0.1, 0.8, 0.8, 0.8, 0.9]], dtype=np.float32)
        weights = runner._tail_weights_matrix(
            ranked, np.asarray([0], dtype=np.int64), 0.4, top=True
        )
        self.assertAlmostEqual(float(weights.sum()), 2.0)
        self.assertAlmostEqual(float(weights[0, 4]), 1.0)
        self.assertTrue(np.allclose(weights[0, 1:4], 1.0 / 3.0))

    def test_company_action_survivor_requires_scale_and_volume_safety(self) -> None:
        safe = runner.runtime_action_safety(
            {"kind": "moving_average_ratio", "params": {}, "company_action_sensitivity": "RAW_PRICE_CROSS_PERIOD__SPLIT_SENSITIVE"}
        )
        rebase = runner.runtime_action_safety(
            {"kind": "supplier_dsl", "params": {}, "company_action_sensitivity": "DIAGNOSTIC_ONLY__ARBITRARY_PER_SECURITY_REBASE_SENSITIVE"}
        )
        volume = runner.runtime_action_safety(
            {"kind": "return_volume_corr", "params": {}, "company_action_sensitivity": "TOTAL_RETURN_WITH_VOLUME__VOLUME_SPLIT_SENSITIVE"}
        )
        macd = runner.runtime_action_safety(
            {"kind": "macd", "params": {}, "company_action_sensitivity": "RAW_PRICE_CROSS_PERIOD__SPLIT_SENSITIVE"}
        )
        self.assertTrue(safe["survivor_eligible"])
        self.assertFalse(rebase["survivor_eligible"])
        self.assertFalse(volume["survivor_eligible"])
        self.assertFalse(macd["survivor_eligible"])

    def test_negative_orientation_cost_uses_bottom_turnover_and_opened_board_stress(self) -> None:
        metrics = runner.FactorMetrics(
            n_dates=200,
            n_observations=1000,
            coverage=1.0,
            mean_ic=-0.05,
            ic_std=0.1,
            icir=-0.5,
            positive_ic_ratio=0.4,
            positive_fold_ratio=0.4,
            worst_fold_ic=-0.1,
            top_excess_mean=-0.01,
            bottom_excess_mean=0.02,
            long_short_spread_mean=-0.03,
            turnover=0.3,
            top_tail_turnover=0.3,
            bottom_tail_turnover=0.7,
            cost_adjusted_top_excess=-0.01,
            stressed_cost_adjusted_top_excess=-0.01,
            p_value=0.01,
        )
        rows = [
            {
                "mechanical": {"completed": True},
                "primary_horizon_sessions": 10,
                "horizons": {"10": metrics.to_dict()},
                "execution_feasibility": {
                    "reopened_limit_board_round_trip_event_rate": 0.5
                },
                "cost": {
                    "round_trip_cost_bps": ["30"],
                    "opened_limit_board_extra_slippage_bps": 20.0,
                },
            }
        ]
        runner.apply_family_by(rows, [10])
        self.assertEqual(rows[0]["cost"]["orientation_tail"], "LOW_FACTOR")
        self.assertAlmostEqual(rows[0]["cost"]["orientation_tail_turnover"], 0.7)
        self.assertAlmostEqual(
            rows[0]["cost"]["oriented_top_excess_after_cost"]["30"],
            0.02 - 30.0 / 10_000.0 * 0.7 - 20.0 / 10_000.0 * 0.5,
        )

    def test_correlation_clustering_is_pairwise_complete_and_direction_invariant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            sketch_dir = stage / "correlation_sketches"
            sketch_dir.mkdir()
            arrays = {
                "a": np.asarray([1.0, 2.0, 3.0, np.nan, 5.0, 6.0]),
                "b": np.asarray([-1.0, -2.0, -3.0, np.nan, -5.0, -6.0]),
                "c": np.asarray([1.0, -1.0, 1.0, -1.0, 1.0, -1.0]),
            }
            records = []
            for factor_id, values in arrays.items():
                path = sketch_dir / f"{factor_id}.npy"
                runner.atomic_npy(path, values)
                records.append(
                    {
                        "factor_id": factor_id,
                        "mechanical": {"completed": True},
                        "correlation_sketch": {
                            "path": str(path.relative_to(stage)).replace("\\", "/"),
                            "sha256": runner.sha256_file(path),
                        },
                    }
                )
            result = runner.correlation_clusters(records, stage, threshold=0.90, minimum_pairs=4)
            groups = [set(row["factor_ids"]) for row in result["clusters"]]
            self.assertIn({"a", "b"}, groups)
            self.assertIn({"c"}, groups)


if __name__ == "__main__":
    unittest.main()
