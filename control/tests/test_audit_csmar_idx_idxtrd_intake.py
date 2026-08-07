from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import duckdb


TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "audit_csmar_idx_idxtrd_intake.py"
)
SPEC = importlib.util.spec_from_file_location("idx_idxtrd_audit", TOOL_PATH)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


class IdxIdxtrdAuditTests(unittest.TestCase):
    def test_accepts_only_two_explicit_headers(self) -> None:
        self.assertEqual(
            AUDIT.validate_csv_header(AUDIT.KEY_COLUMNS), "KEY_ONLY_2"
        )
        self.assertEqual(
            AUDIT.validate_csv_header(AUDIT.EXPECTED_COLUMNS), "FULL_10"
        )
        with self.assertRaises(AUDIT.AuditError):
            AUDIT.validate_csv_header(("Idxtrd01", "Indexcd"))
        with self.assertRaises(AUDIT.AuditError):
            AUDIT.validate_csv_header(AUDIT.EXPECTED_COLUMNS + ("extra",))

    def test_member_path_escape_and_unexpected_name_fail_closed(self) -> None:
        self.assertEqual(
            AUDIT.validate_member_name("IDX_Idxtrd12.csv"),
            "IDX_Idxtrd12.csv",
        )
        for name in (
            "../IDX_Idxtrd.csv",
            "nested/IDX_Idxtrd.csv",
            r"nested\IDX_Idxtrd.csv",
            "IDX_Idxinfo.csv",
        ):
            with self.subTest(name=name), self.assertRaises(AUDIT.AuditError):
                AUDIT.validate_member_name(name)

    def test_exact_merge_keeps_compatible_partial_and_quarantines_conflict(
        self,
    ) -> None:
        connection = duckdb.connect(":memory:")
        try:
            AUDIT.create_source_table(connection)
            rows = [
                # Full observation.
                (
                    "000001",
                    "2026-01-02",
                    "10",
                    "11",
                    "9",
                    "10.5",
                    "100",
                    "1000",
                    "1.0",
                    "Index A",
                    "zip-a",
                    "sha-a",
                    "m-a",
                    "IDX_Idxtrd.csv",
                    "member-a",
                    "FULL_10",
                    1,
                ),
                # Compatible key-only overlap.
                (
                    "000001",
                    "2026-01-02",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "zip-b",
                    "sha-b",
                    "m-b",
                    "IDX_Idxtrd.csv",
                    "member-b",
                    "KEY_ONLY_2",
                    1,
                ),
                # Conflicting close for a second key.
                (
                    "000002",
                    "2026-01-02",
                    "20",
                    "21",
                    "19",
                    "20.5",
                    "200",
                    "2000",
                    "2.0",
                    "Index B",
                    "zip-a",
                    "sha-a",
                    "m-a",
                    "IDX_Idxtrd.csv",
                    "member-a",
                    "FULL_10",
                    2,
                ),
                (
                    "000002",
                    "2026-01-02",
                    "20",
                    "21",
                    "19",
                    "20.6",
                    "200",
                    "2000",
                    "2.0",
                    "Index B",
                    "zip-c",
                    "sha-c",
                    "m-c",
                    "IDX_Idxtrd.csv",
                    "member-c",
                    "FULL_10",
                    1,
                ),
            ]
            placeholders = ",".join("?" for _ in rows[0])
            connection.executemany(
                f"INSERT INTO source_rows VALUES ({placeholders})", rows
            )
            AUDIT.build_analysis_tables(connection)
            self.assertEqual(
                connection.execute("SELECT count(*) FROM key_stats").fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute("SELECT count(*) FROM merged_rows").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT count(*) FROM conflict_keys").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT Idxtrd05 FROM merged_rows WHERE Indexcd='000001'"
                ).fetchone()[0],
                "10.5",
            )
        finally:
            connection.close()

    def test_output_must_remain_below_dedicated_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            allowed = workspace / "tmp/idx-idxtrd-intake-audit"
            allowed.mkdir(parents=True)
            good = allowed / "run"
            self.assertEqual(
                AUDIT.require_output(good, allowed, workspace), good.resolve()
            )
            with self.assertRaises(AUDIT.AuditError):
                AUDIT.require_output(
                    workspace / "data/catalog/datasets.json",
                    allowed,
                    workspace,
                )

    def test_file_record_uses_final_path_before_atomic_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / ".run.staging" / "artifact.json"
            staging.parent.mkdir()
            staging.write_text('{"ok":true}\n', encoding="utf-8")
            final = root / "run" / "artifact.json"
            record = AUDIT.file_record_for_final_path(staging, final)
            self.assertEqual(record["path"], final.resolve().as_posix())
            self.assertEqual(record["bytes"], staging.stat().st_size)
            self.assertEqual(record["sha256"], AUDIT.sha256_file(staging))


if __name__ == "__main__":
    unittest.main()
