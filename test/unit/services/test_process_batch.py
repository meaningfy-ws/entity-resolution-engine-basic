"""Unit tests: resolving a bite of requests (services).

Spec: batched-resolution — "Failures are isolated per request", "One response per request, in request order";
mention-lifecycle-tracing — "Mention lifecycle events with timings" (no duplicate events on fallback).
"""

import logging
from datetime import datetime, timezone

import duckdb
import pytest
from erspec.models.core import EntityMention, EntityMentionIdentifier
from erspec.models.ere import EntityMentionResolutionRequest

from ere.adapters.duckdb_repositories import (
    DuckDBClusterRepository,
    DuckDBMentionRepository,
    DuckDBSimilarityRepository,
)
from ere.adapters.duckdb_schema import init_schema
from ere.adapters.duckdb_unit_of_work import DuckDBUnitOfWork
from ere.models.ports.rdf_mapper import RDFMapper
from ere.models.resolver import Mention, MentionId
from ere.services.entity_resolution_service import (
    EntityResolutionService,
    EntityResolver,
)
from ere.services.resolver_config import ResolverConfig
from ere.services.tracing import MentionTrace, TraceEvent, TraceField
from test.unit.adapters.stubs import FixedSimilarityLinker

ENTITY_FIELDS = ["legal_name", "country_code"]
POISON = "bad"
CONFLICT_ERROR = "ConflictError"
INTERNAL_ERROR = "InternalError"


class NameFromContentMapper(RDFMapper):
    """The request content is the legal name; the request id is the mention id."""

    def map_entity_mention_to_domain(self, entity_mention: EntityMention) -> Mention:
        return Mention(
            id=MentionId(value=entity_mention.identifiedBy.request_id),
            attributes={"legal_name": entity_mention.content, "country_code": "DEU"},
        )


class PoisonLinker(FixedSimilarityLinker):
    """Scoring fails whenever the poisoned mention is part of the call."""

    def find_matches_batch(self, mentions):
        if any(mention.id.value == POISON for mention in mentions):
            raise RuntimeError("scoring failed for one mention")
        return super().find_matches_batch(mentions)


def _request(request_id: str, name: str) -> EntityMentionResolutionRequest:
    return EntityMentionResolutionRequest(
        entity_mention=EntityMention(
            identifiedBy=EntityMentionIdentifier(
                request_id=request_id, source_id="unit", entity_type="ORGANISATION"
            ),
            content_type="text/plain",
            content=name,
        ),
        ere_request_id=request_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@pytest.fixture
def con():
    connection = duckdb.connect(":memory:")
    init_schema(connection, ENTITY_FIELDS)
    yield connection
    connection.close()


class FailingFirstSaveRepository(DuckDBSimilarityRepository):
    """Storing links fails once — after scoring and cluster assignment already happened in the bite."""

    def __init__(self, con):
        super().__init__(con)
        self.failed = False

    def save_table(self, table):
        if not self.failed:
            self.failed = True
            raise RuntimeError("link storage failed")
        super().save_table(table)


def _service(con, linker, similarity_repo=None) -> EntityResolutionService:
    resolver = EntityResolver(
        mention_repo=DuckDBMentionRepository(con, ENTITY_FIELDS),
        similarity_repo=similarity_repo or DuckDBSimilarityRepository(con),
        cluster_repo=DuckDBClusterRepository(con),
        linker=linker,
        config=ResolverConfig(
            threshold=0.8,
            match_weight_threshold=-10,
            top_n=100,
            entity_fields=ENTITY_FIELDS,
            auto_train_threshold=0,
        ),
        unit_of_work=DuckDBUnitOfWork(con),
    )
    return EntityResolutionService(resolver, NameFromContentMapper())


def _stored_ids(con) -> list[str]:
    return sorted(
        row[0] for row in con.execute("SELECT mention_id FROM mentions").fetchall()
    )


def test_one_failing_mention_does_not_fail_the_rest_of_its_bite(con):
    service = _service(con, PoisonLinker({}))

    responses = service.process_batch(
        [_request("a", "Alpha"), _request(POISON, "Bad"), _request("c", "Gamma")]
    )

    assert [getattr(r, "error_type", None) for r in responses] == [
        None,
        INTERNAL_ERROR,
        None,
    ]
    assert [r.ere_request_id for r in responses] == ["a", POISON, "c"]
    assert _stored_ids(con) == ["a", "c"]


def test_a_bite_of_one_that_fails_is_answered_with_an_error_and_leaves_no_rows(con):
    service = _service(con, PoisonLinker({}))

    responses = service.process_batch([_request(POISON, "Bad")])

    assert responses[0].error_type == INTERNAL_ERROR
    assert _stored_ids(con) == []


def test_duplicate_mention_with_same_content_in_one_bite_is_stored_once(con):
    service = _service(con, FixedSimilarityLinker({}))

    first, second = service.process_batch(
        [_request("dup", "Same Corp"), _request("dup", "Same Corp")]
    )

    assert first.candidates == second.candidates
    assert _stored_ids(con) == ["dup"]


def test_duplicate_mention_with_different_content_in_one_bite_is_a_conflict(con):
    service = _service(con, FixedSimilarityLinker({}))

    first, second = service.process_batch(
        [_request("dup", "Same Corp"), _request("dup", "Other Corp")]
    )

    assert getattr(first, "error_type", None) is None
    assert second.error_type == CONFLICT_ERROR
    assert _stored_ids(con) == ["dup"]


def test_empty_bite_gives_no_responses(con):
    assert _service(con, FixedSimilarityLinker({})).process_batch([]) == []


def test_failed_bite_does_not_emit_scoring_events_twice(con, caplog):
    service = _service(con, FixedSimilarityLinker({}), FailingFirstSaveRepository(con))
    traces = [
        MentionTrace(logging.getLogger("unit-trace"), request_id)
        for request_id in ("a", "b", "c")
    ]
    caplog.set_level(logging.DEBUG, logger="unit-trace")

    responses = service.process_batch(
        [_request("a", "Alpha"), _request("b", "Beta"), _request("c", "Gamma")], traces
    )

    assert all(getattr(r, "error_type", None) is None for r in responses)
    assert _stored_ids(con) == ["a", "b", "c"]

    scored_for_a = [
        record
        for record in caplog.records
        if getattr(record, TraceField.EVENT, None) == TraceEvent.SCORED
        and getattr(record, TraceField.REQUEST_ID, None) == "a"
    ]
    assert len(scored_for_a) == 1
