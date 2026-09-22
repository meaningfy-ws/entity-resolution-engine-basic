"""Step definitions for mention_lifecycle_tracing.feature."""

import logging
from datetime import timedelta

from assertpy import assert_that
from erspec.models.core import EntityMention
from pytest_bdd import given, parsers, scenarios, then, when
from test.features.steps.conftest import (
    REQUEST_QUEUE,
    RESPONSE_QUEUE,
    SECRET_ATTRIBUTE_PREFIX,
    SECRET_RDF_PREFIX,
    now_utc,
    raw_request,
)
from test.unit.adapters.stubs import (
    FixedSimilarityLinker,
    InMemoryClusterRepository,
    InMemoryMentionRepository,
    InMemorySimilarityRepository,
)

from ere.entrypoints.queue_worker import RedisQueueWorker
from ere.models.ports.rdf_mapper import RDFMapper
from ere.models.resolver import Mention, MentionId
from ere.services.entity_resolution_service import (
    EntityResolutionService,
    EntityResolver,
)
from ere.services.resolver_config import ResolverConfig
from ere.services.tracing import TraceEvent, TraceField
from ere.utils.logging import TRACE_LEVEL_NUM

scenarios("../mention_lifecycle_tracing.feature")

NON_STAGE_EVENTS = (None, TraceEvent.SUMMARY, TraceEvent.BATCH)
LOG_LEVELS = {"TRACE": TRACE_LEVEL_NUM, "DEBUG": logging.DEBUG, "INFO": logging.INFO}


class RequestIdMapper(RDFMapper):
    """Maps a request to a mention named after its request id, with secret-marked attribute values."""

    def map_entity_mention_to_domain(self, entity_mention: EntityMention) -> Mention:
        request_id = entity_mention.identifiedBy.request_id
        return Mention(
            id=MentionId(value=request_id),
            attributes={
                "legal_name": f"{SECRET_ATTRIBUTE_PREFIX}{entity_mention.content.removeprefix(SECRET_RDF_PREFIX)}",
                "country_code": "DEU",
            },
        )


def _events(records, name):
    return [
        r
        for r in records
        if getattr(r, TraceField.EVENT, None) == TraceEvent[name.upper()]
    ]


def _process(
    worker: RedisQueueWorker,
    fake_redis,
    caplog,
    request_id: str,
    level: str,
    raw: bytes,
):
    fake_redis.lpush(REQUEST_QUEUE, raw)
    caplog.clear()
    caplog.set_level(LOG_LEVELS[level])
    worker.process_single_message()
    return {
        "request_id": request_id,
        "raw": raw,
        "records": list(caplog.records),
        "text": caplog.text,
    }


@given("an ERE worker backed by a resolution service", target_fixture="ere_worker")
def ere_worker(fake_redis, similarity_map):
    config = ResolverConfig(
        threshold=0.8,
        match_weight_threshold=-10,
        top_n=100,
        entity_fields=["legal_name", "country_code"],
        auto_train_threshold=0,
    )
    resolver = EntityResolver(
        mention_repo=InMemoryMentionRepository(),
        similarity_repo=InMemorySimilarityRepository(),
        cluster_repo=InMemoryClusterRepository(),
        linker=FixedSimilarityLinker(similarity_map=similarity_map),
        config=config,
    )
    return RedisQueueWorker(
        redis_client=fake_redis,
        entity_resolution_service=EntityResolutionService(resolver, RequestIdMapper()),
        request_queue=REQUEST_QUEUE,
        response_queue=RESPONSE_QUEUE,
    )


@given(parsers.parse('request "{request_id}" was already processed'))
def request_already_processed(request_id: str, ere_worker, fake_redis, caplog):
    _process(
        ere_worker,
        fake_redis,
        caplog,
        request_id,
        "INFO",
        raw_request(request_id, now_utc()),
    )


@when(
    parsers.parse('request "{request_id}" is processed at log level "{level}"'),
    target_fixture="processed",
)
def process_request(request_id: str, level: str, ere_worker, fake_redis, caplog):
    return _process(
        ere_worker,
        fake_redis,
        caplog,
        request_id,
        level,
        raw_request(request_id, now_utc()),
    )


@when(
    parsers.parse(
        'request "{request_id}" with different content is processed at log level "{level}"'
    ),
    target_fixture="processed",
)
def process_changed_request(
    request_id: str, level: str, ere_worker, fake_redis, caplog
):
    changed = raw_request(
        request_id, now_utc(), content=f"{SECRET_RDF_PREFIX}{request_id}-changed"
    )
    return _process(ere_worker, fake_redis, caplog, request_id, level, changed)


@when(
    parsers.parse(
        'request "{request_id}" created {seconds:d} seconds ago is processed at log level "{level}"'
    ),
    target_fixture="processed",
)
def process_aged_request(
    request_id: str, seconds: int, level: str, ere_worker, fake_redis, caplog
):
    created_at = now_utc() - timedelta(seconds=seconds)
    return _process(
        ere_worker,
        fake_redis,
        caplog,
        request_id,
        level,
        raw_request(request_id, created_at),
    )


@when(
    parsers.parse(
        'request "{request_id}" without timestamp is processed at log level "{level}"'
    ),
    target_fixture="processed",
)
def process_untimed_request(
    request_id: str, level: str, ere_worker, fake_redis, caplog
):
    return _process(
        ere_worker, fake_redis, caplog, request_id, level, raw_request(request_id)
    )


@then("no log line contains the RDF content or attribute values")
def no_payload_in_logs(processed):
    assert_that(processed["text"]).does_not_contain(
        SECRET_RDF_PREFIX, SECRET_ATTRIBUTE_PREFIX
    )
    for record in processed["records"]:
        assert_that(record.getMessage()).does_not_contain(
            SECRET_RDF_PREFIX, SECRET_ATTRIBUTE_PREFIX
        )


@then(parsers.parse('the log carries request id "{request_id}" and its payload size'))
def log_carries_id_and_size(request_id: str, processed):
    carrying = [
        r
        for r in processed["records"]
        if getattr(r, TraceField.REQUEST_ID, None) == request_id
        and getattr(r, TraceField.PAYLOAD_BYTES, None) == len(processed["raw"])
    ]
    assert_that(carrying).is_not_empty()


@then(parsers.parse('the lifecycle events are "{event_list}" in that order'))
def lifecycle_events_in_order(event_list: str, processed):
    expected = [name.strip() for name in event_list.split(",")]
    logged = [
        getattr(r, TraceField.EVENT)
        for r in processed["records"]
        if getattr(r, TraceField.EVENT, None) not in NON_STAGE_EVENTS
    ]
    assert_that([str(event) for event in logged]).is_equal_to(expected)


@then(
    parsers.parse(
        'every lifecycle event carries request id "{request_id}" and a stage duration'
    )
)
def events_carry_id_and_duration(request_id: str, processed):
    stage_records = [
        r
        for r in processed["records"]
        if getattr(r, TraceField.EVENT, None) not in NON_STAGE_EVENTS
    ]
    assert_that(stage_records).is_not_empty()
    for record in stage_records:
        assert_that(getattr(record, TraceField.REQUEST_ID)).is_equal_to(request_id)
        assert_that(getattr(record, TraceField.STAGE_MS)).is_greater_than_or_equal_to(0)


@then(parsers.parse('the guard event reports "{outcome}"'))
def guard_reports(outcome: str, processed):
    guard_events = _events(processed["records"], "guard")
    assert_that(guard_events).is_length(1)
    assert_that(str(getattr(guard_events[0], TraceField.GUARD))).is_equal_to(outcome)


@then(parsers.parse('no "{event}" event is logged'))
def no_event_logged(event: str, processed):
    assert_that(_events(processed["records"], event)).is_empty()


@then(
    parsers.parse("the scored event reports {links:d} links with best score {score:f}")
)
def scored_reports(links: int, score: float, processed):
    scored = _events(processed["records"], "scored")
    assert_that(scored).is_length(1)
    assert_that(getattr(scored[0], TraceField.LINKS)).is_equal_to(links)
    assert_that(getattr(scored[0], TraceField.BEST_SCORE)).is_close_to(score, 0.001)


@then(
    parsers.parse(
        'the clustered event reports decision "{decision}" into cluster "{cluster_id}"'
    )
)
def clustered_reports(decision: str, cluster_id: str, processed):
    clustered = _events(processed["records"], "clustered")
    assert_that(clustered).is_length(1)
    assert_that(str(getattr(clustered[0], TraceField.DECISION))).is_equal_to(decision)
    assert_that(getattr(clustered[0], TraceField.CLUSTER_ID)).is_equal_to(cluster_id)


@then(parsers.parse('exactly one summary line is logged for request "{request_id}"'))
def one_summary_line(request_id: str, processed):
    summaries = [
        r
        for r in _events(processed["records"], "summary")
        if getattr(r, TraceField.REQUEST_ID, None) == request_id
        and r.levelno == logging.INFO
    ]
    assert_that(summaries).is_length(1)


@then(
    parsers.parse(
        'the summary for request "{request_id}" reports mention "{mention_id}", guard "{guard}", decision "{decision}", '
        "its payload size and every stage duration"
    )
)
def summary_content(
    request_id: str, mention_id: str, guard: str, decision: str, processed
):
    (summary,) = [
        r
        for r in _events(processed["records"], "summary")
        if getattr(r, TraceField.REQUEST_ID) == request_id
    ]
    assert_that(getattr(summary, TraceField.MENTION_ID)).is_equal_to(mention_id)
    assert_that(str(getattr(summary, TraceField.GUARD))).is_equal_to(guard)
    assert_that(str(getattr(summary, TraceField.DECISION))).is_equal_to(decision)
    assert_that(getattr(summary, TraceField.PAYLOAD_BYTES)).is_equal_to(
        len(processed["raw"])
    )
    for stage in (
        "dequeued",
        "parsed",
        "guard",
        "scored",
        "clustered",
        "persisted",
        "responded",
    ):
        assert_that(getattr(summary, f"{stage}_ms")).described_as(
            stage
        ).is_greater_than_or_equal_to(0)
    assert_that(getattr(summary, TraceField.TOTAL_MS)).is_greater_than_or_equal_to(0)


@then("no per-stage lifecycle events are logged")
def no_stage_events(processed):
    stage_events = [
        r
        for r in processed["records"]
        if getattr(r, TraceField.EVENT, None) not in NON_STAGE_EVENTS
    ]
    assert_that(stage_events).is_empty()


@then(
    parsers.parse(
        "the dequeued event and the summary report a queue wait between {low:d} and {high:d} ms"
    )
)
def queue_wait_reported(low: int, high: int, processed):
    reporting = _events(processed["records"], "dequeued") + _events(
        processed["records"], "summary"
    )
    assert_that(reporting).is_length(2)
    for record in reporting:
        assert_that(getattr(record, TraceField.QUEUE_WAIT_MS)).is_between(low, high)


@then("no event reports a queue wait")
def no_queue_wait(processed):
    for record in processed["records"]:
        assert_that(getattr(record, TraceField.QUEUE_WAIT_MS, None)).is_none()


@given(
    parsers.parse(
        'requests "{first}", "{second}" and "{third}" are waiting in the request queue'
    )
)
def three_waiting(first: str, second: str, third: str, fake_redis):
    for request_id in (first, second, third):
        fake_redis.lpush(REQUEST_QUEUE, raw_request(request_id, now_utc()))


@when(
    parsers.parse('one bite is processed at log level "{level}"'),
    target_fixture="processed",
)
def process_bite_at_level(level: str, ere_worker, caplog):
    caplog.clear()
    caplog.set_level(LOG_LEVELS[level])
    ere_worker.process_bite()
    return {"records": list(caplog.records), "text": caplog.text}


@then(
    parsers.parse("{count:d} summary lines share one batch id with batch size {size:d}")
)
def summaries_share_batch(count: int, size: int, processed):
    summaries = _events(processed["records"], "summary")
    assert_that(summaries).is_length(count)
    assert_that({getattr(r, TraceField.BATCH_ID) for r in summaries}).is_length(1)
    assert_that({getattr(r, TraceField.BATCH_SIZE) for r in summaries}).is_equal_to(
        {size}
    )


@then(
    parsers.parse(
        "exactly one batch event reports batch size {size:d} with its stage timings"
    )
)
def one_batch_event(size: int, processed):
    batches = _events(processed["records"], "batch")
    assert_that(batches).is_length(1)
    assert_that(getattr(batches[0], TraceField.BATCH_SIZE)).is_equal_to(size)
    for field in (
        TraceField.PARSE_MS,
        TraceField.RESOLVE_MS,
        TraceField.RESPOND_MS,
        TraceField.TOTAL_MS,
    ):
        assert_that(getattr(batches[0], field)).described_as(
            field
        ).is_greater_than_or_equal_to(0)
    stage_sum = sum(
        getattr(batches[0], field)
        for field in (TraceField.PARSE_MS, TraceField.RESOLVE_MS, TraceField.RESPOND_MS)
    )
    assert_that(stage_sum).is_less_than_or_equal_to(
        getattr(batches[0], TraceField.TOTAL_MS) + 0.01
    )
