"""Unit tests: services log the model lifecycle reported by the linker (services).

Spec: resolution-model-lifecycle — "Corrupt model file" (error logged), "Training failure" (warning logged).
"""

import logging

import pytest

from ere.models.resolver import (
    ModelSource,
    ModelStatus,
    TrainingOutcome,
    TrainingStatus,
)
from ere.services.model_lifecycle import log_model_status, log_training_outcome

SERVICE_LOGGER = "ere.services.model_lifecycle"


def _records(caplog, level: int) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == SERVICE_LOGGER and r.levelno == level
    ]


def test_unusable_model_file_is_logged_as_error_naming_the_file(caplog):
    log_model_status(
        ModelStatus(source=ModelSource.COLD_START, load_error_type="JSONDecodeError"),
        "/data/app.json",
    )

    (message,) = _records(caplog, logging.ERROR)
    assert "/data/app.json" in message and "JSONDecodeError" in message


@pytest.mark.parametrize("source", [ModelSource.PERSISTED, ModelSource.COLD_START])
def test_usable_model_state_is_logged_at_info(caplog, source):
    caplog.set_level(logging.INFO)

    log_model_status(ModelStatus(source=source), "/data/app.json")

    assert _records(caplog, logging.ERROR) == []
    assert any(source.value in message for message in _records(caplog, logging.INFO))


def test_failed_training_is_logged_as_warning_with_error_type_only(caplog):
    log_training_outcome(
        TrainingOutcome(status=TrainingStatus.FAILED, error_type="RuntimeError")
    )

    (message,) = _records(caplog, logging.WARNING)
    assert "training" in message.lower() and "RuntimeError" in message


def test_successful_training_is_logged_with_sample_size_and_persistence(caplog):
    caplog.set_level(logging.INFO)

    log_training_outcome(
        TrainingOutcome(
            status=TrainingStatus.TRAINED, sample_size=200, model_persisted=True
        )
    )

    (message,) = _records(caplog, logging.INFO)
    assert "200" in message and "persisted" in message


def test_skipped_training_is_not_logged_above_debug(caplog):
    caplog.set_level(logging.DEBUG)

    log_training_outcome(TrainingOutcome(status=TrainingStatus.SKIPPED))

    assert [
        r
        for r in caplog.records
        if r.name == SERVICE_LOGGER and r.levelno >= logging.INFO
    ] == []
