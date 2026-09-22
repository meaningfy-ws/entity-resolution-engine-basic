"""Logging of the similarity-model lifecycle reported by the linker (services own observability)."""

import logging

from ere.models.ports.linker import SimilarityLinker
from ere.models.resolver import (
    ModelSource,
    ModelStatus,
    TrainingOutcome,
    TrainingStatus,
)

log = logging.getLogger(__name__)


def log_model_status(status: ModelStatus, model_path: str | None) -> None:
    """Log where scoring parameters come from; an unusable model file is an error the operator must see."""
    if status.load_error_type is not None:
        log.error(
            "Could not load model file %s (%s); using cold-start parameters and leaving the file untouched",
            model_path,
            status.load_error_type,
        )
        return
    if status.source == ModelSource.PERSISTED:
        log.info("Similarity model: %s from %s", status.source.value, model_path)
    else:
        log.info(
            "Similarity model: %s (model file %s)",
            status.source.value,
            model_path or "not persisted",
        )


def log_training_outcome(outcome: TrainingOutcome) -> None:
    """Log the result of a training request; skipped requests are routine and stay at DEBUG."""
    if outcome.status == TrainingStatus.FAILED:
        log.warning(
            "EM training failed (%s); scoring keeps cold-start parameters",
            outcome.error_type,
        )
    elif outcome.status == TrainingStatus.TRAINED:
        log.info(
            "EM training complete on %s mentions; model frozen, %s",
            outcome.sample_size,
            "persisted" if outcome.model_persisted else "not persisted",
        )
    else:
        log.debug(
            "EM training skipped: model already trained, persisted, unusable or training in progress"
        )


def train_on_start_if_due(
    linker: SimilarityLinker, stored_mentions: int, threshold: int
) -> None:
    """Train once before consuming when the threshold was passed without a usable model (e.g. after upgrade)."""
    if threshold <= 0 or stored_mentions < threshold or not linker.needs_training():
        return
    log.info(
        "Start-up training: %d stored mentions >= threshold %d and no trained model",
        stored_mentions,
        threshold,
    )
    log_training_outcome(linker.train())
