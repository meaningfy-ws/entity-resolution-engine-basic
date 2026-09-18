"""Unit tests: taking bites of requests from Redis and pushing responses (adapter).

Spec: batched-resolution — "Bites are sized by processing capacity, not by arrival".
"""

import time
from unittest.mock import MagicMock

import redis

from ere.adapters.redis_request_queue import RedisRequestQueue

QUEUE = "ere_requests"
RESPONSES = "ere_responses"
DEFAULT_MAX_BYTES = 50_000_000
DEFAULT_LINGER_MS = 250


def _queue(
    client, max_bytes: int = DEFAULT_MAX_BYTES, linger_ms: int = DEFAULT_LINGER_MS
) -> RedisRequestQueue:
    return RedisRequestQueue(
        client,
        QUEUE,
        RESPONSES,
        timeout_seconds=1,
        max_bytes=max_bytes,
        linger_ms=linger_ms,
    )


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


def test_timeout_gives_an_empty_bite():
    client = MagicMock()
    client.brpop.return_value = None

    assert _queue(client).take(10) == []


def test_limit_one_never_multi_pops():
    client = MagicMock()
    client.brpop.return_value = (QUEUE.encode(), b"m1")

    assert _queue(client).take(1) == [b"m1"]
    client.lmpop.assert_not_called()
    client.rpop.assert_not_called()


def test_falls_back_to_rpop_count_when_lmpop_is_unsupported():
    client = MagicMock()
    client.brpop.return_value = (QUEUE.encode(), b"m1")
    client.lmpop.side_effect = redis.ResponseError("unknown command 'LMPOP'")
    client.rpop.side_effect = [[b"m2", b"m3"], None]

    bite = _queue(client, linger_ms=0).take(3)

    assert bite == [b"m1", b"m2", b"m3"]


def test_request_over_the_byte_cap_is_pushed_back_to_the_front():
    client = MagicMock()
    client.brpop.return_value = (QUEUE.encode(), b"x" * 60)
    client.lmpop.return_value = [QUEUE.encode(), [b"y" * 60]]

    bite = _queue(client, max_bytes=100, linger_ms=0).take(5)

    assert bite == [b"x" * 60]
    client.rpush.assert_called_once_with(QUEUE, b"y" * 60)


def test_single_request_larger_than_the_cap_forms_a_bite_of_one():
    client = MagicMock()
    client.brpop.return_value = (QUEUE.encode(), b"x" * 500)

    assert _queue(client, max_bytes=100, linger_ms=0).take(5) == [b"x" * 500]
    client.lmpop.assert_not_called()


def test_linger_does_not_overshoot_a_short_linger():
    client = MagicMock()
    client.brpop.return_value = (QUEUE.encode(), b"m1")
    client.lmpop.return_value = None

    started = time.perf_counter()
    _queue(client, linger_ms=5).take(10)

    assert (time.perf_counter() - started) * 1000 < 30


def test_requests_over_the_byte_cap_are_taken_next_in_their_original_order():
    queue = _ListQueue(b"a" * 40, b"b" * 40, b"c" * 40, b"d" * 40)
    adapter = _queue(queue, max_bytes=100, linger_ms=0)

    first = adapter.take(10)
    second = adapter.take(10)

    assert first == [b"a" * 40, b"b" * 40]
    assert second == [b"c" * 40, b"d" * 40]


def test_bite_that_reaches_the_byte_cap_exactly_takes_every_request():
    queue = _ListQueue(*(bytes([97 + index]) * 25 for index in range(4)))

    assert len(_queue(queue, max_bytes=100, linger_ms=0).take(10)) == 4


def test_linger_waits_at_least_the_configured_time_when_the_queue_drains():
    client = MagicMock()
    client.brpop.return_value = (QUEUE.encode(), b"m1")
    client.lmpop.return_value = None

    started = time.perf_counter()
    _queue(client, linger_ms=60).take(10)

    assert (time.perf_counter() - started) * 1000 >= 60


def test_responses_are_pushed_in_order_in_one_pipeline():
    client = MagicMock()

    _queue(client).push_responses([b"r1", b"r2", b"r3"])

    client.pipeline.assert_called_once_with(transaction=False)
    pushed = [call.args for call in client.pipeline.return_value.lpush.call_args_list]
    assert pushed == [(RESPONSES, b"r1"), (RESPONSES, b"r2"), (RESPONSES, b"r3")]
    client.pipeline.return_value.execute.assert_called_once()
