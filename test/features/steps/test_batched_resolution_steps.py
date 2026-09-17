"""Step definitions for batched_resolution.feature."""

import contextlib
import json
import random
import time
from pathlib import Path

import duckdb
import pytest
import yaml
from assertpy import assert_that
from erspec.models.core import EntityMention, EntityMentionIdentifier
from erspec.models.ere import EntityMentionResolutionRequest
from pytest_bdd import given, parsers, scenarios, then, when
from test.features.steps.conftest import (
    REQUEST_QUEUE,
    RESPONSE_QUEUE,
    load_organisation_mentions,
    raw_request,
)
from test.unit.adapters.stubs import (
    FixedSimilarityLinker,
    InMemoryClusterRepository,
    InMemoryMentionRepository,
    InMemorySimilarityRepository,
)

from ere.adapters.duckdb_repositories import (
    DuckDBClusterRepository,
    DuckDBMentionRepository,
    DuckDBSimilarityRepository,
)
from ere.adapters.duckdb_schema import init_schema
from ere.adapters.duckdb_unit_of_work import DuckDBUnitOfWork
from ere.adapters.redis_request_queue import RedisRequestQueue
from ere.entrypoints.bootstrap import build_entity_resolver, resolve_batch_settings
from ere.entrypoints.queue_worker import RedisQueueWorker
from ere.models.ports.rdf_mapper import RDFMapper
from ere.models.resolver import Mention, MentionId
from ere.services.bite_sizer import BiteSizer
from ere.services.entity_resolution_service import (
    EntityResolutionService,
    EntityResolver,
)
from ere.services.resolver_config import BatchSettings, ResolverConfig

scenarios("../batched_resolution.feature")

BYTES_PER_MB = 1_000_000
MS_PER_SECOND = 1000
SHIPPED_CONFIG = Path(__file__).parents[3] / "src" / "config" / "resolver.yaml"
SIZER_OBSERVATIONS = 10
SEQUENCE_SEED = 7
SEQUENCE_PAIRS_PER_MENTION = 3
NAME_PREFIX = "name="
ENTITY_FIELDS = ["legal_name", "country_code"]
RESPONSE_ERROR_TYPE = "error_type"
RESPONSE_CANDIDATES = "candidates"


def _config(threshold: float = 0.8) -> ResolverConfig:
    return ResolverConfig(
        threshold=threshold,
        match_weight_threshold=-10,
        top_n=100,
        entity_fields=ENTITY_FIELDS,
        auto_train_threshold=0,
    )


def _mention(mention_id: str, name: str | None = None) -> Mention:
    return Mention(
        id=MentionId(value=mention_id),
        attributes={
            "legal_name": name or f"Company {mention_id}",
            "country_code": "DEU",
        },
    )


def _stub_resolver(similarity_map, threshold: float = 0.8) -> EntityResolver:
    return EntityResolver(
        mention_repo=InMemoryMentionRepository(),
        similarity_repo=InMemorySimilarityRepository(),
        cluster_repo=InMemoryClusterRepository(),
        linker=FixedSimilarityLinker(similarity_map=similarity_map),
        config=_config(threshold),
    )


# ---------------------------------------------------------------------------
# Bite taking
# ---------------------------------------------------------------------------


@pytest.fixture
def batch_overrides() -> dict:
    return {}


@given(
    parsers.parse(
        "{count:d} resolution requests of {megabytes:d} MB each are waiting in the request queue"
    )
)
def large_requests(count: int, megabytes: int, fake_redis):
    content = "x" * (megabytes * BYTES_PER_MB)
    for index in range(count):
        fake_redis.lpush(REQUEST_QUEUE, raw_request(f"large-{index}", content=content))


@given(parsers.parse("the bite byte cap is {megabytes:d} MB"))
def byte_cap(megabytes: int, batch_overrides: dict):
    batch_overrides["max_bytes"] = megabytes * BYTES_PER_MB


@given(parsers.parse("the bite linger is {milliseconds:d} ms"))
def linger(milliseconds: int, batch_overrides: dict):
    batch_overrides["linger_ms"] = milliseconds


@when(parsers.parse("the ERE takes a bite with limit {limit:d}"), target_fixture="bite")
def take_bite(limit: int, fake_redis, batch_overrides: dict):
    settings = BatchSettings(**batch_overrides)
    queue = RedisRequestQueue(
        fake_redis,
        REQUEST_QUEUE,
        RESPONSE_QUEUE,
        timeout_seconds=1,
        max_bytes=settings.max_bytes,
        linger_ms=settings.linger_ms,
    )
    started = time.perf_counter()
    taken = queue.take(limit)
    return {
        "requests": taken,
        "elapsed_ms": (time.perf_counter() - started) * MS_PER_SECOND,
    }


@then(parsers.parse("the bite holds {count:d} requests"))
def bite_holds(count: int, bite: dict):
    assert_that(bite["requests"]).is_length(count)


@then(parsers.parse("the bite holds at most {count:d} requests"))
def bite_holds_at_most(count: int, bite: dict):
    # 2 MB requests plus JSON framing: exactly the requests that fit under the cap
    assert_that(len(bite["requests"])).is_equal_to(count - 1)
    assert_that(sum(len(raw) for raw in bite["requests"])).is_less_than_or_equal_to(
        50 * BYTES_PER_MB
    )


@then("the requests not taken remain in the request queue")
def not_taken_remain(bite: dict, fake_redis):
    assert_that(fake_redis.llen(REQUEST_QUEUE) + len(bite["requests"])).is_equal_to(30)


@then(parsers.parse("taking the bite took less than {milliseconds:d} ms"))
def bite_duration(milliseconds: int, bite: dict):
    assert_that(bite["elapsed_ms"]).is_less_than(milliseconds)


@given(
    parsers.parse(
        "a bite sizer with target {target:d} seconds and at most {maximum:d} mentions"
    ),
    target_fixture="sizer",
)
def bite_sizer(target: int, maximum: int) -> BiteSizer:
    return BiteSizer(BatchSettings(target_seconds=target, max_mentions=maximum))


@when(
    parsers.parse(
        "bites of {mentions:d} mentions were each processed in {seconds:d} seconds"
    )
)
def observe_bites(mentions: int, seconds: int, sizer: BiteSizer):
    for _ in range(SIZER_OBSERVATIONS):
        sizer.observe(mentions, seconds)


@then(parsers.parse("the next bite limit is {limit:d}"))
def next_limit(limit: int, sizer: BiteSizer):
    assert_that(sizer.limit()).is_equal_to(limit)


@given(
    parsers.parse('the environment variable "{name}" is "{value}"'),
    target_fixture="batch_env",
)
def batch_env(name: str, value: str) -> dict:
    return {name: value}


@when("the batch settings are resolved", target_fixture="settings_error")
def resolve_settings(batch_env: dict) -> str:
    with pytest.raises(ValueError) as error:
        resolve_batch_settings(batch_env)
    return str(error.value)


@then(parsers.parse('resolving fails with an error naming "{name}"'))
def resolving_fails(name: str, settings_error: str):
    assert_that(settings_error).contains(name)


# ---------------------------------------------------------------------------
# Intra-bite matching
# ---------------------------------------------------------------------------


@given(
    parsers.parse(
        'a resolver with threshold {threshold:f} whose similarity between "{left}" and "{right}" is {score:f}'
    ),
    target_fixture="bite_resolver",
)
def resolver_with_similarity(
    threshold: float, left: str, right: str, score: float, similarity_map
):
    similarity_map[frozenset([left, right])] = score
    return _stub_resolver(similarity_map, threshold)


@when(
    parsers.parse('mentions "{first}" and "{second}" are resolved in one bite'),
    target_fixture="bite_results",
)
def resolve_in_one_bite(first: str, second: str, bite_resolver: EntityResolver):
    results = bite_resolver.resolve_batch([_mention(first), _mention(second)])
    return dict(zip((first, second), results))


@then(
    parsers.parse('the candidates of "{mention_id}" include the cluster of "{other}"')
)
def candidates_include(
    mention_id: str, other: str, bite_results: dict, bite_resolver: EntityResolver
):
    cluster = bite_resolver._cluster_repo.find_cluster_of(MentionId(value=other))  # pylint: disable=protected-access
    assert_that([c.cluster_id for c in bite_results[mention_id].candidates]).contains(
        cluster
    )


@then(parsers.parse('mention "{mention_id}" is in the cluster of "{other}"'))
def in_cluster_of(mention_id: str, other: str, bite_resolver: EntityResolver):
    clusters = bite_resolver._cluster_repo  # pylint: disable=protected-access
    assert_that(clusters.find_cluster_of(MentionId(value=mention_id))).is_equal_to(
        clusters.find_cluster_of(MentionId(value=other))
    )


@given(
    parsers.parse("a sequence of {count:d} mentions with random pairwise similarities"),
    target_fixture="sequence",
)
def random_sequence(count: int) -> dict:
    generator = random.Random(SEQUENCE_SEED)
    ids = [f"m{index}" for index in range(count)]
    similarities = {}
    for index, mention_id in enumerate(ids[1:], start=1):
        for other in generator.sample(
            ids[:index], min(index, SEQUENCE_PAIRS_PER_MENTION)
        ):
            similarities[frozenset([mention_id, other])] = round(generator.random(), 6)
    return {"ids": ids, "similarities": similarities}


@when(
    parsers.parse(
        "the sequence is resolved once in bites of {bite_size:d} and once one at a time"
    ),
    target_fixture="two_runs",
)
def resolve_sequence_both_ways(bite_size: int, sequence: dict) -> dict:
    mentions = [_mention(mention_id) for mention_id in sequence["ids"]]
    single = _stub_resolver(dict(sequence["similarities"]))
    for mention in mentions:
        single.resolve(mention)
    batched = _stub_resolver(dict(sequence["similarities"]))
    for start in range(0, len(mentions), bite_size):
        batched.resolve_batch(mentions[start : start + bite_size])
    return {"single": single, "batched": batched, "ids": sequence["ids"]}


@then("every mention is in the same cluster in both runs")
def same_clusters(two_runs: dict):
    for mention_id in two_runs["ids"]:
        key = MentionId(value=mention_id)
        assert_that(
            two_runs["batched"]._cluster_repo.find_cluster_of(key)
        ).described_as(mention_id).is_equal_to(  # pylint: disable=protected-access
            two_runs["single"]._cluster_repo.find_cluster_of(key)  # pylint: disable=protected-access
        )


# ---------------------------------------------------------------------------
# Mixed outcomes through the worker
# ---------------------------------------------------------------------------


class ContentNameMapper(RDFMapper):
    """Maps a request to a mention named after its request id; the content carries the legal name."""

    def map_entity_mention_to_domain(self, entity_mention: EntityMention) -> Mention:
        name = entity_mention.content.removeprefix(NAME_PREFIX)
        return _mention(entity_mention.identifiedBy.request_id, name)


@given(
    parsers.parse('an ERE worker with a stored mention "{mention_id}"'),
    target_fixture="bite_worker",
)
def worker_with_stored_mention(
    mention_id: str, fake_redis, similarity_map
) -> RedisQueueWorker:
    service = EntityResolutionService(
        _stub_resolver(similarity_map), ContentNameMapper()
    )
    worker = RedisQueueWorker(
        redis_client=fake_redis,
        entity_resolution_service=service,
        request_queue=REQUEST_QUEUE,
        response_queue=RESPONSE_QUEUE,
    )
    fake_redis.lpush(
        REQUEST_QUEUE, raw_request(mention_id, content=f"{NAME_PREFIX}Seen Corp")
    )
    worker.process_bite()
    fake_redis.lists[RESPONSE_QUEUE].clear()
    return worker


@given(
    parsers.parse(
        'the request queue holds, in order, a new mention "{fresh}", a re-submission of "{seen}", '
        'a conflicting re-submission of "{conflicting}" and an unparsable message'
    )
)
def mixed_queue(fresh: str, seen: str, conflicting: str, fake_redis):
    fake_redis.lpush(
        REQUEST_QUEUE, raw_request(fresh, content=f"{NAME_PREFIX}Fresh Corp")
    )
    fake_redis.lpush(
        REQUEST_QUEUE, raw_request(seen, content=f"{NAME_PREFIX}Seen Corp")
    )
    fake_redis.lpush(
        REQUEST_QUEUE, raw_request(conflicting, content=f"{NAME_PREFIX}Other Corp")
    )
    fake_redis.lpush(REQUEST_QUEUE, b"not a json message")


@when("the ERE processes one bite", target_fixture="responses")
def process_one_bite(bite_worker: RedisQueueWorker, fake_redis) -> list[dict]:
    bite_worker.process_bite()
    return [
        json.loads(payload) for payload in reversed(fake_redis.lists[RESPONSE_QUEUE])
    ]


@then(parsers.parse("{count:d} responses are sent in request order"))
def responses_in_order(count: int, responses: list[dict]):
    assert_that(responses).is_length(count)
    assert_that([r.get("ere_request_id") for r in responses[:3]]).is_equal_to(
        ["fresh", "seen", "seen"]
    )


@then(
    parsers.parse(
        'the responses are a resolution, a resolution, a "{conflict_type}" error and a "{processing_type}" error'
    )
)
def response_kinds(conflict_type: str, processing_type: str, responses: list[dict]):
    assert_that(responses[0]).contains_key(RESPONSE_CANDIDATES)
    assert_that(responses[1]).contains_key(RESPONSE_CANDIDATES)
    assert_that(responses[2].get(RESPONSE_ERROR_TYPE)).is_equal_to(conflict_type)
    assert_that(responses[3].get(RESPONSE_ERROR_TYPE)).is_equal_to(processing_type)


# ---------------------------------------------------------------------------
# Failure isolation and commits (DuckDB-backed)
# ---------------------------------------------------------------------------


class FailingOnceLinker(FixedSimilarityLinker):
    """Batch scoring fails on its first call; single scoring works."""

    def __init__(self, similarity_map):
        super().__init__(similarity_map)
        self.failed = False

    def find_matches_batch(self, mentions):
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated batch scoring failure")
        return super().find_matches_batch(mentions)


class CountingUnitOfWork:
    """Counts committed transactions of the wrapped unit of work."""

    def __init__(self, inner):
        self._inner = inner
        self.commits = 0

    @contextlib.contextmanager
    def __call__(self):
        with self._inner():
            yield
        self.commits += 1


def _duckdb_resolver(linker, unit_of_work_wrapper=None):
    con = duckdb.connect(":memory:")
    init_schema(con, ENTITY_FIELDS)
    unit_of_work = DuckDBUnitOfWork(con)
    if unit_of_work_wrapper is not None:
        unit_of_work = unit_of_work_wrapper(unit_of_work)
    resolver = EntityResolver(
        mention_repo=DuckDBMentionRepository(con, ENTITY_FIELDS),
        similarity_repo=DuckDBSimilarityRepository(con),
        cluster_repo=DuckDBClusterRepository(con),
        linker=linker,
        config=_config(),
        unit_of_work=unit_of_work,
    )
    return resolver, con, unit_of_work


class PoisonedLinker(FixedSimilarityLinker):
    """Scoring fails whenever one given mention is part of the call."""

    def __init__(self, similarity_map, poisoned: str):
        super().__init__(similarity_map)
        self._poisoned = poisoned

    def find_matches_batch(self, mentions):
        if any(mention.id.value == self._poisoned for mention in mentions):
            raise RuntimeError("scoring failed for one mention")
        return super().find_matches_batch(mentions)


def _with_service(resolver, con, unit_of_work) -> dict:
    return {
        "resolver": resolver,
        "service": EntityResolutionService(resolver, ContentNameMapper()),
        "con": con,
        "unit_of_work": unit_of_work,
    }


@given(
    "a DuckDB-backed resolver whose next batch scoring fails",
    target_fixture="duckdb_setup",
)
def failing_duckdb_resolver(similarity_map):
    resolver, con, unit_of_work = _duckdb_resolver(FailingOnceLinker(similarity_map))
    yield _with_service(resolver, con, unit_of_work)
    con.close()


@given(
    parsers.parse('a DuckDB-backed resolver that cannot score mention "{mention_id}"'),
    target_fixture="duckdb_setup",
)
def poisoned_duckdb_resolver(mention_id: str, similarity_map):
    resolver, con, unit_of_work = _duckdb_resolver(
        PoisonedLinker(similarity_map, mention_id)
    )
    yield _with_service(resolver, con, unit_of_work)
    con.close()


def _content_request(request_id: str) -> EntityMentionResolutionRequest:
    return EntityMentionResolutionRequest(
        entity_mention=EntityMention(
            identifiedBy=EntityMentionIdentifier(
                request_id=request_id, source_id="bdd", entity_type="ORGANISATION"
            ),
            content_type="text/plain",
            content=f"{NAME_PREFIX}Company {request_id}",
        ),
        ere_request_id=request_id,
    )


@when(
    parsers.parse("a bite of {count:d} new mention requests is processed"),
    target_fixture="bite_responses",
)
def process_request_bite(count: int, duckdb_setup: dict):
    return duckdb_setup["service"].process_batch(
        [_content_request(f"b{index}") for index in range(count)]
    )


@when(
    parsers.parse(
        'the requests "{first}", "{second}" and "{third}" are processed as one bite'
    ),
    target_fixture="bite_responses",
)
def process_three(first: str, second: str, third: str, duckdb_setup: dict):
    return duckdb_setup["service"].process_batch(
        [_content_request(rid) for rid in (first, second, third)]
    )


@then(parsers.parse("{count:d} responses are returned without errors"))
def responses_without_errors(count: int, bite_responses):
    assert_that(bite_responses).is_length(count)
    assert_that(
        [getattr(r, RESPONSE_ERROR_TYPE, None) for r in bite_responses]
    ).is_equal_to([None] * count)


@then(
    parsers.parse(
        'the responses for "{first}" and "{third}" are resolutions and the response for "{second}" is an "{error}" error'
    )
)
def poisoned_responses(first: str, second: str, third: str, error: str, bite_responses):
    by_id = {r.ere_request_id: r for r in bite_responses}
    assert_that(getattr(by_id[first], RESPONSE_ERROR_TYPE, None)).is_none()
    assert_that(getattr(by_id[third], RESPONSE_ERROR_TYPE, None)).is_none()
    assert_that(by_id[second].error_type).is_equal_to(error)


@then(parsers.parse('only the mentions "{first}" and "{second}" are stored'))
def only_stored(first: str, second: str, duckdb_setup: dict):
    stored = sorted(
        row[0]
        for row in duckdb_setup["con"]
        .execute("SELECT mention_id FROM mentions")
        .fetchall()
    )
    assert_that(stored).is_equal_to(sorted([first, second]))


@given("a DuckDB-backed resolver counting transactions", target_fixture="duckdb_setup")
def counting_duckdb_resolver(similarity_map):
    resolver, con, unit_of_work = _duckdb_resolver(
        FixedSimilarityLinker(similarity_map), CountingUnitOfWork
    )
    yield {"resolver": resolver, "con": con, "unit_of_work": unit_of_work}
    con.close()


@when(
    parsers.parse("a bite of {count:d} new mentions is resolved"),
    target_fixture="duckdb_results",
)
def resolve_bite(count: int, duckdb_setup: dict):
    return duckdb_setup["resolver"].resolve_batch(
        [_mention(f"b{index}") for index in range(count)]
    )


@then("every mention is stored exactly once with exactly one cluster assignment")
def stored_once(duckdb_setup: dict, bite_responses):
    con = duckdb_setup["con"]
    expected = (len(bite_responses), len(bite_responses))
    for table in ("mentions", "clusters"):
        counts = con.execute(
            f"SELECT count(*), count(DISTINCT mention_id) FROM {table}"
        ).fetchone()
        assert_that(counts).described_as(table).is_equal_to(expected)


@then(parsers.parse("exactly {count:d} transaction was committed"))
def transactions_committed(count: int, duckdb_setup: dict):
    assert_that(duckdb_setup["unit_of_work"].commits).is_equal_to(count)


@given(
    "the ERE is started with the shipped configuration",
    target_fixture="shipped_resolver",
)
def shipped_resolver(tmp_path):
    resolver = build_entity_resolver(
        resolver_config_path=SHIPPED_CONFIG, duckdb_path=str(tmp_path / "app.duckdb")
    )
    yield resolver
    resolver._mention_repo._con.close()  # pylint: disable=protected-access  # mirrors app.py shutdown


@when(
    parsers.parse(
        '"{first_name}" and "{second_name}" in "{country}" at post code "{post_code}" in "{post_name}" '
        'are resolved in one bite as "{first_id}" and "{second_id}"'
    )
)
def resolve_production_bite(  # pylint: disable=too-many-arguments,too-many-positional-arguments  # one step parameter per Gherkin placeholder
    first_name,
    second_name,
    country,
    post_code,
    post_name,
    first_id,
    second_id,
    shipped_resolver,
):
    fields = shipped_resolver._config.entity_fields  # pylint: disable=protected-access

    def organisation(mention_id: str, name: str) -> Mention:
        attributes = {field: None for field in fields}
        attributes.update(
            {
                "legal_name": name,
                "country_code": country,
                "post_code": post_code,
                "post_name": post_name,
            }
        )
        return Mention(id=MentionId(value=mention_id), attributes=attributes)

    shipped_resolver.resolve_batch(
        [organisation(first_id, first_name), organisation(second_id, second_name)]
    )


@then(parsers.parse('production mention "{mention_id}" is in the cluster of "{other}"'))
def production_same_cluster(mention_id: str, other: str, shipped_resolver):
    clusters = shipped_resolver._cluster_repo  # pylint: disable=protected-access
    assert_that(clusters.find_cluster_of(MentionId(value=mention_id))).is_equal_to(
        clusters.find_cluster_of(MentionId(value=other))
    )


@when(
    parsers.parse(
        "the first {count:d} reference organisations are resolved once in bites of {bite_size:d} "
        "and once one at a time with the shipped configuration and training disabled"
    ),
    target_fixture="production_runs",
)
def resolve_reference_both_ways(count: int, bite_size: int, tmp_path):
    # Background training finishes at a different moment in each run and would change later scores (DEC-3),
    # so the comparison isolates batching from training timing.
    raw = yaml.safe_load(SHIPPED_CONFIG.read_text(encoding="utf-8"))
    raw["auto_train_threshold"] = 0
    config_path = tmp_path / "resolver.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    runs = {}
    for label, size in (("single", 1), ("batched", bite_size)):
        resolver = build_entity_resolver(
            resolver_config_path=config_path,
            duckdb_path=str(tmp_path / f"{label}.duckdb"),
        )
        mentions = load_organisation_mentions(count, resolver._config.entity_fields)  # pylint: disable=protected-access
        for start in range(0, len(mentions), size):
            resolver.resolve_batch(mentions[start : start + size])
        con = resolver._mention_repo._con  # pylint: disable=protected-access
        runs[label] = dict(
            con.execute("SELECT mention_id, cluster_id FROM clusters").fetchall()
        )
        con.close()
    return runs


@then("every organisation is in the same cluster in both runs")
def same_production_clusters(production_runs: dict):
    assert_that(production_runs["batched"]).is_equal_to(production_runs["single"])
    assert_that(set(production_runs["single"].values())).is_not_empty()
