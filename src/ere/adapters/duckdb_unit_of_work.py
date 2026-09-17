"""Transaction boundary on the shared DuckDB connection (adapter)."""

from collections.abc import Iterator
from contextlib import contextmanager

import duckdb


class DuckDBUnitOfWork:
    """Callable returning a context manager: one transaction, committed on success, rolled back on error."""

    def __init__(self, con: duckdb.DuckDBPyConnection):
        self._con = con

    @contextmanager
    def __call__(self) -> Iterator[None]:
        self._con.execute("BEGIN TRANSACTION")
        try:
            yield
        except BaseException:
            self._con.execute("ROLLBACK")
            raise
        self._con.execute("COMMIT")
