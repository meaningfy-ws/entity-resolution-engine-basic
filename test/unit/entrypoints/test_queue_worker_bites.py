"""Unit tests: taking bites from Redis (entrypoints).

Spec: batched-resolution — "Bites are sized by processing capacity, not by arrival".
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from erspec.models.ere import EREErrorResponse

from ere.entrypoints.queue_worker import UNKNOWN_REQUEST_ID, RedisQueueWorker
from ere.services.resolver_config import BatchSettings

QUEUE = "ere_requests"


def _worker(client, **settings) -> RedisQueueWorker:
    return RedisQueueWorker(
        redis_client=client,
        entity_resolution_service=MagicMock(),
        request_queue=QUEUE,
        batch_settings=BatchSettings(**settings),
    )


# ---------------------------------------------------------------------------
# Adversarial review fixes (B1, N1, N2, N3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("request_id", [None, 42, {"nested": "id"}])
def test_non_string_request_id_is_answered_with_unknown_id(request_id):
    client = MagicMock()
    raw = json.dumps(
        {"ere_request_id": request_id, "type": "EntityMentionResolutionRequest"}
    ).encode()
    client.brpop.return_value = (QUEUE.encode(), raw)

    processed = _worker(client, linger_ms=0).process_single_message()

    assert processed is True
    pushed = json.loads(client.pipeline.return_value.lpush.call_args[0][1])
    assert pushed["ere_request_id"] == UNKNOWN_REQUEST_ID


@pytest.mark.parametrize(
    "created_at",
    [
        datetime(2999, 1, 1, tzinfo=timezone.utc),
        datetime.now(timezone.utc) + timedelta(minutes=5),
    ],
)
def test_timestamp_in_the_future_gives_no_negative_queue_wait(created_at):
    assert RedisQueueWorker._queue_wait_ms(created_at) == 0  # pylint: disable=protected-access


def test_one_unserialisable_response_does_not_lose_the_others():
    client = MagicMock()
    worker = _worker(client)
    good = EREErrorResponse(
        ere_request_id="ok",
        error_type="X",
        error_title="t",
        error_detail="d",
        timestamp=datetime.now(timezone.utc),
    )

    worker._send_responses([good, object()])  # pylint: disable=protected-access  # must not raise

    client.pipeline.return_value.lpush.assert_called_once()
    client.pipeline.return_value.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Test review must-adds (take_bite ordering and caps, whole-bite handling)
# ---------------------------------------------------------------------------


class _ListQueue:
    """Real Redis list semantics for LPUSH producers / right-end consumers, as redis-py returns them."""

    def __init__(self, *oldest_first: bytes):
        self.items = list(reversed(oldest_first))  # index 0 = left end (newest)
        self.pushed_pipelines = []

    def brpop(self, name, timeout=0):  # pylint: disable=unused-argument
        return (name.encode(), self.items.pop()) if self.items else None

    def lmpop(self, num_keys, *names, direction, count=1):  # pylint: disable=unused-argument
        taken = [self.items.pop() for _ in range(min(count, len(self.items)))]
        return [names[0].encode(), taken] if taken else None

    def rpush(self, name, *values):  # pylint: disable=unused-argument
        self.items.extend(values)


def test_bite_of_only_unparsable_requests_answers_each_without_calling_the_service():
    queue = _ListQueue(b"not json 1", b"not json 2", b"not json 3")
    service = MagicMock()
    worker = RedisQueueWorker(
        redis_client=queue,
        entity_resolution_service=service,
        request_queue=QUEUE,
        batch_settings=BatchSettings(linger_ms=0),
    )
    queue.pipeline = MagicMock()

    processed = worker.process_bite()

    assert processed == 3
    service.process_batch.assert_not_called()
    pushed = [
        json.loads(call.args[1])
        for call in queue.pipeline.return_value.lpush.call_args_list
    ]
    assert [response["error_type"] for response in pushed] == ["ProcessingError"] * 3


def test_all_responses_of_a_bite_are_pushed_before_the_next_bite_is_taken():
    calls: list[str] = []
    queue = _ListQueue(b"x1", b"x2", b"x3")
    original_brpop = queue.brpop
    queue.brpop = lambda *a, **k: calls.append("brpop") or original_brpop(*a, **k)
    queue.pipeline = MagicMock()
    queue.pipeline.return_value.execute.side_effect = lambda: calls.append("push")
    worker = RedisQueueWorker(
        redis_client=queue,
        entity_resolution_service=MagicMock(),
        request_queue=QUEUE,
        batch_settings=BatchSettings(linger_ms=0),
    )

    worker.process_bite()
    worker.process_bite()

    assert calls == ["brpop", "push", "brpop"]


def test_failed_response_push_is_logged(caplog):
    client = MagicMock()
    client.pipeline.return_value.execute.side_effect = ConnectionError("redis down")
    response = EREErrorResponse(
        ere_request_id="r",
        error_type="X",
        error_title="t",
        error_detail="d",
        timestamp=datetime.now(timezone.utc),
    )

    _worker(client)._send_responses([response])  # pylint: disable=protected-access

    assert any("Failed to send" in record.getMessage() for record in caplog.records)
