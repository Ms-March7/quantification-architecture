from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = WORKSPACE_ROOT / "control" / "tools" / "run_source_admission_pilot.py"
SPEC = importlib.util.spec_from_file_location("source_admission_pilot", RUNNER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load pilot runner: {RUNNER_PATH}")
PILOT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PILOT)


class FakeResponse:
    def __init__(self, url: str, content: bytes, headers: dict[str, str] | None = None):
        self.url = url
        self.content = content
        self.status_code = 200
        self.headers = headers or {}
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        del chunk_size
        midpoint = max(1, len(self.content) // 2)
        yield self.content[:midpoint]
        yield self.content[midpoint:]

    def close(self) -> None:
        self.closed = True


class SourceAdmissionPilotTests(unittest.TestCase):
    def test_checked_in_matrix_never_grants_formal_actions(self) -> None:
        path = (
            WORKSPACE_ROOT
            / "data"
            / "catalog"
            / "contracts"
            / "source_admission_v1"
            / "source_matrix.json"
        )
        matrix = PILOT.load_source_matrix(path)
        self.assertEqual(
            matrix["status"],
            "PAUSED__BLOCKING_CONFLICTS_REPORTED__NO_FORMAL_ADMISSION",
        )
        self.assertFalse(matrix["execution_authorization_active"])
        self.assertTrue(matrix["resume_requires_user_start"])
        self.assertFalse(matrix["formal_admission"])
        self.assertFalse(matrix["bulk_backfill_authorized"])
        self.assertFalse(matrix["quality_promotion_authorized"])
        self.assertTrue(all(not item["formal_admission"] for item in matrix["sources"]))

    def test_matrix_rejects_formal_admission(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "matrix.json"
            source = {
                "source_key": "x",
                "source_id": "x",
                "plan_ids": ["CORE-01"],
                "pilot_decision": "PILOT_ALLOWED",
                "business_probe_ids": [],
                "purpose_ceiling": "INSPECTION_ONLY",
                "formal_admission": False,
                "formal_or_bulk_status": "BLOCKED",
                "rights": {},
                "pit_status": "BLOCKED",
                "field_semantics_status": "BLOCKED",
                "legal_evidence_urls": [],
                "blockers": [],
            }
            value = {
                "schema_version": 1,
                "contract_id": "x",
                "work_package": "x",
                "status": "ACTIVE__PILOT_ONLY__NO_FORMAL_ADMISSION",
                "execution_authorization_active": True,
                "resume_requires_user_start": False,
                "global_purpose_ceiling": "INSPECTION_ONLY",
                "formal_admission": True,
                "bulk_backfill_authorized": False,
                "quality_promotion_authorized": False,
                "sources": [source],
            }
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must not authorize"):
                PILOT.load_source_matrix(path)

    def test_paused_matrix_blocks_execution_authorization(self) -> None:
        path = (
            WORKSPACE_ROOT
            / "data"
            / "catalog"
            / "contracts"
            / "source_admission_v1"
            / "source_matrix.json"
        )
        matrix = PILOT.load_source_matrix(path)
        with self.assertRaisesRegex(ValueError, "explicit user restart"):
            PILOT.assert_execution_authorized(matrix)

    def test_request_rejects_credentials_and_non_allowlisted_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            target = root / "response.bin"
            with self.assertRaisesRegex(ValueError, "credential-bearing"):
                PILOT.validate_request(
                    "GET",
                    "https://www.sse.com.cn/home/legal/",
                    {"Authorization": "not-a-real-secret"},
                    target,
                    root,
                )
            with self.assertRaisesRegex(ValueError, "allowlist"):
                PILOT.validate_request(
                    "GET",
                    "https://example.com/data",
                    {},
                    target,
                    root,
                )

    def test_request_rejects_output_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            target = root.parent / "outside.bin"
            with self.assertRaisesRegex(ValueError, "inside the pilot"):
                PILOT.validate_request(
                    "GET",
                    "https://www.sse.com.cn/home/legal/",
                    {},
                    target,
                    root,
                )

    def test_cninfo_midnight_values_are_not_treated_as_exact_time(self) -> None:
        rows = [
            {"announcementTime": 1767110400000},
            {"announcementTime": 1767176531000},
            {"announcementTime": None},
        ]
        result = PILOT.summarize_cninfo_time_precision(rows)
        self.assertEqual(result["midnight_rows"], 1)
        self.assertEqual(result["non_midnight_rows"], 1)
        self.assertEqual(result["missing_or_invalid_rows"], 1)
        self.assertIn("DO_NOT_TREAT", result["decision"])

    def test_bounded_request_captures_hash_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            target = root / "evidence.bin"
            response = FakeResponse(
                "https://www.sse.com.cn/home/legal/",
                b"bounded-public-evidence",
                {"Content-Type": "text/plain"},
            )
            session = mock.Mock()
            session.request.return_value = response
            result = PILOT.bounded_request(
                session,
                method="GET",
                url=response.url,
                output_path=target,
                output_root=root,
                headers={"User-Agent": "test"},
                max_bytes=1024,
            )
            self.assertEqual(target.read_bytes(), b"bounded-public-evidence")
            self.assertEqual(result["bytes"], len(b"bounded-public-evidence"))
            self.assertTrue(response.closed)
            supplied_headers = session.request.call_args.kwargs["headers"]
            self.assertNotIn("Authorization", supplied_headers)
            self.assertNotIn("Cookie", supplied_headers)
            self.assertFalse(session.request.call_args.kwargs["allow_redirects"])

    def test_bounded_request_removes_partial_file_on_cap_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            target = root / "evidence.bin"
            response = FakeResponse(
                "https://www.sse.com.cn/home/legal/",
                b"0123456789",
            )
            session = mock.Mock()
            session.request.return_value = response
            with self.assertRaisesRegex(ValueError, "exceeds cap"):
                PILOT.bounded_request(
                    session,
                    method="GET",
                    url=response.url,
                    output_path=target,
                    output_root=root,
                    max_bytes=5,
                )
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".bin.part").exists())
            self.assertTrue(response.closed)

    def test_request_rejects_credentials_in_url_params_and_form(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            target = root / "response.bin"
            with self.assertRaisesRegex(ValueError, "userinfo"):
                PILOT.validate_request(
                    "GET",
                    "https://user:password@www.sse.com.cn/home/legal/",
                    {},
                    target,
                    root,
                )
            with self.assertRaisesRegex(ValueError, "parameter"):
                PILOT.validate_payload_keys({"api_key": "not-a-real-secret"}, "parameter")
            with self.assertRaisesRegex(ValueError, "form"):
                PILOT.validate_payload_keys({"token": "not-a-real-secret"}, "form")

    def test_probe_content_validation_failure_is_not_counted_as_pass(self) -> None:
        probes: dict[str, object] = {}
        PILOT.run_probe(
            probes,
            "invalid_content",
            lambda: {"capture_validation_passed": False},
        )
        self.assertEqual(
            probes["invalid_content"]["status"],
            "CONTENT_VALIDATION_FAILED",
        )

    def test_harden_session_disables_environment_credential_channels(self) -> None:
        session = mock.Mock()
        session.trust_env = True
        session.auth = ("user", "not-a-real-secret")
        session.proxies = {"https": "https://proxy.invalid"}
        session.cookies = mock.MagicMock()
        session.cookies.__bool__.return_value = False
        session.headers = {"Authorization": "not-a-real-secret", "Accept": "*/*"}
        guards = PILOT.harden_session(session)
        self.assertTrue(all(guards.values()))
        self.assertFalse(session.trust_env)
        self.assertIsNone(session.auth)
        self.assertNotIn("Authorization", session.headers)

    def test_public_page_marker_validation_rejects_wrong_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "page.html"
            path.write_text(
                "<html><title>验证码</title><body>请重试</body></html>",
                encoding="utf-8",
            )
            result = PILOT.validate_utf8_markers(
                path,
                ["法律声明", "上海证券交易所"],
            )
            self.assertFalse(result["passed"])
            self.assertEqual(
                result["missing_markers"],
                ["法律声明", "上海证券交易所"],
            )


if __name__ == "__main__":
    unittest.main()
