from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from research_reporting.reporting import IntegrityError, build_report, write_report


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _committed_fixture(root: Path, *, selected: list[object] | None = None) -> Path:
    run = root / "fixture-run"
    run.mkdir()
    candidates = [
        {
            "candidate_id": "weak",
            "formula": "rank($close)",
            "generation": 0,
            "origin": "seed",
            "status": "REJECTED",
            "discovery": {"mean_ic": 0.03, "icir": 0.2, "cost_adjusted_top_excess": 0.01},
            "validation": {"mean_ic": 0.01, "icir": 0.1, "cost_adjusted_top_excess": -0.01},
            "validation_failures": ["double_cost_stress", "icir"],
            "holdout_failures": [],
            "validation_backtest": {
                "passed": False,
                "median_total_return": -0.05,
                "median_sharpe": -0.2,
                "worst_max_drawdown": -0.3,
                "double_cost_stress": {"total_return": -0.08, "max_drawdown": -0.35},
            },
        },
        {
            "candidate_id": "top",
            "formula": "ts_mean($close, 5)",
            "generation": 1,
            "origin": "mutation",
            "status": "VALIDATION_PASSED",
            "discovery": {"mean_ic": 0.02, "icir": 0.3, "cost_adjusted_top_excess": 0.02},
            "validation": {
                "mean_ic": 0.04,
                "icir": 0.5,
                "cost_adjusted_top_excess": 0.015,
                "stressed_cost_adjusted_top_excess": 0.005,
            },
            "validation_failures": ["double_cost_stress"],
            "holdout_failures": [],
            "validation_backtest": {
                "passed": True,
                "median_total_return": 0.12,
                "median_sharpe": 0.8,
                "worst_max_drawdown": -0.18,
                "double_cost_stress": {"total_return": 0.03, "max_drawdown": -0.2},
            },
        },
    ]
    candidate_path = run / "candidates.jsonl"
    candidate_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in candidates),
        encoding="utf-8",
    )
    _write_json(run / "selected.json", selected if selected is not None else [])
    _write_json(run / "backtests.json", {"validation_components": {}})
    artifacts = {}
    for name in ("candidates.jsonl", "selected.json", "backtests.json"):
        path = run / name
        artifacts[name] = {"bytes": path.stat().st_size, "sha256": _sha(path)}
    manifest = {
        "artifacts": artifacts,
        "attempt_terminal_state": "NO_FACTOR" if not selected else "RESULT_COMMITTED",
        "created_at_utc": "2026-07-19T00:00:00+00:00",
        "effective_promotion_ceiling": "RESEARCH_ONLY",
        "safety_ceiling": "SHADOW_ONLY",
        "engine": "fixture-engine",
        "config": {
            "accelerator": "gpu",
            "gpu_column_chunk": 128,
            "protocol_id": "fixture-v1",
            "protocol_version": 1,
        },
        "ensemble": {
            "qualified": bool(selected),
            "holdout_was_opened": bool(selected),
            "validation_failures": [] if selected else ["insufficient_ensemble_components"],
            "holdout_failures": [],
        },
        "runtime": {"python": "3.11"},
        "research_attempt": {
            "validation_evidence": {
                "runtime_signature": {
                    "numerical_backend": {
                        "accelerator": "gpu",
                        "array_library": "cupy",
                        "device_name": "Fixture GPU",
                    }
                }
            }
        },
    }
    _write_json(run / "manifest.json", manifest)
    _write_json(
        run / "COMMITTED.json",
        {
            "committed_at_utc": "2026-07-19T00:00:01+00:00",
            "manifest_sha256": _sha(run / "manifest.json"),
            "registry_schema": 2,
        },
    )
    return run


def _discovery_first_fixture(root: Path) -> Path:
    run = root / "discovery-first-run"
    run.mkdir()
    discovery_event = {
        "passed": False,
        "median_total_return": -0.12,
        "median_sharpe": -0.3,
        "worst_max_drawdown": -0.4,
        "double_cost_stress": {"total_return": -0.2, "max_drawdown": -0.45},
    }
    candidates = [
        {
            "candidate_id": "event-failed",
            "formula": "ts_mean($close, 5)",
            "generation": 1,
            "origin": "mutation",
            "status": "REJECTED",
            "discovery": {
                "mean_ic": 0.03,
                "icir": 0.4,
                "cost_adjusted_top_excess": 0.002,
                "p_value": 1.0,
                "q_value": 1.0,
            },
            "validation": None,
            "holdout": None,
            "discovery_backtest": discovery_event,
            # V6 stores Discovery-first failures in this legacy field.
            "validation_failures": ["discovery_event_phase_gate"],
            "holdout_failures": [],
            "validation_backtest": None,
            "holdout_backtest": None,
        },
        {
            "candidate_id": "numeric-failed",
            "formula": "rank($open)",
            "generation": 0,
            "origin": "seed",
            "status": "REJECTED",
            "discovery": {
                "mean_ic": 0.005,
                "icir": 0.02,
                "cost_adjusted_top_excess": -0.01,
                "p_value": 1.0,
                "q_value": 1.0,
            },
            "validation": None,
            "holdout": None,
            "discovery_backtest": None,
            "validation_failures": ["mean_ic", "double_cost_stress"],
            "holdout_failures": [],
            "validation_backtest": None,
            "holdout_backtest": None,
        },
    ]
    candidate_path = run / "candidates.jsonl"
    candidate_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in candidates),
        encoding="utf-8",
    )
    _write_json(run / "selected.json", [])
    _write_json(
        run / "backtests.json",
        {
            "discovery_components": {"event-failed": discovery_event},
            "validation_components": {},
            "validation_ensemble": None,
            "holdout_ensemble": None,
        },
    )
    artifacts = {}
    for name in ("candidates.jsonl", "selected.json", "backtests.json"):
        path = run / name
        artifacts[name] = {"bytes": path.stat().st_size, "sha256": _sha(path)}
    manifest = {
        "artifacts": artifacts,
        "attempt_terminal_state": "NO_FACTOR",
        "created_at_utc": "2026-07-19T00:00:00+00:00",
        "effective_promotion_ceiling": "RESEARCH_ONLY",
        "safety_ceiling": "SHADOW_ONLY",
        "engine": "factor_factory_v6_discovery_first_gpu",
        "config": {
            "accelerator": "gpu",
            "discovery_first_selection": True,
            "permutations": 19_999,
            "selection_gates": {"maximum_q_value": 0.1},
            "protocol_id": "fixture-v6",
            "protocol_version": 6,
        },
        "selection_protocol": {
            "discovery_first_selection": True,
            "discovery_event_backtest_count": 1,
            "prevalidation_component_ids": [],
            "validation_candidate_count": 0,
            "validation_reselection_allowed": False,
        },
        "ensemble": {
            "qualified": False,
            "validation_metrics": None,
            "validation_failures": ["insufficient_ensemble_components"],
            "holdout_metrics": None,
            "holdout_failures": [],
            "holdout_was_opened": False,
        },
        # This governance state must not be mistaken for candidate metrics.
        "research_attempt": {"state": "VALIDATION_CONSUMED"},
    }
    _write_json(run / "manifest.json", manifest)
    _write_json(
        run / "COMMITTED.json",
        {
            "committed_at_utc": "2026-07-19T00:00:01+00:00",
            "manifest_sha256": _sha(run / "manifest.json"),
            "registry_schema": 2,
        },
    )
    return run


class ReportingTests(unittest.TestCase):
    def test_committed_report_counts_ranks_and_gpu_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = _committed_fixture(root)
            json_path, markdown_path, report = write_report(run, root / "reports", top_n=5)
            self.assertTrue(json_path.is_file())
            self.assertTrue(markdown_path.is_file())
            self.assertEqual(report["integrity"]["status"], "PASSED")
            self.assertEqual(report["summary"]["candidate_count"], 2)
            self.assertEqual(report["candidate_gate_failures"]["validation"]["double_cost_stress"], 2)
            self.assertEqual(report["top_candidate_stage"], "validation")
            self.assertEqual(report["top_candidates"][0]["candidate_id"], "top")
            self.assertEqual(report["summary"]["event_backtest_count"], 2)
            backend = report["runtime_evidence"]["runtime_signature"]["numerical_backend"]
            self.assertEqual(backend["device_name"], "Fixture GPU")
            self.assertIn("未发现可晋级因子", report["outcome"]["verdict_cn"])
            self.assertFalse(report["outcome"]["factor_promoted"])
            self.assertIn("零入选或未通过研究门绝不记为成功", markdown_path.read_text(encoding="utf-8"))

    def test_discovery_first_report_uses_protocol_phases_and_no_significance_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = _discovery_first_fixture(root)
            _, markdown_path, report = write_report(run, root / "reports", top_n=5)

            candidate_gates = report["candidate_gate_failures"]
            self.assertEqual(candidate_gates["validation"], {})
            self.assertEqual(
                candidate_gates["discovery_numeric_screening"],
                {"double_cost_stress": 1, "mean_ic": 1},
            )
            self.assertEqual(
                candidate_gates["discovery_event_gate"],
                {"discovery_event_phase_gate": 1},
            )
            self.assertEqual(
                report["ensemble_gate_failures"]["discovery_event_gate"],
                {"insufficient_ensemble_components": 1},
            )
            semantics = report["stage_semantics"]
            self.assertEqual(semantics["discovery_numeric_screening"]["status"], "CONSUMED")
            self.assertEqual(semantics["discovery_event_gate"]["status"], "CONSUMED")
            self.assertEqual(semantics["validation"]["status"], "NOT_CONSUMED")
            self.assertEqual(semantics["validation"]["candidate_count"], 0)
            self.assertEqual(semantics["validation"]["registry_state"], "VALIDATION_CONSUMED")
            self.assertEqual(semantics["holdout"]["status"], "NOT_OPENED")

            self.assertEqual(report["top_candidate_stage"], "discovery")
            self.assertEqual(
                report["top_candidate_scope"]["phase"],
                "discovery_numeric_screening",
            )
            self.assertFalse(
                report["top_candidate_scope"]["ranking_is_significance_claim"]
            )
            for item in report["top_candidates"]:
                test = item["statistical_test"]
                self.assertEqual(test["permutations"], 0)
                self.assertEqual(test["status"], "NOT_RUN")
                self.assertEqual(test["p_value"], 1.0)
                self.assertEqual(test["q_value"], 1.0)
                self.assertFalse(test["supports_significance_claim"])
            self.assertEqual(
                {item["stage"] for item in report["event_backtests"]},
                {"discovery_event_gate"},
            )

            markdown = markdown_path.read_bytes().decode("utf-8", errors="strict")
            self.assertIn("Discovery 数值筛选", markdown)
            self.assertIn("Discovery 事件门", markdown)
            self.assertIn("NOT_CONSUMED（未消费）", markdown)
            self.assertIn("NOT_OPENED（未打开）", markdown)
            self.assertIn("permutations=0", markdown)
            self.assertIn("不是统计显著性排名", markdown)
            self.assertIn("未检验", markdown)

    def test_tampered_artifact_fails_closed_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = _committed_fixture(root)
            with (run / "candidates.jsonl").open("a", encoding="utf-8") as handle:
                handle.write("{}\n")
            output = root / "reports"
            with self.assertRaises(IntegrityError):
                write_report(run, output)
            self.assertFalse(output.exists())

    def test_tampered_manifest_fails_before_manifest_is_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run = _committed_fixture(Path(temp))
            (run / "manifest.json").write_text("not-json\n", encoding="utf-8")
            with self.assertRaisesRegex(IntegrityError, "SHA-256"):
                build_report(run)

    def test_zero_selected_is_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report = build_report(_committed_fixture(Path(temp)))
            self.assertEqual(report["summary"]["selected_count"], 0)
            self.assertEqual(report["outcome"]["verdict_code"], "COMMITTED_NO_PROMOTION")
            self.assertFalse(report["outcome"]["research_qualified"])
            self.assertFalse(report["outcome"]["factor_promoted"])

    def test_aborted_run_ignores_partial_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "aborted"
            run.mkdir()
            (run / "candidates.jsonl").write_text("not-json\n", encoding="utf-8")
            _write_json(
                run / "ABORTED.json",
                {
                    "state": "ABORTED_TECHNICAL",
                    "reason": "USER_CANCELLED",
                    "cached_factor_matrices": 12,
                    "cached_factor_bytes": 4096,
                    "holdout_was_opened": False,
                    "result_committed": False,
                },
            )
            report = build_report(run)
            self.assertEqual(report["run_kind"], "ABORTED")
            self.assertIsNone(report["summary"]["candidate_count"])
            self.assertEqual(report["summary"]["materialized_cache_matrices"], 12)
            self.assertFalse(report["outcome"]["artifact_committed"])
            self.assertEqual(report["top_candidates"], [])


if __name__ == "__main__":
    unittest.main()
