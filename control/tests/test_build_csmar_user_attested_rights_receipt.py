import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


TOOL = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "build_csmar_user_attested_rights_receipt.py"
)
SPEC = importlib.util.spec_from_file_location("csmar_rights_receipt", TOOL)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class CsmarRightsReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        for relative in MODULE.GUARDED_FILES:
            path = self.workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"guard:{relative}\n", encoding="utf-8")
        for relative in MODULE.GUARDED_TREES:
            (self.workspace / relative).mkdir(parents=True, exist_ok=True)
        (self.workspace / "reports" / "data-governance").mkdir(
            parents=True, exist_ok=True
        )
        self.specs = self._build_three_batches()

    def tearDown(self):
        self.temporary.cleanup()

    def _write_json(self, path: Path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def _common(self, run_id):
        return {
            "schema_version": 1,
            "run_id": run_id,
            "created_at": "2026-07-30T00:00:00+08:00",
            "lifecycle": "quarantine_intake",
            "mutability": "immutable_after_seal",
            "purpose_ceiling": "INSPECTION_ONLY",
            "admission_decision": "NOT_ADMITTED",
            "catalog_registered": False,
            "research_ready": False,
            "hash_algorithm": "sha256",
        }

    def _manifest_spec(self, run_id, manifest_path):
        return {
            "run_id": run_id,
            "manifest_path": manifest_path.relative_to(self.workspace).as_posix(),
            "manifest_sha256": sha256(manifest_path.read_bytes()),
        }

    def _build_three_batches(self):
        parent = (
            self.workspace
            / "data"
            / "market"
            / "quarantine"
            / "csmar-manual-export"
        )
        specs = []

        legacy_id = "fixture-legacy"
        legacy_root = parent / legacy_id
        legacy_payload = b"legacy-package"
        legacy_name = "精确旧包.zip"
        legacy_path = legacy_root / "raw" / legacy_name
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_bytes(legacy_payload)
        legacy = {
            **self._common(legacy_id),
            "package_count": 1,
            "total_bytes": len(legacy_payload),
            "packages": [
                {
                    "filename": legacy_name,
                    "source_path": rf"C:\Downloads\{legacy_name}",
                    "source_drive": "C",
                    "destination_relative_path": f"raw/{legacy_name}",
                    "bytes": len(legacy_payload),
                    "sha256": sha256(legacy_payload),
                }
            ],
        }
        legacy_manifest = legacy_root / "INTAKE_MANIFEST.json"
        self._write_json(legacy_manifest, legacy)
        specs.append(self._manifest_spec(legacy_id, legacy_manifest))

        for batch_number in (2, 3):
            run_id = f"fixture-dedup-{batch_number}"
            root = parent / run_id
            first = f"shared-{batch_number}".encode()
            second = f"unique-{batch_number}".encode()
            canonical_rows = []
            source_rows = []
            definitions = [
                (
                    f"canonical-{batch_number}-a.zip",
                    first,
                    [
                        f"alias-{batch_number}-a.zip",
                        f"alias-{batch_number}-a (1).zip",
                    ],
                ),
                (
                    f"canonical-{batch_number}-b.pdf",
                    second,
                    [f"alias-{batch_number}-b.pdf"],
                ),
            ]
            for canonical_name, payload, aliases in definitions:
                kind = "package" if canonical_name.endswith(".zip") else "documentation"
                subdir = "packages" if kind == "package" else "documentation"
                canonical_path = root / "raw" / subdir / canonical_name
                canonical_path.parent.mkdir(parents=True, exist_ok=True)
                canonical_path.write_bytes(payload)
                canonical_rows.append(
                    {
                        "sha256": sha256(payload),
                        "kind": kind,
                        "canonical_filename": canonical_name,
                        "destination_relative_path": (
                            f"raw/{subdir}/{canonical_name}"
                        ),
                        "bytes": len(payload),
                        "source_aliases": aliases,
                        "source_alias_count": len(aliases),
                        "copy_hash_verification": "PASS",
                    }
                )
                for alias in aliases:
                    source_rows.append(
                        {
                            "filename": alias,
                            "source_path": rf"C:\Downloads\{alias}",
                            "kind": kind,
                            "bytes": len(payload),
                            "sha256": sha256(payload),
                        }
                    )
            manifest = {
                **self._common(run_id),
                "source_root": r"C:\Downloads",
                "source_file_count": len(source_rows),
                "source_total_bytes": sum(row["bytes"] for row in source_rows),
                "unique_blob_count": len(canonical_rows),
                "unique_storage_bytes": sum(
                    row["bytes"] for row in canonical_rows
                ),
                "source_files": source_rows,
                "unique_blobs": canonical_rows,
            }
            manifest_path = root / "INTAKE_MANIFEST.json"
            self._write_json(manifest_path, manifest)
            specs.append(self._manifest_spec(run_id, manifest_path))
        return specs

    def _generate(self):
        return MODULE.generate_report(
            self.workspace,
            batch_specs=self.specs,
            report_relative=(
                "reports/data-governance/"
                "csmar-user-attested-rights-receipt-20260730-v1"
            ),
        )

    def test_declaration_and_scope_are_exact(self):
        self.assertEqual(
            MODULE.DECLARATION,
            "USER_ATTESTED__LOCAL_RETENTION__INTERNAL_ACADEMIC_DERIVATION__"
            "NO_RAW_EXTERNAL_REDISTRIBUTION__NO_COMMERCIAL_USE",
        )
        self.assertEqual(
            [item["code"] for item in MODULE.ALLOWED_SCOPE],
            [
                "LOCAL_LONG_TERM_RETENTION_ON_THIS_MACHINE",
                "CAMPUS_INTERNAL_NONCOMMERCIAL_ACADEMIC_RESEARCH",
                "INTERNAL_DERIVED_FACTORS_AND_STATISTICS",
            ],
        )
        self.assertEqual(
            [item["code"] for item in MODULE.PROHIBITED_SCOPE],
            ["RAW_EXTERNAL_REDISTRIBUTION", "COMMERCIAL_USE"],
        )
        self.assertEqual(
            [item["code"] for item in MODULE.NOT_COVERED_SCOPE],
            [
                "API_OR_AUTOMATED_BULK_COLLECTION",
                "POST_AFFILIATION_DISPOSITION",
                "OFFICIAL_SOURCE_RIGHTS",
                "INDEPENDENT_CONTRACT_VERIFICATION",
            ],
        )

    def test_default_specs_pin_the_three_production_manifests(self):
        self.assertEqual(len(MODULE.DEFAULT_BATCH_SPECS), 3)
        self.assertEqual(
            [item["run_id"] for item in MODULE.DEFAULT_BATCH_SPECS],
            [
                "intake-20260729-33pkg-v1",
                "intake-20260730-08pkg-03manual-v1",
                "intake-20260730-18src-12blob-idx-idxtrd-v1",
            ],
        )
        for item in MODULE.DEFAULT_BATCH_SPECS:
            self.assertRegex(item["manifest_sha256"], r"^[0-9a-f]{64}$")

    def test_generation_binds_every_alias_and_blob_and_is_idempotent(self):
        first = self._generate()
        report = self.workspace / first["report"]
        before = {
            path.name: path.read_bytes()
            for path in report.iterdir()
            if path.is_file()
        }
        second = self._generate()
        after = {
            path.name: path.read_bytes()
            for path in report.iterdir()
            if path.is_file()
        }
        self.assertFalse(first["idempotent_existing_output"])
        self.assertTrue(second["idempotent_existing_output"])
        self.assertEqual(before, after)
        self.assertEqual(set(before), MODULE.OUTPUT_FILES)

        receipt = json.loads(before["RIGHTS_RECEIPT.json"])
        self.assertEqual(receipt["declaration"], MODULE.DECLARATION)
        self.assertEqual(receipt["totals"]["manifest_count"], 3)
        self.assertEqual(receipt["totals"]["source_alias_count"], 7)
        self.assertEqual(receipt["totals"]["canonical_blob_count"], 5)
        self.assertTrue(
            all(
                row["alias_to_canonical_bytes_sha256_match"]
                for row in receipt["source_alias_bindings"]
            )
        )
        self.assertTrue(
            all(
                row["manifest_to_payload_bytes_sha256_verified"]
                for row in receipt["canonical_blob_bindings"]
            )
        )
        verification = json.loads(before["VERIFICATION.json"])
        self.assertFalse(verification["independent_contract_verification"])
        self.assertFalse(verification["formal_admission"])
        self.assertFalse(verification["research_usable"])
        self.assertTrue(verification["formal_guard"]["unchanged"])
        self.assertIn(MODULE.DECLARATION.encode(), before["README.md"])

    def test_alias_coverage_mismatch_fails_closed(self):
        manifest_path = self.workspace / self.specs[1]["manifest_path"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["unique_blobs"][0]["source_aliases"][0] = "unexpected.zip"
        self._write_json(manifest_path, manifest)
        self.specs[1]["manifest_sha256"] = sha256(manifest_path.read_bytes())
        with self.assertRaisesRegex(ValueError, "exact alias coverage mismatch"):
            self._generate()

    def test_tampered_canonical_payload_fails_closed(self):
        manifest_path = self.workspace / self.specs[2]["manifest_path"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        relative = manifest["unique_blobs"][0]["destination_relative_path"]
        payload_path = manifest_path.parent.joinpath(*Path(relative).parts)
        payload_path.write_bytes(b"tampered")
        with self.assertRaisesRegex(RuntimeError, "bytes/SHA-256 mismatch"):
            self._generate()

    def test_canonical_path_escape_fails_closed(self):
        manifest_path = self.workspace / self.specs[1]["manifest_path"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["unique_blobs"][0]["destination_relative_path"] = "../escape.zip"
        manifest["unique_blobs"][0]["canonical_filename"] = "escape.zip"
        self._write_json(manifest_path, manifest)
        self.specs[1]["manifest_sha256"] = sha256(manifest_path.read_bytes())
        with self.assertRaisesRegex(ValueError, "invalid canonical batch-relative"):
            self._generate()

    def test_existing_nonidentical_report_is_never_overwritten(self):
        result = self._generate()
        report = self.workspace / result["report"]
        receipt = report / "RIGHTS_RECEIPT.json"
        original = receipt.read_bytes()
        receipt.write_bytes(original + b"tamper")
        with self.assertRaisesRegex(RuntimeError, "report byte mismatch"):
            self._generate()
        self.assertEqual(receipt.read_bytes(), original + b"tamper")


if __name__ == "__main__":
    unittest.main()
