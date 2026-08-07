from __future__ import annotations

import hashlib
import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
TOOLS = WORKSPACE / "control/tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import build_r0_shsz_pv_publication_package as package_builder


def canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def binding(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "relative_path": path.relative_to(WORKSPACE).as_posix(),
        "sha256": digest(payload),
        "bytes": len(payload),
    }


class R0SHSZPVPublicationPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token = uuid.uuid4().hex
        self.fixture = (
            WORKSPACE
            / "tmp/r0-shsz-pv-publication-package-tests"
            / self.token
        )
        self.candidate = self.fixture / "candidate"
        self.candidate.mkdir(parents=True)
        self.package_id = f"r0-shsz-pv-package-test-{self.token}"
        self.spec = (
            package_builder.REQUEST_ROOT / f"test-{self.token}.json"
        )
        self.package = package_builder.PACKAGE_ROOT / self.package_id
        self.preflight = (
            package_builder.PREFLIGHT_REQUEST_ROOT
            / f"preflight-{self.package_id}.json"
        )
        self.execution = (
            package_builder.EXECUTION_REQUEST_ROOT
            / f"execution-{self.package_id}.json"
        )
        self._write_candidate()
        self._write_spec()
        self.guard_before = package_builder._guard_state(WORKSPACE)  # noqa: SLF001

    def tearDown(self) -> None:
        shutil.rmtree(self.fixture, ignore_errors=True)
        shutil.rmtree(self.package, ignore_errors=True)
        self.spec.unlink(missing_ok=True)
        self.preflight.unlink(missing_ok=True)
        self.execution.unlink(missing_ok=True)

    @staticmethod
    def _write_json(path: Path, value: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical(value))

    def _receipt(self, path: Path) -> dict[str, object]:
        payload = path.read_bytes()
        return {
            "relative_path": path.relative_to(WORKSPACE).as_posix(),
            "sha256": digest(payload),
            "bytes": len(payload),
        }

    def _write_candidate(self) -> None:
        raw = self.candidate / "raw/TERMINAL_SESSION_RECEIPT.json"
        source = self.candidate / "SOURCE_BINDING.json"
        schema = self.candidate / "SCHEMA.json"
        partition = self.candidate / "partition-manifest.json"
        quality = self.candidate / "quality/summary.json"
        provenance = self.candidate / "provenance.json"
        parquet = (
            self.candidate
            / "curated/schema=r0-shsz-pv-panel-v2/year=2026/month=08/part-000.parquet"
        )
        self._write_json(
            raw,
            {
                "schema_version": 1,
                "credential_value_captured": False,
                "status": "TEST_FIXTURE",
            },
        )
        self._write_json(source, {"schema_version": 1, "status": "TEST_FIXTURE"})
        self._write_json(
            schema,
            {
                "schema_version": 1,
                "schema_id": "r0-shsz-pv-panel-v2",
                "primary_key": ["order_book_id", "trade_date"],
            },
        )
        parquet.parent.mkdir(parents=True)
        parquet.write_bytes(b"PAR1 r0 shsz publication package fixture PAR1\n")
        self._write_json(
            partition,
            {
                "schema_version": 1,
                "dataset_id": package_builder.DATASET_ID,
                "snapshot_id": package_builder.SNAPSHOT_ID,
                "partition_count": 1,
                "payload_contract_sha256": "1" * 64,
                "partitions": [
                    {
                        **self._receipt(parquet),
                        "schema": "r0-shsz-pv-panel-v2",
                        "year": 2026,
                        "month": 8,
                        "row_count": 2,
                        "securities": 1,
                        "date_min": "2026-08-03",
                        "date_max": "2026-08-03",
                    }
                ],
            },
        )
        self._write_json(
            quality,
            {
                "schema_version": 1,
                "run_id": "r0-shsz-pv-panel-candidate-20260804-v1",
                "contract_id": package_builder.R0_CONTRACT_ID,
                "dataset_id": package_builder.DATASET_ID,
                "snapshot_id": package_builder.SNAPSHOT_ID,
                "status": "PASS__CANDIDATE_MECHANICAL_QUALITY__NOT_FORMALLY_ADMITTED",
                "all_checks_pass": True,
                "checks": {"fixture_mechanical_check": True},
                "summary": {
                    "rows": 2,
                    "date_min": "2005-01-04",
                    "date_max": "2026-08-03",
                    "securities": 1,
                    "trading_dates": 2,
                },
                "formal_source_admission": False,
                "formal_catalog_written": False,
                "research_ready": False,
            },
        )
        self._write_json(
            provenance,
            {
                "schema_version": 1,
                "contract_id": package_builder.R0_CONTRACT_ID,
                "formal_source_admission": False,
                "formal_catalog_written": False,
                "factor_runs": 0,
            },
        )
        artifacts = [
            self._receipt(path)
            for path in (raw, source, schema, partition, quality, provenance, parquet)
        ]
        self._write_json(
            self.candidate / "CANDIDATE_MANIFEST.json",
            {
                "schema_version": 1,
                "contract_id": "candidate_manifest_v1",
                "candidate_id": "r0-shsz-pv-panel-candidate-20260804-v1",
                "dataset_id": package_builder.DATASET_ID,
                "snapshot_id": package_builder.SNAPSHOT_ID,
                "purpose_ceiling": "INSPECTION_ONLY",
                "formal_source_admission": False,
                "formal_catalog_written": False,
                "research_ready": False,
                "artifacts": artifacts,
            },
        )
        self._write_json(
            self.candidate / "manifest.json",
            {
                "schema_version": 1,
                "contract_id": package_builder.R0_CONTRACT_ID,
                "formal_effect": "NONE__QUARANTINE_ONLY",
            },
        )

    def _write_spec(self) -> None:
        rights_bindings = {
            "csmar_rights_receipt": binding(
                WORKSPACE
                / "reports/data-governance/csmar-user-attested-rights-receipt-20260730-v1/RIGHTS_RECEIPT.json"
            ),
            "csmar_rights_verification": binding(
                WORKSPACE
                / "reports/data-governance/csmar-user-attested-rights-receipt-20260730-v1/VERIFICATION.json"
            ),
            "rqdata_quota_receipt": binding(
                WORKSPACE
                / "reports/data-governance/ricequant-r0-coverage-probe-20260803-v1/POST_RUN_QUOTA.json"
            ),
        }
        candidate_binding = binding(self.candidate / "CANDIDATE_MANIFEST.json")
        authorization_path = self.fixture / "execution-authorization-v2.json"
        self.authorization_path = authorization_path
        plan_stub = {
            "package_id": self.package_id,
            "candidate_binding": candidate_binding,
            "rights_bindings": rights_bindings,
            "license_observation": {
                "license_type": "EDUCATION",
                "valid_until": "2028-08-31",
                "observation_source": "USER_PROVIDED_SCREENSHOT_AND_BOUNDED_QUOTA_RECEIPT",
                "independent_verification": False,
                "credential_value_captured": False,
            },
            "authorization": {
                "source_thread_id": "019fc5c9-f9c2-72c2-8819-f2f6659d1c76"
            },
        }
        paths = package_builder._package_paths(  # noqa: SLF001
            plan_stub,
            workspace_root=WORKSPACE,
            package_root=package_builder.PACKAGE_ROOT,
            preflight_root=package_builder.PREFLIGHT_REQUEST_ROOT,
            execution_root=package_builder.EXECUTION_REQUEST_ROOT,
        )
        _, stage, expected = package_builder._build_rights_chain(  # noqa: SLF001
            plan_stub,
            paths,
            workspace_root=WORKSPACE,
        )
        self._write_json(
            authorization_path,
            {
                "schema_version": 2,
                "contract_id": package_builder.AUTHORIZATION_CONTRACT_ID,
                "authorization_id": f"auth-{self.token}",
                "action": "AUTHORIZE_EXACT_FORMAL_SNAPSHOT_PUBLICATION",
                "authorized_by": "USER",
                "authorized_at_utc": "2026-08-03T18:00:00Z",
                "statement": "User authorized this exact formal R0_SHSZ_PV snapshot publication.",
                "source_thread_id": "019fc5c9-f9c2-72c2-8819-f2f6659d1c76",
                "scope": package_builder.NARROW_STAGE_TARGETS,
                "dataset_id": package_builder.DATASET_ID,
                "snapshot_id": package_builder.SNAPSHOT_ID,
                "target_relative_path": package_builder.TARGET_RELATIVE,
                "candidate_manifest_sha256": candidate_binding["sha256"],
                "stage_a_bundle_sha256": stage["bundle_sha256"],
                **expected,
                "rights_status": "DECLARED__NOT_VERIFIED__NO_LEGAL_OPINION",
                "independent_contract_verification": False,
                "legal_opinion": False,
                "credential_value_captured": False,
                "single_use": True,
            },
        )
        self.request = {
            "schema_version": 2,
            "contract_id": package_builder.CONTRACT_ID,
            "package_id": self.package_id,
            "candidate_manifest": candidate_binding,
            "r0_contract": binding(package_builder.R0_CONTRACT_PATH),
            "rights_basis_bindings": rights_bindings,
            "education_license_observation": {
                "license_type": "EDUCATION",
                "valid_until": "2028-08-31",
                "observation_source": "USER_PROVIDED_SCREENSHOT_AND_BOUNDED_QUOTA_RECEIPT",
                "independent_verification": False,
                "credential_value_captured": False,
            },
            "execution_authorization": binding(authorization_path),
            "publication_timestamps": {
                "available_at": "2026-08-04T09:30:00+08:00",
            },
        }
        self._write_json(self.spec, self.request)

    def _build(self, *, materialize: bool) -> dict[str, object]:
        return package_builder.build_package(
            request=self.spec,
            materialize=materialize,
            allow_test_candidate=True,
        )

    def test_default_plan_performs_no_write(self) -> None:
        result = self._build(materialize=False)
        self.assertEqual(result["status"], "PASS__PACKAGE_PLAN_READY__NO_WRITE")
        self.assertFalse(result["write_performed"])
        self.assertFalse(self.package.exists())
        self.assertFalse(self.preflight.exists())
        self.assertFalse(self.execution.exists())
        self.assertEqual(package_builder._guard_state(WORKSPACE), self.guard_before)  # noqa: SLF001

    def test_materialize_runs_all_dry_runs_and_never_publishes(self) -> None:
        result = self._build(materialize=True)
        self.assertEqual(
            result["status"],
            "PASS__PACKAGE_MATERIALIZED__ALL_PUBLISHERS_DRY_RUN__NO_FORMAL_EFFECT",
        )
        self.assertEqual(
            result["workspace_publisher_status"],
            "PASS__WORKSPACE_EXECUTION_DISABLED__NO_WRITE",
        )
        self.assertFalse(result["formal_publication_performed"])
        self.assertTrue(self.package.is_dir())
        self.assertTrue(self.preflight.is_file())
        self.assertTrue(self.execution.is_file())
        execution = json.loads(self.execution.read_text("utf-8"))
        r0_contract = json.loads(package_builder.R0_CONTRACT_PATH.read_text("utf-8"))
        self.assertEqual(
            execution["publication_profile"]["research_input_contract"],
            r0_contract["research_input_contract"],
        )
        self.assertEqual(execution["publication_profile"]["license_status"], "DECLARED")
        self.assertNotIn("published_at", execution["publication_profile"])
        self.assertEqual(
            execution["publication_profile"]["published_at_policy"],
            "WORKSPACE_ATOMIC_COMMIT_CLOCK",
        )
        self.assertFalse((self.package / "authorization.json").exists())
        stage = json.loads((self.package / "stage-a.json").read_text("utf-8"))
        self.assertEqual(stage["bundle"]["targets"], package_builder.NARROW_STAGE_TARGETS)
        boundary = json.loads(
            (self.package / "PACKAGE_BOUNDARY.json").read_text("utf-8")
        )
        self.assertEqual(boundary["canonical_core20_effect"], "NONE")
        rights = json.loads((self.package / "RIGHTS_BASIS.json").read_text("utf-8"))
        self.assertFalse(rights["independent_contract_verification"])
        self.assertFalse(rights["legal_opinion"])
        self.assertFalse(rights["credential_value_captured"])
        self.assertFalse((WORKSPACE / package_builder.TARGET_RELATIVE).exists())
        self.assertEqual(package_builder._guard_state(WORKSPACE), self.guard_before)  # noqa: SLF001

    def test_candidate_hash_drift_fails_before_package_write(self) -> None:
        self.request["candidate_manifest"]["sha256"] = "0" * 64
        self._write_json(self.spec, self.request)
        with self.assertRaises(package_builder.PackageBuildError):
            self._build(materialize=True)
        self.assertFalse(self.package.exists())
        self.assertFalse(self.preflight.exists())
        self.assertFalse(self.execution.exists())

    def test_candidate_path_escape_fails_before_package_write(self) -> None:
        candidate_path = self.candidate / "CANDIDATE_MANIFEST.json"
        candidate = json.loads(candidate_path.read_text("utf-8"))
        candidate["artifacts"][0]["relative_path"] = "../escape.json"
        self._write_json(candidate_path, candidate)
        self.request["candidate_manifest"] = binding(candidate_path)
        self._write_json(self.spec, self.request)
        with self.assertRaises(package_builder.PackageBuildError):
            self._build(materialize=True)
        self.assertFalse(self.package.exists())

    def test_secret_like_extra_field_is_rejected(self) -> None:
        self.request["password"] = "must-not-be-read"
        self._write_json(self.spec, self.request)
        with self.assertRaises(package_builder.PackageBuildError):
            self._build(materialize=False)
        self.assertFalse(self.package.exists())

    def test_independent_authorization_rights_hash_drift_is_rejected(self) -> None:
        authorization = json.loads(self.authorization_path.read_text("utf-8"))
        authorization["rights_basis_sha256"] = "0" * 64
        self._write_json(self.authorization_path, authorization)
        self.request["execution_authorization"] = binding(self.authorization_path)
        self._write_json(self.spec, self.request)
        with self.assertRaises(package_builder.PackageBuildError):
            self._build(materialize=False)
        self.assertFalse(self.package.exists())

    def test_collision_never_overwrites_existing_package(self) -> None:
        self._build(materialize=True)
        manifest = self.package / "PACKAGE_MANIFEST.json"
        before = manifest.read_bytes()
        with self.assertRaises(package_builder.PackageBuildError):
            self._build(materialize=True)
        self.assertEqual(manifest.read_bytes(), before)

    def test_cli_has_no_formal_publish_or_workspace_override(self) -> None:
        options = {action.dest for action in package_builder.parser()._actions}
        self.assertIn("materialize_package", options)
        self.assertNotIn("execute_workspace", options)
        self.assertNotIn("workspace_root", options)
        source = (TOOLS / "build_r0_shsz_pv_publication_package.py").read_text("utf-8")
        self.assertNotIn("os.environ", source)
        self.assertNotIn("os.getenv", source)
        self.assertNotIn("rqdatac.init", source)

    def test_materialized_package_contains_no_credential_value_markers(self) -> None:
        self._build(materialize=True)
        texts = "\n".join(
            path.read_text("utf-8", errors="ignore")
            for path in list(self.package.rglob("*")) + [self.preflight, self.execution]
            if path.is_file()
        ).lower()
        for forbidden in (
            "tcp://license:",
            '"password":',
            '"api_key":',
            '"private_key":',
            '"access_token":',
        ):
            self.assertNotIn(forbidden, texts)


if __name__ == "__main__":
    unittest.main()
