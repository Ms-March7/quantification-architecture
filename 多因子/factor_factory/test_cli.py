from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from factor_factory.cli import _config_from_mapping, _load_json, build_parser, main


class StrictCliTests(unittest.TestCase):
    def test_json_accepts_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(b"\xef\xbb\xbf{\"schema_version\": 1}")
            self.assertEqual(_load_json(path)["schema_version"], 1)

    def test_json_rejects_non_finite_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"schema_version": 1, "maximum_q_value": NaN}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-finite"):
                _load_json(path)

    def test_json_rejects_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"schema_version": 1, "seed": 1, "seed": 2}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                _load_json(path)

    def test_config_rejects_bad_schema_and_cross_field_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "schema_version=1"):
            _config_from_mapping({"schema_version": 2})
        with self.assertRaisesRegex(ValueError, "ensemble limits"):
            _config_from_mapping({
                "schema_version": 1,
                "maximum_selected": 1,
                "minimum_ensemble_components": 2,
            })

    def test_parser_exposes_explicit_holdout_command(self) -> None:
        args = build_parser().parse_args([
            "open-holdout",
            "--root", "snapshot",
            "--research", "frozen",
            "--confirm-freeze-hash", "a" * 64,
            "--output", "holdout",
        ])
        self.assertEqual(args.command, "open-holdout")

    def test_real_qlib_rejects_implicit_unregistered_protocol_before_store_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "explicit preregistered V3 config"):
                main([
                    "from-qlib",
                    "--root", str(Path(directory) / "snapshot"),
                    "--start-date", "2020-01-01",
                    "--lineage-research", str(Path(directory) / "lineage"),
                    "--output", str(Path(directory) / "run"),
                ])


if __name__ == "__main__":
    unittest.main()
