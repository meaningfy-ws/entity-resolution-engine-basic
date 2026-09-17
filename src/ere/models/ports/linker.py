"""Similarity linker port interface (abstract base class).

This ABC defines the external dependency for pairwise similarity scoring
(e.g. Splink). The resolver algorithm (EntityResolver) depends only on this
port, not on concrete implementations. This enables testing with stub linkers
and swapping the matching algorithm without changing resolver logic.
"""

from abc import ABC, abstractmethod

from ere.models.resolver import (
    LinkTable,
    Mention,
    MentionLink,
    ModelStatus,
    TrainingOutcome,
)


class SimilarityLinker(ABC):
    """
    Port: external dependency for pairwise similarity scoring (e.g. Splink).

    Responsibilities:
    - Score a new mention against previously registered mentions
    - Train the scoring model (EM, estimate parameters)
    - Maintain the search space of mention records
    """

    @abstractmethod
    def find_matches(self, mention: Mention) -> list[MentionLink]:
        """
        Score a mention against previously registered mentions.

        Returns all mention-links (pairs) above match_weight_threshold,
        regardless of cluster threshold. Below-threshold links are included
        so they can be used for candidate discovery in genCand().

        Args:
            mention: The Mention to score against the search space.

        Returns:
            List of MentionLink objects. Empty if no candidates exist or
            all pairs are below match_weight_threshold.
        """

    @abstractmethod
    def find_matches_batch(self, mentions: list[Mention]) -> LinkTable:
        """
        Score several new mentions in one call.

        The mentions are already part of the search space, so links between mentions of the
        batch are included (in both directions); self-links are excluded. Callers decide which
        links to keep (e.g. only links to mentions that arrived earlier).

        Args:
            mentions: The new mentions, in arrival order.

        Returns:
            LinkTable with left = new mention, right = other mention.
        """

    @abstractmethod
    def register_mention(self, mention: Mention) -> None:
        """
        Add a mention to the search space for future find_matches() calls.

        After this call, future find_matches() invocations will include this
        mention as a candidate for scoring.

        Args:
            mention: The Mention to add to the search space.
        """

    @abstractmethod
    def needs_training(self) -> bool:
        """True while the linker scores with untrained (cold-start) parameters and training is allowed."""

    @abstractmethod
    def model_status(self) -> ModelStatus:
        """Where scoring parameters come from, and why a persisted model could not be used (if so)."""

    @abstractmethod
    def train(self) -> TrainingOutcome:
        """
        Estimate model parameters once; later calls are skipped (the model is frozen).

        Returns the outcome instead of logging it; implementations handle insufficient data by
        keeping cold-start parameters and reporting a failed outcome.
        """
