"""Integration tests for DuckDB adapters (resolver layer + DuckDB)."""

import pytest
import duckdb

# Import from submodules directly to avoid circular imports in __init__
from ere.adapters.duckdb_repositories import (
    DuckDBClusterRepository,
    DuckDBMentionRepository,
    DuckDBSimilarityRepository,
)
from ere.adapters.duckdb_schema import init_schema
from ere.models.resolver import (
    ClusterId,
    Mention,
    MentionId,
)
from ere.services.entity_resolution_service import EntityResolver
from ere.services.resolver_config import DuckDBConfig, ResolverConfig
from .stubs import FixedSimilarityLinker

# Avoid importing from ere.services.__init__ which has circular import
# The services are imported directly from their modules above


@pytest.fixture
def entity_fields():
    """Standard entity fields for tests."""
    return ["legal_name", "country_code"]


@pytest.fixture
def con(entity_fields):
    """In-memory DuckDB connection with initialized schema."""
    c = duckdb.connect(":memory:")
    init_schema(c, entity_fields)
    return c


@pytest.fixture
def config():
    """Default config for tests."""
    return ResolverConfig(
        threshold=0.8,
        match_weight_threshold=-10,
        top_n=100,
        cache_strategy="tf_incremental",
        auto_train_threshold=0,
        entity_fields=["legal_name", "country_code"],
        duckdb=DuckDBConfig(type="in-memory", path=":memory:"),
    )


@pytest.fixture
def service(con, entity_fields, config):
    """Create a resolver with DuckDB adapters."""
    mention_repo = DuckDBMentionRepository(con, entity_fields)
    similarity_repo = DuckDBSimilarityRepository(con)
    cluster_repo = DuckDBClusterRepository(con)
    linker = FixedSimilarityLinker(similarity_map={})

    return EntityResolver(
        mention_repo=mention_repo,
        similarity_repo=similarity_repo,
        cluster_repo=cluster_repo,
        linker=linker,
        config=config,
    )


# ===============================================================================
# Integration tests
# ===============================================================================


def test_resolve_first_mention_persists_to_db(service, con):
    """
    Resolve one mention; assert mentions table has 1 row and clusters table
    has 1 row; assert state returns mention_count=1, cluster_count=1.
    """
    mention = Mention(
        id=MentionId(value="m1"),
        attributes={"legal_name": "Acme Corp", "country_code": "US"},
    )

    result = service.resolve(mention)

    # Check database persistence
    mention_count = con.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
    assert mention_count == 1

    cluster_count = con.execute(
        "SELECT COUNT(DISTINCT cluster_id) FROM clusters"
    ).fetchone()[0]
    assert cluster_count == 1

    # Check state
    state = service.state()
    assert state.mention_count == 1
    assert state.cluster_count == 1

    # Verify result
    assert result.top.cluster_id.value == "m1"
    assert result.top.score == 0.0


def test_resolve_strong_match_joins_cluster_in_db(service, con):
    """
    Resolve m1, then m2 with score=0.95; assert clusters table shows both
    in cluster "m1"; assert state cluster_count=1.
    """
    m1 = Mention(
        id=MentionId(value="m1"),
        attributes={"legal_name": "Acme", "country_code": "US"},
    )
    m2 = Mention(
        id=MentionId(value="m2"),
        attributes={"legal_name": "Acme", "country_code": "US"},
    )

    # Set up linker to return high score
    service._linker._similarity_map = {frozenset(["m1", "m2"]): 0.95}

    service.resolve(m1)
    result2 = service.resolve(m2)

    # Check state
    state = service.state()
    assert state.mention_count == 2
    assert state.cluster_count == 1  # Both in same cluster

    # m2 should join m1's cluster
    assert result2.top.cluster_id.value == "m1"
    assert result2.top.score == pytest.approx(0.95, abs=0.01)


def test_resolve_weak_match_creates_separate_cluster(service, con):
    """
    Resolve m1, then m2 with score=0.5 (below threshold 0.8);
    assert two separate clusters created.

    Note: match_weight_threshold filters which links are stored. Even if a
    match score is below the clustering threshold, it may still be stored if
    it's above match_weight_threshold. But clustering assignment uses the
    clustering threshold parameter.
    """
    m1 = Mention(
        id=MentionId(value="m1"),
        attributes={"legal_name": "Acme", "country_code": "US"},
    )
    m2 = Mention(
        id=MentionId(value="m2"),
        attributes={"legal_name": "Similar but different", "country_code": "US"},
    )

    # Linker returns score below clustering threshold (0.8)
    # but above match_weight_threshold (-10), so link is stored
    service._linker._similarity_map = {frozenset(["m1", "m2"]): 0.5}

    service.resolve(m1)
    result2 = service.resolve(m2)

    # With score 0.5 < threshold 0.8, m2 should create its own cluster
    # But the link is still stored in similarities (for genCand output)
    state = service.state()
    assert state.mention_count == 2
    assert state.cluster_count == 2  # Separate clusters due to threshold

    # m2 is assigned to cluster "m2" (own cluster)
    # genCand returns candidates sorted by score
    # Top candidate will be m1 (score 0.5 via link) not m2 (score 0.0 own cluster)
    assert len(result2.candidates) >= 2
    # m2's own cluster should be in the candidates (as lower-scoring option)
    cluster_ids = [c.cluster_id.value for c in result2.candidates]
    assert "m2" in cluster_ids


def test_resolve_no_match_creates_singleton_cluster(service, con):
    """
    Resolve m1, then m2 with no similarity score; m2 creates singleton cluster.
    """
    m1 = Mention(
        id=MentionId(value="m1"),
        attributes={"legal_name": "Acme", "country_code": "US"},
    )
    m2 = Mention(
        id=MentionId(value="m2"),
        attributes={"legal_name": "Completely Different", "country_code": "UK"},
    )

    # No similarity map entry = no match
    service.resolve(m1)
    result2 = service.resolve(m2)

    # Check state
    state = service.state()
    assert state.mention_count == 2
    assert state.cluster_count == 2

    # m2 is its own cluster (singleton)
    assert result2.top.cluster_id.value == "m2"


def test_state_returns_correct_counts(service, con):
    """Verify that service.state() returns accurate counts."""
    m1 = Mention(
        id=MentionId(value="m1"), attributes={"legal_name": "A", "country_code": "US"}
    )
    m2 = Mention(
        id=MentionId(value="m2"), attributes={"legal_name": "B", "country_code": "US"}
    )

    service._linker._similarity_map = {frozenset(["m1", "m2"]): 0.9}

    service.resolve(m1)
    service.resolve(m2)

    state = service.state()
    assert state.mention_count == 2
    assert state.cluster_count == 1
    assert state.similarity_count > 0


def test_cluster_membership_mapping(service, con):
    """Verify cluster_membership dict is correctly structured."""
    m1 = Mention(
        id=MentionId(value="m1"), attributes={"legal_name": "A", "country_code": "US"}
    )
    m2 = Mention(
        id=MentionId(value="m2"), attributes={"legal_name": "B", "country_code": "US"}
    )

    service._linker._similarity_map = {frozenset(["m1", "m2"]): 0.9}

    service.resolve(m1)
    service.resolve(m2)

    state = service.state()
    memberships = state.cluster_membership

    # Should have one cluster with both mentions
    assert len(memberships) == 1
    cluster_id = list(memberships.keys())[0]
    assert len(memberships[cluster_id]) == 2
    assert MentionId(value="m1") in memberships[cluster_id]
    assert MentionId(value="m2") in memberships[cluster_id]


def test_mention_repository_load_all_returns_persisted_mentions(con, entity_fields):
    """load_all should return all mentions previously saved."""
    repo = DuckDBMentionRepository(con, entity_fields)
    m1 = Mention(id=MentionId(value="la1"), attributes={"legal_name": "Alpha", "country_code": "DE"})
    m2 = Mention(id=MentionId(value="la2"), attributes={"legal_name": "Beta", "country_code": "FR"})

    repo.save(m1)
    repo.save(m2)

    loaded = repo.load_all()

    assert len(loaded) == 2
    ids = {m.id.value for m in loaded}
    assert ids == {"la1", "la2"}


def test_similarity_repository_save_all_empty_is_noop(con):
    """save_all with an empty list should not raise and not write any rows."""
    repo = DuckDBSimilarityRepository(con)

    repo.save_all([])  # must not raise

    count = con.execute("SELECT COUNT(*) FROM similarities").fetchone()[0]
    assert count == 0
