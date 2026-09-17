"""Repository port interfaces (abstract base classes) for data persistence.

These ABCs define what infrastructure the entity resolution algorithm needs
for persisting mentions, similarities, and cluster assignments.
The resolver algorithm (EntityResolver) depends only on these ports, not on
concrete implementations. This enables testing with in-memory stubs and
swapping infrastructure without changing resolver logic.
"""

from abc import ABC, abstractmethod

from ere.models.resolver import (
    ClusterId,
    ClusterMembership,
    Mention,
    MentionId,
    MentionLink,
)


class MentionRepository(ABC):
    """
    Port: persistence layer for mentions (entity records).

    Responsibilities:
    - Save new mentions
    - Retrieve all mentions for introspection
    - Count total mentions
    """

    @abstractmethod
    def save(self, mention: Mention) -> None:
        """
        Persist a mention to storage.

        Args:
            mention: The Mention to persist.
        """

    @abstractmethod
    def load_all(self) -> list[Mention]:
        """
        Retrieve all persisted mentions.

        Returns:
            List of all Mention objects.
        """

    @abstractmethod
    def count(self) -> int:
        """
        Return the total number of mentions in storage.

        Returns:
            Non-negative integer count.
        """

    @abstractmethod
    def find_by_id(self, mention_id: MentionId) -> Mention | None:
        """
        Retrieve a single mention by ID.

        Args:
            mention_id: The MentionId to look up.

        Returns:
            The Mention object if found, None otherwise.
        """


class SimilarityRepository(ABC):
    """
    Port: persistence layer for pairwise mention similarities.

    Responsibilities:
    - Save similarity scores (mention-links)
    - Retrieve links for a given mention
    - Count total links
    """

    @abstractmethod
    def save_all(self, links: list[MentionLink]) -> None:
        """
        Persist multiple mention-links (similarity scores).

        Args:
            links: List of MentionLink objects to save.
        """

    @abstractmethod
    def count(self) -> int:
        """
        Return the total number of mention-links in storage.

        Returns:
            Non-negative integer count.
        """

    @abstractmethod
    def find_for(self, mention_id: MentionId) -> list[MentionLink]:
        """
        Retrieve all mention-links involving the given mention.

        Returns all links where this mention appears on either side
        (left_id or right_id).

        Note: N+1 pattern. The DuckDB adapter can override this
        to delegate to an efficient SQL JOIN; the service sees no difference.

        Args:
            mention_id: The MentionId to find links for.

        Returns:
            List of MentionLink objects (may be empty).
        """


class ClusterRepository(ABC):
    """
    Port: persistence layer for cluster membership.

    Responsibilities:
    - Save cluster assignments (mention -> cluster)
    - Look up which cluster a mention belongs to
    - Retrieve full membership mappings for introspection
    """

    @abstractmethod
    def save(self, membership: ClusterMembership) -> None:
        """
        Persist a cluster membership assignment.

        Args:
            membership: ClusterMembership object (mention_id -> cluster_id).
        """

    @abstractmethod
    def find_cluster_of(self, mention_id: MentionId) -> ClusterId:
        """
        Look up the cluster a mention belongs to.

        Args:
            mention_id: The MentionId to look up.

        Returns:
            The ClusterId this mention is assigned to.

        Raises:
            KeyError: If the mention has no cluster assignment.
        """

    @abstractmethod
    def count(self) -> int:
        """
        Return the total number of cluster assignments in storage.

        Returns:
            Non-negative integer count.
        """

    @abstractmethod
    def get_all_memberships(self) -> dict[ClusterId, list[MentionId]]:
        """
        Retrieve the full cluster membership mapping.

        Returns:
            Dict mapping ClusterId -> list of MentionIds in that cluster,
            sorted for determinism.
        """
