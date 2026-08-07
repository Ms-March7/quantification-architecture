import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
PYTHON = (
    WORKSPACE
    / "control"
    / "runtime"
    / "data-engineering"
    / ".venv"
    / "Scripts"
    / "python.exe"
)
SCRIPT = (
    WORKSPACE
    / "control"
    / "tools"
    / "build_stage_a_core02_calendar_session_closure_v2.py"
)
REPORT = (
    WORKSPACE
    / "reports"
    / "data-governance"
    / "stage-a-core02-calendar-session-closure-20260730-v2"
)
CANDIDATE = (
    WORKSPACE
    / "tmp"
    / "d2a04b-source-closure"
    / "stage-a-core02-calendar-session-closure-20260730-v2"
)

spec = importlib.util.spec_from_file_location("core02_v2", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def tree_hash(root: Path) -> str:
    items = [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": module.sha256_file(path),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]
    return module.sha256_bytes(module.canonical_json_bytes(items))


class Core02V2UnitTests(unittest.TestCase):
    def test_semantic_success_requires_all_groups(self):
        source = (
            "<html><body>2014 休市 元旦 春节 清明 劳动节 端午节 中秋节 国庆节"
            "</body></html>"
        ).encode("utf-8")
        test_spec = {
            "required_token_groups": (
                ("2014",),
                ("休市",),
                ("元旦",),
                ("春节",),
                ("清明",),
                ("劳动",),
                ("端午",),
                ("中秋",),
                ("国庆",),
            )
        }
        result = module.classify_html_response(
            test_spec, 200, source, "text/html; charset=utf-8"
        )
        self.assertEqual(result["result"], "RESPONSE_PRESERVED")
        self.assertTrue(result["semantic_required_groups_pass"])

    def test_semantic_missing_token_fails_closed(self):
        source = "<html><body>2014 休市安排</body></html>".encode("utf-8")
        test_spec = {
            "required_token_groups": (("2014",), ("休市",), ("元旦",))
        }
        result = module.classify_html_response(
            test_spec, 200, source, "text/html"
        )
        self.assertEqual(
            result["result"], "SEMANTIC_INSUFFICIENT_BODY_PRESERVED"
        )
        self.assertFalse(result["semantic_required_groups_pass"])

    def test_official_host_allowlist_is_narrow(self):
        self.assertIn("www.sse.com.cn", module.ALLOWED_HOSTS)
        self.assertIn("www.szse.cn", module.ALLOWED_HOSTS)
        self.assertIn("www.bse.cn", module.ALLOWED_HOSTS)
        self.assertNotIn("eastmoney.com", module.ALLOWED_HOSTS)

    def test_strict_file_set_rejects_extra_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "expected.txt").write_text("x", encoding="utf-8")
            (root / "extra.txt").write_text("y", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                module.reject_extra_or_missing(root, {"expected.txt"})


class Core02V2ArtifactTests(unittest.TestCase):
    def load(self, name):
        return json.loads((REPORT / name).read_text(encoding="utf-8"))

    def test_gate_is_fail_closed_and_pit_unknown(self):
        gate = self.load("CORE02_GATE_RECEIPT.json")
        self.assertEqual(gate["gates"]["CORE02-Q1"]["status"], "FAIL")
        self.assertEqual(gate["gates"]["CORE02-Q2"]["status"], "FAIL")
        self.assertEqual(gate["gates"]["CORE02-Q3"]["status"], "UNKNOWN")
        self.assertEqual(gate["gates"]["CORE02-Q4"]["status"], "PASS")
        self.assertFalse(gate["formal_admission"])
        self.assertFalse(gate["research_usable"])
        pit = self.load("PIT_PUBLICATION_ASSESSMENT.json")
        self.assertEqual(pit["row_count"], 28)
        self.assertEqual(pit["first_publication_proven_count"], 0)
        self.assertEqual(pit["available_at_status"], "UNKNOWN")
        self.assertEqual(pit["intraday_publication_status"], "UNKNOWN")

    def test_all_request_bodies_are_hash_bound(self):
        receipts = self.load("REQUEST_RECEIPTS.json")
        self.assertEqual(len(receipts), 7)
        for receipt in receipts:
            path = CANDIDATE / "raw" / receipt["relative_path"]
            self.assertEqual(
                module.sha256_file(path),
                receipt["response_sha256"],
                receipt["request_id"],
            )

    def test_bound_manifests_are_complete_and_source_bound(self):
        if os.environ.get("CORE02_TEST_RECEIPT_RUNNER") == "1":
            self.skipTest("receipt runner precedes rebinding its own output")
        candidate = module.validate_bound_manifest(
            WORKSPACE,
            CANDIDATE,
            "CANDIDATE_MANIFEST.json",
            module.expected_candidate_files(),
        )
        expected_report = module.EXPECTED_REPORT_FILES
        if (
            os.environ.get("CORE02_TEST_RECEIPT_BOOTSTRAP") == "1"
            and not (REPORT / "TEST_RESULTS.json").exists()
        ):
            expected_report = expected_report - {module.TEST_RESULTS_NAME}
        report = module.validate_bound_manifest(
            WORKSPACE,
            REPORT,
            "REPORT_MANIFEST.json",
            expected_report,
        )
        self.assertEqual(candidate["status"], "PASS", candidate["errors"])
        self.assertEqual(report["status"], "PASS", report["errors"])
        self.assertEqual(candidate["build_input_count"], 3)
        self.assertEqual(report["build_input_count"], 3)

    def test_rule_boundaries_do_not_invent_continuity(self):
        sessions = self.load("SESSION_VERSION_CHAIN.json")
        self.assertFalse(sessions["complete_effective_dated_chain"])
        rows = [
            row
            for venue_rows in sessions["chains"].values()
            for row in venue_rows
        ]
        self.assertTrue(
            all(not row["continuous_exact_interval_claimed"] for row in rows)
        )
        bse_2021 = next(
            row
            for row in sessions["chains"]["BSE"]
            if row["rule_version"] == "2021"
        )
        self.assertIn("UNKNOWN", bse_2021["effective_to"])
        sse_2023 = next(
            row
            for row in sessions["chains"]["SSE"]
            if row["rule_version"] == "2023"
        )
        self.assertTrue(sse_2023["effective_from"].startswith("CONDITIONAL"))

    def test_annual_master_requires_seven_classes_and_fragments_stay_fragments(self):
        evidence = self.load("ANNUAL_COMPLETENESS_EVIDENCE.json")
        self.assertEqual(len(evidence["required_holiday_classes"]), 7)
        self.assertEqual(evidence["legacy_master_count"], 24)
        self.assertEqual(evidence["legacy_master_pass_count"], 24)
        fragments = [
            row
            for row in evidence["new_response_assessments"]
            if row["fragment_only"]
        ]
        self.assertEqual(len(fragments), 2)
        self.assertTrue(
            all(not row["master_schedule_eligible"] for row in fragments)
        )
        annual = self.load("ANNUAL_HOLIDAY_NOTICE_CHAIN.json")
        self.assertEqual(
            annual["venues"]["SZSE"]["master_schedule_2014"], "MISSING"
        )

    def test_q3_q4_are_live_recomputed_from_8454_rows(self):
        stored = self.load("Q3_Q4_LIVE_RECOMPUTE.json")
        live = module.recompute_q3_q4(WORKSPACE)
        self.assertEqual(stored, live)
        self.assertEqual(live["measurements"]["row_count"], 8454)
        self.assertEqual(
            live["measurements"]["unique_venue_date_keys"], 8454
        )
        self.assertEqual(
            live["measurements"]["venue_counts"], {"SSE": 4227, "SZSE": 4227}
        )
        self.assertEqual(live["measurements"]["weekend_open_count"], 0)
        self.assertEqual(live["Q3"], "UNKNOWN")
        self.assertEqual(live["Q4"], "PASS")
        self.assertEqual(
            live["mechanical_subchecks"]["calendar_key_uniqueness"], "PASS"
        )
        self.assertEqual(
            live["mechanical_subchecks"]["strict_open_sequence"], "PASS"
        )
        self.assertEqual(
            live["q3_contract_requirements"]["bse_calendar_coverage"],
            "FAIL",
        )
        self.assertEqual(
            live["q3_contract_requirements"][
                "previous_next_trading_day_recomputable"
            ],
            "UNKNOWN",
        )

    def test_formal_guards_and_raw_are_unchanged(self):
        if os.environ.get("CORE02_TEST_RECEIPT_RUNNER") == "1":
            self.skipTest("receipt runner precedes rebuilt verification")
        verification = self.load("VERIFICATION.json")
        self.assertTrue(verification["formal_guards_unchanged"])
        self.assertTrue(verification["response_body_hashes_verified"])
        self.assertTrue(verification["raw_tree_hashes_unchanged"])
        self.assertTrue(verification["request_receipts_unchanged"])
        self.assertEqual(verification["network_requests_this_execution"], 0)
        source_check = verification[
            "source_manifest_live_recursive_validation"
        ]
        self.assertEqual(source_check["status"], "PASS", source_check["errors"])
        self.assertEqual(
            source_check["direct_binding_counts"],
            {
                "new_official_responses": 7,
                "sealed_rule_sources": 11,
                "baseline_sources": 3,
                "iso10383_sources": 2,
                "build_inputs": 3,
            },
        )
        self.assertEqual(source_check["lineage_raw_row_count"], 28)
        boundary = verification["transport_boundary"]
        self.assertEqual(
            boundary["proxy_and_credential_origin_status"], "UNKNOWN"
        )
        self.assertFalse(
            boundary["application_explicit_credentials_configured"]
        )
        self.assertFalse(boundary["direct_connection_claimed"])

    def test_machine_test_receipt_is_current_and_hash_bound(self):
        if os.environ.get("CORE02_TEST_RECEIPT_RUNNER") == "1":
            self.skipTest("receipt runner precedes rebinding its own output")
        receipt = self.load("TEST_RESULTS.json")
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["tests_run"], 15)
        self.assertEqual(receipt["network_requests_performed"], 0)
        self.assertTrue(receipt["zero_network_declaration"])

    def test_second_repair_history_preserves_first_repair(self):
        candidate_history = CANDIDATE / module.REPAIR_HISTORY_DIR
        report_history = REPORT / module.REPAIR_HISTORY_DIR
        self.assertEqual(
            module.sha256_file(candidate_history / "CANDIDATE_MANIFEST.json"),
            "cc52df917952728c26e6b4162d9c316478eb0a25c272e168b8541f714507dea6",
        )
        self.assertEqual(
            module.sha256_file(report_history / "REPORT_MANIFEST.json"),
            "5ec3903fb3e368bce80a121d346efe4d71df9c5e5bfe749ae767b522106db1e1",
        )
        self.assertEqual(
            module.sha256_file(report_history / "QA_REPAIR_RECEIPT.json"),
            "c160201b196c90d6a0475499c9e406b202336eb123f98ff317a399a75814d960",
        )

    def test_recursive_source_manifest_rejects_tampered_binding(self):
        with tempfile.TemporaryDirectory(
            dir=WORKSPACE / "tmp"
        ) as candidate_dir, tempfile.TemporaryDirectory(
            dir=WORKSPACE / "reports"
        ) as report_dir:
            temp_candidate = Path(candidate_dir)
            temp_report = Path(report_dir)
            shutil.copy2(
                CANDIDATE / "REQUEST_RECEIPTS.json",
                temp_candidate / "REQUEST_RECEIPTS.json",
            )
            shutil.copytree(CANDIDATE / "raw", temp_candidate / "raw")
            receipts = json.loads(
                (temp_candidate / "REQUEST_RECEIPTS.json").read_text(
                    encoding="utf-8"
                )
            )
            response_bindings = [
                module.receipt_binding(item, temp_candidate, WORKSPACE)
                for item in receipts
            ]
            rules = module.sealed_rule_bindings(WORKSPACE)
            baseline = module.baseline_receipt_bindings(WORKSPACE)
            lineage, _ = module.legacy_calendar_lineage_binding(WORKSPACE)
            iso = module.mic_bindings(WORKSPACE)
            manifest = module.compose_source_manifest(
                WORKSPACE,
                response_bindings,
                rules,
                baseline,
                lineage,
                iso,
            )
            module.write_json(
                temp_candidate / "SOURCE_MANIFEST.json", manifest
            )
            module.write_json(temp_report / "SOURCE_MANIFEST.json", manifest)
            valid = module.verify_source_manifest_live(
                WORKSPACE, temp_candidate, temp_report
            )
            self.assertEqual(valid["status"], "PASS", valid["errors"])

            manifest["reused_sealed_rule_sources"][0]["sha256"] = "0" * 64
            module.write_json(
                temp_candidate / "SOURCE_MANIFEST.json", manifest
            )
            module.write_json(temp_report / "SOURCE_MANIFEST.json", manifest)
            invalid = module.verify_source_manifest_live(
                WORKSPACE, temp_candidate, temp_report
            )
            self.assertEqual(invalid["status"], "FAIL")
            self.assertIn(
                "SOURCE_MANIFEST differs from live recursive bindings",
                invalid["errors"],
            )

    def test_verify_only_is_read_only_and_passes(self):
        if os.environ.get("CORE02_TEST_RECEIPT_RUNNER") == "1":
            self.skipTest("receipt runner precedes rebinding verification")
        candidate_before = tree_hash(CANDIDATE)
        report_before = tree_hash(REPORT)
        completed = subprocess.run(
            [
                str(PYTHON),
                str(SCRIPT),
                "--workspace",
                str(WORKSPACE),
                "--verify-only",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["writes_performed"], 0)
        self.assertEqual(result["network_requests_performed"], 0)
        self.assertEqual(candidate_before, tree_hash(CANDIDATE))
        self.assertEqual(report_before, tree_hash(REPORT))


if __name__ == "__main__":
    unittest.main()
