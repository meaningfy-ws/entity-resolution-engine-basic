"""Main service layer: entity resolution resolver and public API service."""

import heapq
import logging
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime, timezone

from erspec.models.core import ClusterReference, EntityMention
from erspec.models.ere import (
    EntityMentionResolutionRequest,
    EntityMentionResolutionResponse,
    EREErrorResponse,
    ERERequest,
    EREResponse,
)

from ere.models.exceptions import ConflictError
from ere.models.ports.linker import SimilarityLinker
from ere.models.ports.rdf_mapper import RDFMapper
from ere.models.ports.repositories import (
    ClusterRepository,
    MentionRepository,
    SimilarityRepository,
)
from ere.models.ports.resolver import AbstractResolver
from ere.models.resolver import (
    CandidateCluster,
    ClusterId,
    ClusterMembership,
    LinkTable,
    Mention,
    MentionId,
    MentionLink,
    ResolutionResult,
    ResolverState,
    TrainingOutcome,
)
from ere.services.model_lifecycle import log_training_outcome
from ere.services.resolver_config import ResolverConfig
from ere.services.tracing import (
    ClusterDecision,
    DeferredStage,
    GuardOutcome,
    MentionTrace,
    TraceEvent,
    TraceField,
)

log = logging.getLogger(__name__)

UnitOfWork = Callable[[], AbstractContextManager]

REQUEST_ID_FIELD = "ere_request_id"
UNKNOWN_REQUEST_ID = "unknown"
ERROR_UNSUPPORTED_REQUEST = "UnsupportedRequestType"
ERROR_INTERNAL = "InternalError"


class EntityResolver:
    """
    Core entity resolution algorithm: orchestration of domain objects via ports.

    The resolver implements the entity resolution algorithm using only domain types
    and port interfaces. This enables testing with in-memory stubs and swapping
    infrastructure without changing algorithm logic.

    The resolver is stateless - all state is held in repositories and the linker.
    """

    def __init__(  # pylint: disable=too-many-positional-arguments,too-many-arguments  # 5 domain dependencies + transaction boundary; extracting a container would obscure intent
        self,
        mention_repo: MentionRepository,
        similarity_repo: SimilarityRepository,
        cluster_repo: ClusterRepository,
        linker: SimilarityLinker,
        config: ResolverConfig,
        unit_of_work: UnitOfWork | None = None,
    ):
        """
        Initialize the entity resolution service.

        Args:
            mention_repo: Repository for persisting mentions.
            similarity_repo: Repository for persisting mention-links (similarities).
            cluster_repo: Repository for persisting cluster membership.
            linker: Port for pairwise similarity scoring (e.g. Splink).
            config: Resolver configuration (threshold, top_n, etc.).
            unit_of_work: Transaction boundary for one bite; None means no transaction management.
        """
        self._mention_repo = mention_repo
        self._similarity_repo = similarity_repo
        self._cluster_repo = cluster_repo
        self._linker = linker
        self._config = config
        self._unit_of_work = unit_of_work or nullcontext

    # -----------------------------------------------------------------------
    # Core algorithm
    # -----------------------------------------------------------------------

    def resolve(
        self, mention: Mention, trace: MentionTrace | None = None
    ) -> ResolutionResult:
        """
        Resolve one mention: a bite of one (see `resolve_batch`).

        Args:
            mention: The Mention to resolve.
            trace: Lifecycle trace of the enclosing request; disabled when omitted.

        Returns:
            ResolutionResult: Non-empty, ranked list of CandidateCluster objects,
                            pruned to top-N. The first entry is the algorithm's
                            implied best cluster for this mention.
        """
        return self.resolve_batch([mention], [trace] if trace else None)[0]

    def resolve_batch(
        self, mentions: list[Mention], traces: list[MentionTrace] | None = None
    ) -> list[ResolutionResult]:
        """
        Resolve a bite of new mentions in arrival order, all-or-nothing, in one unit of work.

        Flow (one transaction):
          1. Store the bite's mentions, so they are part of the search space.
          2. Score the whole bite in one linker call.
          3. For each mention in arrival order: keep links to stored mentions and to earlier bite
             members, join the best-scoring cluster if it reaches the threshold (greedy online,
             order matters), rank candidates from all kept links.
          4. Store each mention's best `top_n` links and all cluster assignments.

        Trace events are emitted only after the commit. If anything fails, the transaction is rolled
        back, no events are emitted and the exception propagates (callers decide how to fall back).

        Returns:
            One ResolutionResult per mention, in input order.
        """
        if not mentions:
            return []
        traces = traces or [MentionTrace.disabled() for _ in mentions]
        stored_before = self._mention_repo.count()
        stages: list[DeferredStage] = []
        with self._unit_of_work():
            results = self._resolve_bite(mentions, traces, stages)
        for stage in stages:
            stage.emit()
        for trace in traces:
            trace.stage(TraceEvent.PERSISTED)
        self._maybe_start_training(stored_before, stored_before + len(mentions))
        return results

    def _resolve_bite(
        self,
        mentions: list[Mention],
        traces: list[MentionTrace],
        stages: list["DeferredStage"],
    ) -> list[ResolutionResult]:
        for mention in mentions:
            self._mention_repo.save(mention)
            self._linker.register_mention(mention)
        links_by_mention = self._links_to_earlier(
            mentions, self._linker.find_matches_batch(mentions)
        )
        bite_ids = {mention.id for mention in mentions}
        linked_clusters = self._cluster_repo.clusters_for(
            {link.right_id for links in links_by_mention.values() for link in links}
            - bite_ids
        )

        results, kept_links, bite_clusters = [], [], {}
        for mention, trace in zip(mentions, traces):
            links = links_by_mention[mention.id]
            cluster_id = self._assign_cluster(
                mention, links, linked_clusters, trace=trace, stages=stages
            )
            bite_clusters[mention.id] = cluster_id
            linked_clusters[mention.id] = cluster_id
            results.append(
                self._rank_candidates(mention.id, cluster_id, links, linked_clusters)
            )
            kept_links.extend(self._best_links(links))

        self._similarity_repo.save_table(_to_link_table(kept_links))
        for mention in mentions:
            self._cluster_repo.save(
                ClusterMembership(
                    mention_id=mention.id, cluster_id=bite_clusters[mention.id]
                )
            )
        return results

    @staticmethod
    def _links_to_earlier(
        mentions: list[Mention], table: LinkTable
    ) -> dict[MentionId, list[MentionLink]]:
        """Group links by new mention, keeping only links to stored mentions or earlier bite members."""
        arrival = {
            mention.id.value: position for position, mention in enumerate(mentions)
        }
        grouped: dict[MentionId, list[MentionLink]] = {
            mention.id: [] for mention in mentions
        }
        for left, right, score in zip(table.left_ids, table.right_ids, table.scores):
            if left == right or arrival.get(right, -1) > arrival[left]:
                continue
            left_id = MentionId(value=left)
            grouped[left_id].append(
                MentionLink(
                    left_id=left_id, right_id=MentionId(value=right), score=score
                )
            )
        return grouped

    def _assign_cluster(
        self,
        mention: Mention,
        links: list[MentionLink],
        linked_clusters: dict[MentionId, ClusterId],
        *,
        trace: MentionTrace,
        stages: list["DeferredStage"],
    ) -> ClusterId:
        """Greedy online assignment: join the best match's cluster if it reaches the threshold, else found one."""
        best_id, best_sim = self._find_best_match(links, mention.id)
        stages.append(
            DeferredStage(
                trace,
                TraceEvent.SCORED,
                {
                    TraceField.LINKS: len(links),
                    TraceField.BEST_SCORE: best_sim if links else None,
                },
            )
        )
        best_cluster = linked_clusters.get(best_id) if best_id is not None else None
        if best_cluster is not None and best_sim >= self._config.threshold:
            cluster_id, decision = best_cluster, ClusterDecision.JOIN  # ext
        else:
            cluster_id, decision = (
                ClusterId(value=mention.id.value),
                ClusterDecision.NEW,
            )  # newCl
        stages.append(
            DeferredStage(
                trace,
                TraceEvent.CLUSTERED,
                {
                    TraceField.DECISION: decision,
                    TraceField.CLUSTER_ID: cluster_id.value,
                },
            )
        )
        return cluster_id

    def _best_links(self, links: list[MentionLink]) -> list[MentionLink]:
        """The `top_n` highest-scoring links: all that later readers of stored links can use (DEC-19)."""
        return heapq.nlargest(self._config.top_n, links, key=lambda link: link.score)

    def _maybe_start_training(self, stored_before: int, stored_after: int) -> None:
        """Start the one-off background training when a bite moves the stored count across the threshold."""
        threshold = self._config.auto_train_threshold
        if threshold <= 0 or not stored_before < threshold <= stored_after:
            return
        log.info(
            "Auto-training triggered: %d mentions reached (threshold=%d). "
            "Starting background EM training thread. Scoring continues with current parameters.",
            stored_after,
            threshold,
        )
        threading.Thread(target=self.train, daemon=True, name="linker-training").start()

    def train(self) -> TrainingOutcome:
        """
        Train the linker model once (later requests are skipped) and log the outcome.

        Returns:
            The training outcome reported by the linker.
        """
        outcome = self._linker.train()
        log_training_outcome(outcome)
        return outcome

    def state(self) -> ResolverState:
        """
        Return a snapshot of the resolver's persisted state.

        Includes counts for all repositories and current cluster membership mapping.

        Returns:
            ResolverState: Immutable snapshot with mention/similarity/cluster counts
                         and full cluster membership mapping.
        """
        return ResolverState(
            mention_count=self._mention_repo.count(),
            similarity_count=self._similarity_repo.count(),
            cluster_count=self._cluster_repo.count(),
            cluster_membership=self._cluster_repo.get_all_memberships(),
        )

    def find_cluster_for(self, mention_id: MentionId) -> ResolutionResult | None:
        """
        Return stored resolution candidates for a mention, or None if not yet resolved.

        When a mention was already resolved, this re-runs _gen_cand() against the current
        state of the similarity table. If new mentions have since been added to the cluster,
        the returned scores reflect the updated state - which is the correct behavior for
        an idempotent re-query (the cluster assignment is unchanged, only scores may update).

        Used by resolution.py for idempotency: avoids re-running resolve() (which would add
        duplicate rows) while still returning a valid, current ResolutionResult.

        Args:
            mention_id: The MentionId to look up.

        Returns:
            ResolutionResult if the mention was found, None otherwise.
        """
        try:
            self._cluster_repo.find_cluster_of(mention_id)  # KeyError if not found
            return self._gen_cand(mention_id)
        except KeyError:
            return None

    def check_conflict(self, mention: Mention) -> None:
        """
        Raise ConflictError if the same mention_id exists with different attributes.

        Called before idempotency check to ensure that re-submissions with different
        content are rejected, even if cached in the cluster repo.

        Args:
            mention: The Mention being submitted for resolution.

        Raises:
            ConflictError: If mention_id already exists with different attributes.
        """
        existing = self._mention_repo.find_by_id(mention.id)
        if existing is not None and existing.attributes != mention.attributes:
            raise ConflictError(
                mention_id=mention.id.value,
                existing_attributes=existing.attributes,
                incoming_attributes=mention.attributes,
            )

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _find_best_match(
        self, links: list[MentionLink], mention_id: MentionId
    ) -> tuple[MentionId | None, float]:
        """
        Find the highest-scoring match from a list of mention-links.

        Returns:
            Tuple of (best_other_id, best_score). If links is empty,
            returns (None, 0.0).
        """
        if not links:
            return None, 0.0
        best = max(links, key=lambda l: l.score)
        return best.other(mention_id), best.score

    def _gen_cand(self, mention_id: MentionId) -> ResolutionResult:
        """
        Generate cluster references for an already-resolved mention from stored links (genCand).

        Used by the idempotent path. Reads this mention's links (indexed) and looks up the
        linked mentions' clusters in one bulk call.

        Args:
            mention_id: The MentionId to generate candidates for.

        Returns:
            ResolutionResult: Non-empty tuple of CandidateCluster objects,
                            sorted descending by score, pruned to top_n.
                            Always includes the mention's own cluster.
        """
        links = self._similarity_repo.find_for(mention_id)
        linked_clusters = self._cluster_repo.clusters_for(
            {link.other(mention_id) for link in links}
        )
        own_cluster_id = self._cluster_repo.find_cluster_of(mention_id)
        return self._rank_candidates(mention_id, own_cluster_id, links, linked_clusters)

    def _rank_candidates(
        self,
        mention_id: MentionId,
        own_cluster_id: ClusterId,
        links: list[MentionLink],
        linked_clusters: dict[MentionId, ClusterId],
    ) -> ResolutionResult:
        """
        Group links by the other mention's cluster, keep the max score per cluster, always include
        the mention's own cluster (score 0.0 if unlinked), sort descending and prune to top_n.
        """
        cluster_scores: dict[ClusterId, float] = {}
        for link in links:
            cid = linked_clusters.get(link.other(mention_id))
            if cid is not None:
                cluster_scores[cid] = max(cluster_scores.get(cid, 0.0), link.score)
        cluster_scores.setdefault(own_cluster_id, 0.0)

        sorted_pairs = sorted(cluster_scores.items(), key=lambda x: x[1], reverse=True)
        candidates = tuple(
            CandidateCluster(cluster_id=cid, score=score)
            for cid, score in sorted_pairs[: self._config.top_n]
        )
        return ResolutionResult(candidates=candidates)


def _to_link_table(links: list[MentionLink]) -> LinkTable:
    return LinkTable(
        left_ids=tuple(link.left_id.value for link in links),
        right_ids=tuple(link.right_id.value for link in links),
        scores=tuple(link.score for link in links),
    )


# -----------------------------------------------------------------------
# Public resolution API
# -----------------------------------------------------------------------


def prepare_mention(
    entity_mention: EntityMention,
    resolver: EntityResolver,
    mapper: RDFMapper,
    trace: MentionTrace | None = None,
) -> tuple[Mention, ResolutionResult | None]:
    """
    Map an entity mention to the domain and apply the conflict and idempotency guards.

    Returns:
        The mapped mention and, if it was already resolved, its current result (else None).

    Raises:
        ValueError: If RDF parsing fails or entity type is unknown.
        ConflictError: If the same mention was already resolved with different content.
    """
    trace = trace or MentionTrace.disabled()
    mention = mapper.map_entity_mention_to_domain(entity_mention)
    trace.stage(TraceEvent.PARSED, **{TraceField.MENTION_ID: mention.id.value})

    try:
        resolver.check_conflict(mention)
    except ConflictError:
        trace.stage(TraceEvent.GUARD, **{TraceField.GUARD: GuardOutcome.CONFLICT})
        raise

    cached = resolver.find_cluster_for(mention.id)
    outcome = GuardOutcome.IDEMPOTENT if cached is not None else GuardOutcome.NEW
    trace.stage(TraceEvent.GUARD, **{TraceField.GUARD: outcome})
    return mention, cached


def resolve_to_result(
    entity_mention: EntityMention,
    resolver: EntityResolver,
    mapper: RDFMapper,
    trace: MentionTrace | None = None,
) -> ResolutionResult:
    """
    Core resolution pipeline: RDF parsing -> domain mapping -> guards -> resolver resolution.

    Raises:
        ValueError: If RDF parsing fails or entity type is unknown.
        ConflictError: If the same mention was already resolved with different content.
    """
    mention, cached = prepare_mention(entity_mention, resolver, mapper, trace)
    return cached if cached is not None else resolver.resolve(mention, trace)


def resolve_entity_mention(
    entity_mention: EntityMention,
    resolver: EntityResolver = None,
    mapper: RDFMapper = None,
) -> ClusterReference:
    """
    Resolve an entity mention to a Cluster (public API - returns top candidate).

    Args:
        entity_mention: EntityMention with identifiedBy and content (Turtle RDF).
        resolver: EntityResolver instance. If None, raises ValueError.
                  (In tests, inject the fixture; in production, use build_entity_resolver() factory)
        mapper: RDFMapper implementation. If None, raises ValueError.
                (In tests, inject the fixture; in production, use build_rdf_mapper() factory)

    Returns:
        ClusterReference with cluster_id, confidence_score, similarity_score.

    Raises:
        ValueError: If RDF parsing fails, mapping fails, resolver/mapper is None, or entity type is unknown.
    """
    if resolver is None:
        raise ValueError(
            "resolver must be provided (inject EntityResolver fixture in tests, "
            "or use build_entity_resolver() factory in production)"
        )
    if mapper is None:
        raise ValueError(
            "mapper must be provided (inject RDFMapper fixture in tests, "
            "or use build_rdf_mapper() factory in production)"
        )

    cluster_ref = resolve_to_result(entity_mention, resolver, mapper)
    top = cluster_ref.top

    # For singleton founders (no prior mentions), top.score = 0.0.
    # 0.0 reflects genuine uncertainty: the cluster is unconfirmed (single member).
    return ClusterReference(
        cluster_id=top.cluster_id.value,
        confidence_score=top.score,
        similarity_score=top.score,
    )


# -----------------------------------------------------------------------
# Adapter resolver for pub/sub service
# -----------------------------------------------------------------------


class EntityResolutionService(AbstractResolver):
    """
    Public API service for entity resolution via pub/sub request/response.

    Handles EntityMentionResolutionRequest -> EntityMentionResolutionResponse.
    Returns EREErrorResponse for unknown request types or resolution errors.

    This service receives a pre-constructed resolver and mapper at initialization
    time, avoiding the cost of rebuilding them on every request.
    """

    def __init__(self, resolver: EntityResolver, mapper: RDFMapper):
        """
        Initialize the service with injected dependencies.

        Args:
            resolver: EntityResolver instance (pre-built core resolver).
            mapper: RDFMapper implementation (pre-built).
        """
        self._resolver = resolver
        self._mapper = mapper

    def process_request(
        self, request: ERERequest, trace: MentionTrace | None = None
    ) -> EREResponse:
        """
        Process one resolution request (a bite of one).

        Returns:
            EntityMentionResolutionResponse if request is EntityMentionResolutionRequest,
            EREErrorResponse for unknown request types or resolution errors.
        """
        return self.process_batch([request], [trace] if trace else None)[0]

    def process_batch(
        self, requests: list[ERERequest], traces: list[MentionTrace] | None = None
    ) -> list[EREResponse]:
        """
        Process a bite of requests: guards per request, one resolver bite for the new mentions.

        Returns:
            One response per request, in request order.
        """
        traces = traces or [MentionTrace.disabled() for _ in requests]
        responses: list[EREResponse | None] = [None] * len(requests)
        pending: list[tuple[int, Mention]] = []
        repeats: list[tuple[int, Mention]] = []
        first_seen: dict[MentionId, Mention] = {}

        for position, (request, trace) in enumerate(zip(requests, traces)):
            outcome = self._guard(request, trace, first_seen)
            if isinstance(outcome, tuple):
                (repeats if outcome[0] else pending).append((position, outcome[1]))
            else:
                responses[position] = outcome

        self._resolve_pending(requests, traces, pending, responses)
        for position, mention in repeats:
            responses[position] = self._respond_cached(requests[position], mention)
        return responses

    def _guard(
        self,
        request: ERERequest,
        trace: MentionTrace,
        first_seen: dict[MentionId, Mention],
    ) -> EREResponse | tuple[bool, Mention]:
        """A response for requests that end here, or (is_repeat_in_bite, mention) for those still to resolve."""
        if not isinstance(request, EntityMentionResolutionRequest):
            log.error("Unsupported request type: %s", type(request).__name__)
            return _error_response(
                getattr(request, REQUEST_ID_FIELD, UNKNOWN_REQUEST_ID),
                ERROR_UNSUPPORTED_REQUEST,
                "Unsupported request type",
                f"EntityResolutionService does not handle {type(request).__name__}",
            )
        try:
            mention, cached = prepare_mention(
                request.entity_mention, self._resolver, self._mapper, trace
            )
            earlier = first_seen.get(mention.id)
            if earlier is not None and earlier.attributes != mention.attributes:
                raise ConflictError(
                    mention.id.value, earlier.attributes, mention.attributes
                )
        except (ValueError, ConflictError) as exc:
            return self._controlled_error(request, exc)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return self._unexpected_error(request, exc)
        if cached is not None:
            return _resolution_response(request, cached)
        if earlier is not None:
            return True, mention
        first_seen[mention.id] = mention
        return False, mention

    def _resolve_pending(
        self,
        requests: list[ERERequest],
        traces: list[MentionTrace],
        pending: list[tuple[int, Mention]],
        responses: list[EREResponse | None],
    ) -> None:
        """Resolve new mentions as one bite; if the bite fails, resolve each on its own so failures stay isolated."""
        if not pending:
            return
        mentions = [mention for _, mention in pending]
        try:
            results = self._resolver.resolve_batch(
                mentions, [traces[position] for position, _ in pending]
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught  # any bite failure falls back per request
            if len(pending) == 1:
                position = pending[0][0]
                responses[position] = self._unexpected_error(requests[position], exc)
                return
            log.warning(
                "Bite of %d mentions failed (%s); resolving them one at a time",
                len(pending),
                type(exc).__name__,
            )
            for position, mention in pending:
                responses[position] = self._resolve_alone(
                    requests[position], mention, traces[position]
                )
            return
        for (position, _), resolution in zip(pending, results):
            responses[position] = _resolution_response(requests[position], resolution)

    def _resolve_alone(
        self, request: ERERequest, mention: Mention, trace: MentionTrace
    ) -> EREResponse:
        try:
            return _resolution_response(request, self._resolver.resolve(mention, trace))
        except (ValueError, ConflictError) as exc:
            return self._controlled_error(request, exc)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return self._unexpected_error(request, exc)

    def _respond_cached(self, request: ERERequest, mention: Mention) -> EREResponse:
        cached = self._resolver.find_cluster_for(mention.id)
        if cached is None:
            return self._unexpected_error(
                request, LookupError(f"no resolution stored for {mention.id.value}")
            )
        return _resolution_response(request, cached)

    @staticmethod
    def _controlled_error(request: ERERequest, exc: Exception) -> EREErrorResponse:
        # The exception text can carry mention attributes, so only its type is logged.
        log.error(  # NOSONAR - intentionally no traceback for controlled exceptions
            "Resolution error for request %s: %s",
            request.ere_request_id,
            type(exc).__name__,
        )
        return _error_response(
            request.ere_request_id, type(exc).__name__, "Resolution error", str(exc)
        )

    @staticmethod
    def _unexpected_error(request: ERERequest, exc: Exception) -> EREErrorResponse:
        # Exception text and tracebacks can quote mention data (DEC-10): type at ERROR, details at DEBUG only.
        log.error(
            "Unexpected error for request %s: %s",
            request.ere_request_id,
            type(exc).__name__,
        )
        log.debug(
            "Unexpected error details for request %s",
            request.ere_request_id,
            exc_info=exc,
        )
        return _error_response(
            request.ere_request_id,
            ERROR_INTERNAL,
            "Unexpected error",
            "An unexpected error occurred",
        )

    def __call__(self, request: ERERequest) -> EREResponse:
        """Make the service callable."""
        return self.process_request(request)


def _resolution_response(
    request: EntityMentionResolutionRequest, resolution: ResolutionResult
) -> EREResponse:
    candidates = [
        ClusterReference(
            cluster_id=c.cluster_id.value,
            confidence_score=c.score,
            similarity_score=c.score,
        )
        for c in resolution.candidates
    ]
    return EntityMentionResolutionResponse(
        entity_mention_id=request.entity_mention.identifiedBy,
        candidates=candidates,
        ere_request_id=request.ere_request_id,
        timestamp=datetime.now(timezone.utc),
    )


def _error_response(
    request_id: str, error_type: str, title: str, detail: str
) -> EREErrorResponse:
    return EREErrorResponse(
        ere_request_id=request_id,
        error_type=error_type,
        error_title=title,
        error_detail=detail,
        timestamp=datetime.now(timezone.utc),
    )
