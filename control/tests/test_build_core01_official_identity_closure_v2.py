from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT = (
    WORKSPACE
    / "control"
    / "tools"
    / "build_core01_official_identity_closure_v2.py"
)
SPEC = importlib.util.spec_from_file_location("core01_identity_v2", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Core01OfficialIdentityV2Tests(unittest.TestCase):
    def test_guard_scope_covers_catalog_five_authorities_and_quality(self) -> None:
        expected = {
            "data/catalog/datasets.json",
            "data/catalog/contracts/source_admission_v1/current_source_registry.json",
            "control/ROADMAP.md",
            "control/projects.yaml",
            "control/WORKSPACE_INDEX.md",
            "data/catalog/DATABASE_MANUAL.md",
            "data/catalog/DATA_QUALITY_POLICY.md",
        }
        self.assertEqual(set(MODULE.GUARDED_FILES), expected)
        self.assertEqual(
            set(MODULE.GUARDED_TREES),
            {"data/market/snapshots", "data/published"},
        )

    def test_network_plan_is_official_bounded_and_does_not_retry_bse_waf(
        self,
    ) -> None:
        self.assertEqual(MODULE.GLOBAL_NETWORK_REQUEST_CAP, 40)
        urls = [row["url"] for row in MODULE.FIXED_GET_SOURCES]
        self.assertNotIn(
            "https://www.bse.cn/nq/listedcompany.html",
            urls,
        )
        self.assertTrue(all(MODULE.official_host_allowed(url) for url in urls))
        self.assertTrue(
            all(
                row["max_pages"] <= 15
                for row in MODULE.SZSE_SEARCHES
            )
        )
        bse_mapping = next(
            row
            for row in MODULE.FIXED_GET_SOURCES
            if row["source_id"] == "BSE-CODE-MAPPING-CURRENT-PAGE"
        )
        self.assertIn("原精选层", bse_mapping["markers"])
        self.assertIn("挂牌日期", bse_mapping["markers"])

    def test_normalized_szse_event_never_promotes_publication_or_identity(
        self,
    ) -> None:
        row = MODULE.normalize_szse_event(
            {
                "id": "1",
                "doctitle": "关于000001股票恢复上市",
                "doccontent": "证券代码000001",
                "docpuburl": "http://www.szse.cn/a.html",
                "docpubjsonurl": "/a.json",
                "docpubtime": 1600000000000,
            },
            family="RESUME_LISTING",
            source_path="tmp/raw.json",
            source_sha256="a" * 64,
        )
        self.assertIsNotNone(row["published_at_candidate"])
        self.assertFalse(row["first_publication_semantics_proven"])
        self.assertIsNone(row["available_at_promoted"])
        self.assertEqual(row["local_code_candidates_not_identity"], ["000001"])

    def test_szse_metadata_url_date_conflict_is_explicit(self) -> None:
        timestamp = int(
            MODULE.datetime(
                2020,
                1,
                2,
                8,
                0,
                tzinfo=MODULE.SHANGHAI,
            ).timestamp()
            * 1000
        )
        row = MODULE.normalize_szse_event(
            {
                "id": "2",
                "doctitle": "关于000001股票恢复上市",
                "doccontent": "",
                "docpuburl": (
                    "https://www.szse.cn/disclosure/notice/company/"
                    "t20200101_2.html"
                ),
                "docpubjsonurl": "",
                "docpubtime": timestamp,
            },
            family="RESUME_LISTING",
            source_path="tmp/raw.json",
            source_sha256="b" * 64,
        )
        self.assertEqual(row["metadata_url_date_candidate"], "2020-01-01")
        self.assertEqual(row["docpubtime_date_candidate"], "2020-01-02")
        self.assertFalse(
            row["metadata_url_date_matches_docpubtime_date"]
        )

    def test_variable_width_official_dates_are_normalized(self) -> None:
        self.assertEqual(MODULE.normalize_date("2024/4/8"), "2024-04-08")
        self.assertEqual(MODULE.normalize_date("20200226"), "2020-02-26")
        self.assertIsNone(MODULE.normalize_date("2024/2/30"))

    def test_exact_date_selection_never_falls_back_or_silently_merges(
        self,
    ) -> None:
        rows = [
            {"date": "20200101", "value": "a"},
            {"date": "20200102", "value": "b"},
        ]
        self.assertIsNone(
            MODULE.unique_row_with_date(rows, "date", "1999-01-01")
        )
        self.assertEqual(
            MODULE.unique_row_with_date(rows, "date", "2020-01-02")[
                "value"
            ],
            "b",
        )
        self.assertIsNone(
            MODULE.unique_row_with_date(
                rows + [{"date": "20200102", "value": "c"}],
                "date",
                "2020-01-02",
            )
        )

    def test_relist_marker_requires_full_official_chinese_date(self) -> None:
        self.assertEqual(
            MODULE.iso_date_to_chinese("2019-01-08"),
            "2019年1月8日",
        )
        with self.assertRaises(ValueError):
            MODULE.iso_date_to_chinese("2019-1-8")

    def test_gate_recomputation_fails_closed_with_partial_evidence(self) -> None:
        gate = MODULE.compute_gate_recomputation(
            bse_summary={"interval_candidates": 200},
            sse_relist_count=2,
            szse_coverage=[
                {"complete_keyword_pagination": True}
            ],
            szse_event_count=100,
        )
        self.assertFalse(gate["stage_a"]["ready"])
        self.assertEqual(gate["stage_a"]["total_blocking_reasons"], 11)
        block_gates = [
            row
            for row in gate["gates"]
            if row["severity"] == "BLOCK"
        ]
        self.assertTrue(
            all(row["strict_status_v2"] != "PASS" for row in block_gates)
        )
        self.assertEqual(
            next(
                row
                for row in gate["gates"]
                if row["gate_id"] == "CORE01-Q2"
            )["closure_assessment"],
            "PARTIAL",
        )
        q2 = next(
            row for row in gate["gates"] if row["gate_id"] == "CORE01-Q2"
        )
        overlap_audit = next(
            row
            for row in q2["subchecks"]
            if row["subcheck_id"] == "interval_overlap_and_gap_audit"
        )
        self.assertEqual(
            overlap_audit["closure_assessment"],
            "UNRESOLVED",
        )

    def test_manifest_verifier_rejects_undeclared_extra_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            declared = root / "declared.txt"
            declared.write_text("declared", encoding="utf-8")
            manifest = MODULE.manifest_for(
                [declared],
                logical_root="tmp/test",
                actual_root=root,
                artifact_role="test",
            )
            (root / "extra.txt").write_text("extra", encoding="utf-8")
            errors = MODULE.verify_manifest(
                manifest,
                logical_root="tmp/test",
                actual_root=root,
            )
            self.assertIn(
                "unexpected file not declared: extra.txt",
                errors,
            )

    def test_endpoint_authorization_policy_removes_header(self) -> None:
        prepared = MODULE.requests.Request(
            "GET",
            "https://www.sse.com.cn/",
            headers={"Authorization": "Bearer should-not-survive"},
        ).prepare()
        MODULE.NoEndpointAuthorization()(prepared)
        self.assertNotIn("Authorization", prepared.headers)

    def test_core01_contract_gate_ids_are_frozen(self) -> None:
        contract = MODULE.extract_core01_contract(WORKSPACE)
        self.assertEqual(contract["plan_id"], "CORE-01")
        self.assertEqual(
            [row["gate_id"] for row in contract["quality_gates"]],
            [
                "CORE01-Q1",
                "CORE01-Q2",
                "CORE01-Q3",
                "CORE01-Q4",
                "CORE01-Q5",
            ],
        )


if __name__ == "__main__":
    unittest.main()
