from __future__ import annotations

import importlib.util
import sys
import unittest
from collections import namedtuple
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = WORKSPACE_ROOT / "control" / "tools" / "run_d2a04_candidate_closure.py"
SPEC = importlib.util.spec_from_file_location("run_d2a04_candidate_closure", TOOL_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load D2A-04 runner: {TOOL_PATH}")
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)


class D2A04CandidateClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog_hash = RUNNER.sha256_file(RUNNER.CATALOG_PATH)

    def tearDown(self) -> None:
        self.assertEqual(
            RUNNER.sha256_file(RUNNER.CATALOG_PATH),
            self.catalog_hash,
            "tests must not mutate the formal catalog",
        )

    def test_frozen_plan_contains_only_phase_a1_to_a6(self) -> None:
        plan = RUNNER.validate_plan(RUNNER.DEFAULT_PLAN)
        task_ids = {item["task_id"] for item in plan["phase"]["tasks"]}
        self.assertEqual(task_ids, {f"D2A04-A{index}" for index in range(1, 7)})
        self.assertNotIn("D2A04-A7", task_ids)
        self.assertFalse(plan["phase"]["network_requests_authorized"])

    def test_frozen_inputs_validate_without_writes(self) -> None:
        validated = RUNNER.validate_inputs(RUNNER.DEFAULT_PLAN)
        self.assertEqual(
            validated["catalog_sha256_before"],
            "debdbea74d3597dab66efc850517d3ab8ed6062a58229a61303556351883be00",
        )
        self.assertEqual(
            validated["daily"]["content_hash"],
            "319eaff543125fb41a5b5bc2790591df92909233da37cd685244ae923b1bd266",
        )
        self.assertEqual(
            validated["company"]["content_hash"],
            "2bb196be1fb880b6dc17a8a25c3fd945c4e0f494b61648f14255f84e1f16f152",
        )
        self.assertEqual(
            validated["official"]["tree_sha256"],
            "8dde177a6f2d885e693782f1bfbf055f671b0675aaea5952e7a3fdd62f5d6e4e",
        )
        self.assertEqual(
            validated["daily"]["quality"]["metrics"]["row_count"], 5_852_287
        )
        self.assertEqual(
            validated["company"]["quality"]["metrics"]["row_count"], 6_013
        )

    def test_markettype_mapping_matches_verified_manual(self) -> None:
        self.assertEqual(
            RUNNER.MARKETTYPE_EVIDENCE,
            {
                1: {
                    "exchange_mic_claim": "XSHG",
                    "market_segment_claim": "SSE_A",
                    "a_share_scope": True,
                },
                2: {
                    "exchange_mic_claim": "XSHG",
                    "market_segment_claim": "SSE_B",
                    "a_share_scope": False,
                },
                4: {
                    "exchange_mic_claim": "XSHE",
                    "market_segment_claim": "SZSE_A",
                    "a_share_scope": True,
                },
                8: {
                    "exchange_mic_claim": "XSHE",
                    "market_segment_claim": "SZSE_B",
                    "a_share_scope": False,
                },
                16: {
                    "exchange_mic_claim": "XSHE",
                    "market_segment_claim": "CHINEXT_A",
                    "a_share_scope": True,
                },
                32: {
                    "exchange_mic_claim": "XSHG",
                    "market_segment_claim": "STAR_A",
                    "a_share_scope": True,
                },
                64: {
                    "exchange_mic_claim": "XBSE",
                    "market_segment_claim": "BSE_A",
                    "a_share_scope": True,
                },
            },
        )

    def test_manual_version_mismatch_is_fail_visible(self) -> None:
        self.assertEqual(len(RUNNER.MANUAL_DEFINED_DAILY_FIELDS), 21)
        self.assertEqual(
            RUNNER.UNRESOLVED_EXPORT_EXTENSION_FIELDS,
            {"LimitDown", "LimitUp", "LimitStatus"},
        )
        self.assertEqual(len(RUNNER.EXPECTED_SOURCE_FIELDS), 24)

    def test_storage_guard_checks_worst_case_twenty_gib(self) -> None:
        Usage = namedtuple("Usage", "total used free")
        value = {
            "max_new_footprint_gib": 20,
            "minimum_free_fraction": 0.25,
        }
        passing = RUNNER.storage_guard(
            value,
            Usage(total=100 * 1024**3, used=40 * 1024**3, free=60 * 1024**3),
        )
        self.assertEqual(passing["status"], "PASS")
        with self.assertRaisesRegex(
            RUNNER.ClosureError, "would fall below the minimum free fraction"
        ):
            RUNNER.storage_guard(
                value,
                Usage(
                    total=100 * 1024**3,
                    used=60 * 1024**3,
                    free=40 * 1024**3,
                ),
            )

    def test_output_scope_and_run_id_guards(self) -> None:
        self.assertEqual(
            RUNNER.OUTPUT_BASE,
            WORKSPACE_ROOT / "tmp" / "d2a04-core01-to-core06-candidate",
        )
        self.assertEqual(
            RUNNER.REPORT_DIR,
            WORKSPACE_ROOT
            / "reports"
            / "data-governance"
            / "d2a-04-core01-to-core06-candidate-20260728",
        )
        self.assertEqual(RUNNER.validate_run_id("d2a04-phase-a-20260728-v1"), "d2a04-phase-a-20260728-v1")
        for unsafe in ("", "..", "bad/name", "bad\\name", "name "):
            with self.assertRaises(RUNNER.ClosureError):
                RUNNER.validate_run_id(unsafe)

    def test_runner_has_no_network_client_and_core05_is_no_output(self) -> None:
        source = TOOL_PATH.read_text(encoding="utf-8")
        for forbidden in ("import requests", "import urllib", "import httpx"):
            self.assertNotIn(forbidden, source)
        self.assertIn('"blocked_no_output_scope": ["CORE-05"]', source)
        self.assertNotIn("core05/", source.lower())
        self.assertNotIn("D2A04_COMPLETION_GATE_NOT_MET", source)
        self.assertIn("D2A_04_COMPLETION_GATE_NOT_MET", source)


if __name__ == "__main__":
    unittest.main()
