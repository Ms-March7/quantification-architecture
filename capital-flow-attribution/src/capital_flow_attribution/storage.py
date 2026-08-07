from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Mapping

import duckdb


def _safe_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"unsafe DuckDB identifier: {value!r}")
    return value


def _sql_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def csv_to_parquet(csv_path: str | Path, parquet_path: str | Path) -> dict[str, object]:
    """Convert one normalized CSV to compressed Parquet without pandas materialization."""

    source = Path(csv_path)
    target = Path(parquet_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect() as connection:
        relation = connection.read_csv(str(source), header=True)
        row_count = int(relation.aggregate("count(*) AS n").fetchone()[0])
        relation.write_parquet(str(target), compression="zstd")
    return {
        "source": source.as_posix(),
        "parquet": target.as_posix(),
        "row_count": row_count,
        "bytes": target.stat().st_size,
        "sha256": file_sha256(target),
    }


def create_read_only_catalog(database_path: str | Path, views: Mapping[str, str | Path]) -> dict[str, int]:
    """Create DuckDB views over Parquet files and return their row counts."""

    database = Path(database_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    with duckdb.connect(str(database)) as connection:
        for view_name, parquet_path in views.items():
            name = _safe_identifier(view_name)
            path_literal = _sql_literal(Path(parquet_path).as_posix())
            connection.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet({path_literal})")
            counts[name] = int(connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0])
    return counts
