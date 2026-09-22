"""Unit tests: indexed lookups and bulk cluster lookup (adapters).

Spec: resolution-resource-bounds — "Per-request work is independent of cluster membership size".
"""

import duckdb
import pytest

from ere.adapters.duckdb_repositories import DuckDBClusterRepository
from ere.adapters.duckdb_schema import init_schema
from ere.models.resolver import ClusterId, ClusterMembership, MentionId
from test.unit.adapters.stubs import InMemoryClusterRepository

ENTITY_FIELDS = ["legal_name", "country_code"]
EXPECTED_INDEXED_COLUMNS = {
    ("mentions", "mention_id"),
    ("clusters", "mention_id"),
}
UNINDEXED_TABLE = "similarities"  # written only on the request path (DEC-19)


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    init_schema(c, ENTITY_FIELDS)
    yield c
    c.close()


def _indexed_columns(con) -> set[tuple[str, str]]:
    rows = con.execute(
        "SELECT table_name, expressions FROM duckdb_indexes()"
    ).fetchall()
    return {(table, expressions.strip("[]'\"")) for table, expressions in rows}


def test_lookup_columns_are_indexed(con):
    assert EXPECTED_INDEXED_COLUMNS <= _indexed_columns(con)


def test_similarities_is_not_indexed(con):
    assert all(table != UNINDEXED_TABLE for table, _ in _indexed_columns(con))


def test_retired_similarities_indexes_are_dropped_from_existing_database(tmp_path):
    db_path = str(tmp_path / "app.duckdb")
    old = duckdb.connect(db_path)
    init_schema(old, ENTITY_FIELDS)
    old.execute(
        "CREATE INDEX idx_similarities_mention_id_l ON similarities (mention_id_l)"
    )
    old.close()

    reopened = duckdb.connect(db_path)
    try:
        init_schema(reopened, ENTITY_FIELDS)
        assert all(table != UNINDEXED_TABLE for table, _ in _indexed_columns(reopened))
    finally:
        reopened.close()


def test_schema_initialisation_is_idempotent_on_existing_database(tmp_path):
    db_path = str(tmp_path / "app.duckdb")
    first = duckdb.connect(db_path)
    init_schema(first, ENTITY_FIELDS)
    first.close()

    second = duckdb.connect(db_path)
    try:
        init_schema(second, ENTITY_FIELDS)
        assert EXPECTED_INDEXED_COLUMNS <= _indexed_columns(second)
    finally:
        second.close()


@pytest.fixture(params=["duckdb", "in_memory"])
def cluster_repo(request, con):
    if request.param == "duckdb":
        return DuckDBClusterRepository(con)
    return InMemoryClusterRepository()


def test_clusters_for_returns_cluster_of_each_known_mention(cluster_repo):
    cluster_repo.save(
        ClusterMembership(
            mention_id=MentionId(value="m1"), cluster_id=ClusterId(value="c1")
        )
    )
    cluster_repo.save(
        ClusterMembership(
            mention_id=MentionId(value="m2"), cluster_id=ClusterId(value="c1")
        )
    )
    cluster_repo.save(
        ClusterMembership(
            mention_id=MentionId(value="m3"), cluster_id=ClusterId(value="c3")
        )
    )

    result = cluster_repo.clusters_for([MentionId(value="m1"), MentionId(value="m3")])

    assert result == {
        MentionId(value="m1"): ClusterId(value="c1"),
        MentionId(value="m3"): ClusterId(value="c3"),
    }


def test_clusters_for_omits_unknown_mentions(cluster_repo):
    cluster_repo.save(
        ClusterMembership(
            mention_id=MentionId(value="m1"), cluster_id=ClusterId(value="c1")
        )
    )

    result = cluster_repo.clusters_for(
        [MentionId(value="m1"), MentionId(value="ghost")]
    )

    assert result == {MentionId(value="m1"): ClusterId(value="c1")}


def test_clusters_for_empty_input_returns_empty_mapping(cluster_repo):
    assert cluster_repo.clusters_for([]) == {}
