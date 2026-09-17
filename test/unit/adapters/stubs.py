"""In-memory stub implementations of service ports for testing."""

from typing import Protocol, runtime_checkable

from erspec.models.core import EntityMention

from ere.models.ports.rdf_mapper import RDFMapper
from ere.models.resolver import (
    ClusterId,
    ClusterMembership,
    LinkTable,
    Mention,
    MentionId,
    MentionLink,
    ModelSource,
    ModelStatus,
    TrainingOutcome,
    TrainingStatus,
)

# Define base classes as protocols to avoid circular import


@runtime_checkable
class MentionRepository(Protocol):
    """Protocol for mention repository."""

    def save(self, mention: Mention) -> None: ...
    def load_all(self) -> list[Mention]: ...
    def count(self) -> int: ...


@runtime_checkable
class SimilarityRepository(Protocol):
    """Protocol for similarity repository."""

    def save_all(self, links: list[MentionLink]) -> None: ...
    def count(self) -> int: ...
    def find_for(self, mention_id: MentionId) -> list[MentionLink]: ...


@runtime_checkable
class ClusterRepository(Protocol):
    """Protocol for cluster repository."""

    def save(self, membership: ClusterMembership) -> None: ...
    def find_cluster_of(self, mention_id: MentionId) -> ClusterId: ...
    def count(self) -> int: ...
    def get_all_memberships(self) -> dict[ClusterId, list[MentionId]]: ...


@runtime_checkable
class SimilarityLinker(Protocol):
    """Protocol for similarity linker."""

    def find_matches(self, mention: Mention) -> list[MentionLink]: ...
    def register_mention(self, mention: Mention) -> None: ...
    def model_status(self) -> ModelStatus: ...
    def train(self) -> TrainingOutcome: ...


class InMemoryMentionRepository(MentionRepository):
    """In-memory mention repository backed by a dict."""

    def __init__(self):
        self._mentions: dict[MentionId, Mention] = {}

    def save(self, mention: Mention) -> None:
        self._mentions[mention.id] = mention

    def load_all(self) -> list[Mention]:
        return list(self._mentions.values())

    def find_by_id(self, mention_id: MentionId) -> Mention | None:
        return self._mentions.get(mention_id)

    def count(self) -> int:
        return len(self._mentions)


class InMemorySimilarityRepository(SimilarityRepository):
    """In-memory similarity repository backed by a list."""

    def __init__(self):
        self._links: list[MentionLink] = []

    def save_all(self, links: list[MentionLink]) -> None:
        self._links.extend(links)

    def save_table(self, table: LinkTable) -> None:
        self._links.extend(
            MentionLink(
                left_id=MentionId(value=left),
                right_id=MentionId(value=right),
                score=score,
            )
            for left, right, score in zip(table.left_ids, table.right_ids, table.scores)
        )

    def count(self) -> int:
        return len(self._links)

    def find_for(self, mention_id: MentionId) -> list[MentionLink]:
        """Find all links involving the given mention (either side)."""
        return [
            link for link in self._links if mention_id in (link.left_id, link.right_id)
        ]


class InMemoryClusterRepository(ClusterRepository):
    """In-memory cluster repository backed by a dict."""

    def __init__(self):
        self._memberships: dict[MentionId, ClusterId] = {}

    def save(self, membership: ClusterMembership) -> None:
        self._memberships[membership.mention_id] = membership.cluster_id

    def find_cluster_of(self, mention_id: MentionId) -> ClusterId:
        if mention_id not in self._memberships:
            raise KeyError(f"No cluster assignment for mention {mention_id}")
        return self._memberships[mention_id]

    def clusters_for(self, mention_ids) -> dict[MentionId, ClusterId]:
        return {m: self._memberships[m] for m in mention_ids if m in self._memberships}

    def count(self) -> int:
        # Count distinct clusters, not membership entries
        return len(set(self._memberships.values()))

    def get_all_memberships(self) -> dict[ClusterId, list[MentionId]]:
        """Group memberships by cluster ID."""
        memberships: dict[ClusterId, list[MentionId]] = {}
        for mention_id, cluster_id in self._memberships.items():
            if cluster_id not in memberships:
                memberships[cluster_id] = []
            memberships[cluster_id].append(mention_id)

        # Sort member lists for determinism
        for cluster_id, members in memberships.items():
            members.sort(key=lambda m: m.value)

        return memberships


class FixedSimilarityLinker(SimilarityLinker):
    """
    In-memory linker for testing.

    Pre-configured with a similarity map keyed by frozenset of mention IDs.
    Simulates Splink without actually training or scoring.
    """

    def __init__(self, similarity_map: dict[frozenset[str], float]):
        """
        Initialize with a pre-configured similarity map.

        Args:
            similarity_map: Dict keyed by frozenset({id1, id2}) with float scores.
                          Example: {frozenset(["m1", "m2"]): 0.95, ...}
        """
        self._similarity_map = similarity_map
        self._registered_mentions: dict[MentionId, Mention] = {}

    def find_matches(self, mention: Mention) -> list[MentionLink]:
        """
        Find matches for a mention by looking up scores in the similarity map.

        Returns all links where this mention's ID (as a string) appears in the
        frozenset key and the score is non-zero (simulating match_weight_threshold).
        """
        links = []
        mention_id_str = mention.id.value

        for pair_set, score in self._similarity_map.items():
            pair_list = list(pair_set)
            if len(pair_list) != 2:
                continue

            id1_str, id2_str = pair_list[0], pair_list[1]

            if mention_id_str == id1_str:
                other_id_str = id2_str
            elif mention_id_str == id2_str:
                other_id_str = id1_str
            else:
                continue

            # Check if other mention has been registered
            other_id = MentionId(value=other_id_str)
            if other_id in self._registered_mentions:
                links.append(
                    MentionLink(left_id=mention.id, right_id=other_id, score=score)
                )

        return links

    def find_matches_batch(self, mentions: list[Mention]) -> LinkTable:
        """Score each mention of the batch against the registered search space (which includes the batch)."""
        links = [link for mention in mentions for link in self.find_matches(mention)]
        return LinkTable(
            left_ids=tuple(link.left_id.value for link in links),
            right_ids=tuple(link.right_id.value for link in links),
            scores=tuple(link.score for link in links),
        )

    def register_mention(self, mention: Mention) -> None:
        """Add a mention to the search space."""
        self._registered_mentions[mention.id] = mention

    def needs_training(self) -> bool:
        """Fixed scores never need training."""
        return False

    def model_status(self) -> ModelStatus:
        """Fixed scores behave like an untrained model."""
        return ModelStatus(source=ModelSource.COLD_START)

    def train(self) -> TrainingOutcome:
        """Fixed scores are never trained."""
        return TrainingOutcome(status=TrainingStatus.SKIPPED)


class StubRDFMapper(RDFMapper):
    """
    RDFMapper stub for unit testing.

    Returns a pre-configured Mention without performing any RDF parsing.
    Optionally raises a configured exception to test error paths.
    """

    def __init__(
        self,
        mention_to_return: Mention = None,
        error: Exception = None,
    ):
        self._mention = mention_to_return or Mention(
            id=MentionId(value="stub-mention-id"),
            attributes={"legal_name": "Stub Corp", "country_code": "US"},
        )
        self._error = error

    def map_entity_mention_to_domain(self, entity_mention: EntityMention) -> Mention:
        if self._error is not None:
            raise self._error
        return self._mention
