"""Unit tests for EntityResolver and EntityResolutionService (no DuckDB, no Splink)."""

import pytest
from datetime import datetime, timezone

from erspec.models.core import EntityMention, EntityMentionIdentifier
from erspec.models.ere import (
    EREErrorResponse,
    EntityMentionResolutionRequest,
    EntityMentionResolutionResponse,
)

from ere.models.resolver import (
    ClusterId,
    Mention,
    MentionId,
    MentionLink,
)
from ere.services.entity_resolution_service import (
    EntityResolutionService,
    EntityResolver,
    resolve_entity_mention,
)
from ere.services.resolver_config import DuckDBConfig, ResolverConfig
from test.unit.adapters.stubs import (
    FixedSimilarityLinker,
    InMemoryClusterRepository,
    InMemoryMentionRepository,
    InMemorySimilarityRepository,
    StubRDFMapper,
)


@pytest.fixture
def config() -> ResolverConfig:
    """Default config for tests."""
    return ResolverConfig(
        threshold=0.8,
        match_weight_threshold=-10,
        top_n=100,
        cache_strategy="tf_incremental",
        entity_fields=["legal_name", "country_code"],
        duckdb=DuckDBConfig(type="in-memory", path=":memory:"),
    )


@pytest.fixture
def service(config: ResolverConfig) -> EntityResolver:
    """Create a resolver with in-memory stubs."""
    mention_repo = InMemoryMentionRepository()
    similarity_repo = InMemorySimilarityRepository()
    cluster_repo = InMemoryClusterRepository()
    linker = FixedSimilarityLinker(similarity_map={})

    return EntityResolver(
        mention_repo=mention_repo,
        similarity_repo=similarity_repo,
        cluster_repo=cluster_repo,
        linker=linker,
        config=config,
    )


# ===============================================================================
# Core algorithm tests
# ===============================================================================


def test_first_mention_is_singleton(service):
    """Resolving the first mention should create a singleton cluster."""
    mention = Mention(
        id=MentionId(value="m1"),
        attributes={"legal_name": "Acme Corp", "country_code": "US"},
    )

    result = service.resolve(mention)

    # Result should have one candidate: the mention's own cluster
    assert len(result.candidates) == 1
    assert result.top.cluster_id.value == "m1"
    assert result.top.score == 0.0

    # State should reflect the mention
    state = service.state()
    assert state.mention_count == 1
    assert state.cluster_count == 1
    assert "m1" in [m.value for m in state.cluster_membership[ClusterId(value="m1")]]


def test_strong_match_joins_cluster(service):
    """A mention matching >= threshold should join the best match's cluster."""
    # Resolve m1 first
    m1 = Mention(
        id=MentionId(value="m1"),
        attributes={"legal_name": "Acme", "country_code": "US"},
    )
    result1 = service.resolve(m1)
    assert result1.top.cluster_id.value == "m1"

    # Now resolve m2 with strong match to m1
    m2 = Mention(
        id=MentionId(value="m2"),
        attributes={"legal_name": "Acme Corp", "country_code": "US"},
    )

    # Set up the linker to return a strong match (m1, m2, 0.95)
    service._linker = FixedSimilarityLinker(
        similarity_map={frozenset(["m1", "m2"]): 0.95}
    )
    service._linker.register_mention(m1)

    result2 = service.resolve(m2)

    # m2 should join m1's cluster (cluster "m1")
    assert result2.top.cluster_id.value == "m1"
    assert result2.top.score == pytest.approx(0.95, abs=0.01)

    # State should show both in cluster m1
    state = service.state()
    assert state.mention_count == 2
    assert state.cluster_count == 1  # Still one cluster
    cluster_m1 = state.cluster_membership[ClusterId(value="m1")]
    assert len(cluster_m1) == 2
    assert set(m.value for m in cluster_m1) == {"m1", "m2"}


def test_below_threshold_becomes_singleton(service):
    """A mention with only weak matches (< threshold) should become singleton cluster assignment."""
    # Resolve m1 first
    m1 = Mention(
        id=MentionId(value="m1"),
        attributes={"legal_name": "Acme", "country_code": "US"},
    )
    service.resolve(m1)

    # Resolve m2 with weak match to m1
    m2 = Mention(
        id=MentionId(value="m2"),
        attributes={"legal_name": "ACME Inc", "country_code": "US"},
    )

    # Set up weak match (0.7 < threshold 0.8)
    service._linker = FixedSimilarityLinker(
        similarity_map={frozenset(["m1", "m2"]): 0.7}
    )
    service._linker.register_mention(m1)

    result2 = service.resolve(m2)

    # m2 should be assigned to its own cluster (cluster "m2"),
    # but genCand still includes m1's cluster (via the below-threshold link)
    assert (
        result2.top.cluster_id.value == "m1"
    )  # Still top by score, but own cluster also present
    assert result2.top.score == pytest.approx(0.7, abs=0.01)

    # Verify the new invariant: own cluster is always included
    assert len(result2.candidates) == 2
    assert result2.candidates[1].cluster_id.value == "m2"
    assert result2.candidates[1].score == 0.0

    # State should show two clusters (m2 was assigned to its own cluster "m2")
    state = service.state()
    assert state.mention_count == 2
    assert state.cluster_count == 2  # Two separate clusters
    assert set(state.cluster_membership.keys()) == {
        ClusterId(value="m1"),
        ClusterId(value="m2"),
    }


def test_gen_cand_includes_below_threshold_links(service):
    """
    If a mention has a below-threshold link to a cluster, that cluster
    should appear in the candidates list.

    This tests the bridge case: a mention may not join a cluster (score < THR)
    but that cluster should still appear in genCand output.
    """
    # Resolve m1 and m3 in cluster 1, m3 in cluster 3
    m1 = Mention(
        id=MentionId(value="m1"),
        attributes={"legal_name": "Acme", "country_code": "US"},
    )
    m3 = Mention(
        id=MentionId(value="m3"),
        attributes={"legal_name": "Globex", "country_code": "US"},
    )
    service.resolve(m1)
    service.resolve(m3)  # m3 forms its own cluster

    # Resolve m2 with:
    # - strong link (0.85) to m1 (cluster "m1") -> joins cluster "m1"
    # - weak link (0.7) to m3 (cluster "m3") -> below threshold
    m2 = Mention(
        id=MentionId(value="m2"),
        attributes={"legal_name": "Acme Corp", "country_code": "US"},
    )

    service._linker = FixedSimilarityLinker(
        similarity_map={
            frozenset(["m1", "m2"]): 0.85,  # strong
            frozenset(["m2", "m3"]): 0.7,  # weak
        }
    )
    service._linker.register_mention(m1)
    service._linker.register_mention(m3)

    result = service.resolve(m2)

    # Result should include both clusters
    cluster_ids = {c.cluster_id.value for c in result.candidates}
    assert cluster_ids == {"m1", "m3"}

    # m1 should be first (higher score)
    assert result.top.cluster_id.value == "m1"
    assert result.top.score == pytest.approx(0.85, abs=0.01)


def test_gen_cand_groups_by_cluster(service):
    """
    If a mention has multiple links to members of the same cluster,
    genCand should group them and use the max similarity as the cluster score.
    """
    # Cluster 1: m1, m2
    m1 = Mention(
        id=MentionId(value="m1"),
        attributes={"legal_name": "Acme", "country_code": "US"},
    )
    m2 = Mention(
        id=MentionId(value="m2"),
        attributes={"legal_name": "Acme Corp", "country_code": "US"},
    )
    service.resolve(m1)
    service._linker = FixedSimilarityLinker({frozenset(["m1", "m2"]): 0.95})
    service._linker.register_mention(m1)
    service.resolve(m2)

    # m3 has weak links to both m1 (0.75) and m2 (0.85) in the same cluster
    m3 = Mention(
        id=MentionId(value="m3"),
        attributes={"legal_name": "Acme Industries", "country_code": "US"},
    )

    service._linker = FixedSimilarityLinker(
        similarity_map={
            frozenset(["m1", "m2"]): 0.95,
            frozenset(["m1", "m3"]): 0.75,  # to m1
            frozenset(["m2", "m3"]): 0.85,  # to m2, same cluster
        }
    )
    service._linker.register_mention(m1)
    service._linker.register_mention(m2)

    result = service.resolve(m3)

    # Result should have one candidate: cluster m1 with max score (0.85)
    assert len(result.candidates) == 1
    assert result.top.cluster_id.value == "m1"
    assert result.top.score == pytest.approx(0.85, abs=0.01)


# ===============================================================================
# Training and state management
# ===============================================================================


def test_train_can_be_called_anytime(service):
    """train() should succeed even with very few mentions (uses cold-start defaults)."""
    # Add just 1 mention
    mention = Mention(
        id=MentionId(value="m1"),
        attributes={
            "legal_name": "Company 1",
            "country_code": "US",
        },
    )
    service.resolve(mention)

    # train() should succeed (linker is a no-op stub, uses cold-start)
    service.train()  # Should not raise


def test_auto_training_triggers_at_threshold(service):
    """
    Auto-training should trigger non-blocking when mention count reaches threshold.

    We use a spy wrapper to count train() calls on the linker.
    """
    # Create config with low threshold (3 mentions)
    config = ResolverConfig(
        threshold=0.8,
        match_weight_threshold=-10,
        top_n=100,
        cache_strategy="tf_incremental",
        auto_train_threshold=3,
        entity_fields=["legal_name", "country_code"],
        duckdb=DuckDBConfig(type="in-memory", path=":memory:"),
    )

    mention_repo = InMemoryMentionRepository()
    similarity_repo = InMemorySimilarityRepository()
    cluster_repo = InMemoryClusterRepository()

    # Wrap linker with a call counter
    base_linker = FixedSimilarityLinker(similarity_map={})
    call_count = {"train": 0}
    original_train = base_linker.train

    def counting_train():
        call_count["train"] += 1
        return original_train()

    base_linker.train = counting_train

    service = EntityResolver(
        mention_repo=mention_repo,
        similarity_repo=similarity_repo,
        cluster_repo=cluster_repo,
        linker=base_linker,
        config=config,
    )

    # Resolve 3 mentions: at the 3rd, training should trigger
    for i in range(3):
        mention = Mention(
            id=MentionId(value=f"m{i}"),
            attributes={
                "legal_name": f"Company {i}",
                "country_code": "US",
            },
        )
        service.resolve(mention)
        service._linker.register_mention(mention)

    # After resolving the 3rd mention, train should have been called once
    assert call_count["train"] == 1


def test_state_reflects_mentions(service):
    """State should reflect all resolved mentions."""
    m1 = Mention(
        id=MentionId(value="m1"),
        attributes={"legal_name": "Acme", "country_code": "US"},
    )
    m2 = Mention(
        id=MentionId(value="m2"),
        attributes={"legal_name": "Acme Corp", "country_code": "US"},
    )

    service.resolve(m1)
    state1 = service.state()
    assert state1.mention_count == 1

    service._linker = FixedSimilarityLinker({frozenset(["m1", "m2"]): 0.95})
    service._linker.register_mention(m1)
    service.resolve(m2)
    state2 = service.state()
    assert state2.mention_count == 2


def test_state_reflects_clusters(service):
    """State should reflect cluster membership."""
    m1 = Mention(
        id=MentionId(value="m1"),
        attributes={"legal_name": "Acme", "country_code": "US"},
    )
    service.resolve(m1)

    state = service.state()
    assert state.cluster_count == 1
    assert ClusterId(value="m1") in state.cluster_membership
    assert state.cluster_membership[ClusterId(value="m1")] == [MentionId(value="m1")]


def test_state_reflects_similarities(service):
    """State should reflect all stored similarities."""
    m1 = Mention(
        id=MentionId(value="m1"),
        attributes={"legal_name": "Acme", "country_code": "US"},
    )
    m2 = Mention(
        id=MentionId(value="m2"),
        attributes={"legal_name": "Acme Corp", "country_code": "US"},
    )

    service.resolve(m1)

    state1 = service.state()
    assert state1.similarity_count == 0

    service._linker = FixedSimilarityLinker({frozenset(["m1", "m2"]): 0.95})
    service._linker.register_mention(m1)
    service.resolve(m2)

    state2 = service.state()
    # One similarity link: (m1, m2, 0.95)
    assert state2.similarity_count == 1


# ===============================================================================
# Edge cases and invariants
# ===============================================================================


def test_resolution_result_never_empty(service):
    """Every resolve() call should return non-empty ResolutionResult."""
    m1 = Mention(
        id=MentionId(value="m1"),
        attributes={"legal_name": "Acme", "country_code": "US"},
    )
    result = service.resolve(m1)

    assert len(result.candidates) >= 1


def test_resolution_result_always_top_n_pruned(service):
    """Results should be pruned to top_n."""
    # Set a small top_n
    config_small = ResolverConfig(
        threshold=0.5,
        match_weight_threshold=-10,
        top_n=2,  # Small limit
        entity_fields=["legal_name", "country_code"],
        duckdb=DuckDBConfig(type="in-memory", path=":memory:"),
    )

    mention_repo = InMemoryMentionRepository()
    similarity_repo = InMemorySimilarityRepository()
    cluster_repo = InMemoryClusterRepository()

    # Set up linker to return links to 5 different clusters
    linker = FixedSimilarityLinker(
        similarity_map={
            frozenset(["m1", "m2"]): 0.9,
            frozenset(["m1", "m3"]): 0.8,
            frozenset(["m1", "m4"]): 0.7,
            frozenset(["m1", "m5"]): 0.6,
            frozenset(["m1", "m6"]): 0.5,
        }
    )

    service = EntityResolver(
        mention_repo=mention_repo,
        similarity_repo=similarity_repo,
        cluster_repo=cluster_repo,
        linker=linker,
        config=config_small,
    )

    # Add 5 mentions to different clusters
    for i in range(2, 7):
        mention = Mention(
            id=MentionId(value=f"m{i}"),
            attributes={"legal_name": f"Company {i}", "country_code": "US"},
        )
        service.resolve(mention)

    # Register all existing mentions with linker
    for mention in service._mention_repo.load_all():
        linker.register_mention(mention)

    m1 = Mention(
        id=MentionId(value="m1"),
        attributes={"legal_name": "Company 1", "country_code": "US"},
    )
    result = service.resolve(m1)

    # Result should be pruned to top_n (2)
    assert len(result.candidates) <= config_small.top_n


def test_multiple_independent_clusters(service):
    """Mentions with no links should form independent clusters."""
    m1 = Mention(
        id=MentionId(value="m1"),
        attributes={"legal_name": "Acme", "country_code": "US"},
    )
    m2 = Mention(
        id=MentionId(value="m2"),
        attributes={"legal_name": "Globex", "country_code": "US"},
    )
    m3 = Mention(
        id=MentionId(value="m3"),
        attributes={"legal_name": "Initech", "country_code": "US"},
    )

    # No links between any of them
    service._linker = FixedSimilarityLinker(similarity_map={})

    service.resolve(m1)
    service._linker.register_mention(m1)
    service.resolve(m2)
    service._linker.register_mention(m2)
    service.resolve(m3)

    state = service.state()
    assert state.cluster_count == 3
    assert state.mention_count == 3


# ===============================================================================
# resolve_entity_mention guard tests
# ===============================================================================


def test_resolve_entity_mention_raises_when_resolver_is_none():
    mention = EntityMention(
        identifiedBy=EntityMentionIdentifier(
            request_id="m1",
            source_id="src",
            entity_type="http://test.org/Org",
        ),
        content_type="text/turtle",
        content="<>",
    )
    with pytest.raises(ValueError, match="resolver must be provided"):
        resolve_entity_mention(mention, resolver=None, mapper=StubRDFMapper())


def test_resolve_entity_mention_raises_when_mapper_is_none(service):
    mention = EntityMention(
        identifiedBy=EntityMentionIdentifier(
            request_id="m1",
            source_id="src",
            entity_type="http://test.org/Org",
        ),
        content_type="text/turtle",
        content="<>",
    )
    with pytest.raises(ValueError, match="mapper must be provided"):
        resolve_entity_mention(mention, resolver=service, mapper=None)


# ===============================================================================
# EntityResolutionService tests
# ===============================================================================


@pytest.fixture
def stub_mapper() -> StubRDFMapper:
    return StubRDFMapper()


@pytest.fixture
def resolution_service(service: EntityResolver, stub_mapper: StubRDFMapper) -> EntityResolutionService:
    return EntityResolutionService(resolver=service, mapper=stub_mapper)


def _make_request(request_id: str = "req-001") -> EntityMentionResolutionRequest:
    return EntityMentionResolutionRequest(
        entity_mention=EntityMention(
            identifiedBy=EntityMentionIdentifier(
                request_id=request_id,
                source_id="test-src",
                entity_type="http://test.org/Org",
            ),
            content_type="text/turtle",
            content="<>",
        ),
        ere_request_id=request_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def test_process_request_unsupported_type_returns_error_response(resolution_service):
    class UnknownRequest:
        ere_request_id = "unknown-001"

    response = resolution_service.process_request(UnknownRequest())

    assert isinstance(response, EREErrorResponse)
    assert response.error_type == "UnsupportedRequestType"


def test_process_request_happy_path_returns_resolution_response(resolution_service):
    request = _make_request("req-happy")

    response = resolution_service.process_request(request)

    assert isinstance(response, EntityMentionResolutionResponse)
    assert response.ere_request_id == "req-happy"
    assert len(response.candidates) >= 1


def test_process_request_mapper_error_returns_error_response(service: EntityResolver):
    failing_mapper = StubRDFMapper(error=ValueError("RDF parse failure"))
    svc = EntityResolutionService(resolver=service, mapper=failing_mapper)

    response = svc.process_request(_make_request("req-fail"))

    assert isinstance(response, EREErrorResponse)
    assert response.error_type == "ValueError"
    assert "RDF parse failure" in response.error_detail


def test_call_delegates_to_process_request(resolution_service):
    request = _make_request("req-call")
    response = resolution_service(request)
    assert isinstance(response, EntityMentionResolutionResponse)
    assert response.ere_request_id == "req-call"
