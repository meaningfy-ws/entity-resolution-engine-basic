"""
Tests the :class:`RedisResolutionService` and :class:`RedisEREClient` with the mock resolver.
"""

import logging
from typing import Generator

import pytest
import redis
from assertpy import assert_that
from ere_test import (
    EPD_NS,
    ORG_NS,
    MockResolver,
    catch_response,
    create_timestamp,
    prefix_common_namespaces,
)
from testcontainers.redis import RedisContainer

from ere.adapters.redis import AbstractClient
from ere.adapters.redis import RedisEREClient
from erspec.models.ere import (
    EntityMentionResolutionRequest,
    EntityMentionResolutionResponse,
    EREErrorResponse,
)
from erspec.models.core import (
    ClusterReference,
    EntityMention,
    EntityMentionIdentifier,
)
from ere.services.redis import RedisResolutionService

log = logging.getLogger(__name__)


@pytest.mark.integration
def test_known_entity_resolution(mock_ere_client: AbstractClient):
    """
    Scenario: A resolution request returns existing cluster candidate references
    """
    log.info("test_known_entity_resolution: starting")
    test_entity_uri = (
        f"{EPD_NS}id_2023-S-210-661238_ReviewerOrganisation_LLhJHMi9mby8ixbkfyGoWj"
    )

    expected_cluster = ClusterReference(
        cluster_id=f"{EPD_NS}id_2023-S-210-662860_ReviewerOrganisation_LLhJHMi9mby8ixbkfyGoWj_Cluster",
        confidence_score=0.98,
        similarity_score=0.98,
    )
    expected_alt_cluster = ClusterReference(
        cluster_id=f"{EPD_NS}id_2023-S-210-661238_ReviewerOrganisation_LLhJHMi9mby8ixbkfyGoWj_alt_Cluster",
        confidence_score=0.80,
        similarity_score=0.80,
    )

    test_entity_mention = EntityMention(
        identifiedBy=EntityMentionIdentifier(
            request_id=test_entity_uri,
            source_id="test-module",
            entity_type=f"{ORG_NS}Organization",
        ),
        # Not important here, the mock resolver just looks up static test data
        # TODO: validation of ID/content match
        content_type="text/turtle",
        content="<foo>",
    )
    test_req = EntityMentionResolutionRequest(
        entity_mention=test_entity_mention,
        ere_request_id="test-known-entity-resolution-001",
        timestamp=create_timestamp(),
    )

    mock_ere_client.push_request(test_req)
    entity_resolution = catch_response(
        mock_ere_client, test_req.ere_request_id, EntityMentionResolutionResponse
    )

    assert_that(
        entity_resolution.entity_mention_id,
        "Resolution response has the source entity mention ID",
    ).is_equal_to(test_entity_mention.identifier)

    candidate_clusters = entity_resolution.candidates

    assert_that(
        candidate_clusters, "Resolution response has the expected candidate clusters"
    ).contains(expected_cluster, expected_alt_cluster)


@pytest.mark.integration
def test_ere_replies_with_error_response_to_malformed_request(
    mock_ere_client: AbstractClient,
):
    """
    Scenario: The ERE replies with an error response to a malformed request
    """
    # Send a malformed request (content type is unsupported)
    malformed_request = EntityMentionResolutionRequest(
        ere_request_id="test-bad-resolution-req-001",
        entity_mention=EntityMention(
            identifiedBy=EntityMentionIdentifier(
                request_id="", source_id="test-module", entity_type="FooType"
            ),  # Malformed part
            content_type="text/turtle",
            content="<foo>",
        ),
        timestamp=create_timestamp(),
    )

    mock_ere_client.push_request(malformed_request)
    error_response = catch_response(
        mock_ere_client, malformed_request.ere_request_id, EREErrorResponse
    )

    assert_that(
        error_response.error_title, "The response has the expected error title"
    ).contains("MockResolver, unsupported entity type")
    assert_that(
        error_response.error_detail, "The response has the expected error detail"
    ).contains("MockResolver, unsupported entity type")
    assert_that(error_response.error_type, "The response has an error type").is_equal_to(
        "ValueError"
    )


@pytest.fixture(autouse=True)
def create_mock_service(redisdb_client: redis.Redis) -> Generator[None, None, None]:
    """
    As in similar cases, the service fixture isn't directly used by the tests, in fact,
    here the client uses Redis networking.

    """

    log.info("Creating mock_service")
    mock_service = RedisResolutionService(
        resolver=MockResolver(), config_or_client=redisdb_client
    )
    mock_service.async_timeout = 1.0  # make tests faster
    mock_service.start()  # Starts in the background

    log.info("mock_service started, handing control to tests")

    try:
        yield
    finally:
        mock_service.stop()


@pytest.fixture
def mock_ere_client(redisdb_client: redis.Redis) -> AbstractClient:
    return RedisEREClient(config_or_client=redisdb_client)


@pytest.fixture
def redisdb_client() -> Generator[redis.Redis, None, None]:
    """
    Provides a Redis client through Test Containers.
    """
    with RedisContainer() as redis_container:
        yield redis_container.get_client()
