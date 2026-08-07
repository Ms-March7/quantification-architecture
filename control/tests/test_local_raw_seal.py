from __future__ import annotations

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
SCRIPT = WORKSPACE / "control" / "tools" / "run_local_raw_seal.py"
SPEC = importlib.util.spec_from_file_location("local_raw_seal", SCRIPT)
assert SPEC and SPEC.loader
SEAL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SEAL
SPEC.loader.exec_module(SEAL)


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class LocalRawSealTests(unittest.TestCase):
    def setUp(self) -> None:
        SEAL.INPUT_ROOT.mkdir(parents=True, exist_ok=True)
        SEAL.OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        self.source_temp = tempfile.TemporaryDirectory(
            dir=SEAL.INPUT_ROOT, prefix="test-local-raw-seal-"
        )
        self.source_root = Path(self.source_temp.name)
        self.source_run_id = self.source_root.name
        self.raw_root = self.source_root / "raw"
        self.raw_root.mkdir()
        self.outputs: list[Path] = []

    def tearDown(self) -> None:
        for output in self.outputs:
            shutil.rmtree(output, ignore_errors=True)
        self.source_temp.cleanup()

    def _write(self, name: str, payload: bytes) -> Path:
        path = self.raw_root / name
        path.write_bytes(payload)
        return path

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
        run_id = f"test-seal-{uuid.uuid4().hex}"
        output = SEAL.OUTPUT_ROOT / run_id
        self.outputs.append(output)
        return run_id, output

    def test_success_is_byte_identical_write_once_and_no_grants(self) -> None:
        left_payload = b"sina raw bytes\x00\x01"
        right_payload = b"tencent raw bytes\xff"
        left = self._write("sina.response", left_payload)
        right = self._write("tencent.response", right_payload)
        unexpected = self._write("not-allowlisted.response", b"do not seal me")
        run_id, output = self._output()
        prepared = SEAL.prepare(
            run_id,
            self.source_run_id,
            [
                self._spec("SINA_WEB_QUOTE", left, left_payload),
                self._spec("TENCENT_WEB_QUOTE", right, right_payload),
            ],
            output,
        )
        result = SEAL.execute(prepared)

        self.assertEqual(result["status"], "PASS_LOCAL_RAW_SEAL_INSPECTION_ONLY")
        self.assertEqual(left.read_bytes(), left_payload)
        self.assertEqual(right.read_bytes(), right_payload)
        self.assertEqual(unexpected.read_bytes(), b"do not seal me")
        self.assertEqual(
            (output / "sealed" / "01-SINA_WEB_QUOTE.raw").read_bytes(), left_payload
        )
        self.assertEqual(
            (output / "sealed" / "02-TENCENT_WEB_QUOTE.raw").read_bytes(),
            right_payload,
        )
        self.assertFalse((output / "sealed" / "not-allowlisted.response").exists())
        manifest = json.loads(
            (output / "SEAL_MANIFEST.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["input_count"], 2)
        self.assertFalse(manifest["input_discovery_used"])
        self.assertFalse(manifest["formal_source_admission"])
        self.assertFalse(manifest["formal_catalog_written"])
        self.assertFalse(manifest["quality_promotion"])
        self.assertFalse(manifest["production_raw_store_written"])
        self.assertFalse(manifest["scheduler_created"])
        self.assertFalse(manifest["research_ready"])
        self.assertEqual(manifest["formal_effect"], "NONE")
        manifest_hash = hashlib.sha256(
            (output / "SEAL_MANIFEST.json").read_bytes()
        ).hexdigest()
        self.assertTrue(
            (output / "SEAL_MANIFEST.sha256")
            .read_text(encoding="ascii")
            .startswith(manifest_hash)
        )

        with self.assertRaises(SEAL.RawSealError):
            SEAL.prepare(
                run_id,
                self.source_run_id,
                [self._spec("SINA_WEB_QUOTE", left, left_payload)],
                output,
            )

    def test_wrong_hash_fails_before_output(self) -> None:
        payload = b"payload"
        source = self._write("source.response", payload)
        run_id, output = self._output()
        spec = "|".join(
            [
                "SINA_WEB_QUOTE",
                source.relative_to(WORKSPACE).as_posix(),
                "0" * 64,
                str(len(payload)),
            ]
        )
        with self.assertRaisesRegex(SEAL.RawSealError, "SHA-256 mismatch"):
            SEAL.prepare(run_id, self.source_run_id, [spec], output)
        self.assertFalse(output.exists())

    def test_wrong_byte_count_fails_before_output(self) -> None:
        payload = b"payload"
        source = self._write("source.response", payload)
        run_id, output = self._output()
        spec = "|".join(
            [
                "SINA_WEB_QUOTE",
                source.relative_to(WORKSPACE).as_posix(),
                digest(payload),
                str(len(payload) + 1),
            ]
        )
        with self.assertRaisesRegex(SEAL.RawSealError, "byte-count mismatch"):
            SEAL.prepare(run_id, self.source_run_id, [spec], output)
        self.assertFalse(output.exists())

    def test_input_outside_probe_raw_root_is_rejected(self) -> None:
        payload = b"outside"
        with tempfile.TemporaryDirectory(dir=WORKSPACE / "tmp") as directory:
            source = Path(directory) / "outside.response"
            source.write_bytes(payload)
            run_id, output = self._output()
            spec = self._spec("SINA_WEB_QUOTE", source, payload)
            with self.assertRaisesRegex(SEAL.RawSealError, "must stay below"):
                SEAL.prepare(run_id, self.source_run_id, [spec], output)

    def test_mixed_probe_runs_are_rejected(self) -> None:
        first_payload = b"first"
        first = self._write("first.response", first_payload)
        with tempfile.TemporaryDirectory(
            dir=SEAL.INPUT_ROOT, prefix="test-local-raw-seal-other-"
        ) as other_directory:
            other_raw = Path(other_directory) / "raw"
            other_raw.mkdir()
            second_payload = b"second"
            second = other_raw / "second.response"
            second.write_bytes(second_payload)
            run_id, output = self._output()
            with self.assertRaisesRegex(SEAL.RawSealError, "does not match"):
                SEAL.prepare(
                    run_id,
                    self.source_run_id,
                    [
                        self._spec("SINA_WEB_QUOTE", first, first_payload),
                        self._spec("TENCENT_WEB_QUOTE", second, second_payload),
                    ],
                    output,
                )

    def test_duplicate_source_or_path_is_rejected(self) -> None:
        payload = b"payload"
        source = self._write("source.response", payload)
        spec = self._spec("SINA_WEB_QUOTE", source, payload)
        with self.assertRaisesRegex(SEAL.RawSealError, "duplicate source_id"):
            SEAL.parse_input_specs([spec, spec])

    def test_output_must_match_run_id_and_stay_in_seal_root(self) -> None:
        run_id = f"test-seal-{uuid.uuid4().hex}"
        with self.assertRaisesRegex(SEAL.RawSealError, "must be exactly"):
            SEAL.validate_output_path(
                WORKSPACE / "tmp" / "another-root" / run_id, run_id
            )
        with self.assertRaisesRegex(SEAL.RawSealError, "must be exactly"):
            SEAL.validate_output_path(SEAL.OUTPUT_ROOT / "different", run_id)

    def test_plan_preparation_performs_no_write(self) -> None:
        payload = b"payload"
        source = self._write("source.response", payload)
        run_id, output = self._output()
        prepared = SEAL.prepare(
            run_id,
            self.source_run_id,
            [self._spec("SINA_WEB_QUOTE", source, payload)],
            output,
        )
        self.assertEqual(len(prepared["verified"]), 1)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
