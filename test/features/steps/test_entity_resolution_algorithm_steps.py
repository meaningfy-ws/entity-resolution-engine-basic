"""Step definitions for entity_resolution_algorithm.feature.

Tests the core entity resolution algorithm with simple mentions and configurable similarities.
"""

import pytest
from assertpy import assert_that
from pytest_bdd import given, when, then, parsers, scenarios

from ere.models.resolver import Mention, MentionId, ClusterId
from ere.services.entity_resolution_service import EntityResolver
from ere.services.resolver_config import DuckDBConfig, ResolverConfig
from test.unit.adapters.stubs import (
    InMemoryMentionRepository,
    InMemorySimilarityRepository,
    InMemoryClusterRepository,
    FixedSimilarityLinker,
)

scenarios("../entity_resolution_algorithm.feature")


# ===============================================================================
# Fixtures for scenario context
# ===============================================================================


@pytest.fixture
def algorithm_context():
    """Mutable container to hold scenario state."""
    return {
        "service": None,
        "last_result": None,
        "similarities": {},  # frozenset([id1, id2]) -> score
    }


# ===============================================================================
# Given steps
# ===============================================================================


@given(parsers.parse("an entity resolution service with threshold {threshold}"))
def create_service(threshold: str, algorithm_context):
    """Create a fresh EntityResolver with specified threshold."""
    threshold_value = float(threshold)
    config = ResolverConfig(
        threshold=threshold_value,
        match_weight_threshold=-10,
        top_n=100,
        cache_strategy="tf_incremental",
        entity_fields=["legal_name", "country_code"],
        duckdb=DuckDBConfig(type="in-memory", path=":memory:"),
    )

    mention_repo = InMemoryMentionRepository()
    similarity_repo = InMemorySimilarityRepository()
    cluster_repo = InMemoryClusterRepository()
    linker = FixedSimilarityLinker(similarity_map={})

    algorithm_context["service"] = EntityResolver(
        mention_repo=mention_repo,
        similarity_repo=similarity_repo,
        cluster_repo=cluster_repo,
        linker=linker,
        config=config,
    )
    algorithm_context["similarities"] = {}


# ===============================================================================
# When steps
# ===============================================================================


@when(parsers.parse('I resolve mention "{mention_id}"'))
def resolve_mention(mention_id: str, algorithm_context):
    """Resolve a mention with the configured similarities."""
    service = algorithm_context["service"]

    # Create mention
    mention = Mention(
        id=MentionId(value=mention_id),
        attributes={"legal_name": f"Company {mention_id}", "country_code": "US"},
    )

    # Update linker with new similarities
    similarities = algorithm_context["similarities"]
    linker = FixedSimilarityLinker(similarity_map=similarities)

    # Register previously resolved mentions
    for prev_mention in service._mention_repo.load_all():
        linker.register_mention(prev_mention)

    # Update service linker
    service._linker = linker

    # Resolve the mention
    result = service.resolve(mention)

    # Store the result for Then steps
    algorithm_context["last_result"] = result


@when(
    parsers.parse('I set similarity between "{left_id}" and "{right_id}" to {score:f}')
)
def set_similarity(left_id: str, right_id: str, score: float, algorithm_context):
    """Set similarity between two mentions."""
    pair_set = frozenset([left_id, right_id])
    algorithm_context["similarities"][pair_set] = score


# ===============================================================================
# Then steps
# ===============================================================================


@then(
    parsers.parse(
        'mention "{mention_id}" is in cluster "{cluster_id}" with score {score:f}'
    )
)
def check_mention_cluster(
    mention_id: str, cluster_id: str, score: float, algorithm_context
):
    """Verify that a mention is assigned to a cluster with the expected score."""
    result = algorithm_context["last_result"]
    assert_that(result.top.cluster_id.value).is_equal_to(cluster_id)
    assert_that(result.top.score).is_close_to(score, 0.01)


@then(parsers.parse("the result has {count:d} candidate clusters"))
def check_candidate_count(count: int, algorithm_context):
    """Verify the number of candidate clusters in the result."""
    result = algorithm_context["last_result"]
    assert_that(len(result.candidates)).is_equal_to(count)


@then(
    parsers.parse('candidate {index:d} is cluster "{cluster_id}" with score {score:f}')
)
def check_candidate(index: int, cluster_id: str, score: float, algorithm_context):
    """Verify a specific candidate cluster and its score."""
    result = algorithm_context["last_result"]
    assert_that(index).is_less_than(len(result.candidates))
    candidate = result.candidates[index]
    assert_that(candidate.cluster_id.value).is_equal_to(cluster_id)
    assert_that(candidate.score).is_close_to(score, 0.01)


@then(
    parsers.parse('the cluster assignment for mention "{mention_id}" is "{cluster_id}"')
)
def check_cluster_assignment(mention_id: str, cluster_id: str, algorithm_context):
    """Verify the cluster assignment from state."""
    service = algorithm_context["service"]
    state = service.state()

    # Find which cluster this mention is assigned to
    found_cluster = None
    for cid, mention_list in state.cluster_membership.items():
        for m in mention_list:
            if m.value == mention_id:
                found_cluster = cid.value
                break

    assert_that(found_cluster).is_equal_to(cluster_id)
