import importlib.util
import unittest
from pathlib import Path


TOOL = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "build_stage_a_core03_core06_official_rules_closure.py"
)
SPEC = importlib.util.spec_from_file_location("official_rules_closure", TOOL)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class OfficialRulesClosureTests(unittest.TestCase):
    def test_all_sources_are_official_allowlisted(self):
        self.assertGreaterEqual(len(MODULE.SOURCES), 18)
        for source in MODULE.SOURCES:
            host = MODULE.urlparse(source["url"]).hostname
            self.assertIn(host, MODULE.ALLOWED_HOSTS)
            self.assertTrue(source["markers"])

    def test_gate_receipt_is_fail_closed(self):
        receipt = MODULE.gate_receipt()
        self.assertEqual(receipt["gates"]["CORE03-Q2"]["status"], "UNKNOWN")
        self.assertEqual(receipt["gates"]["CORE03-Q3"]["status"], "UNKNOWN")
        self.assertEqual(receipt["gates"]["CORE06-Q3"]["status"], "FAIL")
        self.assertEqual(receipt["gates"]["CORE06-Q4"]["status"], "FAIL")
        self.assertFalse(receipt["stage_a_status_changed"])
        self.assertFalse(receipt["formal_admission"])
        self.assertFalse(receipt["research_usable"])

    def test_every_fact_source_is_declared(self):
        source_ids = {source["source_id"] for source in MODULE.SOURCES}
        coverage = MODULE.official_rule_coverage()
        for fact in coverage["facts"]:
            self.assertTrue(fact["source_ids"])
            self.assertTrue(set(fact["source_ids"]).issubset(source_ids))

    def test_historical_gaps_are_not_empty(self):
        gaps = MODULE.historical_gaps()
        statuses = {row["status"] for row in gaps["gaps"]}
        self.assertIn("FAIL", statuses)
        self.assertIn("UNKNOWN", statuses)
        self.assertIn("CORE03-Q2", gaps["gate_effect"])
        self.assertIn("CORE06-Q3", gaps["gate_effect"])

    def test_report_contract_is_exact(self):
        self.assertIn("REPORT_MANIFEST.json", MODULE.REPORT_FILES)
        self.assertIn("VERIFICATION.json", MODULE.REPORT_FILES)
        self.assertIn("GATE_RECEIPT.json", MODULE.REPORT_FILES)
        self.assertEqual(len(MODULE.REPORT_FILES), 8)

    def test_marker_normalization(self):
        self.assertEqual(
            MODULE.normalize_marker("涨跌幅 比例为 20％"),
            "涨跌幅比例为20%",
        )
        contract = MODULE.MARKER_NORMALIZATION_CONTRACT
        self.assertEqual(contract["contract_id"], "MARKER_NORMALIZATION_V1")
        self.assertEqual(
            contract["canonical_expression"],
            "remove_all_whitespace_then_fullwidth_percent_to_ascii",
        )
        self.assertFalse(contract["other_substitutions"])

    def test_marker_evidence_records_raw_and_normalized_match(self):
        checks, evidence, normalized_sha256 = MODULE.marker_evidence(
            "前五个交易日不实行价格\n涨跌幅限制",
            ("前五个交易日不实行价格涨跌幅限制",),
        )
        self.assertTrue(checks["前五个交易日不实行价格涨跌幅限制"])
        self.assertFalse(evidence[0]["raw_exact_match"])
        self.assertTrue(evidence[0]["normalized_match"])
        self.assertGreaterEqual(evidence[0]["normalized_match_start"], 0)
        self.assertEqual(len(normalized_sha256), 64)


if __name__ == "__main__":
    unittest.main()
