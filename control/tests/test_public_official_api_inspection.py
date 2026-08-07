from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "control" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from run_public_official_api_inspection import (  # noqa: E402
    InspectionError,
    execute_run,
    live_fetch,
    load_json,
    parse_bis_sdmx_xml,
    parse_ecb_sdmx_csv,
    parse_hkma_json,
    parse_us_treasury_atom_xml,
    validate_contract,
    validate_run_id,
)


CONTRACT_PATH = (
    ROOT
    / "data"
    / "catalog"
    / "contracts"
    / "public_official_api_inspection_v1"
    / "contract.json"
)


class FakeResponse:
    def __init__(
        self,
        *,
        raw: bytes,
        url: str,
        content_type: str,
        status: int = 200,
    ) -> None:
        self._raw = raw
        self._url = url
        self.status = status
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        return self._raw if size < 0 else self._raw[:size]


class PublicOfficialApiInspectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = load_json(CONTRACT_PATH)
        cls.contract_root = CONTRACT_PATH.parent
        cls.sources = {
            source["source_id"]: source
            for source in cls.contract["sources"]
        }

    def fixture_bytes(self, source_id: str) -> bytes:
        source = self.sources[source_id]
        return (self.contract_root / source["fixture_path"]).read_bytes()

    def test_contract_is_strictly_inspection_only(self) -> None:
        validate_contract(self.contract)
        self.assertEqual(
            self.contract["status"],
            "INSPECTION_ONLY__NOT_ADMITTED",
        )
        self.assertEqual(
            self.contract["network_policy"]["requests_per_source_max"],
            1,
        )
        self.assertEqual(self.contract["network_policy"]["retries"], 0)
        self.assertFalse(
            self.contract["publication_policy"]["formal_catalog_write"]
        )
        self.assertFalse(
            self.contract["publication_policy"]["formal_data_write"]
        )
        self.assertFalse(
            self.contract["publication_policy"]["source_admission"]
        )

    def test_contract_rejects_any_admission_flag(self) -> None:
        drifted = copy.deepcopy(self.contract)
        drifted["publication_policy"]["source_admission"] = True
        with self.assertRaises(InspectionError):
            validate_contract(drifted)

    def test_all_four_fixture_parsers_pass(self) -> None:
        parsers = {
            "ecb_data_portal": parse_ecb_sdmx_csv,
            "bis_statistics": parse_bis_sdmx_xml,
            "us_treasury_yield_curve": parse_us_treasury_atom_xml,
            "hkma_public_api": parse_hkma_json,
        }
        for source_id, parser in parsers.items():
            with self.subTest(source_id=source_id):
                rows, receipt = parser(
                    self.fixture_bytes(source_id),
                    self.sources[source_id],
                )
                self.assertGreaterEqual(len(rows), 1)
                self.assertTrue(receipt["raw_schema_sha256"])
                for row in rows:
                    self.assertIsNone(row["source_published_at"])
                    self.assertIsNone(row["available_at"])
                    self.assertIsNone(row["revision_id"])

    def test_offline_fixture_run_writes_receipts_and_no_network(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="public-official-adapter-test-"
        ) as temp:
            output = Path(temp) / "fixture-run"
            with mock.patch(
                "run_public_official_api_inspection.urllib.request.urlopen"
            ) as mocked_urlopen:
                summary = execute_run(
                    contract_path=CONTRACT_PATH,
                    output=output,
                    mode="fixture",
                )
            mocked_urlopen.assert_not_called()
            self.assertEqual(
                summary["status"],
                "FIXTURE_MECHANICS_PASS__NO_SOURCE_ADMISSION",
            )
            self.assertEqual(summary["source_pass_count"], 4)
            self.assertEqual(summary["network_requests"], 0)
            self.assertFalse(summary["formal_catalog_write"])
            self.assertFalse(summary["formal_data_write"])
            self.assertFalse(summary["source_admission"])
            for source_id in self.sources:
                receipt = json.loads(
                    (
                        output
                        / "sources"
                        / source_id
                        / "RECEIPT.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    receipt["candidate_status"],
                    "INSPECTION_ONLY__NOT_ADMITTED",
                )
                self.assertIsNone(
                    receipt["pit_evidence"]["available_at"]
                )
                self.assertEqual(
                    receipt["rights_evidence"]["status"][
                        "automated_incremental_access"
                    ],
                    "NOT_ADMITTED",
                )
                self.assertTrue(receipt["raw"]["sha256"])

    def test_schema_drift_is_quarantined_without_normalized_output(self) -> None:
        source = self.sources["hkma_public_api"]
        payload = json.loads(self.fixture_bytes("hkma_public_api"))
        del payload["result"]["records"][0]["cny"]
        raw = json.dumps(payload).encode("utf-8")

        def drift_fetcher(source_arg, policy):
            self.assertEqual(source_arg["source_id"], "hkma_public_api")
            self.assertEqual(policy["retries"], 0)
            return raw, {
                "request_count": 1,
                "retries": 0,
                "pagination": False,
                "method": "GET",
                "url": source_arg["endpoint"],
                "requested_at_utc": "2026-07-30T00:00:00+00:00",
                "fetched_at_utc": "2026-07-30T00:00:01+00:00",
                "http_status": 200,
                "final_url": source_arg["endpoint"],
                "content_type": "application/json",
            }

        with tempfile.TemporaryDirectory(
            prefix="public-official-adapter-drift-"
        ) as temp:
            output = Path(temp) / "drift-run"
            summary = execute_run(
                contract_path=CONTRACT_PATH,
                output=output,
                mode="live",
                selected_source_ids=["hkma_public_api"],
                fetcher=drift_fetcher,
            )
            self.assertEqual(
                summary["status"],
                "FAIL_CLOSED__ONE_OR_MORE_SOURCES_QUARANTINED",
            )
            source_output = output / "sources" / "hkma_public_api"
            self.assertTrue((source_output / "QUARANTINE.json").is_file())
            self.assertFalse(
                (source_output / "normalized-inspection.jsonl").exists()
            )
            self.assertTrue((source_output / "raw-response.json").is_file())

    def test_live_transport_failure_records_the_single_attempt(self) -> None:
        def failed_fetcher(source_arg, policy):
            self.assertEqual(policy["retries"], 0)
            raise OSError("synthetic transport failure")

        with tempfile.TemporaryDirectory(
            prefix="public-official-adapter-transport-"
        ) as temp:
            output = Path(temp) / "transport-run"
            summary = execute_run(
                contract_path=CONTRACT_PATH,
                output=output,
                mode="live",
                selected_source_ids=["ecb_data_portal"],
                fetcher=failed_fetcher,
            )
            self.assertEqual(summary["network_requests"], 1)
            self.assertEqual(summary["source_quarantine_count"], 1)
            receipt = json.loads(
                (
                    output
                    / "sources"
                    / "ecb_data_portal"
                    / "RECEIPT.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["request"]["request_count"], 1)
            self.assertEqual(receipt["request"]["retries"], 0)
            self.assertEqual(
                receipt["request"]["outcome"],
                "ATTEMPT_STARTED__NO_RESPONSE_YET",
            )

    def test_live_fetch_rejects_redirect_host_and_oversized_payload(self) -> None:
        source = self.sources["ecb_data_portal"]
        policy = copy.deepcopy(self.contract["network_policy"])
        valid_raw = self.fixture_bytes("ecb_data_portal")

        redirected = FakeResponse(
            raw=valid_raw,
            url="https://example.com/escaped",
            content_type="text/csv",
        )
        with mock.patch(
            "run_public_official_api_inspection.urllib.request.urlopen",
            return_value=redirected,
        ):
            with self.assertRaises(InspectionError):
                live_fetch(source, policy)

        policy["response_bytes_max"] = 8
        oversized = FakeResponse(
            raw=b"123456789",
            url=source["endpoint"],
            content_type="text/csv",
        )
        with mock.patch(
            "run_public_official_api_inspection.urllib.request.urlopen",
            return_value=oversized,
        ):
            with self.assertRaises(InspectionError):
                live_fetch(source, policy)

    def test_invalid_run_ids_and_existing_output_fail_closed(self) -> None:
        for invalid in ("", "../escape", "space id", "x" * 129):
            with self.subTest(invalid=invalid):
                with self.assertRaises(InspectionError):
                    validate_run_id(invalid)
        self.assertEqual(validate_run_id("safe-run_20260730.v1"), "safe-run_20260730.v1")

        with tempfile.TemporaryDirectory(
            prefix="public-official-adapter-existing-"
        ) as temp:
            output = Path(temp) / "existing"
            output.mkdir()
            with self.assertRaises(InspectionError):
                execute_run(
                    contract_path=CONTRACT_PATH,
                    output=output,
                    mode="fixture",
                )


if __name__ == "__main__":
    unittest.main()
