from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = (
    WORKSPACE_ROOT
    / "control"
    / "tools"
    / "audit_csmar_33pkg_local_closure.py"
)
SPEC = importlib.util.spec_from_file_location(
    "audit_csmar_33pkg_local_closure", TOOL_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load closure audit tool: {TOOL_PATH}")
AUDIT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = AUDIT
SPEC.loader.exec_module(AUDIT)


def minimal_audit(*, failure_count: int = 0) -> dict:
    status = (
        "PASS__LOCAL_EVIDENCE_REPRODUCIBLE__FORMAL_ADMISSION_0"
        if failure_count == 0
        else "FAIL__LOCAL_CLOSURE_EVIDENCE_INCOMPLETE"
    )
    return {
        "schema_version": 1,
        "tool_id": AUDIT.TOOL_ID,
        "as_of_date": AUDIT.AS_OF_DATE,
        "status": status,
        "formal_catalog": {
            "path": "data/catalog/datasets.json",
            "sha256_before": "a" * 64,
            "sha256_after": "a" * 64,
            "unchanged": True,
        },
        "formal_counts": {"CORE": "0/20", "GCTX": "0/8", "HK": "0/4", "GLEQ": "0/3"},
        "intake": {"package_count": 33},
        "candidate_batch": {"candidate_count": 32, "records": []},
        "schema_inventory": {
            "schema_version": 1,
            "as_of_date": AUDIT.AS_OF_DATE,
            "package_count": 33,
            "logical_table_count": 25,
            "table_field_occurrence_count": 1197,
            "unique_observed_field_name_count_case_sensitive": 559,
            "unique_observed_field_name_count_casefold": 474,
            "tables": [],
        },
        "closure_summary": {
            "packages": "33/33",
            "logical_tables": "25/25",
            "candidate_packages": "32/32",
            "overlap_calibration_packages": "1/1",
            "core_items_formally_closed": "0/20",
        },
        "financial_statement_overlap": {"statement_overlap": []},
        "audit_three_table_reconciliation": {
            "stats_rows": 41178,
            "audit_rows": 80301,
            "detail_rows": 82264,
            "stats_without_detail_keys": 0,
            "detail_without_stats_keys": 0,
            "auditno_detail_count_mismatch_keys": 0,
            "stats_without_audit_keys": 1,
        },
        "audit_text_hash_bindings": {"schema_version": 1, "entries": []},
        "core_mapping": AUDIT.core_mapping(),
        "verification": {
            "checks": [],
            "failure_count": failure_count,
        },
    }


class Csmar33PackageClosureAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="csmar-33pkg-audit-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_default_run_is_write_free(self) -> None:
        result = AUDIT.run(
            workspace_root=self.root,
            execute=False,
            audit_builder=lambda _root: minimal_audit(),
        )
        self.assertFalse(result["write_performed"])
        self.assertFalse((self.root / AUDIT.REPORT_RELATIVE).exists())

    def test_output_outside_fixed_report_root_is_rejected(self) -> None:
        with self.assertRaises(AUDIT.ClosureAuditError):
            AUDIT.write_reports(
                self.root,
                minimal_audit(),
                report_root=self.root / "outside",
            )
        self.assertFalse((self.root / "outside").exists())

    def test_failed_audit_never_writes(self) -> None:
        with self.assertRaises(AUDIT.ClosureAuditError):
            AUDIT.run(
                workspace_root=self.root,
                execute=True,
                audit_builder=lambda _root: minimal_audit(failure_count=1),
            )
        self.assertFalse((self.root / AUDIT.REPORT_RELATIVE).exists())

    def test_execute_writes_only_allowlisted_report_files(self) -> None:
        result = AUDIT.run(
            workspace_root=self.root,
            execute=True,
            audit_builder=lambda _root: minimal_audit(),
        )
        self.assertTrue(result["write_performed"])
        target = self.root / AUDIT.REPORT_RELATIVE
        self.assertEqual(
            sorted(AUDIT.REPORT_FILENAMES),
            sorted(path.name for path in target.iterdir()),
        )

    def test_candidate_paths_are_explicit_and_unique(self) -> None:
        self.assertEqual(30, len(AUDIT.BASE_CANDIDATE_RELATIVES))
        self.assertEqual(30, len(set(AUDIT.BASE_CANDIDATE_RELATIVES)))
        self.assertEqual(32, len(AUDIT.authoritative_candidate_relatives()))
        for binding in AUDIT.AUDIT_TEXT_BINDINGS.values():
            self.assertNotEqual(
                binding["authoritative_candidate"],
                binding["superseded_candidate"],
                "final v2 binding must supersede, not alias, the v1 candidate",
            )

    def test_formal_counts_are_derived_from_catalog_intersection(self) -> None:
        catalog_root = self.root / "data" / "catalog"
        catalog_root.mkdir(parents=True)
        (catalog_root / "datasets.json").write_text(
            '{"datasets":[{"dataset_id":"core-a"},{"dataset_id":"unrelated"}]}',
            encoding="utf-8",
        )
        (catalog_root / "planned_datasets.json").write_text(
            '{"datasets":['
            '{"plan_id":"CORE-01","dataset_id":"core-a","gate":"G_DATA_CORE"},'
            '{"plan_id":"CORE-02","dataset_id":"core-b","gate":"G_DATA_CORE"},'
            '{"plan_id":"GCTX-01","dataset_id":"gctx-a","gate":"G_DATA_GLOBAL_CONTEXT"}'
            "]}",
            encoding="utf-8",
        )
        targets = {
            "CORE": ("G_DATA_CORE", 2),
            "GCTX": ("G_DATA_GLOBAL_CONTEXT", 1),
        }
        with mock.patch.object(AUDIT, "FORMAL_GATE_TARGETS", targets):
            result = AUDIT.derive_formal_counts(self.root)
        self.assertEqual("1/2", result["counts"]["CORE"]["display"])
        self.assertEqual(["CORE-01"], result["counts"]["CORE"]["plan_ids"])
        self.assertEqual("0/1", result["counts"]["GCTX"]["display"])


if __name__ == "__main__":
    unittest.main()
