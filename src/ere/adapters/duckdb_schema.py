"""Schema initialization for DuckDB adapter."""

import duckdb


def init_schema(con: duckdb.DuckDBPyConnection, entity_fields: list[str]) -> None:
    """
    Create application tables if they do not already exist.

    This mirrors the standard database initialization logic, allowing adapters to be used
    with a fresh connection.

    Args:
        con: DuckDB connection.
        entity_fields: List of field names (e.g. ["legal_name", "country_code"]).
    """
    # mentions table: mention_id + dynamic entity fields (all TEXT)
    col_defs = ",\n    ".join(f"{f}  TEXT" for f in entity_fields)
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
