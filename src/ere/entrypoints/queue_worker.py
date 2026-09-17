"""Redis queue entrypoint driver for entity resolution requests."""

import json
import logging
from datetime import datetime, timezone
from erspec.models.ere import EREErrorResponse, EREResponse
from linkml_runtime.dumpers import JSONDumper

from ere.adapters.redis_request_queue import RedisRequestQueue
from ere.adapters.utils import get_request_from_message
from ere.services.bite_sizer import BiteSizer
from ere.services.entity_resolution_service import EntityResolutionService
from ere.services.resolver_config import BatchSettings
from ere.services.tracing import (
    MS_PER_SECOND,
    BatchTrace,
    MentionTrace,
    TraceEvent,
    TraceField,
)

log = logging.getLogger(__name__)

REQUEST_ID_KEY = "ere_request_id"
TIMESTAMP_KEY = "timestamp"
UNKNOWN_REQUEST_ID = "unknown"
PROCESSING_ERROR_TYPE = "ProcessingError"
PROCESSING_ERROR_TITLE = "Request processing error"


class RedisQueueWorker:
    """Entrypoint: Process entity resolution requests from Redis queue.

    Acts as a driver between Redis infrastructure and the service layer.
    Dependency injection enables testing with mock Redis and services.
    """

    def __init__(  # pylint: disable=too-many-positional-arguments,too-many-arguments  # redis, service, queue config and bite settings; no natural grouping
        self,
        redis_client,
        entity_resolution_service: EntityResolutionService,
        request_queue: str = "ere_requests",
        response_queue: str = "ere_responses",
        queue_timeout: int = 1,
        batch_settings: BatchSettings | None = None,
    ):
        """Initialize worker with dependencies."""
        self.service = entity_resolution_service
        batch_settings = batch_settings or BatchSettings()
        self._queue = RedisRequestQueue(
            redis_client,
            request_queue,
            response_queue,
            timeout_seconds=queue_timeout,
            max_bytes=batch_settings.max_bytes,
            linger_ms=batch_settings.linger_ms,
        )
        self._sizer = BiteSizer(batch_settings)
        self._dumper = JSONDumper()

    def process_single_message(self) -> bool:
        """Process at most one request (a bite of one). Returns True if a request was processed."""
        return self._process(self._queue.take(1), bite_limit=1) > 0

    def process_bite(self) -> int:
        """
        Take a bite sized by recent processing speed, resolve it, and send all responses before returning.

        Returns:
            Number of requests processed (0 on queue timeout).
        """
        limit = self._sizer.limit()
        return self._process(self._queue.take(limit), bite_limit=limit)

    def _process(self, bite: list[bytes], bite_limit: int) -> int:
        if not bite:
            return 0
        batch_trace = BatchTrace(
            log, bite_limit, len(bite), sum(len(raw) for raw in bite)
        )
        traces = [self._dequeued_trace(batch_trace, raw) for raw in bite]
        responses: list[EREResponse | None] = [None] * len(bite)
        parsed = []
        for position, raw in enumerate(bite):
            try:
                parsed.append((position, get_request_from_message(raw)))
            except Exception as e:  # pylint: disable=broad-exception-caught
                # The exception text can quote the payload, so only its type is logged.
                request_id = traces[position].request_id
                log.error(
                    "Failed to parse request %s: %s", request_id, type(e).__name__
                )
                responses[position] = self._build_error_response(str(e), request_id)
        batch_trace.mark(TraceField.PARSE_MS)
        self._resolve(parsed, traces, responses)
        batch_trace.mark(TraceField.RESOLVE_MS)
        self._send_responses(responses)
        batch_trace.mark(TraceField.RESPOND_MS)
        for trace in traces:
            trace.stage(TraceEvent.RESPONDED)
            trace.summary()
        self._sizer.observe(len(bite), batch_trace.summary())
        return len(bite)

    def _dequeued_trace(self, batch_trace: BatchTrace, raw: bytes) -> MentionTrace:
        request_id, created_at = self._peek_identity(raw)
        trace = batch_trace.request_trace(request_id)
        trace.stage(
            TraceEvent.DEQUEUED,
            **{
                TraceField.PAYLOAD_BYTES: len(raw),
                TraceField.QUEUE_WAIT_MS: self._queue_wait_ms(created_at),
            },
        )
        return trace

    def _resolve(
        self, parsed: list, traces: list[MentionTrace], responses: list
    ) -> None:
        if not parsed:
            return
        try:
            results = self.service.process_batch(
                [request for _, request in parsed],
                [traces[position] for position, _ in parsed],
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.error("Failed to process bite: %s", type(e).__name__)
            results = [
                self._build_error_response(str(e), traces[position].request_id)
                for position, _ in parsed
            ]
        for (position, _), response in zip(parsed, results):
            responses[position] = response

    @staticmethod
    def _peek_identity(raw_msg: bytes) -> tuple[str, datetime | None]:
        """Read the request id and creation time from the raw message, tolerating malformed input."""
        try:
            msg_json = json.loads(raw_msg)
        except ValueError:
            return UNKNOWN_REQUEST_ID, None
        if not isinstance(msg_json, dict):
            return UNKNOWN_REQUEST_ID, None
        request_id = msg_json.get(REQUEST_ID_KEY)
        if not isinstance(request_id, str):
            request_id = UNKNOWN_REQUEST_ID
        try:
            created_at = datetime.fromisoformat(msg_json[TIMESTAMP_KEY])
        except (KeyError, TypeError, ValueError):
            created_at = None
        return request_id, created_at

    @staticmethod
    def _queue_wait_ms(created_at: datetime | None) -> float | None:
        if created_at is None:
            return None
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        waited_ms = (
            datetime.now(timezone.utc) - created_at
        ).total_seconds() * MS_PER_SECOND
        return round(
            max(waited_ms, 0.0), 3
        )  # a producer clock ahead of ours must not give a negative wait

    def _send_responses(self, responses: list[EREResponse]) -> None:
        """Serialise all responses of a bite and push them in request order, in one pipeline."""
        payloads = []
        for response in responses:
            try:
                payloads.append(self._dumper.dumps(response))
            except Exception as e:  # pylint: disable=broad-exception-caught  # one bad response must not lose the others
                log.error("Failed to serialise a response: %s", type(e).__name__)
        try:
            self._queue.push_responses(payloads)
            log.debug("Sent %d responses", len(payloads))
        except Exception:  # pylint: disable=broad-exception-caught
            log.exception("Failed to send %d responses", len(payloads))

    @staticmethod
    def _build_error_response(
        error_detail: str, ere_request_id: str = UNKNOWN_REQUEST_ID
    ) -> EREErrorResponse:
        """Build error response for request processing failures."""
        return EREErrorResponse(
            ere_request_id=ere_request_id,
            error_type=PROCESSING_ERROR_TYPE,
            error_title=PROCESSING_ERROR_TITLE,
            error_detail=error_detail,
            timestamp=datetime.now(timezone.utc),
        )
