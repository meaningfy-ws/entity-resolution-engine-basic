"""Similarity domain model: pairwise mention links."""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class LinkTable:
    """
    Scored links of a bite in columnar form: parallel tuples, one row per scored pair.

    Keeps links as plain values between scoring and storage; `MentionLink` objects are built only
    for the rows a caller actually needs (`links_for`).
    """

    left_ids: tuple[str, ...]
    right_ids: tuple[str, ...]
    scores: tuple[float, ...]

    def __len__(self) -> int:
        return len(self.scores)

    def links_for(self, mention_id: MentionId) -> list[MentionLink]:
        """Links whose left side is the given mention, in table order."""
        return [
            MentionLink(
                left_id=mention_id, right_id=MentionId(value=right), score=score
            )
            for left, right, score in zip(self.left_ids, self.right_ids, self.scores)
            if left == mention_id.value
        ]
