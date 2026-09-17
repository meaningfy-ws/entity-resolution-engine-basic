"""Redis request and response queues: taking bites of waiting requests and pushing responses (adapter)."""

import logging
from time import monotonic, sleep

import redis

log = logging.getLogger(__name__)

POP_DIRECTION = "RIGHT"  # producers LPUSH, so the oldest request is at the right end
LINGER_POLL_SECONDS = 0.05
MS_PER_SECOND = 1000


class RedisRequestQueue:
    """Takes requests in bites without prefetching, and pushes responses in one pipeline."""

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments  # queue names and bite limits; no natural grouping
        self,
        redis_client,
        request_queue: str,
        response_queue: str,
        timeout_seconds: int,
        max_bytes: int,
        linger_ms: int,
    ):
        self._client = redis_client
        self._request_queue = request_queue
        self._response_queue = response_queue
        self._timeout_seconds = timeout_seconds
        self._max_bytes = max_bytes
        self._linger_ms = linger_ms
        self._multi_pop_supported = True

    def take(self, limit: int) -> list[bytes]:
        """
        Take up to `limit` requests: wait for the first, then only those already waiting.

        Stops at `max_bytes` (a request that would exceed it goes back to the front of the queue);
        when the queue drains, waits up to `linger_ms` for more.
        """
        first = self._client.brpop(self._request_queue, timeout=self._timeout_seconds)
        if not first:
            return []
        bite = [first[1]]
        bite_bytes = len(first[1])
        deadline = monotonic() + self._linger_ms / MS_PER_SECOND
        while len(bite) < limit and bite_bytes < self._max_bytes:
            waiting = self._pop_waiting(limit - len(bite))
            if not waiting:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                sleep(min(LINGER_POLL_SECONDS, remaining))
                continue
            taken = self._within_byte_cap(waiting, self._max_bytes - bite_bytes)
            bite.extend(taken)
            bite_bytes += sum(len(raw) for raw in taken)
            if len(taken) < len(waiting):
                break
        return bite

    def push_responses(self, payloads: list[bytes | str]) -> None:
        """Push serialised responses in order, in one pipeline round-trip."""
        pipeline = self._client.pipeline(transaction=False)
        for payload in payloads:
            pipeline.lpush(self._response_queue, payload)
        pipeline.execute()

    def _within_byte_cap(
        self, waiting: list[bytes], remaining_bytes: int
    ) -> list[bytes]:
        """Requests fitting in the remaining bytes; the rest are pushed back so they are taken next, in order."""
        taken, used = [], 0
        for position, raw in enumerate(waiting):
            if used + len(raw) > remaining_bytes:
                self._client.rpush(self._request_queue, *reversed(waiting[position:]))
                break
            taken.append(raw)
            used += len(raw)
        return taken

    def _pop_waiting(self, count: int) -> list[bytes]:
        """Pop up to `count` waiting requests without blocking (LMPOP, or RPOP with count on Redis < 7)."""
        if self._multi_pop_supported:
            try:
                popped = self._client.lmpop(
                    1, self._request_queue, direction=POP_DIRECTION, count=count
                )
                return list(popped[1]) if popped else []
            except redis.ResponseError:
                self._multi_pop_supported = False
                log.debug(
                    "LMPOP not supported; taking waiting requests with RPOP count"
                )
        return list(self._client.rpop(self._request_queue, count) or [])
