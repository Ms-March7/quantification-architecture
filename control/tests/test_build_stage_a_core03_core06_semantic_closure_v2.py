import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


TOOL = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "build_stage_a_core03_core06_semantic_closure_v2.py"
)
SPEC = importlib.util.spec_from_file_location("semantic_closure_v2", TOOL)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class SemanticClosureV2Tests(unittest.TestCase):
    def test_gate_impact_is_exactly_fail_closed(self):
        result = MODULE.gate_impact("a" * 64)
        self.assertEqual(result["gates"]["CORE03-Q2"]["status"], "UNKNOWN")
        self.assertEqual(result["gates"]["CORE03-Q3"]["status"], "UNKNOWN")
        self.assertEqual(result["gates"]["CORE06-Q3"]["status"], "FAIL")
        self.assertEqual(result["gates"]["CORE06-Q4"]["status"], "FAIL")
        self.assertEqual(result["stage_a_ready"], "0/4")
        self.assertFalse(result["stage_a_status_changed"])
        self.assertFalse(result["formal_admission"])
        self.assertFalse(result["research_usable"])

    def test_dnvaltrd_literal_is_preserved_without_correction(self):
        matrix = MODULE.field_semantics_matrix()
        row = next(
            item
            for item in matrix["proved_for_hash_bound_manual_and_exports"]
            if item.get("field") == "Dnvaltrd"
        )
        self.assertIn("0=没有交易量", row["literal"])
        self.assertIn("POSSIBLE_LABEL_COPY_ERROR", row["classification"])
        self.assertFalse(row["automatic_correction"])

    def test_other_table_filling_semantics_are_not_transferred(self):
        matrix = MODULE.field_semantics_matrix()
        rule = matrix["non_transfer_rule"]
        self.assertEqual(rule["classification"], "FAIL_CLOSED")
        self.assertIn("not transferred", rule["statement"])
        self.assertIn(
            "TRD_Dalyr row emission policy on full-day suspension",
            matrix["not_proved"],
        )

    def test_expected_one_sided_rows_are_exact_and_hash_bound(self):
        fake_semantics = {
            "global_consistency_checks": {
                "volume_amount_inconsistency_examples": list(
                    MODULE.EXPECTED_ONE_SIDED_ROWS
                ),
                "volume_zero_amount_nonzero_rows": 1,
                "volume_nonzero_amount_zero_rows": 4,
            },
            "manual_claims": {
                "Dnvaltrd_zero": "0 means no trading amount."
            },
            "scope": {"rows_excluding_conflicting_keys": 18691664},
            "mechanical_findings": {
                "volume_amount_zero_pair_consistent": False
            },
        }
        ledger = MODULE.anomaly_ledger(fake_semantics)
        anomaly = ledger["anomalies"][1]
        self.assertEqual(anomaly["row_count"], 5)
        self.assertEqual(
            anomaly["rows_sha256"],
            MODULE.canonical_sha256(anomaly["rows"]),
        )
        self.assertEqual(len(anomaly["rows_sha256"]), 64)
        self.assertFalse(ledger["anomalies"][0]["automatic_correction"])

    def test_write_json_is_deterministic_and_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.json"
            value = {"中文": "没有交易量", "gate": "UNKNOWN"}
            MODULE.write_json(path, value)
            first = path.read_bytes()
            MODULE.write_json(path, value)
            self.assertEqual(first, path.read_bytes())
            self.assertEqual(json.loads(first.decode("utf-8")), value)

    def test_source_paths_are_workspace_relative(self):
        for path in MODULE.INPUT_FILES.values():
            self.assertFalse(Path(path).is_absolute())
            self.assertNotIn("..", Path(path).parts)
        for source in MODULE.OFFICIAL_TEXT_MARKERS:
            self.assertFalse(Path(source["path"]).is_absolute())
            self.assertNotIn("..", Path(source["path"]).parts)

    def test_guarded_authority_contract_includes_five_files(self):
        self.assertEqual(len(MODULE.GUARDED_FILES), 5)
        self.assertIn("control/WORKSPACE_INDEX.md", MODULE.GUARDED_FILES)
        self.assertIn("data/catalog/datasets.json", MODULE.GUARDED_FILES)

    def test_manifest_verifier_rejects_undeclared_extra_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "candidate"
            package.mkdir()
            declared = package / "declared.json"
            declared.write_text("{}\n", encoding="utf-8")
            manifest = {
                "self_excluded_from_files": "CANDIDATE_MANIFEST.json",
                "files": [
                    MODULE.file_receipt(root, "candidate/declared.json")
                ],
            }
            (package / "extra.txt").write_text("unexpected", encoding="utf-8")
            errors = MODULE.verify_manifest(
                root,
                manifest,
                logical_root="candidate",
                actual_root=package,
            )
            self.assertIn(
                "unexpected file not declared: extra.txt",
                errors,
            )

    def test_expected_payload_allowlist_rejects_extra_file(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / "expected.json").write_text("{}\n", encoding="utf-8")
            (package / "extra.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                RuntimeError, "Strict payload allow-list mismatch"
            ):
                MODULE.expected_payload_paths(package, {"expected.json"})

    def test_executable_source_bindings_are_explicit(self):
        self.assertEqual(
            MODULE.BUILD_TOOL_PATH,
            "control/tools/build_stage_a_core03_core06_semantic_closure_v2.py",
        )
        self.assertEqual(
            MODULE.TEST_SOURCE_PATH,
            "control/tests/test_build_stage_a_core03_core06_semantic_closure_v2.py",
        )
        self.assertIn("TEST_RESULTS.json", MODULE.REPORT_PAYLOAD_NAMES)
        self.assertEqual(
            MODULE.REPAIR_ID,
            "strict-evidence-chain-repair-20260730-v1",
        )


if __name__ == "__main__":
    unittest.main()
