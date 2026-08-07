from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from factor_factory.ensemble_first import (
    EnsembleFirstPolicy,
    build_selected_ensemble,
    select_single_equal_weight_ensemble,
)
from factor_factory.backtest import CostModel
from factor_factory.expressions import Expression
from factor_factory.generation import make_candidate
from factor_factory.pipeline import FactorFactory, FactorFactoryConfig
from factor_factory.registry import ResearchRegistry
from factor_factory.selection import SelectionGates
from factor_factory.synthetic import planted_signal_panel


class EnsembleFirstSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        close = Expression.terminal("close")
        self.candidates = [
            make_candidate(Expression("delta", (close,), window=window))
            for window in (1, 2, 3, 4)
        ]
        rng = np.random.default_rng(41)
        first = rng.normal(size=(12, 8)).astype(np.float32)
        redundant = (first + 1e-4 * rng.normal(size=first.shape)).astype(np.float32)
        independent = rng.normal(size=first.shape).astype(np.float32)
        reserve = rng.normal(size=first.shape).astype(np.float32)
        self.values = {
            self.candidates[0].candidate_id: first,
            self.candidates[1].candidate_id: redundant,
            self.candidates[2].candidate_id: independent,
            self.candidates[3].candidate_id: reserve,
        }
        self.scores = {
            candidate.candidate_id: float(10 - index)
            for index, candidate in enumerate(self.candidates)
        }
        self.mask = np.ones(first.shape, dtype=bool)
        self.discovery = np.arange(0, 8, dtype=np.int64)
        self.policy = EnsembleFirstPolicy(
            component_count=2,
            candidate_cap=4,
            correlation_cap=0.80,
            minimum_joint_coverage=0.90,
        )

    def select(self, candidates=None, values=None):
        return select_single_equal_weight_ensemble(
            self.candidates if candidates is None else candidates,
            self.scores,
            self.values if values is None else values,
            self.discovery,
            self.mask,
            self.policy,
        )

    def test_one_deterministic_greedy_equal_weight_hypothesis_is_frozen(self) -> None:
        selected = self.select()
        self.assertTrue(selected.ready)
        self.assertEqual(
            selected.selected_component_ids,
            (self.candidates[0].candidate_id, self.candidates[2].candidate_id),
        )
        self.assertEqual(selected.definition.weights, (0.5, 0.5))
        audit = selected.to_audit_dict()
        self.assertEqual(audit["defined_ensemble_count"], 1)
        self.assertEqual(audit["validation_hypothesis_count"], 1)
        self.assertEqual(audit["subset_enumeration_count"], 0)
        self.assertFalse(audit["policy"]["subset_enumeration_allowed"])
        decisions = {
            row["candidate_id"]: row["decision"] for row in audit["selection_trace"]
        }
        self.assertEqual(
            decisions[self.candidates[1].candidate_id], "REJECTED_CORRELATION_CAP"
        )

    def test_input_order_and_post_discovery_values_cannot_change_freeze(self) -> None:
        baseline = self.select()
        changed = {identifier: matrix.copy() for identifier, matrix in self.values.items()}
        for matrix in changed.values():
            matrix[8:] = np.arange(matrix[8:].size, dtype=np.float32).reshape(matrix[8:].shape)
        reordered = self.select(candidates=list(reversed(self.candidates)), values=changed)
        self.assertEqual(reordered.selected_component_ids, baseline.selected_component_ids)
        self.assertEqual(reordered.selection_hash, baseline.selection_hash)

    def test_incomplete_greedy_pass_does_not_enumerate_alternative_subsets(self) -> None:
        policy = EnsembleFirstPolicy(
            component_count=3,
            candidate_cap=3,
            correlation_cap=0.10,
            minimum_joint_coverage=0.90,
        )
        result = select_single_equal_weight_ensemble(
            self.candidates[:3],
            self.scores,
            self.values,
            self.discovery,
            self.mask,
            policy,
        )
        self.assertFalse(result.ready)
        self.assertIsNone(result.definition)
        self.assertEqual(result.to_audit_dict()["subset_enumeration_count"], 0)

    def test_selected_definition_builds_only_the_frozen_components(self) -> None:
        selected = self.select()
        ensemble = build_selected_ensemble(selected, self.values, self.mask)
        self.assertEqual(ensemble.shape, self.mask.shape)
        self.assertTrue(np.isfinite(ensemble).all())
        changed_unselected = {identifier: matrix.copy() for identifier, matrix in self.values.items()}
        changed_unselected[self.candidates[1].candidate_id][:] = np.nan
        changed_unselected[self.candidates[3].candidate_id][:] = np.nan
        np.testing.assert_allclose(
            build_selected_ensemble(selected, changed_unselected, self.mask),
            ensemble,
            equal_nan=True,
        )

    def test_policy_forbids_multiple_ensemble_trials(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one"):
            EnsembleFirstPolicy(
                component_count=2,
                candidate_cap=4,
                correlation_cap=0.8,
                minimum_joint_coverage=0.9,
                maximum_ensemble_trials=2,
            )


class EnsembleFirstPipelineTests(unittest.TestCase):
    @staticmethod
    def relaxed_gates() -> SelectionGates:
        return SelectionGates(
            minimum_evaluation_dates=20,
            minimum_coverage=0.0,
            minimum_evaluable_date_ratio=0.0,
            minimum_fold_evaluable_ratio=0.0,
            minimum_mean_ic=-1.0,
            minimum_icir=-10.0,
            minimum_positive_fold_ratio=0.0,
            minimum_worst_fold_ic=-1.0,
            maximum_q_value=1.0,
            require_positive_stressed_return=False,
        )

    def test_v7_validation_tests_only_one_discovery_frozen_ensemble(self) -> None:
        panel = planted_signal_panel(dates=220, assets=16, seed=43)
        config = FactorFactoryConfig(
            protocol_version=7,
            protocol_id="v7-ensemble-first-test",
            seed=43,
            population_size=16,
            generations=0,
            elite_size=4,
            maximum_selected=3,
            minimum_ensemble_components=2,
            maximum_ensemble_components=3,
            frozen_ensemble_component_count=2,
            maximum_ensemble_trials=1,
            discovery_first_selection=True,
            ensemble_first_validation=True,
            maximum_discovery_event_candidates=16,
            minimum_assets=8,
            permutations=9,
            selection_gates=self.relaxed_gates(),
            correlation_cap=0.999999,
            minimum_ensemble_coverage=0.0001,
            minimum_backtest_sharpe=-999.0,
            maximum_backtest_drawdown=-1.0,
            require_positive_backtest_return=False,
            require_positive_stressed_backtest_return=False,
            cost_model=CostModel(
                commission_bps=0.0,
                minimum_commission=0.0,
                sell_stamp_tax_bps=0.0,
                slippage_bps=0.0,
                lot_size=1,
                minimum_trade_notional=0.0,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            result = FactorFactory(config).run(panel, directory)
            self.assertEqual(result.status, "FROZEN_AWAITING_HOLDOUT")
            manifest = json.loads(
                (Path(directory) / "manifest.json").read_text(encoding="utf-8")
            )
            protocol = manifest["selection_protocol"]
            self.assertTrue(protocol["ensemble_first_validation"])
            self.assertEqual(protocol["validation_component_test_count"], 0)
            self.assertEqual(protocol["validation_candidate_count"], 0)
            self.assertEqual(protocol["validation_hypothesis_count"], 1)
            self.assertFalse(protocol["validation_component_event_gates_required"])
            self.assertFalse(protocol["validation_reselection_allowed"])
            selection = protocol["ensemble_first_selection"]
            self.assertEqual(selection["defined_ensemble_count"], 1)
            self.assertEqual(selection["subset_enumeration_count"], 0)
            self.assertEqual(selection["policy"]["maximum_ensemble_trials"], 1)
            self.assertTrue((Path(directory) / "discovery-freeze.json").is_file())
            self.assertIn("discovery-freeze.json", manifest["artifacts"])
            backtests = json.loads(
                (Path(directory) / "backtests.json").read_text(encoding="utf-8")
            )
            self.assertEqual(backtests["validation_components"], {})
            self.assertIsNotNone(backtests["validation_ensemble"])
            rows = [
                json.loads(line)
                for line in (Path(directory) / "candidates.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            components = [row for row in rows if row["status"] == "ENSEMBLE_COMPONENT"]
            self.assertEqual(len(components), 2)
            self.assertTrue(all(row["validation"] is None for row in components))
            self.assertTrue(all(row["validation_backtest"] is None for row in components))
            self.assertFalse(manifest["ensemble"]["holdout_was_opened"])
            self.assertIsNone(manifest["ensemble"]["holdout_metrics"])
            discovery_freeze = Path(directory) / "discovery-freeze.json"
            original = discovery_freeze.read_bytes()
            discovery_freeze.write_bytes(original + b"\n")
            with self.assertRaisesRegex(RuntimeError, "size mismatch"):
                ResearchRegistry(directory).verify_completed()

    def test_v7_config_requires_single_trial_discovery_first_contract(self) -> None:
        with self.assertRaisesRegex(ValueError, "discovery_first_selection"):
            FactorFactoryConfig(
                protocol_version=7,
                ensemble_first_validation=True,
            )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            FactorFactoryConfig(
                protocol_version=7,
                discovery_first_selection=True,
                ensemble_first_validation=True,
                maximum_ensemble_trials=2,
            )


if __name__ == "__main__":
    unittest.main()
