"""Unit tests: opening DuckDB with explicit settings (adapter).

Spec: resolution-resource-bounds — "DuckDB storage is configurable by environment with on-disk default".
"""

import pytest

from ere.adapters.duckdb_connection import DuckDBSettings, DuckDBStorage, open_duckdb


def _setting(con, name: str):
    return con.execute(f"SELECT current_setting('{name}')").fetchone()[0]


def test_disk_database_is_file_backed_with_temp_dir_beside_it(tmp_path):
    db_path = tmp_path / "app.duckdb"
    settings = DuckDBSettings(storage=DuckDBStorage.DISK, path=str(db_path))

    con = open_duckdb(settings)
    try:
        con.execute("CREATE TABLE probe (x INTEGER)")
        assert db_path.exists()
        assert _setting(con, "temp_directory") == f"{db_path}.tmp"
    finally:
        con.close()


@pytest.mark.parametrize("storage", ["disk", "memory"])
def test_memory_limit_and_threads_are_applied(tmp_path, storage):
    settings = DuckDBSettings(
        storage=DuckDBStorage(storage),
        path=str(tmp_path / "app.duckdb"),
        memory_limit="4GiB",
        threads=2,
        temp_directory=str(tmp_path / "spill"),
    )

    con = open_duckdb(settings)
    try:
        assert _setting(con, "memory_limit") == "4.0 GiB"
        assert _setting(con, "threads") == 2
        assert _setting(con, "temp_directory") == str(tmp_path / "spill")
    finally:
        con.close()


def test_memory_database_writes_no_file(tmp_path):
    db_path = tmp_path / "app.duckdb"
    settings = DuckDBSettings(storage=DuckDBStorage.MEMORY, path=str(db_path))

    con = open_duckdb(settings)
    try:
        con.execute("CREATE TABLE probe (x INTEGER)")
        assert not db_path.exists()
    finally:
        con.close()
