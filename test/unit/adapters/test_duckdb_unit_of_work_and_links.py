"""Unit tests: transaction boundary and columnar link storage (adapters).

Spec: batched-resolution — "One database commit per bite", "Failures are isolated per request";
candidate-scoring — "Only the best links are stored".
"""

import duckdb
import pytest

from ere.adapters.duckdb_repositories import DuckDBSimilarityRepository
from ere.adapters.duckdb_schema import init_schema
from ere.adapters.duckdb_unit_of_work import DuckDBUnitOfWork
from ere.models.resolver import LinkTable, MentionId, MentionLink
from test.unit.adapters.stubs import InMemorySimilarityRepository

ENTITY_FIELDS = ["legal_name", "country_code"]
TABLE = LinkTable(
    left_ids=("n1", "n1", "n2"), right_ids=("a", "b", "a"), scores=(0.9, 0.2, 0.7)
)


@pytest.fixture
def con():
    connection = duckdb.connect(":memory:")
    init_schema(connection, ENTITY_FIELDS)
    yield connection
    connection.close()


def _count(con) -> int:
    return con.execute("SELECT count(*) FROM similarities").fetchone()[0]


def test_unit_of_work_commits_on_success(con):
    with DuckDBUnitOfWork(con)():
        DuckDBSimilarityRepository(con).save_table(TABLE)

    assert _count(con) == 3


def test_unit_of_work_rolls_back_on_exception(con):
    with pytest.raises(RuntimeError), DuckDBUnitOfWork(con)():
        DuckDBSimilarityRepository(con).save_table(TABLE)
        raise RuntimeError("fail inside the bite")

    assert _count(con) == 0


def test_link_table_gives_links_of_one_mention():
    assert TABLE.links_for(MentionId(value="n1")) == [
        MentionLink(
            left_id=MentionId(value="n1"), right_id=MentionId(value="a"), score=0.9
        ),
        MentionLink(
            left_id=MentionId(value="n1"), right_id=MentionId(value="b"), score=0.2
        ),
    ]
    assert len(TABLE) == 3


@pytest.mark.parametrize("repo_kind", ["duckdb", "in_memory"])
def test_save_table_stores_every_row(repo_kind, con):
    repo = (
        DuckDBSimilarityRepository(con)
        if repo_kind == "duckdb"
        else InMemorySimilarityRepository()
    )

    repo.save_table(TABLE)

    assert repo.count() == 3
    stored_scores = sorted(link.score for link in repo.find_for(MentionId(value="n1")))
    assert stored_scores == pytest.approx(
        [0.2, 0.9]
    )  # similarities stores REAL (32-bit)
