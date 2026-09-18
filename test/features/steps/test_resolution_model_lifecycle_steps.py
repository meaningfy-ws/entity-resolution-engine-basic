"""Step definitions for resolution_model_lifecycle.feature."""

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import duckdb
import yaml
from assertpy import assert_that
from pytest_bdd import given, parsers, scenarios, then, when
from test.conftest import TEST_RESOURCES_DIR
from test.features.steps.conftest import load_organisation_mentions
from test.unit.adapters.stubs import (
    FixedSimilarityLinker,
    InMemoryClusterRepository,
    InMemoryMentionRepository,
    InMemorySimilarityRepository,
)

from ere.adapters import splink_linker_impl
from ere.adapters.duckdb_repositories import DuckDBMentionRepository
from ere.adapters.duckdb_schema import init_schema
from ere.adapters.splink_linker_impl import ModelSource, SpLinkSimilarityLinker
from ere.entrypoints.bootstrap import build_entity_resolver
from ere.models.resolver import Mention, MentionId, TrainingOutcome, TrainingStatus
from ere.services.entity_resolution_service import EntityResolver
from ere.services.resolver_config import ResolverConfig

scenarios("../resolution_model_lifecycle.feature")

TRAINING_THREAD_NAME = "linker-training"
MODEL_FILE_SUFFIX = ".splink_model.json"
CORRUPT_MODEL_CONTENT = "{ this is not a splink model"


@dataclass
class EreHome:
    db_path: Path
    config_path: Path
    entity_fields: list[str]

    @property
    def model_path(self) -> Path:
        return Path(f"{self.db_path}{MODEL_FILE_SUFFIX}")


class EreProcess:
    """Starts and restarts the resolver the way the service entrypoint does."""

    def __init__(self, home: EreHome):
        self._home = home
        self.resolver: EntityResolver | None = None
        self.start()

    def start(self) -> None:
        self.resolver = build_entity_resolver(
            resolver_config_path=self._home.config_path,
            duckdb_path=str(self._home.db_path),
        )

    def stop(self) -> None:
        self.resolver._mention_repo._con.close()  # pylint: disable=protected-access  # mirrors app.py shutdown

    def restart(self) -> None:
        self.stop()
        self.start()

    @property
    def linker(self):
        return self.resolver._linker  # pylint: disable=protected-access  # no public accessor on EntityResolver


def _organisation(
    mention_id: str,
    legal_name: str,
    country_code: str,
    entity_fields: list[str] | None = None,
) -> Mention:
    attributes = {field: None for field in entity_fields or []}
    attributes.update({"legal_name": legal_name, "country_code": country_code})
    return Mention(id=MentionId(value=mention_id), attributes=attributes)


def _join_training_threads() -> None:
    for thread in threading.enumerate():
        if thread.name == TRAINING_THREAD_NAME:
            thread.join(timeout=30)


# ---------------------------------------------------------------------------
# Background and start-up
# ---------------------------------------------------------------------------


@given("an on-disk ERE database in a temporary directory", target_fixture="ere_home")
def ere_home(tmp_path):
    raw_config = yaml.safe_load(
        (TEST_RESOURCES_DIR / "resolver.yaml").read_text(encoding="utf-8")
    )
    db_path = tmp_path / "app.duckdb"
    raw_config["duckdb"] = {"type": "persistent", "path": str(db_path)}
    raw_config.pop("auto_train_threshold", None)  # the default threshold is under test
    config_path = tmp_path / "resolver.yaml"
    config_path.write_text(yaml.safe_dump(raw_config), encoding="utf-8")
    return EreHome(
        db_path=db_path,
        config_path=config_path,
        entity_fields=raw_config["entity_fields"],
    )


@given("the ERE is started", target_fixture="ere")
@when("the ERE is started", target_fixture="ere")
def start_ere(ere_home: EreHome):
    process = EreProcess(ere_home)
    yield process
    process.stop()


@when("the ERE is restarted")
def restart_ere(ere: EreProcess):
    ere.restart()


# ---------------------------------------------------------------------------
# Search space survives restart
# ---------------------------------------------------------------------------


@given(
    parsers.parse(
        'the ERE has resolved "{legal_name}" in "{country}" as mention "{mention_id}"'
    ),
    target_fixture="results",
)
def ere_has_resolved(
    legal_name: str, country: str, mention_id: str, ere: EreProcess, ere_home: EreHome
):
    mention = _organisation(mention_id, legal_name, country, ere_home.entity_fields)
    return {mention_id: ere.resolver.resolve(mention)}


@when(
    parsers.parse(
        'the ERE resolves "{legal_name}" in "{country}" as mention "{mention_id}"'
    )
)
def ere_resolves(
    legal_name: str,
    country: str,
    mention_id: str,
    ere: EreProcess,
    ere_home: EreHome,
    results: dict,
):
    mention = _organisation(mention_id, legal_name, country, ere_home.entity_fields)
    results[mention_id] = ere.resolver.resolve(mention)


@then(
    parsers.parse(
        'the candidates of mention "{new_id}" include the cluster of mention "{stored_id}"'
    )
)
def candidates_include_cluster(
    new_id: str, stored_id: str, ere: EreProcess, results: dict
):
    stored_cluster = ere.resolver._cluster_repo.find_cluster_of(
        MentionId(value=stored_id)
    )  # pylint: disable=protected-access
    candidate_clusters = [c.cluster_id for c in results[new_id].candidates]
    assert_that(candidate_clusters).contains(stored_cluster)


# ---------------------------------------------------------------------------
# Training happens once and is frozen
# ---------------------------------------------------------------------------


class CountingLinker(FixedSimilarityLinker):
    """Records how many times training started."""

    def __init__(self, similarity_map):
        super().__init__(similarity_map)
        self.trainings = 0
        self._lock = threading.Lock()

    def train(self) -> TrainingOutcome:
        with self._lock:
            self.trainings += 1
        return TrainingOutcome(status=TrainingStatus.TRAINED, sample_size=0)


def _stub_resolver(linker, auto_train_threshold: int | None) -> EntityResolver:
    config_dict = {
        "threshold": 0.8,
        "match_weight_threshold": -10,
        "top_n": 100,
        "entity_fields": ["legal_name", "country_code"],
    }
    if auto_train_threshold is not None:
        config_dict["auto_train_threshold"] = auto_train_threshold
    return EntityResolver(
        mention_repo=InMemoryMentionRepository(),
        similarity_repo=InMemorySimilarityRepository(),
        cluster_repo=InMemoryClusterRepository(),
        linker=linker,
        config=ResolverConfig.from_dict(config_dict),
    )


@given(
    "a resolver with the default training threshold and a counting linker",
    target_fixture="training_setup",
)
def default_threshold_resolver(similarity_map):
    linker = CountingLinker(similarity_map)
    return _stub_resolver(linker, auto_train_threshold=None), linker


@when(parsers.parse("{count:d} mentions are resolved"))
def resolve_many(count: int, training_setup):
    resolver, _ = training_setup
    for index in range(count):
        resolver.resolve(_organisation(f"m{index}", f"Company {index}", "DEU"))
    _join_training_threads()


@when(parsers.parse("a bite of {count:d} more mentions is resolved"))
def resolve_bite(count: int, training_setup):
    resolver, _ = training_setup
    stored = resolver._mention_repo.count()  # pylint: disable=protected-access
    resolver.resolve_batch(
        [
            _organisation(f"m{stored + index}", f"Company {stored + index}", "DEU")
            for index in range(count)
        ]
    )
    _join_training_threads()


@then("the default training threshold is 200")
def default_threshold_is_200(training_setup):
    resolver, _ = training_setup
    assert_that(resolver._config.auto_train_threshold).is_equal_to(200)  # pylint: disable=protected-access


@then(parsers.parse("training has started {count:d} times"))
def training_started(count: int, training_setup):
    _, linker = training_setup
    assert_that(linker.trainings).is_equal_to(count)


@given("a trained model file exists beside the database")
def trained_model_file(ere_home: EreHome):
    raw_config = yaml.safe_load(ere_home.config_path.read_text(encoding="utf-8"))
    linker = SpLinkSimilarityLinker(ere_home.entity_fields, raw_config)
    linker._linker.misc.save_model_to_json(str(ere_home.model_path), overwrite=True)  # pylint: disable=protected-access


@given("a corrupt model file exists beside the database")
def corrupt_model_file(ere_home: EreHome):
    ere_home.model_path.write_text(CORRUPT_MODEL_CONTENT, encoding="utf-8")


@given(parsers.parse("the database already stores {count:d} organisation mentions"))
def database_with_mentions(count: int, ere_home: EreHome):
    con = duckdb.connect(str(ere_home.db_path))
    try:
        init_schema(con, ere_home.entity_fields)
        repo = DuckDBMentionRepository(con, ere_home.entity_fields)
        for mention in load_organisation_mentions(count, ere_home.entity_fields):
            repo.save(mention)
    finally:
        con.close()


@given("model training will fail")
def training_will_fail(monkeypatch):
    def _broken_linker(*_args, **_kwargs):
        raise RuntimeError("simulated training failure")

    monkeypatch.setattr(splink_linker_impl, "Linker", _broken_linker)


@when("training is triggered")
def trigger_training(ere: EreProcess):
    ere.resolver.train()


def _assert_model_source(ere: EreProcess, expected_name: str) -> None:
    assert_that(ere.linker.model_source).is_equal_to(ModelSource[expected_name])


@then("the linker scores with the persisted model")
def scores_with_persisted_model(ere: EreProcess):
    _assert_model_source(ere, "PERSISTED")


@then("the linker scores with a freshly trained model")
def scores_with_trained_model(ere: EreProcess):
    _assert_model_source(ere, "TRAINED")


@then("the linker scores with cold-start parameters")
def scores_with_cold_start(ere: EreProcess):
    _assert_model_source(ere, "COLD_START")


@then("a model file exists beside the database")
def model_file_exists(ere_home: EreHome):
    assert_that(ere_home.model_path.exists()).is_true()


@then("no model file exists beside the database")
def no_model_file(ere_home: EreHome):
    assert_that(ere_home.model_path.exists()).is_false()


@then("the model file is unchanged")
def model_file_unchanged(ere_home: EreHome):
    assert_that(ere_home.model_path.read_text(encoding="utf-8")).is_equal_to(
        CORRUPT_MODEL_CONTENT
    )


@then("an error about the model file is logged")
def model_error_logged(ere_home: EreHome, caplog):
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert_that(
        any(ere_home.model_path.name in message for message in errors)
    ).is_true()


@then("a warning about training is logged")
def training_warning_logged(caplog):
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert_that(any("training" in message.lower() for message in warnings)).is_true()
