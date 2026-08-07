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
    / "parse_csmar_idx_idxinfo_zero_loss_candidate.py"
)
SPEC = importlib.util.spec_from_file_location(
    "parse_csmar_idx_idxinfo_zero_loss_candidate",
    TOOL_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load IDX parser: {TOOL_PATH}")
PARSER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PARSER
SPEC.loader.exec_module(PARSER)


class CsmarIdxIdxinfoZeroLossCandidateTests(unittest.TestCase):
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
        self.assertEqual(result["semantic_metrics"]["logical_row_count"], 5574)
        self.assertEqual(result["semantic_metrics"]["column_count"], 12)
        self.assertEqual(
            result["semantic_metrics"]["candidate_key_duplicate_rows"],
            0,
        )
        self.assertEqual(PARSER.catalog_hash(WORKSPACE_ROOT), catalog_before)
        self.assertFalse(
            Path(result["candidate_path"]).exists(),
            "PLAN must not materialize a candidate",
        )

    def test_output_root_cannot_escape_workspace_tmp(self) -> None:
        with self.assertRaises(PARSER.IdxZeroLossError):
            PARSER.require_output_location(
                WORKSPACE_ROOT,
                WORKSPACE_ROOT / "data" / "market",
                "unsafe-target",
            )


if __name__ == "__main__":
    unittest.main()
