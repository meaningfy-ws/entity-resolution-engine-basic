"""Mention lifecycle tracing: structured, payload-free log events with per-stage timings."""

import logging
from enum import StrEnum
from time import perf_counter
from uuid import uuid4

STAGE_MS_SUFFIX = "_ms"
MS_PER_SECOND = 1000


class TraceEvent(StrEnum):
    """Lifecycle stages of one resolution request, in order, plus the per-request summary."""

    DEQUEUED = "dequeued"
    PARSED = "parsed"
    GUARD = "guard"
    SCORED = "scored"
    CLUSTERED = "clustered"
    PERSISTED = "persisted"
    RESPONDED = "responded"
    SUMMARY = "summary"
    BATCH = "batch"


class TraceField(StrEnum):
    """LogRecord attribute names carried by trace events (set through `extra`)."""

    EVENT = "ere_event"
    REQUEST_ID = "ere_request_id"
    MENTION_ID = "mention_id"
    PAYLOAD_BYTES = "payload_bytes"
    QUEUE_WAIT_MS = "queue_wait_ms"
    GUARD = "guard"
    LINKS = "links"
    BEST_SCORE = "best_score"
    DECISION = "decision"
    CLUSTER_ID = "cluster_id"
    STAGE_MS = "stage_ms"
    TOTAL_MS = "total_ms"
    BATCH_ID = "batch_id"
    BATCH_SIZE = "batch_size"
    BITE_LIMIT = "bite_limit"
    PARSE_MS = "parse_ms"
    RESOLVE_MS = "resolve_ms"
    RESPOND_MS = "respond_ms"


class GuardOutcome(StrEnum):
    """What the conflict/idempotency guard decided."""

    CONFLICT = "conflict"
    IDEMPOTENT = "idempotent"
    NEW = "new"


class ClusterDecision(StrEnum):
    """Whether a new mention joined an existing cluster or founded a new one."""

    JOIN = "join"
    NEW = "new"


def _elapsed_ms(since: float, now: float) -> float:
    return round((now - since) * MS_PER_SECOND, 3)


class MentionTrace:
    """
    Trace of one request: each `stage()` logs a DEBUG event with the time since the previous stage;
    `summary()` logs one INFO line with every field and stage duration. Only ids, sizes, scores and
    timings are recorded — never request content or mention attributes.
    """

    def __init__(
        self,
        logger: logging.Logger,
        request_id: str | None,
        enabled: bool = True,
        context: dict[str, object] | None = None,
    ):
        self._log = logger
        self._enabled = enabled
        self._started = self._last = perf_counter()
        self._fields: dict[str, object] = {
            TraceField.REQUEST_ID.value: request_id,
            **(context or {}),
        }
        self._stage_durations: dict[str, float] = {}

    @property
    def request_id(self) -> str | None:
        """Request id this trace belongs to."""
        return self._fields[TraceField.REQUEST_ID.value]

    @classmethod
    def disabled(cls) -> "MentionTrace":
        """A trace that records nothing, for callers outside the queue worker."""
        return cls(logging.getLogger(__name__), request_id=None, enabled=False)

    def stage(
        self, event: TraceEvent, at: float | None = None, **fields: object
    ) -> None:
        """Close the current stage (ending now, or at a recorded `perf_counter` time): record its duration and fields, log at DEBUG."""
        if not self._enabled:
            return
        now = perf_counter() if at is None else at
        stage_ms = _elapsed_ms(self._last, now)
        self._last = now
        self._fields.update(
            {str(key): value for key, value in fields.items() if value is not None}
        )
        self._stage_durations[f"{event.value}{STAGE_MS_SUFFIX}"] = stage_ms
        if self._log.isEnabledFor(logging.DEBUG):
            self._log.debug(
                "ere.%s %s",
                event.value,
                self._describe(),
                extra={
                    TraceField.EVENT.value: event,
                    TraceField.STAGE_MS.value: stage_ms,
                    **self._fields,
                },
            )

    def summary(self) -> None:
        """Log the single INFO summary line of the request."""
        if not self._enabled:
            return
        total_ms = _elapsed_ms(self._started, perf_counter())
        self._log.info(
            "ere.%s %s total_ms=%s %s",
            TraceEvent.SUMMARY.value,
            self._describe(),
            total_ms,
            " ".join(f"{name}={ms}" for name, ms in self._stage_durations.items()),
            extra={
                TraceField.EVENT.value: TraceEvent.SUMMARY,
                TraceField.TOTAL_MS.value: total_ms,
                **self._stage_durations,
                **self._fields,
            },
        )

    def _describe(self) -> str:
        return " ".join(f"{key}={value}" for key, value in self._fields.items())


class DeferredStage:
    """A stage whose end time is recorded now but which is emitted later (e.g. only after a commit)."""

    def __init__(self, trace: MentionTrace, event: TraceEvent, fields: dict):
        self._trace = trace
        self._event = event
        self._fields = {str(key): value for key, value in fields.items()}
        self._at = perf_counter()

    def emit(self) -> None:
        self._trace.stage(self._event, at=self._at, **self._fields)


class BatchTrace:
    """Trace of one bite: per-request traces sharing the bite context, and one INFO `batch` event."""

    def __init__(
        self,
        logger: logging.Logger,
        bite_limit: int,
        batch_size: int,
        payload_bytes: int,
    ):
        self._log = logger
        self._started = perf_counter()
        self._context: dict[str, object] = {
            TraceField.BATCH_ID.value: uuid4().hex,
            TraceField.BATCH_SIZE.value: batch_size,
        }
        self._bite_limit = bite_limit
        self._payload_bytes = payload_bytes
        self._last = self._started
        self._stage_ms: dict[str, float] = {}

    def mark(self, stage: TraceField) -> None:
        """Close a bite stage (`PARSE_MS`, `RESOLVE_MS`, `RESPOND_MS`) with the time since the previous one."""
        now = perf_counter()
        self._stage_ms[stage.value] = _elapsed_ms(self._last, now)
        self._last = now

    def request_trace(self, request_id: str) -> MentionTrace:
        """A request trace carrying this bite's id and size."""
        return MentionTrace(self._log, request_id, context=self._context)

    def summary(self) -> float:
        """Log the bite's INFO `batch` event; returns the bite's elapsed seconds."""
        seconds = perf_counter() - self._started
        total_ms = round(seconds * MS_PER_SECOND, 3)
        self._log.info(
            "ere.%s %s total_ms=%s bite_limit=%s payload_bytes=%s %s",
            TraceEvent.BATCH.value,
            " ".join(f"{key}={value}" for key, value in self._context.items()),
            total_ms,
            self._bite_limit,
            self._payload_bytes,
            " ".join(f"{name}={ms}" for name, ms in self._stage_ms.items()),
            extra={
                TraceField.EVENT.value: TraceEvent.BATCH,
                TraceField.TOTAL_MS.value: total_ms,
                TraceField.BITE_LIMIT.value: self._bite_limit,
                TraceField.PAYLOAD_BYTES.value: self._payload_bytes,
                **self._stage_ms,
                **self._context,
            },
        )
        return seconds
