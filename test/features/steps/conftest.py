"""Shared fixtures and steps for the memory-improvement features."""

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest
from assertpy import assert_that
from erspec.models.core import EntityMention, EntityMentionIdentifier
from erspec.models.ere import EntityMentionResolutionRequest
from linkml_runtime.dumpers import JSONDumper
from pytest_bdd import given, parsers, then

from ere.models.resolver import Mention, MentionId

REQUEST_QUEUE = "ere_requests"
RESPONSE_QUEUE = "ere_responses"
ORGANISATION_CORPUS = Path(__file__).parents[2] / "stress" / "data" / "org-mid.csv"
SECRET_RDF_PREFIX = "SECRET-RDF-CONTENT-"
SECRET_ATTRIBUTE_PREFIX = "SECRET-ATTR-"

_dumper = JSONDumper()


class FakeRedisQueues:
    """Minimal list-backed stand-in for the Redis commands the worker uses."""

    def __init__(self):
        self.lists: dict[str, list[bytes]] = {REQUEST_QUEUE: [], RESPONSE_QUEUE: []}

    def lpush(self, name: str, value) -> int:
        payload = value.encode("utf-8") if isinstance(value, str) else value
        self.lists.setdefault(name, []).insert(0, payload)
        return len(self.lists[name])

    def brpop(self, name: str, timeout: int = 0):  # pylint: disable=unused-argument
        queue = self.lists.get(name, [])
        return (name.encode("utf-8"), queue.pop()) if queue else None

    def llen(self, name: str) -> int:
        return len(self.lists.get(name, []))

    def rpop(self, name: str, count: int | None = None):
        queue = self.lists.get(name, [])
        if count is None:
            return queue.pop() if queue else None
        taken = [queue.pop() for _ in range(min(count, len(queue)))]
        return taken or None

    def lmpop(self, num_keys: int, *names: str, direction: str, count: int = 1):  # pylint: disable=unused-argument
        taken = self.rpop(names[0], count)
        return [names[0].encode("utf-8"), taken] if taken else None

    def rpush(self, name: str, *values) -> int:
        queue = self.lists.setdefault(name, [])
        for value in values:
            queue.append(value.encode("utf-8") if isinstance(value, str) else value)
        return len(queue)

    def pipeline(self, transaction: bool = True):  # pylint: disable=unused-argument
        return FakePipeline(self)


class FakePipeline:
    """Buffers LPUSH calls and applies them on execute, like a redis-py pipeline."""

    def __init__(self, queues: FakeRedisQueues):
        self._queues = queues
        self._pushes: list[tuple[str, object]] = []

    def lpush(self, name: str, value) -> "FakePipeline":
        self._pushes.append((name, value))
        return self

    def execute(self) -> list[int]:
        return [self._queues.lpush(name, value) for name, value in self._pushes]


def raw_request(
    request_id: str, created_at: datetime | None = None, content: str | None = None
) -> bytes:
    """Serialise a resolution request whose RDF content is a recognisable secret marker."""
    kwargs = {}
    if created_at is not None:
        kwargs["timestamp"] = created_at.isoformat()
    request = EntityMentionResolutionRequest(
        entity_mention=EntityMention(
            identifiedBy=EntityMentionIdentifier(
                request_id=request_id,
                source_id="bdd",
                entity_type="http://test.org/Org",
            ),
            content_type="text/turtle",
            content=content
            if content is not None
            else f"{SECRET_RDF_PREFIX}{request_id}",
        ),
        ere_request_id=request_id,
        **kwargs,
    )
    return _dumper.dumps(request).encode("utf-8")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_organisation_mentions(limit: int, entity_fields: list[str]) -> list[Mention]:
    """Real organisation records from the stress corpus, as domain mentions."""
    with ORGANISATION_CORPUS.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))[:limit]
    return [
        Mention(
            id=MentionId(value=row["mention_id"]),
            attributes={field: row.get(field) or None for field in entity_fields},
        )
        for row in rows
    ]


@pytest.fixture
def fake_redis() -> FakeRedisQueues:
    return FakeRedisQueues()


@pytest.fixture
def similarity_map() -> dict[frozenset[str], float]:
    """Shared, mutable similarity map read by FixedSimilarityLinker instances."""
    return {}


@given(parsers.parse('mentions "{left}" and "{right}" have similarity {score:f}'))
def set_similarity(left: str, right: str, score: float, similarity_map):
    similarity_map[frozenset([left, right])] = score


@then(parsers.parse("{count:d} response is in the response queue"))
def response_count(count: int, fake_redis):
    assert_that(fake_redis.llen(RESPONSE_QUEUE)).is_equal_to(count)


@given(parsers.parse("{count:d} resolution requests are waiting in the request queue"))
def queued_requests(count: int, fake_redis):
    for index in range(count):
        fake_redis.lpush(REQUEST_QUEUE, raw_request(f"backlog-{index}"))


@then(parsers.parse("{count:d} requests remain in the request queue"))
def requests_remaining(count: int, fake_redis):
    assert_that(fake_redis.llen(REQUEST_QUEUE)).is_equal_to(count)
