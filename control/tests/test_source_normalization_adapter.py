from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT = WORKSPACE / "control" / "tools" / "run_source_normalization_adapter.py"
SPEC = importlib.util.spec_from_file_location("source_normalization_adapter", SCRIPT)
assert SPEC and SPEC.loader
NORM = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = NORM
SPEC.loader.exec_module(NORM)


HEADER = "sid,dt,o,h,l,c,pc,vol,amt\n"
ROW_SH = "sh600000,2026-07-31,9.59,9.60,9.50,9.51,9.71,138263755,1299502653\n"
ROW_SZ = "sz000001,2026-07-31,10.50,10.60,10.40,10.55,10.45,1000,10550\n"


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class SourceNormalizationAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        NORM.INPUT_ROOT.mkdir(parents=True, exist_ok=True)
        NORM.REQUEST_ROOT.mkdir(parents=True, exist_ok=True)
        NORM.OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        self.seal_run_id = f"test-normalize-seal-{uuid.uuid4().hex}"
        self.run_id = f"test-normalize-run-{uuid.uuid4().hex}"
        self.seal_root = NORM.INPUT_ROOT / self.seal_run_id
        self.sealed = self.seal_root / "sealed" / "01-TEST_FIXTURE.raw"
        self.request_path = NORM.REQUEST_ROOT / f"{self.run_id}.json"
        self.output = NORM.OUTPUT_ROOT / self.run_id
        self._write_seal((HEADER + ROW_SH + ROW_SZ).encode("utf-8"))
        self.request = self._base_request()
        self._write_request()

    def tearDown(self) -> None:
        shutil.rmtree(self.seal_root, ignore_errors=True)
        shutil.rmtree(self.output, ignore_errors=True)
        self.request_path.unlink(missing_ok=True)
        for stage in NORM.OUTPUT_ROOT.glob(f".{self.run_id}.staging-*"):
            shutil.rmtree(stage, ignore_errors=True)

    def _write_seal(self, payload: bytes) -> None:
        shutil.rmtree(self.seal_root, ignore_errors=True)
        self.sealed.parent.mkdir(parents=True)
        self.sealed.write_bytes(payload)
        manifest = {
            "schema_version": 1,
            "contract_id": "local_raw_seal_v1",
            "seal_run_id": self.seal_run_id,
            "purpose_ceiling": "INSPECTION_ONLY",
            "formal_source_admission": False,
            "formal_catalog_written": False,
            "quality_promotion": False,
            "production_raw_store_written": False,
            "research_ready": False,
            "inputs": [
                {
                    "source_id": "TEST_FIXTURE",
                    "sealed_relative_path": "sealed/01-TEST_FIXTURE.raw",
                    "sealed_bytes": len(payload),
                    "sealed_sha256": digest(payload),
                }
            ],
        }
        manifest_payload = (NORM.canonical_json(manifest) + "\n").encode("utf-8")
        (self.seal_root / "SEAL_MANIFEST.json").write_bytes(manifest_payload)
        (self.seal_root / "SEAL_MANIFEST.sha256").write_text(
            f"{digest(manifest_payload)}  SEAL_MANIFEST.json\n", encoding="ascii"
        )

    def _base_request(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "contract_id": NORM.CONTRACT_ID,
            "run_id": self.run_id,
            "seal_run_id": self.seal_run_id,
            "sealed_relative_path": "sealed/01-TEST_FIXTURE.raw",
            "sealed_sha256": digest(self.sealed.read_bytes()),
            "source_id": "TEST_FIXTURE",
            "dataset_id": "fixture-a-share-daily",
            "trade_date": "2026-07-31",
            "timezone": "Asia/Shanghai",
            "available_at": "2026-07-31T16:15:00+08:00",
            "field_map": {
                "source_security_id": "sid",
                "trade_date": "dt",
                "open": "o",
                "high": "h",
                "low": "l",
                "close": "c",
                "previous_close": "pc",
                "volume": "vol",
                "amount": "amt",
            },
            "unit_map": dict(NORM.REQUIRED_UNITS),
            "security_id_map": {
                "sh600000": "CN.XSHG.600000",
                "sz000001": "CN.XSHE.000001",
            },
            "expected_permanent_security_ids": [
                "CN.XSHG.600000",
                "CN.XSHE.000001",
            ],
        }

    def _write_request(self) -> None:
        self.request_path.write_text(
            NORM.canonical_json(self.request) + "\n", encoding="utf-8"
        )

    def _replace_payload(self, text: str) -> None:
        payload = text.encode("utf-8")
        self._write_seal(payload)
        self.request["sealed_sha256"] = digest(payload)
        self._write_request()

    def test_plan_is_read_only_and_hash_bound(self) -> None:
        prepared = NORM.prepare(self.request_path)
        self.assertEqual(len(prepared["normalized"]), 2)
        self.assertFalse(self.output.exists())
        process = subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPT), "--request", str(self.request_path)],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["status"], "PLAN_SOURCE_NORMALIZATION_INSPECTION_ONLY")
        self.assertFalse(result["write_performed"])
        self.assertFalse(result["formal_source_admission"])
        self.assertFalse(self.output.exists())

    def test_execute_writes_exact_normalized_rows_and_row_lineage(self) -> None:
        receipt = NORM.execute(NORM.prepare(self.request_path))
        self.assertEqual(receipt["status"], "PASS_SOURCE_NORMALIZATION_INSPECTION_ONLY")
        self.assertEqual(receipt["row_count"], 2)
        self.assertFalse(receipt["formal_catalog_written"])
        self.assertFalse(receipt["research_ready"])
        self.assertEqual(
            sorted(path.name for path in self.output.iterdir()),
            ["MANIFEST.json", "RESULTS.json", "normalized.csv", "row_lineage.jsonl"],
        )
        with (self.output / "normalized.csv").open(
            "r", encoding="utf-8", newline=""
        ) as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["permanent_security_id"] for row in rows], [
            "CN.XSHE.000001",
            "CN.XSHG.600000",
        ])
        self.assertEqual(rows[1]["volume_shares"], "138263755")
        self.assertEqual(rows[1]["source_file_sha256"], digest(self.sealed.read_bytes()))
        lineage = [
            json.loads(line)
            for line in (self.output / "row_lineage.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(len(lineage), 2)
        self.assertEqual(lineage[0]["source_file_absolute_path"], str(self.sealed))
        self.assertEqual(
            lineage[0]["source_file_relative_path"],
            self.sealed.relative_to(WORKSPACE).as_posix(),
        )
        self.assertEqual(lineage[1]["raw_row_sha256"], digest(ROW_SH.rstrip("\n").encode()))
        self.assertEqual(lineage[1]["available_at"], "2026-07-31T16:15:00+08:00")
        manifest = json.loads((self.output / "MANIFEST.json").read_text(encoding="utf-8"))
        for entry in manifest["files"]:
            path = self.output / entry["path"]
            self.assertEqual(entry["bytes"], path.stat().st_size)
            self.assertEqual(entry["sha256"], digest(path.read_bytes()))

    def test_unknown_or_extra_security_fails_closed(self) -> None:
        extra = "bj920080,2026-07-31,5,5.1,4.9,5,5,100,500\n"
        self._replace_payload(HEADER + ROW_SH + ROW_SZ + extra)
        with self.assertRaisesRegex(NORM.NormalizationError, "unknown or extra security"):
            NORM.prepare(self.request_path)
        self.assertFalse(self.output.exists())

    def test_missing_expected_security_fails_closed(self) -> None:
        self._replace_payload(HEADER + ROW_SH)
        with self.assertRaisesRegex(NORM.NormalizationError, "security coverage mismatch"):
            NORM.prepare(self.request_path)

    def test_negative_nonfinite_blank_and_extra_field_fail_closed(self) -> None:
        bad_cases = [
            (HEADER + ROW_SH.replace(",138263755,", ",-1,") + ROW_SZ, "unsigned canonical"),
            (HEADER + ROW_SH.replace(",9.59,9.60,", ",NaN,9.60,") + ROW_SZ, "unsigned canonical"),
            (HEADER + ROW_SH.replace(",9.51,9.71,", ",,9.71,") + ROW_SZ, "blank or padded"),
            ("sid,dt,o,h,l,c,pc,vol,amt,extra\n" + ROW_SH.rstrip("\n") + ",x\n" + ROW_SZ.rstrip("\n") + ",x\n", "CSV header mismatch"),
        ]
        for text, error in bad_cases:
            with self.subTest(error=error):
                self._replace_payload(text)
                with self.assertRaisesRegex(NORM.NormalizationError, error):
                    NORM.prepare(self.request_path)

    def test_duplicate_date_ohlc_and_hash_tamper_fail_closed(self) -> None:
        self._replace_payload(HEADER + ROW_SH + ROW_SH)
        with self.assertRaisesRegex(NORM.NormalizationError, "duplicates a security"):
            NORM.prepare(self.request_path)

        self._replace_payload(HEADER + ROW_SH.replace("2026-07-31", "2026-07-30") + ROW_SZ)
        with self.assertRaisesRegex(NORM.NormalizationError, "trade_date mismatch"):
            NORM.prepare(self.request_path)

        self._replace_payload(HEADER + ROW_SH.replace("9.59,9.60,9.50,9.51", "9.59,9.55,9.50,9.56") + ROW_SZ)
        with self.assertRaisesRegex(NORM.NormalizationError, "high violates OHLC"):
            NORM.prepare(self.request_path)

        self._replace_payload(HEADER + ROW_SH + ROW_SZ)
        with self.sealed.open("ab") as handle:
            handle.write(b"tamper\n")
        with self.assertRaisesRegex(NORM.NormalizationError, "byte-count mismatch"):
            NORM.prepare(self.request_path)

    def test_identity_units_timezone_available_at_and_request_scope_fail_closed(self) -> None:
        cases = [
            ("unit_map", {**NORM.REQUIRED_UNITS, "volume": "LOT"}, "unit_map"),
            ("timezone", "UTC", "timezone"),
            ("available_at", "2026-07-31T08:15:00+00:00", "available_at"),
            ("available_at", "2026-07-30T16:15:00+08:00", "cannot precede"),
        ]
        for field, value, error in cases:
            with self.subTest(field=field, value=value):
                original = self.request[field]
                self.request[field] = value
                self._write_request()
                with self.assertRaisesRegex(NORM.NormalizationError, error):
                    NORM.prepare(self.request_path)
                self.request[field] = original
                self._write_request()

        outside = WORKSPACE / "tmp" / f"outside-{uuid.uuid4().hex}.json"
        try:
            outside.write_text(NORM.canonical_json(self.request), encoding="utf-8")
            with self.assertRaisesRegex(NORM.NormalizationError, "direct JSON file"):
                NORM.prepare(outside)
        finally:
            outside.unlink(missing_ok=True)

    def test_execute_is_write_once(self) -> None:
        prepared = NORM.prepare(self.request_path)
        NORM.execute(prepared)
        with self.assertRaisesRegex(NORM.NormalizationError, "never overwrite"):
            NORM.prepare(self.request_path)

    def test_cli_failure_is_nonzero_and_leaves_no_output(self) -> None:
        self.request["sealed_sha256"] = "0" * 64
        self._write_request()
        process = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                str(SCRIPT),
                "--request",
                str(self.request_path),
                "--execute",
            ],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(process.returncode, 2)
        self.assertIn('"status": "FAIL_CLOSED"', process.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
