"""
Step definitions for entity resolution BDD features.

These steps wire pytest-bdd scenarios to the ERE service and client implementations.
"""

import pytest
from assertpy import assert_that
from pytest_bdd import given, when, then, parsers

from erspec.models.ere import (
    EntityMentionResolutionRequest,
    EntityMentionResolutionResponse,
    EREErrorResponse,
)
from erspec.models.core import (
    EntityMention,
    EntityMentionIdentifier,
)
from ere_test import MockEREClient, ORG_NS, create_timestamp


@pytest.fixture
def ere_client():
    """Provides a fresh MockEREClient for each scenario."""
    return MockEREClient()


@pytest.fixture
def resolution_context():
    """Shared context for a scenario."""
    return {"client": None, "last_request": None, "last_response": None}


@given("an ERE client is connected")
def step_client_connected(ere_client, resolution_context):
    """Initialize the ERE client."""
    resolution_context["client"] = ere_client
    assert_that(ere_client).is_not_none()


@given("the entity knowledge base is loaded")
def step_knowledge_base_loaded(resolution_context):
    """
    Verify that the knowledge base (test data) is loaded.

    In the mock setup, this happens automatically during MockEREClient initialization.
    """
    client = resolution_context["client"]
    assert_that(client).is_not_none()
    # The MockResolver has loaded test data in its __init__
    assert_that(client._resolver._member_index).is_not_empty()


@when(parsers.parse('I submit a resolution request for entity "{entity_id}"'))
def step_submit_known_entity_request(entity_id, resolution_context):
    """Submit a resolution request for a known entity."""
    client = resolution_context["client"]

    # Construct request using test data conventions
    entity_mention = EntityMention(
        identifier=EntityMentionIdentifier(
            requestId=entity_id,
            sourceId="bdd-test",
            entityType=f"{ORG_NS}Organization",
        ),
        contentType="text/turtle",
        content="<test-content>",
    )

    request = EntityMentionResolutionRequest(
        entityMention=entity_mention,
        ere_request_id=f"bdd-test-{entity_id}",
        timestamp=create_timestamp(),
    )

    resolution_context["last_request"] = request
    client.push_request(request)


@when("I submit a resolution request for an unknown entity")
def step_submit_unknown_entity_request(resolution_context):
    """Submit a resolution request for an entity not in the knowledge base."""
    client = resolution_context["client"]

    unknown_entity_id = "http://data.europa.eu/a4g/resource/unknown_entity_9999"

    entity_mention = EntityMention(
        identifier=EntityMentionIdentifier(
            requestId=unknown_entity_id,
            sourceId="bdd-test",
            entityType=f"{ORG_NS}Organization",
        ),
        contentType="text/turtle",
        content="<test-content>",
    )

    request = EntityMentionResolutionRequest(
        entityMention=entity_mention,
        ere_request_id="bdd-test-unknown-entity",
        timestamp=create_timestamp(),
    )

    resolution_context["last_request"] = request
    client.push_request(request)


@when("I submit a malformed resolution request")
def step_submit_malformed_request(resolution_context):
    """Submit a request with invalid data (unsupported entity type)."""
    client = resolution_context["client"]

    # Use an unsupported entity type to trigger an error
    entity_mention = EntityMention(
        identifier=EntityMentionIdentifier(
            requestId="http://example.com/test-entity",
            sourceId="bdd-test",
            entityType="http://example.com/UnsupportedType",  # Not in SUPPORTED_ENTITY_TYPES
        ),
        contentType="text/turtle",
        content="<test-content>",
    )

    request = EntityMentionResolutionRequest(
        entityMention=entity_mention,
        ere_request_id="bdd-test-malformed",
        timestamp=create_timestamp(),
    )

    resolution_context["last_request"] = request
    client.push_request(request)


@then("I receive a resolution response")
def step_receive_resolution_response(resolution_context):
    """Verify that a response was received."""
    client = resolution_context["client"]
    request_id = resolution_context["last_request"].ere_request_id

    # Collect responses until we find the one for our request
    response = None
    for resp in client.subscribe_responses():
        if resp.ere_request_id == request_id:
            response = resp
            break

    assert_that(response).is_not_none()
    resolution_context["last_response"] = response


@then("the response contains at least one cluster candidate")
def step_response_has_cluster_candidates(resolution_context):
    """Verify that the response includes cluster candidates."""
    response = resolution_context["last_response"]

    assert_that(response).is_instance_of(EntityMentionResolutionResponse)
    assert_that(response.candidates).is_not_none()
    assert_that(response.candidates).is_not_empty()
    assert_that(len(response.candidates)).is_greater_than_or_equal_to(1)


@then("the response contains a new singleton cluster")
def step_response_has_singleton_cluster(resolution_context):
    """Verify that a new singleton cluster was created for the unknown entity."""
    response = resolution_context["last_response"]

    assert_that(response).is_instance_of(EntityMentionResolutionResponse)
    assert_that(response.candidates).is_not_none()
    assert_that(response.candidates).is_not_empty()

    # A singleton cluster should have exactly one candidate
    # (the newly created cluster for the unknown entity)
    assert_that(len(response.candidates)).is_equal_to(1)
    assert_that(response.candidates[0].confidence_score).is_equal_to(1.0)


@then("I receive an error response")
def step_receive_error_response(resolution_context):
    """Verify that an error response was received."""
    client = resolution_context["client"]
    request_id = resolution_context["last_request"].ere_request_id

    # Collect responses until we find the one for our request
    response = None
    for resp in client.subscribe_responses():
        if resp.ere_request_id == request_id:
            response = resp
            break

    assert_that(response).is_not_none()
    assert_that(response).is_instance_of(EREErrorResponse)
    assert_that(response.error_title).is_not_none()
    assert_that(response.error_detail).is_not_none()

    resolution_context["last_response"] = response
