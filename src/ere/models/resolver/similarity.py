"""Similarity domain model: pairwise mention links."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from .ids import MentionId


class MentionLink(BaseModel):
    """
    A pairwise similarity score between two mentions.

    Stored regardless of threshold - below-threshold links are used by genCand()
    to discover candidate clusters.

    Invariant: left_id != right_id (enforced at construction time).
    """

    model_config = ConfigDict(frozen=True)

    left_id: MentionId
    right_id: MentionId
    score: float

    @model_validator(mode="after")
    def _validate_ids_differ(self) -> MentionLink:
        """Enforce that left and right mentions are different."""
        if self.left_id == self.right_id:
            raise ValueError("left_id and right_id must differ")
        return self

    def other(self, from_id: MentionId) -> MentionId:
        """
        Return the mention on the other side of this link.

        Raises ValueError if from_id is not part of this link.
        """
        if from_id == self.left_id:
            return self.right_id
        if from_id == self.right_id:
            return self.left_id
        raise ValueError(f"{from_id!r} is not part of this link")

    def meets_threshold(self, threshold: float) -> bool:
        """Check if this link's score meets or exceeds the threshold."""
        return self.score >= threshold
