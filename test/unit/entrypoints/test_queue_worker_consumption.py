"""Unit tests: the worker consumes one request at a time (regression guard, passes today).

Spec: resolution-resource-bounds — "Single-threaded consumption at processing rate".
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from erspec.models.core import EntityMention, EntityMentionIdentifier
from erspec.models.ere import (
    EntityMentionResolutionRequest,
    EntityMentionResolutionResponse,
)
from linkml_runtime.dumpers import JSONDumper

from ere.entrypoints.queue_worker import RedisQueueWorker

_dumper = JSONDumper()
QUEUE_READ_METHODS = (
    "rpop",
    "lpop",
    "lrange",
    "lmove",
    "blmove",
    "brpoplpush",
    "rpoplpush",
)


def _raw_request(request_id: str) -> bytes:
    request = EntityMentionResolutionRequest(
        entity_mention=EntityMention(
            identifiedBy=EntityMentionIdentifier(
                request_id=request_id,
                source_id="src",
                entity_type="http://test.org/Org",
            ),
            content_type="text/turtle",
            content="<>",
        ),
        ere_request_id=request_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    return _dumper.dumps(request).encode("utf-8")


def test_one_call_takes_exactly_one_request_from_the_queue():
    redis_client = MagicMock()
    redis_client.brpop.return_value = ("ere_requests", _raw_request("r1"))
    service = MagicMock()
    service.process_batch.return_value = [
        EntityMentionResolutionResponse(
            entity_mention_id=EntityMentionIdentifier(
                request_id="r1", source_id="src", entity_type="http://test.org/Org"
            ),
            candidates=[],
            ere_request_id="r1",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    ]
    worker = RedisQueueWorker(
        redis_client=redis_client, entity_resolution_service=service
    )

    worker.process_single_message()

    redis_client.brpop.assert_called_once()
    for method in QUEUE_READ_METHODS:
        getattr(redis_client, method).assert_not_called()


def test_response_is_sent_before_the_next_request_is_taken():
    calls: list[str] = []
    redis_client = MagicMock()
    redis_client.brpop.side_effect = lambda *a, **k: (
        calls.append("brpop") or ("q", _raw_request("r1"))
    )
    redis_client.pipeline.return_value.execute.side_effect = lambda *a, **k: (
        calls.append("lpush")
    )
    service = MagicMock()
    service.process_batch.return_value = [
        EntityMentionResolutionResponse(
            entity_mention_id=EntityMentionIdentifier(
                request_id="r1", source_id="src", entity_type="http://test.org/Org"
            ),
            candidates=[],
            ere_request_id="r1",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    ]
    worker = RedisQueueWorker(
        redis_client=redis_client, entity_resolution_service=service
    )

    worker.process_single_message()
    worker.process_single_message()

    assert calls == ["brpop", "lpush", "brpop", "lpush"]
