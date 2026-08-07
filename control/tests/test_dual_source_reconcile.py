from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT = WORKSPACE / "control" / "tools" / "run_dual_source_reconcile.py"
SPEC = importlib.util.spec_from_file_location("dual_source_reconcile", SCRIPT)
assert SPEC and SPEC.loader
RECON = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RECON
SPEC.loader.exec_module(RECON)


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class DualSourceReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        RECON.INPUT_ROOT.mkdir(parents=True, exist_ok=True)
        RECON.OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        self.input_temp = tempfile.TemporaryDirectory(
            dir=RECON.INPUT_ROOT, prefix="test-dual-reconcile-input-"
        )
        self.input_root = Path(self.input_temp.name)
        self.outputs: list[Path] = []

    def tearDown(self) -> None:
        for output in self.outputs:
            shutil.rmtree(output, ignore_errors=True)
        self.input_temp.cleanup()

    def _write(self, name: str, text: str) -> tuple[Path, bytes]:
        payload = text.encode("utf-8")
        path = self.input_root / name
        path.write_bytes(payload)
        return path, payload

    def _spec(self, source_id: str, path: Path, payload: bytes) -> str:
        return "|".join(
            [
                source_id,
                path.relative_to(WORKSPACE).as_posix(),
                digest(payload),
                str(len(payload)),
            ]
        )

    def _output(self) -> tuple[str, Path]:
        run_id = f"test-reconcile-{uuid.uuid4().hex}"
        output = RECON.OUTPUT_ROOT / run_id
        self.outputs.append(output)
        return run_id, output

    def _prepare(
        self, left_text: str, right_text: str, rules: list[str] | None = None
    ) -> tuple[dict, Path, Path]:
        left, left_payload = self._write("left.csv", left_text)
        right, right_payload = self._write("right.csv", right_text)
        run_id, output = self._output()
        prepared = RECON.prepare(
            run_id,
            self._spec("SOURCE_LEFT", left, left_payload),
            self._spec("SOURCE_RIGHT", right, right_payload),
            ["symbol", "trade_date"],
            rules
            or [
                "close|numeric|0.01|critical",
                "route_code|exact|0|noncritical",
            ],
            output,
        )
        return prepared, output, left

    def test_match_and_tolerance_pass_with_both_values_and_no_grants(self) -> None:
        header = "symbol,trade_date,close,route_code\n"
        prepared, output, _ = self._prepare(
            header + "000001,2026-07-31,10.000,A\n",
            header + "000001,2026-07-31,10.009,A\n",
        )
        result = RECON.execute(prepared)
        self.assertEqual(result["status"], "PASS_RECONCILED_INSPECTION_ONLY")
        self.assertFalse(result["fail_closed"])
        with (output / "RECONCILIATION.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "MATCH")
        self.assertEqual(rows[0]["close__left"], "10.000")
        self.assertEqual(rows[0]["close__right"], "10.009")
        self.assertEqual(rows[0]["close__delta"], "0.009")
        manifest = json.loads(
            (output / "RECONCILIATION_MANIFEST.json").read_text(encoding="utf-8")
        )
        self.assertFalse(manifest["canonical_source_selected"])
        self.assertFalse(manifest["silent_overwrite_used"])
        self.assertFalse(manifest["formal_catalog_written"])
        self.assertFalse(manifest["quality_promotion"])
        self.assertFalse(manifest["formal_snapshot_written"])
        self.assertFalse(manifest["research_ready"])
        self.assertFalse(
            manifest["evidence_csv"]["canonical_value_column_present"]
        )
        manifest_hash = hashlib.sha256(
            (output / "RECONCILIATION_MANIFEST.json").read_bytes()
        ).hexdigest()
        self.assertTrue(
            (output / "RECONCILIATION_MANIFEST.sha256")
            .read_text(encoding="ascii")
            .startswith(manifest_hash)
        )

    def test_one_side_missing_writes_evidence_and_fails_closed(self) -> None:
        header = "symbol,trade_date,close,route_code\n"
        prepared, output, _ = self._prepare(
            header
            + "000001,2026-07-31,10,A\n"
            + "000002,2026-07-31,20,A\n",
            header + "000001,2026-07-31,10,A\n",
        )
        result = RECON.execute(prepared)
        self.assertTrue(result["fail_closed"])
        self.assertEqual(
            result["status"], "FAIL_CLOSED_CRITICAL_RECONCILIATION_GAP"
        )
        self.assertEqual(result["outcome_counts"]["ONE_SIDE_MISSING"], 1)
        with (output / "RECONCILIATION.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            rows = list(csv.DictReader(handle))
        missing = next(row for row in rows if row["symbol"] == "000002")
        self.assertEqual(missing["outcome"], "ONE_SIDE_MISSING")
        self.assertEqual(missing["left_present"], "True")
        self.assertEqual(missing["right_present"], "False")

    def test_critical_field_conflict_fails_closed_without_selecting_winner(self) -> None:
        header = "symbol,trade_date,close,route_code\n"
        prepared, output, _ = self._prepare(
            header + "000001,2026-07-31,10,A\n",
            header + "000001,2026-07-31,11,A\n",
        )
        result = RECON.execute(prepared)
        self.assertTrue(result["fail_closed"])
        self.assertEqual(result["outcome_counts"]["critical_field_conflicts"], 1)
        csv_header = (output / "RECONCILIATION.csv").read_text(
            encoding="utf-8"
        ).splitlines()[0]
        self.assertNotIn("canonical", csv_header)
        self.assertIn("close__left", csv_header)
        self.assertIn("close__right", csv_header)

    def test_blank_critical_field_on_both_sides_is_not_a_match(self) -> None:
        header = "symbol,trade_date,close,route_code\n"
        prepared, _, _ = self._prepare(
            header + "000001,2026-07-31,,A\n",
            header + "000001,2026-07-31,,A\n",
        )
        result = RECON.execute(prepared)
        self.assertTrue(result["fail_closed"])
        self.assertEqual(result["outcome_counts"]["critical_field_conflicts"], 1)

    def test_noncritical_only_conflict_is_visible_qualified_pass(self) -> None:
        header = "symbol,trade_date,close,route_code\n"
        prepared, output, _ = self._prepare(
            header + "000001,2026-07-31,10,A\n",
            header + "000001,2026-07-31,10,B\n",
        )
        result = RECON.execute(prepared)
        self.assertFalse(result["fail_closed"])
        self.assertEqual(
            result["status"],
            "PASS_WITH_NONCRITICAL_CONFLICTS_INSPECTION_ONLY",
        )
        with (output / "RECONCILIATION.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["outcome"], "FIELD_CONFLICT")
        self.assertEqual(row["route_code__left"], "A")
        self.assertEqual(row["route_code__right"], "B")

    def test_duplicate_keys_fail_before_output_publish(self) -> None:
        header = "symbol,trade_date,close,route_code\n"
        prepared, output, _ = self._prepare(
            header
            + "000001,2026-07-31,10,A\n"
            + "000001,2026-07-31,10,A\n",
            header + "000001,2026-07-31,10,A\n",
        )
        with self.assertRaisesRegex(RECON.ReconcileError, "duplicate key"):
            RECON.execute(prepared)
        self.assertFalse(output.exists())

    def test_bad_hash_and_input_outside_tmp_are_rejected(self) -> None:
        left, left_payload = self._write(
            "left.csv", "symbol,trade_date,close\n000001,2026-07-31,10\n"
        )
        right, right_payload = self._write(
            "right.csv", "symbol,trade_date,close\n000001,2026-07-31,10\n"
        )
        run_id, output = self._output()
        bad_left = "|".join(
            [
                "SOURCE_LEFT",
                left.relative_to(WORKSPACE).as_posix(),
                "0" * 64,
                str(len(left_payload)),
            ]
        )
        with self.assertRaisesRegex(RECON.ReconcileError, "SHA-256 mismatch"):
            RECON.prepare(
                run_id,
                bad_left,
                self._spec("SOURCE_RIGHT", right, right_payload),
                ["symbol", "trade_date"],
                ["close|numeric|0|critical"],
                output,
            )
        with tempfile.TemporaryDirectory(dir=WORKSPACE) as outside_dir:
            outside = Path(outside_dir) / "outside.csv"
            outside_payload = b"symbol,trade_date,close\n000001,2026-07-31,10\n"
            outside.write_bytes(outside_payload)
            with self.assertRaisesRegex(RECON.ReconcileError, "stay below tmp"):
                RECON.prepare(
                    run_id,
                    self._spec("SOURCE_LEFT", outside, outside_payload),
                    self._spec("SOURCE_RIGHT", right, right_payload),
                    ["symbol", "trade_date"],
                    ["close|numeric|0|critical"],
                    output,
                )

    def test_invalid_numeric_nonfinite_and_schema_fail_closed(self) -> None:
        header = "symbol,trade_date,close,route_code\n"
        prepared, output, _ = self._prepare(
            header + "000001,2026-07-31,not-a-number,A\n",
            header + "000001,2026-07-31,10,A\n",
        )
        with self.assertRaisesRegex(RECON.ReconcileError, "non-numeric"):
            RECON.execute(prepared)
        self.assertFalse(output.exists())

        prepared2, output2, _ = self._prepare(
            header + "000001,2026-07-31,NaN,A\n",
            header + "000001,2026-07-31,10,A\n",
        )
        with self.assertRaisesRegex(RECON.ReconcileError, "non-finite"):
            RECON.execute(prepared2)
        self.assertFalse(output2.exists())

    def test_output_is_write_once_and_plan_preparation_writes_nothing(self) -> None:
        header = "symbol,trade_date,close,route_code\n"
        prepared, output, _ = self._prepare(
            header + "000001,2026-07-31,10,A\n",
            header + "000001,2026-07-31,10,A\n",
        )
        self.assertFalse(output.exists())
        RECON.execute(prepared)
        with self.assertRaisesRegex(RECON.ReconcileError, "write-once"):
            RECON.prepare(
                prepared["run_id"],
                "|".join(
                    [
                        prepared["left"].spec.source_id,
                        prepared["left"].spec.relative_path,
                        prepared["left"].actual_sha256,
                        str(prepared["left"].actual_bytes),
                    ]
                ),
                "|".join(
                    [
                        prepared["right"].spec.source_id,
                        prepared["right"].spec.relative_path,
                        prepared["right"].actual_sha256,
                        str(prepared["right"].actual_bytes),
                    ]
                ),
                prepared["keys"],
                ["close|numeric|0.01|critical"],
                output,
            )

    def test_rule_validation_rejects_ambiguity(self) -> None:
        keys = RECON.parse_keys(["symbol", "trade_date"])
        with self.assertRaisesRegex(RECON.ReconcileError, "overlaps"):
            RECON.parse_field_rules(["symbol|exact|0|critical"], keys)
        with self.assertRaisesRegex(RECON.ReconcileError, "zero tolerance"):
            RECON.parse_field_rules(["route_code|exact|1|critical"], keys)
        with self.assertRaisesRegex(RECON.ReconcileError, "finite and nonnegative"):
            RECON.parse_field_rules(["close|numeric|NaN|critical"], keys)


if __name__ == "__main__":
    unittest.main()
