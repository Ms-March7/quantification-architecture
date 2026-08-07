from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = (
    WORKSPACE_ROOT
    / "control"
    / "tools"
    / "parse_csmar_idx_chgsmp_zero_loss_candidate.py"
)
SPEC = importlib.util.spec_from_file_location(
    "parse_csmar_idx_chgsmp_zero_loss_candidate",
    TOOL_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load IDX_Chgsmp parser: {TOOL_PATH}")
PARSER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PARSER
SPEC.loader.exec_module(PARSER)


class CsmarIdxChgsmpZeroLossCandidateTests(unittest.TestCase):
    def test_bound_live_package_plan_is_complete_and_write_free(self) -> None:
        source = Path.home() / "Downloads" / PARSER.EXPECTED_ZIP_NAME
        if not source.is_file():
            self.skipTest(f"reviewed local export is unavailable: {source}")
        catalog_before = PARSER.catalog_hash(WORKSPACE_ROOT)
        result = PARSER.run(
            workspace_root=WORKSPACE_ROOT,
            zip_path=source,
            input_root=source.parent,
            run_id="unit-plan-no-write",
            output_root=None,
            execute=False,
        )
        self.assertEqual(result["status"], "PLAN_COMPLETE__NO_WRITES")
        self.assertFalse(result["write_performed"])
        self.assertEqual(
            result["source"]["zip_sha256"],
            PARSER.EXPECTED_ZIP_SHA256,
        )
        self.assertEqual(
            result["parser_metrics"]["accounted_source_bytes"],
            result["parser_metrics"]["source_csv_bytes"],
        )
        self.assertEqual(result["parser_metrics"]["unaccounted_source_bytes"], 0)
        self.assertEqual(result["parser_metrics"]["repaired_record_count"], 0)
        metrics = result["semantic_metrics"]
        self.assertEqual(metrics["logical_row_count"], 279_744)
        self.assertEqual(metrics["column_count"], 8)
        self.assertEqual(metrics["actual_field_selection"], "8_OF_8")
        self.assertEqual(metrics["candidate_key_duplicate_rows"], 0)
        self.assertIn("Chgsmp07", metrics["candidate_key"])
        self.assertEqual(metrics["market_missing_count"], 804)
        self.assertEqual(metrics["future_effective_boundary_count"], 122)
        self.assertEqual(metrics["advance_announcement_count"], 684)
        self.assertEqual(metrics["maximum_announcement_lead_days"], 1_113)
        self.assertEqual(metrics["bond_record_count"], 268_608)
        self.assertEqual(metrics["bond_record_percentage"], 96.0192)
        self.assertEqual(metrics["quality_queue_unique_record_count"], 1_488)
        self.assertEqual(PARSER.catalog_hash(WORKSPACE_ROOT), catalog_before)
        self.assertFalse(
            Path(result["candidate_path"]).exists(),
            "PLAN must not materialize a candidate",
        )

    def test_empty_market_is_preserved_and_queued(self) -> None:
        row = {
            "Indexcd": "TEST",
            "Chgsmp01": "2026-07-01",
            "Chgsmp02": "000001",
            "Chgsmp04": "1",
            "Chgsmp05": "1",
            "Chgsmp06": "2026-07-01",
            "Chgsmp07": "",
        }
        reasons, lead_days = PARSER.row_quality_reasons(row)
        self.assertEqual(reasons, ["SOURCE_MARKET_CODE_EMPTY"])
        self.assertEqual(lead_days, 0)
        self.assertEqual(row["Chgsmp07"], "")

    def test_output_root_cannot_escape_dedicated_tmp_tree(self) -> None:
        with self.assertRaises(PARSER.IdxChgsmpError):
            PARSER.require_output_location(
                WORKSPACE_ROOT,
                WORKSPACE_ROOT / "tmp" / "another-candidate",
                "unsafe-target",
            )


if __name__ == "__main__":
    unittest.main()
