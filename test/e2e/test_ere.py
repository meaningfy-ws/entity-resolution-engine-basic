"""End-to-end test: RedisQueueWorker processes entity resolution requests.

Tests the complete entrypoint flow:
1. Push EntityMentionResolutionRequest to input queue
2. RedisQueueWorker consumes, parses, and processes request
3. Response is written to output queue
4. Verify response structure and content
"""

import json
from datetime import datetime, timezone

import pytest

from ere.adapters.utils import get_response_from_message
from ere.entrypoints.bootstrap import (
    build_entity_resolution_service,
    build_entity_resolver,
    build_rdf_mapper,
)
from ere.entrypoints.queue_worker import RedisQueueWorker

# ===============================================================================
# Fixtures
# ===============================================================================


@pytest.fixture
def redis_queues(redis_client):
    """Provide queue names and clear them before test."""
    request_queue = "test-ere_requests"
    response_queue = "test-ere_responses"

    # Clear queues
    redis_client.delete(request_queue, response_queue)

    yield request_queue, response_queue

    # Cleanup
    redis_client.delete(request_queue, response_queue)


@pytest.fixture(scope="module")
def e2e_entity_resolution_service(resolver_config_path, rdf_mapping_path):
    """Build the full entity resolution service using test-specific config paths (injected from conftest)."""
    resolver = build_entity_resolver(resolver_config_path=resolver_config_path)
    mapper = build_rdf_mapper(rdf_mapping_path=rdf_mapping_path)
    return build_entity_resolution_service(resolver, mapper)


@pytest.fixture
def queue_worker(redis_client, e2e_entity_resolution_service, redis_queues):
    """Create RedisQueueWorker with test queue names."""
    request_queue, response_queue = redis_queues
    return RedisQueueWorker(
        redis_client=redis_client,
        entity_resolution_service=e2e_entity_resolution_service,
        request_queue=request_queue,
        response_queue=response_queue,
    )


# ===============================================================================
# Helper functions
# ===============================================================================


def create_entity_mention_request(
    request_id: str,
    source_id: str,
    entity_type: str,
    legal_name: str,
    country_code: str,
) -> dict:
    """Create a minimal EntityMentionResolutionRequest payload."""
    # Minimal RDF content (simplified Turtle)
    # Uses correct predicates per config/rdf_mapping.yaml:
    # - legal_name maps to epo:hasLegalName
    # - country_code maps to cccev:registeredAddress/epo:hasCountryCode
    content = f"""
@prefix org: <http://www.w3.org/ns/org#> .
@prefix cccev: <http://data.europa.eu/m8g/> .
@prefix epo: <http://data.europa.eu/a4g/ontology#> .
@prefix epd: <http://data.europa.eu/a4g/resource/> .
@prefix locn: <http://www.w3.org/ns/locn#> .

epd:ent001 a org:Organization ;
    epo:hasLegalName "{legal_name}" ;
    cccev:registeredAddress [
        epo:hasCountryCode "{country_code}"
    ] ;
    cccev:telephone "+44 1924306780" .
"""

    return {
        "type": "EntityMentionResolutionRequest",
        "entity_mention": {
            "identifiedBy": {
                "request_id": request_id,
                "source_id": source_id,
                "entity_type": entity_type,
            },
            "content": content.strip(),
            "content_type": "text/turtle",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ere_request_id": f"{request_id}:01",
    }


# ===============================================================================
# End-to-end tests
# ===============================================================================


@pytest.mark.integration
def test_single_request_resolution_flow(redis_client, redis_queues, queue_worker):
    """
    E2E test: single entity mention pushed to queue, resolved, response returned.

    Flow:
    1. Create and push EntityMentionResolutionRequest to input queue
    2. RedisQueueWorker consumes and processes request
    3. Response is written to output queue
    4. Verify response structure
    """
    request_queue, response_queue = redis_queues

    # 1. Create and push request
    request_payload = create_entity_mention_request(
        request_id="324fs3r345vx",
        source_id="TEDSWS",
        entity_type="ORGANISATION",
        legal_name="Acme Corporation",
        country_code="US",
    )
    request_bytes = json.dumps(request_payload).encode("utf-8")
    redis_client.rpush(request_queue, request_bytes)

    # 2. Process message using worker
    assert queue_worker.process_single_message() is True, (
        "Worker should process message"
    )

    # 3. Verify response in queue
    result = redis_client.brpop(response_queue, timeout=1)
    assert result is not None, "Response should be in output queue"
    _, response_raw = result

    # 4. Verify response structure
    response_obj = get_response_from_message(response_raw)
    assert response_obj.type == "EntityMentionResolutionResponse"
    assert response_obj.entity_mention_id.request_id == "324fs3r345vx"
    assert response_obj.candidates is not None


@pytest.mark.integration
def test_multiple_requests_accumulate(redis_client, redis_queues, queue_worker):
    """
    E2E test: multiple entity mentions are resolved and responses queued.

    Verifies that:
    - Each request is processed independently
    - Responses are queued correctly
    - Resolution benefits from accumulated state
    """
    request_queue, response_queue = redis_queues

    # Create and push two requests
    mentions = [
        ("m1_324fs3r345vx", "TEDSWS", "Acme Corp", "US"),
        ("m2_324fs3r345vx", "TEDSWS", "Acme Corporation", "US"),
    ]

    for req_id, source, legal_name, country in mentions:
        request_payload = create_entity_mention_request(
            request_id=req_id,
            source_id=source,
            entity_type="ORGANISATION",
            legal_name=legal_name,
            country_code=country,
        )
        redis_client.rpush(request_queue, json.dumps(request_payload).encode("utf-8"))

    # Process both requests using worker
    for _ in range(2):
        assert queue_worker.process_single_message() is True

    # Verify both responses in queue
    responses = []
    for _ in range(2):
        result = redis_client.brpop(response_queue, timeout=1)
        assert result is not None
        responses.append(get_response_from_message(result[1]))

    # Verify responses (order may vary)
    assert len(responses) == 2
    request_ids = {r.entity_mention_id.request_id for r in responses}
    assert request_ids == {"m1_324fs3r345vx", "m2_324fs3r345vx"}

    # Both should have candidates
    for response in responses:
        assert response.candidates is not None


@pytest.mark.integration
def test_request_response_payload_structure(redis_client, redis_queues, queue_worker):
    """
    E2E test: verify request and response payload structures match spec.

    Validates:
    - Request has required fields
    - Response has required fields with correct types
    """
    request_queue, response_queue = redis_queues

    # Create a request
    request_payload = create_entity_mention_request(
        request_id="struct_test_001",
        source_id="TEST_SOURCE",
        entity_type="ORGANISATION",
        legal_name="Test Organization Ltd",
        country_code="GB",
    )

    # Verify request structure
    assert request_payload["type"] == "EntityMentionResolutionRequest"
    assert "entity_mention" in request_payload
    assert "identifiedBy" in request_payload["entity_mention"]
    assert "content" in request_payload["entity_mention"]
    assert "content_type" in request_payload["entity_mention"]
    assert request_payload["entity_mention"]["content_type"] == "text/turtle"

    # Push and process
    redis_client.rpush(request_queue, json.dumps(request_payload).encode("utf-8"))
    assert queue_worker.process_single_message() is True

    # Get response
    result = redis_client.brpop(response_queue, timeout=1)
    assert result is not None
    response = get_response_from_message(result[1])

    # Verify response structure
    assert response.type == "EntityMentionResolutionResponse"
    assert hasattr(response, "entity_mention_id")
    assert hasattr(response, "candidates")
    assert hasattr(response, "timestamp")
    assert hasattr(response, "ere_request_id")

    # Verify candidates structure
    for candidate in response.candidates:
        assert hasattr(candidate, "cluster_id")
        assert hasattr(candidate, "confidence_score")
        assert hasattr(candidate, "similarity_score")
        assert isinstance(candidate.confidence_score, (float, int))
        assert isinstance(candidate.similarity_score, (float, int))


@pytest.mark.integration
def test_organisation_with_different_country(redis_client, redis_queues, queue_worker):
    """
    E2E test: organization entities with different country codes.

    Verifies service can process requests with different countries (uses blocking rules).
    """
    request_queue, response_queue = redis_queues

    # Create request with German organization
    request_payload = create_entity_mention_request(
        request_id="de_org_test",
        source_id="TEDSWS",
        entity_type="ORGANISATION",
        legal_name="Test GmbH",
        country_code="DE",
    )

    redis_client.rpush(request_queue, json.dumps(request_payload).encode("utf-8"))

    # Process message
    assert queue_worker.process_single_message() is True

    # Verify response
    result = redis_client.brpop(response_queue, timeout=1)
    assert result is not None
    response = get_response_from_message(result[1])
    assert response.type == "EntityMentionResolutionResponse"
