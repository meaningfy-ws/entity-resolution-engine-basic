"""DuckDB connection settings and factory (adapter)."""

from enum import StrEnum
from pathlib import Path

import duckdb
from pydantic import BaseModel, ConfigDict

IN_MEMORY_DATABASE = ":memory:"
TEMP_DIRECTORY_SUFFIX = ".tmp"


class DuckDBStorage(StrEnum):
    """Where the DuckDB database lives."""

    DISK = "disk"
    MEMORY = "memory"


class DuckDBSettings(BaseModel):
    """Resolved DuckDB settings; `None` leaves DuckDB's own default in place."""

    model_config = ConfigDict(frozen=True)

    storage: DuckDBStorage
    path: str
    memory_limit: str | None = None
    threads: int | None = None
    temp_directory: str | None = None


def open_duckdb(settings: DuckDBSettings) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection with memory, thread and spill settings applied."""
    config: dict[str, str | int] = {}
    if settings.memory_limit is not None:
        config["memory_limit"] = settings.memory_limit
    if settings.threads is not None:
        config["threads"] = settings.threads

    if settings.storage == DuckDBStorage.DISK:
        database = settings.path
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        config["temp_directory"] = (
            settings.temp_directory or f"{settings.path}{TEMP_DIRECTORY_SUFFIX}"
        )
    else:
        database = IN_MEMORY_DATABASE
        if settings.temp_directory is not None:
            config["temp_directory"] = settings.temp_directory

    return duckdb.connect(database, config=config)
