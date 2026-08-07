from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import pandas as pd


SCRIPT = Path(__file__).resolve().parents[1] / "tools/build_joinquant_share_capital_pit_candidate.py"
SPEC = importlib.util.spec_from_file_location("jq_share_capital_pit", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class JoinQuantShareCapitalPITTests(unittest.TestCase):
    def test_publication_maps_strictly_after_and_effective_not_before(self) -> None:
        sessions = [date(2020, 1, 2), date(2020, 1, 3), date(2020, 1, 6)]
        self.assertEqual(
            MODULE.map_usable_session(date(2020, 1, 2), date(2020, 1, 2), sessions),
            date(2020, 1, 3),
        )
        self.assertEqual(
            MODULE.map_usable_session(date(2020, 1, 2), date(2020, 1, 6), sessions),
            date(2020, 1, 6),
        )

    def test_late_old_disclosure_does_not_overwrite_new_state(self) -> None:
        rows = pd.DataFrame(
            [
                {"order_book_id": "000001.XSHE", "usable_from": date(2020, 1, 2), "change_date": date(2019, 12, 31), "pub_date": date(2020, 1, 1), "source_id": "1", "share_total_reported": 100.0, "share_trade_total_reported": 80.0, "share_rmb_reported": 80.0, "change_reason_id": "x", "change_reason": "x", "source_row_sha256": "a" * 64},
                {"order_book_id": "000001.XSHE", "usable_from": date(2020, 2, 3), "change_date": date(2020, 1, 31), "pub_date": date(2020, 2, 1), "source_id": "2", "share_total_reported": 120.0, "share_trade_total_reported": 90.0, "share_rmb_reported": 90.0, "change_reason_id": "y", "change_reason": "y", "source_row_sha256": "b" * 64},
                {"order_book_id": "000001.XSHE", "usable_from": date(2020, 3, 3), "change_date": date(2019, 6, 30), "pub_date": date(2020, 3, 1), "source_id": "3", "share_total_reported": 70.0, "share_trade_total_reported": 60.0, "share_rmb_reported": 60.0, "change_reason_id": "z", "change_reason": "z", "source_row_sha256": "c" * 64},
            ]
        )
        result, diagnostics = MODULE.build_state_intervals(rows, coverage_end_exclusive=date(2020, 4, 1))
        self.assertEqual(result["share_total_reported"].tolist(), [100.0, 120.0])
        self.assertEqual(diagnostics["ignored_late_disclosures_of_older_effective_state"], 1)
        self.assertTrue((result["valid_to_exclusive"] > result["valid_from"]).all())

    def test_same_effective_date_later_publication_is_local_revision(self) -> None:
        rows = pd.DataFrame(
            [
                {"order_book_id": "600000.XSHG", "usable_from": date(2020, 1, 2), "change_date": date(2019, 12, 31), "pub_date": date(2020, 1, 1), "source_id": "1", "share_total_reported": 100.0, "share_trade_total_reported": 80.0, "share_rmb_reported": 80.0, "change_reason_id": "x", "change_reason": "x", "source_row_sha256": "a" * 64},
                {"order_book_id": "600000.XSHG", "usable_from": date(2020, 2, 3), "change_date": date(2019, 12, 31), "pub_date": date(2020, 2, 1), "source_id": "2", "share_total_reported": 101.0, "share_trade_total_reported": 81.0, "share_rmb_reported": 81.0, "change_reason_id": "x", "change_reason": "x", "source_row_sha256": "b" * 64},
            ]
        )
        result, _ = MODULE.build_state_intervals(rows, coverage_end_exclusive=date(2020, 3, 1))
        self.assertEqual(len(result), 2)
        self.assertEqual(result.iloc[1]["supersedes_local_state_revision_id"], result.iloc[0]["local_state_revision_id"])
        self.assertIn("NOT_VENDOR_REVISION_CHAIN", result.iloc[0]["local_chain_semantics"])

    def test_missing_local_source_is_explicitly_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(MODULE, "ROOT", root), \
                 mock.patch.object(MODULE, "SOURCE", root / "missing.parquet"), \
                 mock.patch.object(MODULE, "SOURCE_MANIFEST", root / "missing-manifest.json"), \
                 mock.patch.object(MODULE, "FINAL", root / "candidate"), \
                 mock.patch.object(MODULE, "REPORT", root / "report"):
                result = MODULE.plan(output_root=MODULE.FINAL, report_root=MODULE.REPORT)
        self.assertEqual(
            result["status"],
            "SKIPPED__LOCAL_JOINQUANT_SOURCE_NOT_AVAILABLE__NO_NETWORK_NO_WRITE",
        )

    def test_committed_candidate_is_verified_and_source_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "candidate"
            output.mkdir()
            bindings = {
                "joinquant_capital_change": {"path": "source.parquet", "bytes": 1, "sha256": "a" * 64},
                "source_manifest": {"path": "source-manifest.json", "bytes": 1, "sha256": "b" * 64},
                "formal_calendar_snapshot": {
                    "dataset_id": "calendar",
                    "snapshot_id": "v1",
                    "content_hash": "c" * 64,
                    "manifest": {"path": "calendar.json", "bytes": 1, "sha256": "d" * 64},
                },
            }
            artifacts = {}
            for name in (
                "normalized_source_observations",
                "pit_share_capital_intervals",
                "pending_after_r0_cutoff",
                "quality",
            ):
                path = output / f"{name}.bin"
                path.write_bytes(name.encode("utf-8"))
                artifacts[name] = {
                    "path": path.relative_to(root).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": MODULE.sha256_file(path),
                }
            manifest = {
                "schema_version": 1,
                "candidate_id": MODULE.CANDIDATE_ID,
                "status": "COMPLETED__INSPECTION_ONLY__NOT_ADMITTED",
                "purpose_ceiling": "INSPECTION_ONLY",
                "formal_admission": False,
                "research_use": False,
                "catalog_write": False,
                "source": bindings,
                "artifacts": artifacts,
            }
            manifest_path = output / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (output / "COMMITTED.json").write_text(json.dumps({
                "status": "COMMITTED_INSPECTION_ONLY",
                "candidate_id": MODULE.CANDIDATE_ID,
                "manifest_sha256": MODULE.sha256_file(manifest_path),
            }), encoding="utf-8")
            with mock.patch.object(MODULE, "ROOT", root):
                result = MODULE.verify_committed_candidate(
                    bindings,
                    output_root=output,
                    report_root=root / "absent-report",
                )
                changed = json.loads(json.dumps(bindings))
                changed["joinquant_capital_change"]["sha256"] = "e" * 64
                with self.assertRaises(MODULE.BuildError):
                    MODULE.verify_committed_candidate(
                        changed,
                        output_root=output,
                        report_root=root / "absent-report",
                    )
        self.assertEqual(
            result["status"],
            "NO_CHANGE__SOURCE_UNCHANGED__COMMITTED_CANDIDATE_VERIFIED__NO_NETWORK",
        )


if __name__ == "__main__":
    unittest.main()
