"""State of the similarity model and the outcome of training (domain values, no I/O)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ModelSource(StrEnum):
    """Where the scoring parameters come from."""

    COLD_START = "cold_start"
    PERSISTED = "persisted"
    TRAINED = "trained"


class TrainingStatus(StrEnum):
    """What a training request did."""

    TRAINED = "trained"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class ModelStatus:
    """The linker's current model; `load_error_type` names why a model file could not be used."""

    source: ModelSource
    load_error_type: str | None = None


@dataclass(frozen=True)
class TrainingOutcome:
    """Result of one training request; `error_type` only (never the exception text, DEC-10)."""

    status: TrainingStatus
    sample_size: int | None = None
    model_persisted: bool = False
    error_type: str | None = None
