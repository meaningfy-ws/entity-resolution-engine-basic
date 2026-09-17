"""Unit tests: the Splink adapter reports model lifecycle as values and does not log it (adapter).

Spec: resolution-model-lifecycle — "Trained model is persisted and reloaded" (corrupt file, training failure).
Design: review finding L3 #4 — lifecycle logging belongs to services.
"""

import logging

import yaml

from ere.adapters import splink_linker_impl
from ere.adapters.splink_linker_impl import SpLinkSimilarityLinker, build_tf_df
from ere.models.resolver import Mention, ModelSource, TrainingStatus
from test.conftest import TEST_RESOURCES_DIR

ADAPTER_LOGGER = splink_linker_impl.__name__
ENTITY_FIELDS = ["legal_name", "country_code"]
SEEDS = [
    Mention(mention_id=f"s{i}", legal_name=f"Company {i}", country_code="DEU")
    for i in range(3)
]


def _config() -> dict:
    raw = yaml.safe_load(
        (TEST_RESOURCES_DIR / "resolver.yaml").read_text(encoding="utf-8")
    )
    raw["entity_fields"] = ENTITY_FIELDS
    raw["splink"]["comparisons"] = [
        c for c in raw["splink"]["comparisons"] if c["field"] in ENTITY_FIELDS
    ]
    return raw


def _linker(**kwargs) -> SpLinkSimilarityLinker:
    return SpLinkSimilarityLinker(
        ENTITY_FIELDS, _config(), initial_df=build_tf_df(SEEDS, ENTITY_FIELDS), **kwargs
    )


def _adapter_records_at_info_or_above(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == ADAPTER_LOGGER and r.levelno >= logging.INFO
    ]


def test_cold_start_status_without_model_file(caplog):
    caplog.set_level(logging.DEBUG)

    status = _linker().model_status()

    assert status.source == ModelSource.COLD_START
    assert status.load_error_type is None
    assert _adapter_records_at_info_or_above(caplog) == []


def test_unusable_model_file_is_reported_in_the_status_not_logged(tmp_path, caplog):
    model_path = tmp_path / "model.json"
    model_path.write_text("{ not json", encoding="utf-8")
    caplog.set_level(logging.DEBUG)

    status = _linker(model_path=str(model_path)).model_status()

    assert status.source == ModelSource.COLD_START
    assert status.load_error_type is not None
    assert _adapter_records_at_info_or_above(caplog) == []


def test_failed_training_is_returned_as_an_outcome_not_logged(monkeypatch, caplog):
    linker = _linker()

    def _broken(*_args, **_kwargs):
        raise RuntimeError("simulated training failure")

    monkeypatch.setattr(splink_linker_impl, "Linker", _broken)
    caplog.set_level(logging.DEBUG)

    outcome = linker.train()

    assert outcome.status == TrainingStatus.FAILED
    assert outcome.error_type == "RuntimeError"
    assert _adapter_records_at_info_or_above(caplog) == []


def test_training_is_skipped_when_a_persisted_model_is_loaded(tmp_path):
    model_path = tmp_path / "model.json"
    model_path.write_text(
        __import__("json").dumps(_linker()._linker.misc.save_model_to_json()),  # pylint: disable=protected-access
        encoding="utf-8",
    )

    outcome = _linker(model_path=str(model_path)).train()

    assert outcome.status == TrainingStatus.SKIPPED
