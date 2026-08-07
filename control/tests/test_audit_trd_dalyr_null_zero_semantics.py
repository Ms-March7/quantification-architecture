from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import duckdb


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "audit_trd_dalyr_null_zero_semantics.py"
)
SPEC = importlib.util.spec_from_file_location("trd_semantics_audit", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TrdSemanticsAuditTest(unittest.TestCase):
    def test_clean_rows_exclude_conflicting_keys(self) -> None:
        connection = duckdb.connect(database=":memory:")
        try:
            columns = ", ".join(
                f'"{field}" DOUBLE' if field not in {"Stkcd", "Trddt"}
                else (
                    '"Stkcd" VARCHAR'
                    if field == "Stkcd"
                    else '"Trddt" DATE'
                )
                for field in MODULE.DATA_FIELDS
            )
            connection.execute(f"CREATE TABLE raw_data ({columns})")
            field_names = ", ".join(
                f'"{field}"' for field in MODULE.DATA_FIELDS
            )
            values = {field: None for field in MODULE.DATA_FIELDS}
            values.update(
                {
                    "Stkcd": "000001",
                    "Trddt": "2024-01-02",
                    "Opnprc": 10.0,
                    "Hiprc": 11.0,
                    "Loprc": 9.0,
                    "Clsprc": 10.5,
                    "Dnshrtrd": 0,
                    "Dnvaltrd": 0,
                    "Trdsta": 1,
                }
            )
            placeholders = ", ".join("?" for _ in MODULE.DATA_FIELDS)
            row = [values[field] for field in MODULE.DATA_FIELDS]
            connection.execute(
                f"INSERT INTO raw_data ({field_names}) VALUES ({placeholders})",
                row,
            )
            connection.execute(
                f"INSERT INTO raw_data ({field_names}) VALUES ({placeholders})",
                row,
            )
            conflicting = list(row)
            conflicting[MODULE.DATA_FIELDS.index("Clsprc")] = 10.6
            connection.execute(
                f"INSERT INTO raw_data ({field_names}) VALUES ({placeholders})",
                conflicting,
            )
            connection.execute(
                "CREATE TEMP TABLE raw AS "
                "SELECT 'fixture' AS __candidate_id, * FROM raw_data"
            )
            semantics, conflicts = MODULE.build_audit(connection)
            self.assertEqual(conflicts["raw_rows"], 3)
            self.assertEqual(conflicts["exact_unique_rows"], 2)
            self.assertEqual(conflicts["conflict_key_count"], 1)
            self.assertEqual(conflicts["clean_nonconflicting_rows"], 0)
            self.assertEqual(
                semantics["gate_effect"]["formal_admission"],
                False,
            )
        finally:
            connection.close()

    def test_sql_quote(self) -> None:
        self.assertEqual(MODULE.sql_quote("a'b"), "'a''b'")


if __name__ == "__main__":
    unittest.main()
