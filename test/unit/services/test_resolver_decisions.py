"""Unit tests: resolver decisions on stored links, candidates and start-up training (services).

Spec: candidate-scoring — "Only the best links are stored"; resolution-model-lifecycle — "Trained model is persisted
and reloaded" (start-up training decision).
"""

from unittest.mock import MagicMock

import pytest

from ere.models.resolver import (
    ClusterId,
    ClusterMembership,
    LinkTable,
    Mention,
    MentionId,
)
from ere.services.entity_resolution_service import EntityResolver
from ere.services.model_lifecycle import train_on_start_if_due
from ere.services.resolver_config import ResolverConfig
from test.unit.adapters.stubs import (
    InMemoryClusterRepository,
    InMemoryMentionRepository,
    InMemorySimilarityRepository,
)

NEW = "new"


class TableLinker:
    """Returns a fixed link table for the next bite."""

    def __init__(self, rights: list[str], scores: list[float]):
        self._table = LinkTable(
            left_ids=(NEW,) * len(rights), right_ids=tuple(rights), scores=tuple(scores)
        )

    def find_matches_batch(self, mentions):  # pylint: disable=unused-argument
        return self._table

    def register_mention(self, mention):  # pylint: disable=unused-argument
        return None

    def needs_training(self):
        return False

    def train(self):
        return None


def _resolver(
    clusters: dict[str, str], linker, top_n: int
) -> tuple[EntityResolver, InMemorySimilarityRepository]:
    cluster_repo = InMemoryClusterRepository()
    for mention_id, cluster_id in clusters.items():
        cluster_repo.save(
            ClusterMembership(
                mention_id=MentionId(value=mention_id),
                cluster_id=ClusterId(value=cluster_id),
            )
        )
    similarity_repo = InMemorySimilarityRepository()
    resolver = EntityResolver(
        mention_repo=InMemoryMentionRepository(),
        similarity_repo=similarity_repo,
        cluster_repo=cluster_repo,
        linker=linker,
        config=ResolverConfig(
            threshold=0.99,
            match_weight_threshold=-10,
            top_n=top_n,
            entity_fields=["legal_name"],
            auto_train_threshold=0,
        ),
    )
    return resolver, similarity_repo


def _new_mention() -> Mention:
    return Mention(id=MentionId(value=NEW), attributes={"legal_name": "New"})


def test_candidates_include_clusters_reached_only_by_links_below_the_stored_top_n():
    # the 4 best links all point into 2 clusters; weaker links reach 3 more clusters
    rights = ["h1", "h2", "h3", "h4", "w1", "w2", "w3"]
    scores = [0.9, 0.89, 0.88, 0.87, 0.5, 0.4, 0.3]
    clusters = {
        "h1": "hot1",
        "h2": "hot1",
        "h3": "hot2",
        "h4": "hot2",
        "w1": "warm1",
        "w2": "warm2",
        "w3": "warm3",
    }
    resolver, similarity_repo = _resolver(
        clusters, TableLinker(rights, scores), top_n=4
    )

    result = resolver.resolve(_new_mention())

    assert [c.cluster_id.value for c in result.candidates] == [
        "hot1",
        "hot2",
        "warm1",
        "warm2",
    ]
    assert sorted(
        link.right_id.value for link in similarity_repo.find_for(MentionId(value=NEW))
    ) == ["h1", "h2", "h3", "h4"]


def test_ties_at_the_cut_off_still_store_exactly_top_n_links():
    rights = [f"m{index}" for index in range(6)]
    resolver, similarity_repo = _resolver(
        {r: r for r in rights}, TableLinker(rights, [0.5] * 6), top_n=4
    )

    resolver.resolve(_new_mention())

    assert len(similarity_repo.find_for(MentionId(value=NEW))) == 4


@pytest.mark.parametrize(
    "threshold, stored, needs_training, trains",
    [
        (200, 250, True, True),  # past threshold without a model → train once at start
        (200, 250, False, False),  # model already persisted or trained
        (200, 199, True, False),  # threshold not reached yet
        (0, 5000, True, False),  # training disabled
    ],
)
def test_start_up_training_decision(threshold, stored, needs_training, trains):
    linker = MagicMock()
    linker.needs_training.return_value = needs_training

    train_on_start_if_due(linker, stored, threshold)

    assert linker.train.called is trains
