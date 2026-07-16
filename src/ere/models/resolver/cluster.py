"""Cluster domain models: membership, candidates, and resolution results."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from .ids import ClusterId, MentionId


class ClusterMembership(BaseModel):
    """
    One mention's assignment to one cluster.

    One record per mention in the resolver's state.
    """

    model_config = ConfigDict(frozen=True)

    mention_id: MentionId
    cluster_id: ClusterId


class CandidateCluster(BaseModel):
    """
    A cluster reference in the resolution output, with its score.

    Represents the algorithm's confidence that a mention belongs to this cluster.
    """

    model_config = ConfigDict(frozen=True)

    cluster_id: ClusterId
    score: float

    def as_tuple(self) -> tuple[str, float]:
        """Return backward-compatible tuple form: (cluster_id_str, score)."""
        return (self.cluster_id.value, self.score)


class ResolutionResult(BaseModel):
    """
    Non-empty, descending-score ranked list of CandidateCluster references.

    Pruned to top-N by the service layer before construction.

    Invariant: len(candidates) >= 1 (enforced at construction time).
    """

    model_config = ConfigDict(frozen=True)

    candidates: tuple[CandidateCluster, ...]

    @field_validator("candidates")
    @classmethod
    def _must_be_non_empty(cls, v: tuple) -> tuple:
        """Enforce that candidates list is non-empty."""
        if len(v) == 0:
            raise ValueError("ResolutionResult candidates must be non-empty")
        return v

    def as_tuples(self) -> list[tuple[str, float]]:
        """
        Return backward-compatible list-of-tuples form.

        Current API contract: list[tuple[str, float]].
        """
        return [c.as_tuple() for c in self.candidates]

    @property
    def top(self) -> CandidateCluster:
        """Return the algorithm's implied best cluster for this mention."""
        return self.candidates[0]
