"""Unit tests for RedisQueueWorker entrypoint (mocked Redis and service)."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from erspec.models.core import EntityMention, EntityMentionIdentifier
from erspec.models.ere import (
    EntityMentionResolutionRequest,
    EntityMentionResolutionResponse,
    EREErrorResponse,
)
from linkml_runtime.dumpers import JSONDumper

from ere.entrypoints.queue_worker import RedisQueueWorker

_dumper = JSONDumper()


def _make_request(request_id: str = "qw-test-001") -> EntityMentionResolutionRequest:
    return EntityMentionResolutionRequest(
        entity_mention=EntityMention(
            identifiedBy=EntityMentionIdentifier(
                request_id=request_id,
                source_id="qw-src",
                entity_type="http://test.org/Org",
            ),
            content_type="text/turtle",
            content="<>",
        ),
        ere_request_id=request_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _make_response(request_id: str = "qw-test-001") -> EntityMentionResolutionResponse:
    return EntityMentionResolutionResponse(
        entity_mention_id=EntityMentionIdentifier(
            request_id=request_id,
            source_id="qw-src",
            entity_type="http://test.org/Org",
        ),
        candidates=[],
        ere_request_id=request_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@pytest.fixture
def mock_redis():
    return MagicMock()


@pytest.fixture
def mock_service():
    return MagicMock()


@pytest.fixture
def worker(mock_redis, mock_service) -> RedisQueueWorker:
    return RedisQueueWorker(
        redis_client=mock_redis,
        entity_resolution_service=mock_service,
        request_queue="ere_requests",
        response_queue="ere_responses",
        queue_timeout=1,
    )


def test_process_single_message_returns_false_on_timeout(worker, mock_redis):
    mock_redis.brpop.return_value = None

    result = worker.process_single_message()

    assert result is False


def test_process_single_message_returns_true_on_success(
    worker, mock_redis, mock_service
):
    request = _make_request("qw-happy")
    raw_msg = _dumper.dumps(request).encode("utf-8")
    mock_redis.brpop.return_value = ("ere_requests", raw_msg)
    mock_service.process_batch.return_value = [_make_response("qw-happy")]

    result = worker.process_single_message()

    assert result is True
    mock_service.process_batch.assert_called_once()
    mock_redis.pipeline.return_value.lpush.assert_called_once()
    mock_redis.pipeline.return_value.execute.assert_called_once()


def test_process_single_message_sends_error_response_on_parse_failure(
    worker, mock_redis, mock_service
):
    mock_redis.brpop.return_value = ("ere_requests", b"not valid json at all")

    result = worker.process_single_message()

    assert result is True
    pipeline = mock_redis.pipeline.return_value
    pipeline.lpush.assert_called_once()
    pushed_payload = pipeline.lpush.call_args[0][1]
    pushed_json = json.loads(pushed_payload)
    assert pushed_json.get("error_type") == "ProcessingError"


def test_send_responses_logs_error_on_redis_failure(worker, mock_redis):
    mock_redis.pipeline.return_value.execute.side_effect = ConnectionError("redis down")
    response = EREErrorResponse(
        ere_request_id="err-resp",
        error_type="TestError",
        error_title="Test",
        error_detail="detail",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    worker._send_responses([response])  # must not raise


def test_build_error_response_returns_ere_error_response():
    response = RedisQueueWorker._build_error_response("something broke", "req-err")

    assert isinstance(response, EREErrorResponse)
    assert response.ere_request_id == "req-err"
    assert response.error_type == "ProcessingError"
    assert "something broke" in response.error_detail
