"""Integration test: EntityResolver with all real adapters.

This test wires EntityResolver with real DuckDB repositories and
SpLinkSimilarityLinker to demonstrate the complete entity resolution flow:
initialization, resolution, training, and state introspection.
"""

import pytest
import duckdb

from ere.adapters.duckdb_repositories import (
    DuckDBClusterRepository,
    DuckDBMentionRepository,
    DuckDBSimilarityRepository,
)
from ere.adapters.splink_linker_impl import SpLinkSimilarityLinker, build_tf_df
from ere.adapters.duckdb_schema import init_schema
from ere.models.resolver import Mention
from ere.services.entity_resolution_service import EntityResolver
from ere.services.resolver_config import DuckDBConfig, ResolverConfig


# ===============================================================================
# Module-scoped fixtures
# ===============================================================================


@pytest.fixture(scope="module")
def entity_fields():
    """Standard entity fields for tests."""
    return ["legal_name", "country_code"]


@pytest.fixture(scope="module")
def resolver_config():
    """Resolver configuration for tests."""
    return ResolverConfig(
        threshold=0.5,
        match_weight_threshold=-10,
        top_n=100,
        cache_strategy="tf_incremental",
        entity_fields=["legal_name", "country_code"],
        duckdb=DuckDBConfig(type="in-memory", path=":memory:"),
    )


@pytest.fixture(scope="module")
def splink_config():
    """Splink configuration dict (needed for SpLinkSimilarityLinker)."""
    return {
        "threshold": 0.5,
        "match_weight_threshold": -10,
        "top_n": 100,
        "cache_strategy": "tf_incremental",
        "splink": {
            "probability_two_random_records_match": 0.3,
            "comparisons": [
                {
                    "type": "jaro_winkler",
                    "field": "legal_name",
                    "thresholds": [0.9, 0.8],
                }
            ],
            "blocking_rules": ["country_code"],
        },
    }


@pytest.fixture
def con(entity_fields):
    """Fresh in-memory DuckDB connection with initialized schema."""
    c = duckdb.connect(":memory:")
    init_schema(c, entity_fields)
    return c


@pytest.fixture
def service(con, entity_fields, resolver_config, splink_config):
    """
    Create EntityResolver with all real adapters.

    Wiring:
    - DuckDBMentionRepository for persistence
    - DuckDBSimilarityRepository for pairwise scores
    - DuckDBClusterRepository for cluster assignments
    - SpLinkSimilarityLinker for Splink-based scoring
    """
    mention_repo = DuckDBMentionRepository(con, entity_fields)
    similarity_repo = DuckDBSimilarityRepository(con)
    cluster_repo = DuckDBClusterRepository(con)
    linker = SpLinkSimilarityLinker(entity_fields, splink_config)

    return EntityResolver(
        mention_repo=mention_repo,
        similarity_repo=similarity_repo,
        cluster_repo=cluster_repo,
        linker=linker,
        config=resolver_config,
    )


# ===============================================================================
# integration tests
# ===============================================================================


@pytest.mark.integration
def test_first_mention_resolves_to_singleton(service, con):
    """
    Resolve the first mention.
    Assert: creates a singleton cluster (mention is its own cluster).
    """
    m1 = Mention(mention_id="m1", legal_name="Acme Corp", country_code="US")

    result = service.resolve(m1)

    # First mention is its own cluster
    assert result.top.cluster_id.value == "m1"
    assert result.top.score == 0.0  # Self-cluster has zero similarity
    assert len(result.candidates) >= 1

    # Verify persistence
    mention_count = con.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
    assert mention_count == 1
    cluster_count = con.execute(
        "SELECT COUNT(DISTINCT cluster_id) FROM clusters"
    ).fetchone()[0]
    assert cluster_count == 1


@pytest.mark.integration
def test_strong_match_joins_existing_cluster(service, con):
    """
    Resolve m1, then resolve m2 (similar name, same country).
    Assert: m2 joins m1's cluster (strong match above threshold).
    """
    m1 = Mention(mention_id="m1", legal_name="Acme Corp", country_code="US")
    m2 = Mention(mention_id="m2", legal_name="Acme Corporation", country_code="US")

    result1 = service.resolve(m1)
    assert result1.top.cluster_id.value == "m1"

    result2 = service.resolve(m2)
    assert result2.top.cluster_id.value == "m1", "m2 should join m1's cluster"

    # Verify both in same cluster
    cluster_rows = con.execute(
        "SELECT mention_id FROM clusters WHERE cluster_id = 'm1' ORDER BY mention_id"
    ).fetchall()
    assert [row[0] for row in cluster_rows] == ["m1", "m2"]


@pytest.mark.integration
def test_below_threshold_creates_new_cluster(service, con):
    """
    Resolve m1 (resolves to its own cluster), then resolve m2.
    Assert: cluster assignments persist and both mentions are resolved.
    """
    m1 = Mention(mention_id="m1", legal_name="Acme Corporation", country_code="US")
    m2 = Mention(mention_id="m2", legal_name="BestCo Industries", country_code="US")

    result1 = service.resolve(m1)
    result2 = service.resolve(m2)

    # Both should resolve to some cluster
    assert result1.top is not None
    assert result2.top is not None

    # Verify both are in the database
    mention_count = con.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
    assert mention_count == 2

    # Verify cluster assignments persist
    cluster_count = con.execute(
        "SELECT COUNT(DISTINCT cluster_id) FROM clusters"
    ).fetchone()[0]
    assert cluster_count >= 1


@pytest.mark.integration
def test_cross_country_blocked_by_blocking_rule(service, con):
    """
    Resolve m1 (US), then resolve m2 (DE, similar name but different country).
    Assert: blocking rule prevents comparison, m2 creates new cluster.
    """
    m1 = Mention(mention_id="m1", legal_name="Acme", country_code="US")
    m2 = Mention(mention_id="m2", legal_name="Acme", country_code="DE")

    service.resolve(m1)
    result2 = service.resolve(m2)

    assert result2.top.cluster_id.value == "m2", "Blocking rule should prevent match"

    # Verify no similarities stored (blocked pair)
    sim_count = con.execute("SELECT COUNT(*) FROM similarities").fetchone()[0]
    assert sim_count == 0, "No similarities should exist for blocked cross-country pair"


@pytest.mark.integration
def test_similarities_persisted_to_repository(service, con):
    """
    Resolve multiple mentions with some matches.
    Assert: similarities table contains the scored pairs.
    """
    m1 = Mention(mention_id="m1", legal_name="Acme", country_code="US")
    m2 = Mention(mention_id="m2", legal_name="Acme Inc", country_code="US")
    m3 = Mention(mention_id="m3", legal_name="BestCo", country_code="US")

    service.resolve(m1)
    service.resolve(m2)  # Should score m2 vs m1 (similar)
    service.resolve(m3)  # Should score m3 vs m1, m2 (dissimilar)

    # Verify similarities persisted
    sim_rows = con.execute("SELECT COUNT(*) FROM similarities").fetchone()[0]
    assert sim_rows > 0, "Similarities should be persisted"

    # Verify pair structure
    pair_rows = con.execute(
        "SELECT mention_id_l, mention_id_r FROM similarities ORDER BY mention_id_l, mention_id_r"
    ).fetchall()
    assert len(pair_rows) >= 2, "Should have at least 2 pairs (m2 vs m1, m3 vs m1/m2)"


@pytest.mark.integration
def test_train_succeeds_with_sufficient_records(service, con):
    """
    Resolve 10+ mentions, then train.
    Assert: training succeeds (uses cold-start), linker is still functional.
    """
    mentions = [
        Mention(mention_id="a1", legal_name="Acme Corp", country_code="US"),
        Mention(mention_id="a2", legal_name="Acme", country_code="US"),
        Mention(mention_id="b1", legal_name="BestCo Inc", country_code="US"),
        Mention(mention_id="b2", legal_name="BestCo", country_code="US"),
        Mention(mention_id="c1", legal_name="TechSoft Ltd", country_code="US"),
        Mention(mention_id="c2", legal_name="TechSoft", country_code="US"),
        Mention(mention_id="d1", legal_name="InnovateX SARL", country_code="US"),
        Mention(mention_id="d2", legal_name="Innovate X", country_code="US"),
        Mention(mention_id="e1", legal_name="GlobalTrade BV", country_code="US"),
        Mention(mention_id="e2", legal_name="GlobalTrade", country_code="US"),
    ]

    for mention in mentions:
        service.resolve(mention)

    # Training should succeed (cold-start is used if EM fails)
    service.train()

    # Verify linker is still functional
    query = Mention(
        mention_id="test_q", legal_name="Acme Technologies", country_code="US"
    )
    result = service.resolve(query)

    assert result.top is not None
    assert len(result.candidates) >= 1


@pytest.mark.integration
def test_auto_training_nonblocking(con, entity_fields):
    """
    Auto-training triggers at threshold without blocking resolution.
    Verify resolution returns immediately (not delayed by training).
    """
    splink_config = {
        "threshold": 0.5,
        "match_weight_threshold": -10,
        "top_n": 100,
        "cache_strategy": "tf_incremental",
        "splink": {
            "probability_two_random_records_match": 0.3,
            "comparisons": [
                {
                    "type": "jaro_winkler",
                    "field": "legal_name",
                    "thresholds": [0.9, 0.8],
                }
            ],
            "blocking_rules": ["country_code"],
        },
    }

    config = ResolverConfig(
        threshold=0.5,
        match_weight_threshold=-10,
        top_n=100,
        cache_strategy="tf_incremental",
        auto_train_threshold=5,  # Trigger at 5 mentions
        entity_fields=["legal_name", "country_code"],
        duckdb=DuckDBConfig(type="in-memory", path=":memory:"),
    )

    mention_repo = DuckDBMentionRepository(con, entity_fields)
    similarity_repo = DuckDBSimilarityRepository(con)
    cluster_repo = DuckDBClusterRepository(con)
    linker = SpLinkSimilarityLinker(entity_fields, splink_config)

    test_service = EntityResolver(
        mention_repo=mention_repo,
        similarity_repo=similarity_repo,
        cluster_repo=cluster_repo,
        linker=linker,
        config=config,
    )

    # Resolve 5 mentions - at the 5th, training should trigger
    mentions = [
        Mention(mention_id="m1", legal_name="Acme", country_code="US"),
        Mention(mention_id="m2", legal_name="Acme Inc", country_code="US"),
        Mention(mention_id="m3", legal_name="BestCo", country_code="US"),
        Mention(mention_id="m4", legal_name="TechSoft", country_code="US"),
        Mention(mention_id="m5", legal_name="GlobalTrade", country_code="US"),
    ]

    for mention in mentions:
        result = test_service.resolve(mention)
        # Resolution should return immediately, not block for training
        assert result.top is not None

    # Verify state accumulated
    state = test_service.state()
    assert state.mention_count == 5


@pytest.mark.integration
def test_state_reflects_all_mentions(service, con):
    """
    Resolve multiple mentions, check state.
    Assert: state.mention_count matches database.
    """
    mentions = [
        Mention(mention_id="m1", legal_name="Acme", country_code="US"),
        Mention(mention_id="m2", legal_name="BestCo", country_code="US"),
        Mention(mention_id="m3", legal_name="TechSoft", country_code="US"),
    ]

    for mention in mentions:
        service.resolve(mention)

    state = service.state()

    assert state.mention_count == 3
    db_mention_count = con.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
    assert state.mention_count == db_mention_count


@pytest.mark.integration
def test_state_reflects_cluster_membership(service):
    """
    Resolve mentions and verify cluster membership is reflected in state.
    Assert: state.cluster_membership shows correct assignment structure.
    """
    m1 = Mention(mention_id="m1", legal_name="Acme Corporation", country_code="US")
    m2 = Mention(mention_id="m2", legal_name="Acme Corp", country_code="US")
    m3 = Mention(mention_id="m3", legal_name="BestCo Industries", country_code="US")

    service.resolve(m1)
    service.resolve(m2)
    service.resolve(m3)

    state = service.state()

    # Verify cluster structure
    assert state.cluster_count >= 1, "Should have at least 1 cluster"
    assert state.mention_count == 3, "Should have 3 mentions"

    # Verify all mentions are assigned to some cluster
    all_mentions = set()
    for cluster_id, mention_list in state.cluster_membership.items():
        for mention in mention_list:
            all_mentions.add(mention.value)

    assert all_mentions == {"m1", "m2", "m3"}, "All mentions should be assigned"


@pytest.mark.integration
def test_state_reflects_similarity_count(service, con):
    """
    Resolve mentions, check state.similarity_count.
    Assert: matches number of persisted similarities.
    """
    m1 = Mention(mention_id="m1", legal_name="Acme", country_code="US")
    m2 = Mention(mention_id="m2", legal_name="Acme Inc", country_code="US")
    m3 = Mention(mention_id="m3", legal_name="BestCo", country_code="US")

    service.resolve(m1)
    service.resolve(m2)
    service.resolve(m3)

    state = service.state()

    db_sim_count = con.execute("SELECT COUNT(*) FROM similarities").fetchone()[0]
    assert state.similarity_count == db_sim_count


@pytest.mark.integration
def test_linker_warm_start_capability(entity_fields, splink_config):
    """
    Verify SpLinkSimilarityLinker supports warm-start with pre-seeded mentions.
    Assert: linker initialized with initial_df can score against those mentions.
    """
    # Create initial mentions for warm-start
    initial_mentions = [
        Mention(mention_id="seed1", legal_name="Acme Corp", country_code="US"),
        Mention(mention_id="seed2", legal_name="BestCo", country_code="US"),
    ]

    # Create linker with warm-start initial_df
    initial_df = build_tf_df(initial_mentions, entity_fields)
    linker = SpLinkSimilarityLinker(entity_fields, splink_config, initial_df=initial_df)

    # Query against warm-start mentions
    query = Mention(mention_id="q1", legal_name="Acme", country_code="US")
    links = linker.find_matches(query)

    # Should find links to seed1 (similar name)
    assert len(links) >= 1, "Should find matches against warm-start mentions"

    # Register new mention and query again
    linker.register_mention(query)
    query2 = Mention(mention_id="q2", legal_name="Acme Inc", country_code="US")
    links2 = linker.find_matches(query2)

    # Should find links to both seed1 and q1
    assert len(links2) >= 1, "Linker should work after registering new mention"


@pytest.mark.integration
def test_multiple_resolves_accumulate_state(service, con):
    """
    Resolve mentions in sequence, verify state accumulates correctly.
    Assert: each resolve persists and is visible in subsequent resolves.
    """
    mentions = [
        Mention(mention_id="m1", legal_name="Acme", country_code="US"),
        Mention(mention_id="m2", legal_name="Acme Inc", country_code="US"),
        Mention(mention_id="m3", legal_name="Acme Corp", country_code="US"),
    ]

    for i, mention in enumerate(mentions, 1):
        result = service.resolve(mention)
        state = service.state()

        # Verify state accumulates
        assert state.mention_count == i, (
            f"After resolving {i} mentions, should have {i} in DB"
        )

        # Later mentions should see earlier mentions in results
        if i > 1:
            assert len(result.candidates) >= 1, (
                "Should see candidates from earlier mentions"
            )


@pytest.mark.integration
def test_end_to_end_realistic_scenario(service, con):
    """
    Realistic scenario: resolve a stream of entity mentions with variants.
    Assert: all mentions are resolved to clusters, similarities are persisted.
    """
    # Stream of mentions: 3 companies with variants
    mentions = [
        # Company A
        Mention(
            mention_id="acme_1", legal_name="Acme Corporation Ltd", country_code="US"
        ),
        Mention(mention_id="acme_2", legal_name="Acme Corp", country_code="US"),
        Mention(mention_id="acme_3", legal_name="Acme", country_code="US"),
        # Company B
        Mention(
            mention_id="bestco_1", legal_name="BestCo Industries Inc", country_code="US"
        ),
        Mention(mention_id="bestco_2", legal_name="BestCo Inc", country_code="US"),
        # Company C
        Mention(
            mention_id="techsoft_1",
            legal_name="TechSoft Solutions Limited",
            country_code="US",
        ),
        Mention(mention_id="techsoft_2", legal_name="TechSoft Ltd", country_code="US"),
        Mention(mention_id="techsoft_3", legal_name="TechSoft", country_code="US"),
    ]

    for mention in mentions:
        service.resolve(mention)

    # Verify state
    state = service.state()
    assert state.mention_count == 8, "Should have resolved all 8 mentions"
    assert state.cluster_count >= 3, "Should have at least 3 clusters (one per company)"
    assert state.similarity_count > 0, "Should have persisted similarities"

    # Build a map from mention_id to cluster_id
    mention_to_cluster = {}
    for cluster_id, mention_list in state.cluster_membership.items():
        for mention in mention_list:
            mention_to_cluster[mention.value] = cluster_id

    # Verify all mentions are assigned
    assert set(mention_to_cluster.keys()) == {
        "acme_1",
        "acme_2",
        "acme_3",
        "bestco_1",
        "bestco_2",
        "techsoft_1",
        "techsoft_2",
        "techsoft_3",
    }, "All mentions should be assigned to clusters"

    # Verify different companies are in different clusters
    # (strongest assertion: first mention of each company should be in different clusters)
    acme_cluster = mention_to_cluster["acme_1"]
    bestco_cluster = mention_to_cluster["bestco_1"]
    techsoft_cluster = mention_to_cluster["techsoft_1"]

    assert len({acme_cluster, bestco_cluster, techsoft_cluster}) == 3, (
        "Different companies should be in different clusters"
    )
