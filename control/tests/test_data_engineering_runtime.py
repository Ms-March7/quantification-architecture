from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import platform
import struct
import sys
import tempfile
import unittest
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
VERIFY_RUNTIME_PATH = (
    WORKSPACE_ROOT / "control" / "runtime" / "data-engineering" / "verify_runtime.py"
)
SPEC = importlib.util.spec_from_file_location(
    "qdata_data_engineering_verify_runtime",
    VERIFY_RUNTIME_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load runtime verifier: {VERIFY_RUNTIME_PATH}")
VERIFY_RUNTIME = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY_RUNTIME)


class DataEngineeringRuntimeTests(unittest.TestCase):
    def make_runtime(self, root: Path) -> tuple[Path, dict[str, object]]:
        package_version = importlib.metadata.version("pip")
        direct_path = root / "requirements.in"
        requirements_path = root / "requirements.lock"
        direct_path.write_text(
            f"pip=={package_version}\n",
            encoding="utf-8",
        )
        requirements_path.write_text(
            f"pip=={package_version}\n",
            encoding="utf-8",
        )
        direct_hash = hashlib.sha256(direct_path.read_bytes()).hexdigest()
        lock_hash = hashlib.sha256(requirements_path.read_bytes()).hexdigest()
        metadata: dict[str, object] = {
            "schema_version": 1,
            "runtime_id": "test-runtime",
            "python": {
                "implementation": "CPython",
                "major": sys.version_info.major,
                "minor": sys.version_info.minor,
            },
            "platform": {
                "system": platform.system(),
                "machine": platform.machine(),
                "pointer_bits": struct.calcsize("P") * 8,
            },
            "status": "VERSION_LOCKED_NOT_INSTALLED_AS_CENTRAL_VENV",
            "lock_semantics": "EXACT_PYTHON_PACKAGE_VERSIONS__NO_WHEEL_HASHES",
            "direct_requirements": "requirements.in",
            "direct_requirements_sha256": direct_hash,
            "locked_requirements": "requirements.lock",
            "locked_requirements_sha256": lock_hash,
        }
        (root / "runtime-lock.json").write_text(
            json.dumps(metadata),
            encoding="utf-8",
        )
        return requirements_path, metadata

    def test_exact_current_runtime_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir)
            self.make_runtime(runtime_root)

            result = VERIFY_RUNTIME.verify(runtime_root)

            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["packages_locked"], 1)
            self.assertEqual(result["packages_checked"], 1)
            self.assertEqual(result["direct_packages"], 1)
            self.assertEqual(result["errors"], [])

    def test_tampered_lock_hash_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir)
            requirements_path, _ = self.make_runtime(runtime_root)
            requirements_path.write_text("pip==0.0.0\n", encoding="utf-8")

            result = VERIFY_RUNTIME.verify(runtime_root)

            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(
                any("SHA-256 mismatch" in error for error in result["errors"])
            )
            self.assertTrue(
                any("version mismatch" in error for error in result["errors"])
            )
            self.assertTrue(
                any("direct requirement mismatch" in error for error in result["errors"])
            )

    def test_direct_requirements_drift_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir)
            self.make_runtime(runtime_root)
            (runtime_root / "requirements.in").write_text(
                "pip==0.0.0\n",
                encoding="utf-8",
            )

            result = VERIFY_RUNTIME.verify(runtime_root)

            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(
                any("requirements.in SHA-256 mismatch" in error for error in result["errors"])
            )
            self.assertTrue(
                any("direct requirement mismatch" in error for error in result["errors"])
            )

    def test_unpinned_requirement_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir)
            requirements_path, metadata = self.make_runtime(runtime_root)
            requirements_path.write_text("pip>=1\n", encoding="utf-8")
            metadata["locked_requirements_sha256"] = hashlib.sha256(
                requirements_path.read_bytes()
            ).hexdigest()
            (runtime_root / "runtime-lock.json").write_text(
                json.dumps(metadata),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "not exactly pinned"):
                VERIFY_RUNTIME.verify(runtime_root)

    def test_runtime_contract_and_platform_drift_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir)
            _, metadata = self.make_runtime(runtime_root)
            metadata["schema_version"] = 999
            metadata["python"]["implementation"] = "PyPy"
            metadata["platform"]["machine"] = "unexpected-machine"
            (runtime_root / "runtime-lock.json").write_text(
                json.dumps(metadata),
                encoding="utf-8",
            )

            result = VERIFY_RUNTIME.verify(runtime_root)

            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(
                any("schema_version" in error for error in result["errors"])
            )
            self.assertTrue(
                any("implementation mismatch" in error for error in result["errors"])
            )
            self.assertTrue(
                any("platform mismatch" in error for error in result["errors"])
            )

    def test_runtime_requirement_paths_cannot_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir)
            _, metadata = self.make_runtime(runtime_root)
            metadata["direct_requirements"] = "../outside.txt"
            (runtime_root / "runtime-lock.json").write_text(
                json.dumps(metadata),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "stay inside"):
                VERIFY_RUNTIME.verify(runtime_root)


if __name__ == "__main__":
    unittest.main()
