from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT = WORKSPACE / "control" / "tools" / "verify_free_web_daily_probe_run.py"
SPEC = importlib.util.spec_from_file_location("free_web_daily_run_health", SCRIPT)
assert SPEC and SPEC.loader
MONITOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MONITOR
SPEC.loader.exec_module(MONITOR)

SINA = (
    'var hq_str_sh600000="浦发银行,9.590,9.710,9.510,9.590,9.280,9.510,'
    '9.520,138263755,1299502653.000,57372,9.510,135100,9.500,25000,9.490,'
    '25300,9.480,28700,9.470,142400,9.520,63800,9.530,135600,9.540,257260,'
    '9.550,163400,9.560,2026-07-31,15:34:59,00,D|14400|136944.00";\n'
).encode("gb18030")

TENCENT = (
    'v_sh600000="1~浦发银行~600000~9.51~9.71~9.59~1382638~671700~710937~'
    '9.51~574~9.50~1351~9.49~250~9.48~253~9.47~287~9.52~1424~9.53~638~'
    '9.54~1356~9.55~2573~9.56~1634~~20260731161454~-0.20~-2.06~9.59~9.28~'
    '9.51/1382638/1299502653~1382638~129950";\n'
).encode("gb18030")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FreeWebDailyRunHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        MONITOR.ALLOWED_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=MONITOR.ALLOWED_ROOT)
        self.parent = Path(self.temp.name)
        self.run = self.parent / "run"

        def fake_request(url: str, *, referer: str):
            del referer
            if "sinajs" in url:
                payload = SINA
                content_type = "application/javascript; charset=GB18030"
            else:
                payload = TENCENT
                content_type = "text/html; charset=GBK"
            started = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)
            completed = datetime(2026, 8, 1, 0, 0, 1, tzinfo=timezone.utc)
            return payload, {
                "http_status": 200,
                "content_type": content_type,
                "content_length": len(payload),
                "requested_url": url,
                "final_url": url,
                "redirect_count": 0,
                "network_attempt_count": 1,
                "request_started_at_utc": started.isoformat(),
                "response_completed_at_utc": completed.isoformat(),
                "response_completed_at_asia_shanghai": completed.astimezone(
                    MONITOR.PROBE.SHANGHAI_TZ
                ).isoformat(),
            }

        with mock.patch.object(MONITOR.PROBE, "request_once", side_effect=fake_request):
            MONITOR.PROBE.execute("2026-07-31", ["sh600000"], self.run)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_healthy_run_passes_without_modifying_any_file(self) -> None:
        before = {
            path.relative_to(self.run).as_posix(): (
                path.stat().st_size,
                path.stat().st_mtime_ns,
                digest(path),
            )
            for path in self.run.rglob("*")
            if path.is_file()
        }
        receipt = MONITOR.verify_run(self.run, "2026-07-31")
        after = {
            path.relative_to(self.run).as_posix(): (
                path.stat().st_size,
                path.stat().st_mtime_ns,
                digest(path),
            )
            for path in self.run.rglob("*")
            if path.is_file()
        }
        self.assertEqual(receipt["status"], "PASS_HEALTHY_BOUNDED_INSPECTION_ONLY")
        self.assertEqual(receipt["accepted_row_count"], 1)
        self.assertEqual(receipt["raw_source_count"], 2)
        self.assertFalse(receipt["formal_source_admission"])
        self.assertFalse(receipt["research_ready"])
        self.assertEqual(before, after)

    def test_raw_hash_tamper_fails_closed(self) -> None:
        with (self.run / "raw" / "sina.response").open("ab") as handle:
            handle.write(b"tamper")
        with self.assertRaisesRegex(MONITOR.HealthError, "byte count mismatch"):
            MONITOR.verify_run(self.run, "2026-07-31")

    def test_missing_source_fails_closed(self) -> None:
        (self.run / "raw" / "tencent.response").unlink()
        with self.assertRaisesRegex(MONITOR.HealthError, "required file is missing"):
            MONITOR.verify_run(self.run, "2026-07-31")

    def test_extra_file_fails_closed(self) -> None:
        (self.run / "unexpected.txt").write_text("extra", encoding="utf-8")
        with self.assertRaisesRegex(MONITOR.HealthError, "disk file set mismatch"):
            MONITOR.verify_run(self.run, "2026-07-31")

    def test_trade_date_and_accepted_count_mismatch_fail_closed(self) -> None:
        with self.assertRaisesRegex(MONITOR.HealthError, "target trade date"):
            MONITOR.verify_run(self.run, "2026-07-30")

        results_path = self.run / "RESULTS.json"
        results = json.loads(results_path.read_text(encoding="utf-8"))
        results["accepted_row_count"] = 0
        results_path.write_text(json.dumps(results), encoding="utf-8")
        self._refresh_manifest_entry("RESULTS.json")
        with self.assertRaisesRegex(MONITOR.HealthError, "all requested rows accepted"):
            MONITOR.verify_run(self.run, "2026-07-31")

    def test_formal_grant_fails_closed_even_when_rehashed(self) -> None:
        results_path = self.run / "RESULTS.json"
        results = json.loads(results_path.read_text(encoding="utf-8"))
        results["formal_source_admission"] = True
        results_path.write_text(json.dumps(results), encoding="utf-8")
        self._refresh_manifest_entry("RESULTS.json")
        with self.assertRaisesRegex(MONITOR.HealthError, "formal_source_admission"):
            MONITOR.verify_run(self.run, "2026-07-31")

    def test_normalized_semantic_tamper_fails_even_when_rehashed(self) -> None:
        csv_path = self.run / "normalized" / "reconciled.csv"
        text = csv_path.read_text(encoding="utf-8")
        csv_path.write_text(text.replace(",2\n", ",1\n"), encoding="utf-8")
        self._refresh_manifest_entry("normalized/reconciled.csv")
        with self.assertRaisesRegex(MONITOR.HealthError, "raw-source reconciliation"):
            MONITOR.verify_run(self.run, "2026-07-31")

    def test_capture_redirect_receipt_tamper_fails_closed(self) -> None:
        results_path = self.run / "RESULTS.json"
        results = json.loads(results_path.read_text(encoding="utf-8"))
        results["captures"][0]["redirect_count"] = 1
        results_path.write_text(json.dumps(results), encoding="utf-8")
        self._refresh_manifest_entry("RESULTS.json")
        manifest_path = self.run / "MANIFEST.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["execution_provenance"]["request_metadata"] = results["captures"]
        manifest_path.write_text(
            MONITOR.PROBE.canonical_json(manifest) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(MONITOR.HealthError, "zero redirects"):
            MONITOR.verify_run(self.run, "2026-07-31")

    def test_cli_anomaly_returns_nonzero_and_writes_nothing(self) -> None:
        (self.run / "quality" / "findings.json").write_text("[{}]", encoding="utf-8")
        self._refresh_manifest_entry("quality/findings.json")
        before = sorted(path.relative_to(self.run).as_posix() for path in self.run.rglob("*") if path.is_file())
        process = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                str(SCRIPT),
                "--run-root",
                str(self.run),
                "--trade-date",
                "2026-07-31",
            ],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            check=False,
        )
        after = sorted(path.relative_to(self.run).as_posix() for path in self.run.rglob("*") if path.is_file())
        self.assertEqual(process.returncode, 2)
        self.assertIn('"status": "FAIL_CLOSED"', process.stderr)
        self.assertEqual(before, after)

    def test_outside_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE / "tmp") as outside:
            with self.assertRaisesRegex(MONITOR.HealthError, "must stay below"):
                MONITOR.verify_run(Path(outside), "2026-07-31")

    def _refresh_manifest_entry(self, relative: str) -> None:
        manifest_path = self.run / "MANIFEST.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        target = self.run / Path(*relative.split("/"))
        for entry in manifest["files"]:
            if entry["path"] == relative:
                entry["bytes"] = target.stat().st_size
                entry["sha256"] = digest(target)
                break
        manifest_path.write_text(
            MONITOR.PROBE.canonical_json(manifest) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
