"""Schema initialization for DuckDB adapter."""

from collections.abc import Sequence

import duckdb

from ere.models.resolver.blocking import DEFAULT_NORMALISED_FIELDS

# Lookup columns used on the per-request path; indexed so lookups do not scan whole tables.
LOOKUP_INDEXES = {
    "idx_mentions_mention_id": ("mentions", "mention_id"),
    "idx_clusters_mention_id": ("clusters", "mention_id"),
}
# `similarities` is only written on the request path; its indexes cost insert time and memory (DEC-19).
RETIRED_INDEXES = ("idx_similarities_mention_id_l", "idx_similarities_mention_id_r")

# Stored normalised copies of name fields are named <field><suffix> (DEC-21); which fields is configuration.
NORMALISED_SUFFIX = "_norm"


class OutdatedSchemaError(RuntimeError):
    """The database was created before a schema change that requires a reset."""


def normalised_name_sql(expression: str) -> str:
    """SQL normalising a name: lower-case, accents stripped, Unicode letters and digits only, empty → NULL."""
    return f"NULLIF(regexp_replace(strip_accents(lower({expression})), '[^\\p{{L}}\\p{{N}}]', '', 'g'), '')"


def normalised_columns(normalised_fields: Sequence[str]) -> list[str]:
    """Names of the stored normalised columns for the given fields."""
    return [f"{field}{NORMALISED_SUFFIX}" for field in normalised_fields]


def init_schema(
    con: duckdb.DuckDBPyConnection,
    entity_fields: list[str],
    normalised_fields: Sequence[str] = DEFAULT_NORMALISED_FIELDS,
) -> None:
    """
    Create application tables if they do not already exist.

    This mirrors the standard database initialization logic, allowing adapters to be used
    with a fresh connection.

    Args:
        con: DuckDB connection.
        entity_fields: List of field names (e.g. ["legal_name", "country_code"]).
        normalised_fields: Entity fields that also get a stored normalised copy (for name-similarity blocking).
    """
    stored_normalised = _present(normalised_fields, entity_fields)
    _refuse_outdated_mentions(con, stored_normalised)

    # mentions table: mention_id + dynamic entity fields + normalised copies (all TEXT)
    col_defs = ",\n    ".join(
        f"{f}  TEXT" for f in entity_fields + normalised_columns(stored_normalised)
    )
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS mentions (
            mention_id TEXT,
            {col_defs}
        )
    """)

    # similarities table: mention pairs with match probability
    con.execute("""
        CREATE TABLE IF NOT EXISTS similarities (
            mention_id_l      TEXT,
            mention_id_r      TEXT,
            match_probability REAL
        )
    """)

    # clusters table: mention -> cluster_id mapping
    con.execute("""
        CREATE TABLE IF NOT EXISTS clusters (
            mention_id TEXT,
            cluster_id TEXT
        )
    """)

    for index_name, (table, column) in LOOKUP_INDEXES.items():
        con.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({column})")
    for index_name in RETIRED_INDEXES:
        con.execute(f"DROP INDEX IF EXISTS {index_name}")


def _present(
    normalised_fields: Sequence[str], entity_fields: Sequence[str]
) -> list[str]:
    return [field for field in normalised_fields if field in entity_fields]


def _refuse_outdated_mentions(
    con: duckdb.DuckDBPyConnection, normalised_fields: Sequence[str]
) -> None:
    existing = {
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'mentions'"
        ).fetchall()
    }
    missing = [
        column
        for column in normalised_columns(normalised_fields)
        if column not in existing
    ]
    if existing and missing:
        raise OutdatedSchemaError(
            f"The mentions table lacks {', '.join(missing)}: the database was created before the name-similarity "
            "blocking change. Reset the database file (delete it) and restart."
        )
