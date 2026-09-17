"""Unit tests: stored normalised names, outdated schemas and NULL blocking keys (adapters).

Spec: candidate-scoring — "Only similarly named organisations of the same country are scored",
"Existing databases without the normalised name are refused".
"""

import duckdb
import pytest

from ere.adapters.duckdb_repositories import DuckDBMentionRepository
from ere.adapters.duckdb_schema import (
    OutdatedSchemaError,
    init_schema,
    normalised_name_sql,
)
from ere.adapters.splink_linker_impl import render_blocking_rules
from ere.models.resolver import BlockingSettings, Mention, MentionId

ENTITY_FIELDS = ["legal_name", "country_code"]


@pytest.mark.parametrize(
    "name, normalised",
    [
        ("ACME-Industries GmbH.", "acmeindustriesgmbh"),
        ("Bäckerei Müller & Söhne", "backereimullersohne"),
        ("Комисия за защита", "комисиязазащита"),
        ("Ελληνική Δημοκρατία", "ελληνικηδημοκρατια"),
        ("  123 Holding  ", "123holding"),
        ("... --- !!!", None),
        ("", None),
        (None, None),
    ],
)
def test_name_normalisation(name, normalised):
    con = duckdb.connect()
    assert (
        con.execute(f"SELECT {normalised_name_sql('?')}", [name]).fetchone()[0]
        == normalised
    )


def test_saved_mention_stores_its_normalised_name():
    con = duckdb.connect()
    init_schema(con, ENTITY_FIELDS)
    DuckDBMentionRepository(con, ENTITY_FIELDS).save(
        Mention(
            id=MentionId(value="m1"),
            attributes={"legal_name": "ACME Corp.", "country_code": "DEU"},
        )
    )

    assert con.execute(
        "SELECT legal_name, legal_name_norm FROM mentions"
    ).fetchone() == ("ACME Corp.", "acmecorp")


@pytest.mark.parametrize(
    "existing_columns",
    [
        "mention_id TEXT, legal_name TEXT, country_code TEXT",
        "mention_id TEXT, legal_name TEXT",
    ],
)
def test_existing_mentions_table_without_normalised_name_is_refused(existing_columns):
    con = duckdb.connect()
    con.execute(f"CREATE TABLE mentions ({existing_columns})")

    with pytest.raises(OutdatedSchemaError, match="legal_name_norm"):
        init_schema(con, ENTITY_FIELDS)


def test_fields_without_a_normalised_copy_need_no_new_column():
    con = duckdb.connect()
    con.execute("CREATE TABLE mentions (mention_id TEXT, country_code TEXT)")

    init_schema(con, ["country_code"])  # must not raise


def test_mentions_without_country_or_name_never_block_together():
    con = duckdb.connect()
    init_schema(con, ENTITY_FIELDS)
    repo = DuckDBMentionRepository(con, ENTITY_FIELDS)
    for mention_id, name, country in [
        ("a", "Acme", None),
        ("b", "Acme", None),
        ("c", "...", "DEU"),
        ("d", "!!", "DEU"),
    ]:
        repo.save(
            Mention(
                id=MentionId(value=mention_id),
                attributes={"legal_name": name, "country_code": country},
            )
        )
    settings = BlockingSettings.from_config(
        [
            {
                "same": "country_code",
                "similar": {"field": "legal_name", "min_jaro_winkler": 0.8},
            }
        ],
        entity_fields=ENTITY_FIELDS,
    )
    (rule,) = render_blocking_rules(settings.rules)

    pairs = con.execute(
        f"SELECT count(*) FROM mentions l JOIN mentions r ON l.mention_id < r.mention_id AND ({rule})"
    ).fetchone()[0]

    assert pairs == 0
