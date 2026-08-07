from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "probe_ricequant_environment.py"
)
SPEC = importlib.util.spec_from_file_location("probe_ricequant_environment", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ProbeRicequantEnvironmentTests(unittest.TestCase):
    def test_payload_never_reads_credentials_or_calls_network(self) -> None:
        payload = MODULE.build_payload()
        self.assertEqual(payload["network_requests"], 0)
        self.assertEqual(payload["api_calls"], 0)
        self.assertIs(payload["credentials_read"], False)
        self.assertIs(payload["credentials_written"], False)
        self.assertIs(payload["license_value_captured"], False)
        self.assertEqual(payload["formal_effect"], "NONE")

    def test_output_path_is_confined_to_probe_root(self) -> None:
        allowed = MODULE.validate_output_path("probe-test/result.json")
        allowed.relative_to(MODULE.DEFAULT_OUTPUT_ROOT)

        with self.assertRaisesRegex(ValueError, "must stay inside"):
            MODULE.validate_output_path(
                MODULE.WORKSPACE_ROOT / "reports" / "escaped.json"
            )

    def test_output_requires_json_extension(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be a .json file"):
            MODULE.validate_output_path("probe-test/result.txt")


if __name__ == "__main__":
    unittest.main()
