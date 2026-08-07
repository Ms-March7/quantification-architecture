from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT = WORKSPACE / "control" / "tools" / "run_free_web_daily_probe.py"
SPEC = importlib.util.spec_from_file_location("free_web_daily_probe", SCRIPT)
assert SPEC and SPEC.loader
PROBE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PROBE
SPEC.loader.exec_module(PROBE)


SINA = (
    'var hq_str_sh600000="浦发银行,9.590,9.710,9.510,9.590,9.280,9.510,'
    '9.520,138263755,1299502653.000,57372,9.510,135100,9.500,25000,9.490,'
    '25300,9.480,28700,9.470,142400,9.520,63800,9.530,135600,9.540,257260,'
    '9.550,163400,9.560,2026-07-31,15:34:59,00,D|14400|136944.00";\n'
    'var hq_str_bj920080="奥美森,20.800,20.600,20.750,20.980,20.510,20.730,'
    '20.750,170061,3544056.860,200,20.730,717,20.700,1000,20.580,1500,20.530,'
    '250,20.520,51707,20.750,1400,20.790,720,20.870,146,20.910,400,20.930,'
    '2026-07-31,15:30:02,00,22.9296,0.0000,0,8300000,B,T";\n'
).encode("gb18030")

TENCENT = (
    'v_sh600000="1~浦发银行~600000~9.51~9.71~9.59~1382638~671700~710937~'
    '9.51~574~9.50~1351~9.49~250~9.48~253~9.47~287~9.52~1424~9.53~638~'
    '9.54~1356~9.55~2573~9.56~1634~~20260731161454~-0.20~-2.06~9.59~9.28~'
    '9.51/1382638/1299502653~1382638~129950";\n'
    'v_bj920080="62~奥美森~920080~20.75~20.60~20.80~1701~834~867~20.73~2~'
    '20.70~7~20.58~10~20.53~15~20.52~3~20.75~517~20.79~14~20.87~7~'
    '20.91~1~20.93~4~~20260731153802~0.15~0.73~20.98~20.51~'
    '20.75/1701/3544057~1701~354.41";\n'
).encode("gb18030")


class FreeWebDailyProbeTests(unittest.TestCase):
    def test_dual_source_reconciliation_accepts_sh_and_bj(self) -> None:
        sina = PROBE.parse_sina(SINA)
        tencent = PROBE.parse_tencent(TENCENT)
        accepted, findings = PROBE.reconcile(
            ["sh600000", "bj920080"], "2026-07-31", sina, tencent
        )
        self.assertEqual(len(accepted), 2)
        self.assertEqual(findings, [])
        self.assertEqual(accepted[0]["volume_cross_source_delta_shares"], 45)
        self.assertAlmostEqual(accepted[1]["amount_cross_source_delta_cny"], 0.14)
        self.assertEqual(accepted[0]["tencent_route_code"], "1")
        self.assertIn(
            "NOT_TRADING_STATUS", accepted[0]["tencent_route_code_semantics"]
        )

    def test_wrong_date_fails_closed(self) -> None:
        sina = {"sh600000": PROBE.parse_sina(SINA)["sh600000"]}
        tencent = {"sh600000": PROBE.parse_tencent(TENCENT)["sh600000"]}
        accepted, findings = PROBE.reconcile(
            ["sh600000"], "2026-07-30", sina, tencent
        )
        self.assertEqual(accepted, [])
        self.assertIn("SINA_DATE_MISMATCH", findings[0]["reasons"])
        self.assertIn("TENCENT_DATE_MISMATCH", findings[0]["reasons"])

    def test_amount_disagreement_fails_closed(self) -> None:
        sina = {"sh600000": PROBE.parse_sina(SINA)["sh600000"]}
        tencent = {"sh600000": PROBE.parse_tencent(TENCENT)["sh600000"]}
        quote = tencent["sh600000"]
        tencent["sh600000"] = PROBE.Quote(
            **{**PROBE.asdict(quote), "amount_cny": quote.amount_cny + 2.0}
        )
        accepted, findings = PROBE.reconcile(
            ["sh600000"], "2026-07-31", sina, tencent
        )
        self.assertEqual(accepted, [])
        self.assertIn("AMOUNT_MISMATCH", findings[0]["reasons"])

    def test_negative_volume_and_amount_fail_closed(self) -> None:
        sina = {"sh600000": PROBE.parse_sina(SINA)["sh600000"]}
        tencent = {"sh600000": PROBE.parse_tencent(TENCENT)["sh600000"]}
        left = sina["sh600000"]
        right = tencent["sh600000"]
        sina["sh600000"] = PROBE.Quote(
            **{**PROBE.asdict(left), "volume_shares": -1, "amount_cny": -1.0}
        )
        tencent["sh600000"] = PROBE.Quote(
            **{**PROBE.asdict(right), "volume_shares": -1, "amount_cny": -1.0}
        )
        accepted, findings = PROBE.reconcile(
            ["sh600000"], "2026-07-31", sina, tencent
        )
        self.assertEqual(accepted, [])
        self.assertIn("SINA_NEGATIVE_VOLUME", findings[0]["reasons"])
        self.assertIn("TENCENT_NEGATIVE_AMOUNT", findings[0]["reasons"])

    def test_unexpected_response_symbol_fails_closed(self) -> None:
        sina = PROBE.parse_sina(SINA)
        tencent = PROBE.parse_tencent(TENCENT)
        with self.assertRaisesRegex(PROBE.ProbeError, "unexpected symbols"):
            PROBE.reconcile(["sh600000"], "2026-07-31", sina, tencent)

    def test_same_day_waits_until_shanghai_close_buffer(self) -> None:
        before = datetime(2026, 7, 31, 15, 44, tzinfo=PROBE.SHANGHAI_TZ)
        after = datetime(2026, 7, 31, 15, 45, tzinfo=PROBE.SHANGHAI_TZ)
        with self.assertRaisesRegex(PROBE.ProbeError, "before 15:45"):
            PROBE.validate_same_day_finality("2026-07-31", now=before)
        receipt = PROBE.validate_same_day_finality("2026-07-31", now=after)
        self.assertEqual(receipt["timezone"], "Asia/Shanghai")

    def test_redirect_handler_never_follows(self) -> None:
        handler = PROBE.NoRedirectHandler()
        request = PROBE.urllib.request.Request("https://example.invalid/a")
        self.assertIsNone(
            handler.redirect_request(
                request, None, 302, "Found", {}, "https://example.invalid/b"
            )
        )

    def test_malformed_symbol_and_oversized_batch_are_rejected(self) -> None:
        with self.assertRaises(PROBE.ProbeError):
            PROBE.validate_symbols(["600000"])
        with self.assertRaises(PROBE.ProbeError):
            PROBE.validate_symbols([f"sh{index:06d}" for index in range(21)])

    def test_output_must_stay_in_inspection_root(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE / "tmp") as directory:
            outside = Path(directory) / "run"
            with self.assertRaises(PROBE.ProbeError):
                PROBE.validate_output_root(outside)

    def test_result_boundary_never_grants_formal_use(self) -> None:
        result = PROBE.boundary("PASS_BOUNDED_DUAL_SOURCE_INSPECTION_ONLY")
        self.assertEqual(result["purpose_ceiling"], "INSPECTION_ONLY")
        self.assertFalse(result["formal_source_admission"])
        self.assertFalse(result["formal_catalog_written"])
        self.assertFalse(result["quality_promotion"])
        self.assertFalse(result["research_ready"])

    def test_execute_publishes_atomically_and_binds_provenance(self) -> None:
        allowed = WORKSPACE / "tmp" / "free-web-daily-probe"
        allowed.mkdir(parents=True, exist_ok=True)
        def fake_request(url: str, *, referer: str):
            del referer
            payload = SINA if "sinajs" in url else TENCENT
            return payload, {
                "http_status": 200,
                "content_type": "text/plain",
                "content_length": len(payload),
                "requested_url": url,
                "final_url": url,
                "redirect_count": 0,
                "network_attempt_count": 1,
                "request_started_at_utc": "2026-08-01T00:00:00+00:00",
                "response_completed_at_utc": "2026-08-01T00:00:01+00:00",
                "response_completed_at_asia_shanghai": "2026-08-01T08:00:01+08:00",
            }
        with tempfile.TemporaryDirectory(dir=allowed) as directory:
            output = Path(directory) / "run"
            with mock.patch.object(
                PROBE,
                "request_once",
                side_effect=fake_request,
            ):
                result = PROBE.execute(
                    "2026-07-31", ["sh600000", "bj920080"], output
                )
            self.assertTrue(output.is_dir())
            self.assertTrue(result["status"].startswith("PASS"))
            manifest = PROBE.json.loads(
                (output / "MANIFEST.json").read_text(encoding="utf-8")
            )
            provenance = manifest["execution_provenance"]
            self.assertEqual(len(provenance["script"]["sha256"]), 64)
            self.assertEqual(len(provenance["contract"]["contract_sha256"]), 64)
            self.assertEqual(len(provenance["request_metadata"]), 2)
            self.assertFalse(
                list(Path(directory).glob(".run.staging-*")),
                "staging must be atomically consumed",
            )

    def test_failed_execute_removes_staging_and_never_publishes(self) -> None:
        allowed = WORKSPACE / "tmp" / "free-web-daily-probe"
        allowed.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=allowed) as directory:
            output = Path(directory) / "run"
            with mock.patch.object(
                PROBE,
                "request_once",
                side_effect=PROBE.ProbeError("synthetic failure"),
            ):
                with self.assertRaisesRegex(PROBE.ProbeError, "synthetic failure"):
                    PROBE.execute("2026-07-31", ["sh600000"], output)
            self.assertFalse(output.exists())
            self.assertFalse(list(Path(directory).glob(".run.staging-*")))

    def test_intraday_observation_fails_closed(self) -> None:
        sina = {"sh600000": PROBE.parse_sina(SINA)["sh600000"]}
        tencent = {"sh600000": PROBE.parse_tencent(TENCENT)["sh600000"]}
        quote = sina["sh600000"]
        sina["sh600000"] = PROBE.Quote(
            **{**PROBE.asdict(quote), "observed_time": "14:59:59"}
        )
        accepted, findings = PROBE.reconcile(
            ["sh600000"], "2026-07-31", sina, tencent
        )
        self.assertEqual(accepted, [])
        self.assertIn("SINA_INTRADAY_OBSERVATION", findings[0]["reasons"])

    def test_incomplete_capture_metadata_fails_before_publication(self) -> None:
        allowed = WORKSPACE / "tmp" / "free-web-daily-probe"
        allowed.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=allowed) as directory:
            output = Path(directory) / "run"
            with mock.patch.object(
                PROBE,
                "request_once",
                return_value=(SINA, {"http_status": 200}),
            ):
                with self.assertRaisesRegex(PROBE.ProbeError, "metadata missing"):
                    PROBE.execute("2026-07-31", ["sh600000"], output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
