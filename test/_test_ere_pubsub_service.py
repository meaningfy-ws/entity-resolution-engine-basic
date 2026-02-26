"""
Tests the generic working logic in :class:`AbstractPubSubResolutionService`,

by means of mock implementations that use 'channels' based on in-memory queues.
"""

import asyncio
import logging
import queue
from collections.abc import Generator

import pytest
from assertpy import assert_that
from ere_test import EPD_NS, ORG_NS, MockResolver, catch_response, create_timestamp

from ere.adapters.redis import AbstractClient
from erspec.models.ere import (
    EntityMentionResolutionRequest,
    EntityMentionResolutionResponse,
    ERERequest,
    EREResponse,
    EREErrorResponse,
)
from erspec.models.core import (
    ClusterReference,
    EntityMention,
    EntityMentionIdentifier,
)
from ere.services import AbstractPubSubResolutionService

log = logging.getLogger(__name__)


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
    entity_resolution: EntityMentionResolutionResponse = catch_response(
        mock_ere_client, test_req.ere_request_id, EntityMentionResolutionResponse
    )

    assert_that(
        entity_resolution.entity_mention_id,
        "Resolution response has the source entity mention ID",
    ).is_equal_to(test_entity_mention.identifier)


@pytest.fixture
def mock_ere_client() -> AbstractClient:
    return FooPubSubClient()


@pytest.fixture(autouse=True)
def create_mock_service():
    """
    The service fixture isn't directly used by the tests, for they interact with the client fixture
    through network communication, or mechanisms that emulate it (like in-memory queues used hereby).

    """
    log.info("Creating mock_service")
    mock_service = FooPubSubResolutionService()
    mock_service.async_timeout = 1.0  # make tests faster

    mock_service.start()  # Starts in the background

    log.info("mock_service started, handing control to tests")

    try:
        yield
    finally:
        mock_service.stop()


# The "channels" used by the mock service/client to emulate the interaction in a real service
# implemented with Redis queues, or similar.
#
_request_queue = queue.Queue()
_response_queue = queue.Queue()


class FooPubSubResolutionService(AbstractPubSubResolutionService):
    """
    A mock PubSubResolutionService that uses in-memory queues to emulate a real
    message queue service.
    """

    def __init__(self):
        super().__init__(resolver=MockResolver())

    async def _pull_request(self) -> ERERequest | None:
        def guarded_get() -> ERERequest | None:
            """
            Pulls a request from the request 'channel', enforcing a timeout and managing
            exceptions like timeout, empty queue, etc.
            """
            try:
                return _request_queue.get(timeout=self.async_timeout / 2)
            except (queue.Empty, queue.ShutDown):
                return None

        log.debug("Service: pulling request from queue")
        # Needs to go in a thread, in order to not block the event loop in waiting
        request = await asyncio.to_thread(guarded_get)
        id = request.ere_request_id if request else "None"
        log.debug(f"Service: got a request from queue, id: {id}")
        return request

    def _push_response(self, response: EREResponse):
        log.debug(f"Service: pushing response to queue, id: {response.ere_request_id}")
        _response_queue.put_nowait(response)
        log.debug(f"Service: pushed response to queue, id: {response.ere_request_id}")


class FooPubSubClient(AbstractClient):
    """
    The counterpart of :class:`FooPubSubResolutionService`

    Uses the in-memory queues to emulate a client interacting with an ERE service through
    a message queue service.
    """

    def push_request(self, request: ERERequest):
        log.debug(f"Client: pushing request to queue, id: {request.ere_request_id}")
        _request_queue.put_nowait(request)
        log.debug(f"Client: pushed request to queue, id: {request.ere_request_id}")

    def subscribe_responses(self) -> Generator[EREResponse, None, None]:
        while True:
            log.debug("Client: waiting for response from queue")
            response = _response_queue.get()
            log.debug(f"Client: got a response from queue, id: {response.ere_request_id}")
            yield response
