"""Step definitions for resolution_resource_bounds.feature."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from unittest.mock import MagicMock

import duckdb
import pytest
import yaml
from assertpy import assert_that
from erspec.models.core import EntityMentionIdentifier
from erspec.models.ere import EntityMentionResolutionResponse
from pytest_bdd import given, parsers, scenarios, then, when
from test.conftest import TEST_RESOURCES_DIR
from test.features.steps.conftest import (
    REQUEST_QUEUE,
    RESPONSE_QUEUE,
    load_organisation_mentions,
)
from test.unit.adapters.stubs import (
    FixedSimilarityLinker,
    InMemoryClusterRepository,
    InMemoryMentionRepository,
    InMemorySimilarityRepository,
)

from ere.adapters.duckdb_connection import open_duckdb
from ere.adapters.duckdb_repositories import (
    DuckDBClusterRepository,
    DuckDBMentionRepository,
    DuckDBSimilarityRepository,
)
from ere.adapters.duckdb_schema import init_schema
from ere.adapters.splink_linker_impl import SpLinkSimilarityLinker
from ere.entrypoints.bootstrap import DuckDBEnvVar, resolve_duckdb_settings
from ere.entrypoints.queue_worker import RedisQueueWorker
from ere.models.resolver import Mention, MentionId
from ere.services.entity_resolution_service import EntityResolver
from ere.services.resolver_config import BatchSettings, DuckDBConfig, ResolverConfig

scenarios("../resolution_resource_bounds.feature")


@dataclass
class SplinkResolverUnderTest:
    con: duckdb.DuckDBPyConnection
    resolver: EntityResolver
    entity_fields: list[str]


@dataclass
class ResolutionRun:
    results: list = field(default_factory=list)
    catalog_before: int = 0
    catalog_after_last: int = 0
    splink_cache_before: int = 0
    splink_cache_after_last: int = 0


def _catalog_size(con: duckdb.DuckDBPyConnection) -> int:
    tables = con.execute("SELECT count(*) FROM duckdb_tables()").fetchone()[0]
    views = con.execute(
        "SELECT count(*) FROM duckdb_views() WHERE NOT internal"
    ).fetchone()[0]
    return tables + views


# ---------------------------------------------------------------------------
# No per-request database artefacts are retained
# ---------------------------------------------------------------------------


@given(
    "a Splink-backed resolver on a fresh on-disk database",
    target_fixture="splink_resolver",
)
def splink_resolver(tmp_path):
    raw_config = yaml.safe_load(
        (TEST_RESOURCES_DIR / "resolver.yaml").read_text(encoding="utf-8")
    )
    raw_config["auto_train_threshold"] = 0
    config = ResolverConfig.from_dict(raw_config)
    entity_fields = config.entity_fields

    con = duckdb.connect(str(tmp_path / "app.duckdb"))
    init_schema(con, entity_fields)
    linker = SpLinkSimilarityLinker(entity_fields, raw_config, connection=con)
    resolver = EntityResolver(
        DuckDBMentionRepository(con, entity_fields),
        DuckDBSimilarityRepository(con),
        DuckDBClusterRepository(con),
        linker,
        config,
    )
    yield SplinkResolverUnderTest(
        con=con, resolver=resolver, entity_fields=entity_fields
    )
    con.close()


@given(parsers.parse('releasing per-request artefacts fails with "{message}"'))
def releasing_artefacts_fails(message: str, monkeypatch):
    def _fail(*_args, **_kwargs):
        raise RuntimeError(message)

    monkeypatch.setattr(SpLinkSimilarityLinker, "_release_request_artefacts", _fail)


@when(
    parsers.parse("{count:d} distinct organisation mentions are resolved in sequence"),
    target_fixture="resolution_run",
)
def resolve_organisations(count: int, splink_resolver: SplinkResolverUnderTest):
    run = ResolutionRun()
    mentions = load_organisation_mentions(count, splink_resolver.entity_fields)
    run.catalog_before = _catalog_size(splink_resolver.con)
    run.splink_cache_before = _splink_cache_size(splink_resolver.resolver)
    for mention in mentions:
        run.results.append(splink_resolver.resolver.resolve(mention))
    run.catalog_after_last = _catalog_size(splink_resolver.con)
    run.splink_cache_after_last = _splink_cache_size(splink_resolver.resolver)
    return run


def _splink_cache_size(resolver: EntityResolver) -> int:
    linker = resolver._linker  # pylint: disable=protected-access
    cache = linker._linker._intermediate_table_cache  # pylint: disable=protected-access  # Splink internals are what leaked
    return (
        len(cache)
        + len(cache.executed_queries)
        + len(cache.queries_retrieved_from_cache)
    )


@then(
    "the database catalog and Splink's cache hold as much as before the first request"
)
def catalog_is_flat(resolution_run: ResolutionRun):
    assert_that(resolution_run.catalog_after_last).described_as("catalog").is_equal_to(
        resolution_run.catalog_before
    )
    assert_that(resolution_run.splink_cache_after_last).described_as(
        "Splink cache"
    ).is_equal_to(resolution_run.splink_cache_before)


@then("every resolution returned at least one candidate")
def every_resolution_has_candidates(resolution_run: ResolutionRun):
    for result in resolution_run.results:
        assert_that(result.candidates).is_not_empty()


@then(parsers.parse('a warning mentions "{text}"'))
def warning_mentions(text: str, caplog):
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert_that(any(text in message for message in warnings)).is_true()


# ---------------------------------------------------------------------------
# Per-request work is independent of cluster membership size
# ---------------------------------------------------------------------------


class _NoFullLoadClusterRepository(InMemoryClusterRepository):
    """Fails the scenario if the resolver loads all memberships or looks clusters up link by link."""

    def get_all_memberships(self):
        raise AssertionError("full membership map loaded during resolution")

    def find_cluster_of(self, mention_id):
        raise AssertionError("per-link cluster lookup during resolution")


class _NoReReadSimilarityRepository(InMemorySimilarityRepository):
    """Fails the scenario if the resolver re-reads links it already computed."""

    def find_for(self, mention_id):
        raise AssertionError("links re-read from the similarity repository")


@given(
    "a resolver that refuses full membership loads, link re-reads and per-link cluster lookups",
    target_fixture="strict_resolver",
)
def strict_resolver(similarity_map):
    config = ResolverConfig(
        threshold=0.8,
        match_weight_threshold=-10,
        top_n=100,
        entity_fields=["legal_name", "country_code"],
        auto_train_threshold=0,
    )
    return EntityResolver(
        mention_repo=InMemoryMentionRepository(),
        similarity_repo=_NoReReadSimilarityRepository(),
        cluster_repo=_NoFullLoadClusterRepository(),
        linker=FixedSimilarityLinker(similarity_map=similarity_map),
        config=config,
    )


@when(
    parsers.parse(
        'mentions "{first}", "{second}" and "{third}" are resolved in that order'
    ),
    target_fixture="last_result",
)
def resolve_three(first: str, second: str, third: str, strict_resolver: EntityResolver):
    result = None
    for mention_id in (first, second, third):
        result = strict_resolver.resolve(
            Mention(
                id=MentionId(value=mention_id),
                attributes={
                    "legal_name": f"Company {mention_id}",
                    "country_code": "DEU",
                },
            )
        )
    return result


@then(parsers.parse("the last result has {count:d} candidates"))
def candidate_count(count: int, last_result):
    assert_that(last_result.candidates).is_length(count)


@then(
    parsers.parse('candidate {index:d} is cluster "{cluster_id}" with score {score:f}')
)
def candidate_at(index: int, cluster_id: str, score: float, last_result):
    candidate = last_result.candidates[index]
    assert_that(candidate.cluster_id.value).is_equal_to(cluster_id)
    assert_that(candidate.score).is_close_to(score, 0.001)


# ---------------------------------------------------------------------------
# DuckDB storage is configurable by environment with on-disk default
# ---------------------------------------------------------------------------


@given("no DuckDB environment variables are set", target_fixture="duckdb_env")
def no_duckdb_env(tmp_path):
    # Only the file location is pinned, so the test never writes outside tmp_path.
    return {DuckDBEnvVar.PATH: str(tmp_path / "app.duckdb")}


@given(
    parsers.parse(
        'the DuckDB environment variables storage "{storage}" and memory limit "{memory_limit}"'
    ),
    target_fixture="duckdb_env",
)
def duckdb_env_values(storage: str, memory_limit: str, tmp_path):
    return {
        DuckDBEnvVar.PATH: str(tmp_path / "app.duckdb"),
        DuckDBEnvVar.STORAGE: storage,
        DuckDBEnvVar.MEMORY_LIMIT: memory_limit,
        DuckDBEnvVar.TEMP_DIR: str(tmp_path / "spill"),
    }


@when("the ERE database is opened", target_fixture="opened_db")
def open_database(duckdb_env):
    settings = resolve_duckdb_settings(DuckDBConfig(), duckdb_env)
    con = open_duckdb(settings)
    yield settings, con
    con.close()


@when("the ERE database settings are resolved", target_fixture="settings_error")
def resolve_settings_expecting_error(duckdb_env):
    with pytest.raises(ValueError) as error:
        resolve_duckdb_settings(DuckDBConfig(), duckdb_env)
    return str(error.value)


@then("the database is file-backed")
def database_is_file_backed(opened_db):
    settings, con = opened_db
    con.execute("CREATE TABLE IF NOT EXISTS probe (x INTEGER)")
    con.execute("CHECKPOINT")
    database_file = con.execute(
        "SELECT path FROM duckdb_databases() WHERE database_name = current_database()"
    ).fetchone()[0]
    assert_that(database_file).is_equal_to(settings.path)


@then("its temp directory is beside the database file")
def temp_dir_beside_file(opened_db):
    settings, con = opened_db
    temp_directory = con.execute("SELECT current_setting('temp_directory')").fetchone()[
        0
    ]
    assert_that(temp_directory).is_equal_to(f"{settings.path}.tmp")


@then(parsers.parse('the database reports a memory limit of "{reported}"'))
def reported_memory_limit(reported: str, opened_db):
    _, con = opened_db
    assert_that(
        con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    ).is_equal_to(reported)


@then(
    parsers.parse(
        'start-up is refused with an error naming "{variable}" and the values "{first}" and "{second}"'
    )
)
def startup_refused(variable: str, first: str, second: str, settings_error: str):
    assert_that(settings_error).contains(variable, first, second)


# ---------------------------------------------------------------------------
# Single-threaded consumption at processing rate
# ---------------------------------------------------------------------------


@when(parsers.parse("the ERE processes one bite with limit {limit:d}"))
def process_one_bite(limit: int, fake_redis):
    service = MagicMock()
    service.process_batch.side_effect = lambda requests, *args, **kwargs: [
        EntityMentionResolutionResponse(
            entity_mention_id=EntityMentionIdentifier(
                request_id=request.ere_request_id,
                source_id="bdd",
                entity_type="http://test.org/Org",
            ),
            candidates=[],
            ere_request_id=request.ere_request_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        for request in requests
    ]
    worker = RedisQueueWorker(
        redis_client=fake_redis,
        entity_resolution_service=service,
        request_queue=REQUEST_QUEUE,
        response_queue=RESPONSE_QUEUE,
        batch_settings=BatchSettings(max_mentions=limit),
    )
    worker.process_bite()
