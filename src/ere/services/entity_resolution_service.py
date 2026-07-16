"""Main service layer: entity resolution resolver and public API service."""

import logging
import threading
from datetime import datetime, timezone

from erspec.models.core import ClusterReference, EntityMention
from erspec.models.ere import (
    EntityMentionResolutionRequest,
    EntityMentionResolutionResponse,
    EREErrorResponse,
    ERERequest,
    EREResponse,
)

from ere.adapters import AbstractResolver
from ere.adapters.rdf_mapper_port import RDFMapper
from ere.adapters.repositories import (
    ClusterRepository,
    MentionRepository,
    SimilarityRepository,
)
from ere.models.exceptions import ConflictError
from ere.models.resolver import (
    CandidateCluster,
    ClusterId,
    ClusterMembership,
    Mention,
    MentionId,
    MentionLink,
    ResolutionResult,
    ResolverState,
)
from ere.services.resolver_config import ResolverConfig
from ere.services.linker import SimilarityLinker

log = logging.getLogger(__name__)


class EntityResolver:
    """
    Core entity resolution algorithm: orchestration of domain objects via ports.

    The resolver implements the entity resolution algorithm using only domain types
    and port interfaces. This enables testing with in-memory stubs and swapping
    infrastructure without changing algorithm logic.

    The resolver is stateless - all state is held in repositories and the linker.
    """

    def __init__(  # pylint: disable=too-many-positional-arguments  # 5 domain dependencies; extracting a container would obscure intent
        self,
        mention_repo: MentionRepository,
        similarity_repo: SimilarityRepository,
        cluster_repo: ClusterRepository,
        linker: SimilarityLinker,
        config: ResolverConfig,
    ):
        """
        Initialize the entity resolution service.

        Args:
            mention_repo: Repository for persisting mentions.
            similarity_repo: Repository for persisting mention-links (similarities).
            cluster_repo: Repository for persisting cluster membership.
            linker: Port for pairwise similarity scoring (e.g. Splink).
            config: Resolver configuration (threshold, top_n, etc.).
        """
        self._mention_repo = mention_repo
        self._similarity_repo = similarity_repo
        self._cluster_repo = cluster_repo
        self._linker = linker
        self._config = config

    # -----------------------------------------------------------------------
    # Core algorithm
    # -----------------------------------------------------------------------

    def resolve(self, mention: Mention) -> ResolutionResult:
        """
        Resolve a mention: score against existing mentions, assign to a cluster,
        and return ranked cluster references.

        Implements the resolution flow:
          1. Score new mention against existing search space via linker.
          2. Persist all pairwise scores (mention-link graph).
          3. Assign to best-matching cluster (ext) or create singleton (newCl).
          4. Insert mention into search space and repositories.
          5. Return genCand output: ranked (cluster_id, score) pairs.

        Args:
            mention: The Mention to resolve.

        Returns:
            ResolutionResult: Non-empty, ranked list of CandidateCluster objects,
                            pruned to top-N. The first entry is the algorithm's
                            implied best cluster for this mention.
        """
        # Step 1: Score mention against existing search space.
        # The linker sees mention as-is (not yet persisted), so it can find
        # matches without the mention being in the mentions table yet.
        links = self._linker.find_matches(mention)

        # Step 2: Persist all pairwise scores into the similarities repository.
        if links:
            self._similarity_repo.save_all(links)

        # Step 3: Cluster assignment - greedy online, best match only, threshold-gated.
        # This implements the greedy online clustering approach: the incoming mention
        # is compared only against existing records, and it joins the cluster of the
        # single best-scoring match (if that score meets the threshold). No
        # retrospective re-clustering is performed.
        #
        # Consequence: order of arrival matters. This is the fundamental trade-off
        # of the online greedy approach.
        best_id, best_sim = self._find_best_match(links, mention.id)

        if best_id is not None and best_sim >= self._config.threshold:
            # ext: join the cluster of the best match
            cluster_id = self._cluster_repo.find_cluster_of(best_id)
            log.trace(
                "Mention %s assigned to cluster %s (similarity score=%.4f)",
                mention.id.value,
                cluster_id.value,
                best_sim,
            )
        else:
            # newCl: create a new singleton cluster with this mention's ID
            cluster_id = ClusterId(value=mention.id.value)
            log.trace("New cluster generated for mention with id=%s", mention.id.value)

        self._cluster_repo.save(
            ClusterMembership(mention_id=mention.id, cluster_id=cluster_id)
        )

        # Log cluster contents after assignment
        all_memberships = self._cluster_repo.get_all_memberships()
        cluster_members = all_memberships.get(cluster_id, [])
        member_ids = ", ".join([m.value for m in cluster_members])
        log.trace(
            "Cluster %s now contains %d mentions: %s",
            cluster_id.value,
            len(cluster_members),
            member_ids,
        )

        # Step 4: Persist mention and update the linker's search space.
        self._mention_repo.save(mention)
        self._linker.register_mention(mention)

        # Trigger auto-training if threshold is reached (non-blocking background thread).
        count = self._mention_repo.count()
        if (
            self._config.auto_train_threshold > 0
            and count == self._config.auto_train_threshold
        ):
            log.info(
                "Auto-training triggered: %d mentions reached (threshold=%d). "
                "Starting background EM training thread. Scoring continues with current parameters.",
                count,
                self._config.auto_train_threshold,
            )
            threading.Thread(
                target=self._linker.train, daemon=True, name="linker-training"
            ).start()

        # Step 5: Return cluster references (non-empty, always top-N).
        return self._gen_cand(mention.id)

    def train(self) -> None:
        """
        Train the linker model (estimate parameters via EM or other algorithm).

        Safe to call multiple times (retraining is idempotent).
        The linker handles insufficient data gracefully (uses cold-start defaults).
        """
        self._linker.train()

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
        Generate cluster references for a mention (genCand from algorithm).

        For each stored mention-link involving this mention, identify the other
        mention and look up its cluster. Group by cluster and take the maximum
        similarity as the cluster-level score. Always include the mention's own
        assigned cluster (with score 0.0 if no link to it exists). Sort descending,
        prune to top-N.

        The own cluster is always present because it is the algorithm's actual
        cluster assignment for the mention.

        Implementation note: This uses N+1 repository calls (one find_for(),
        then N cluster lookups). This is intentional for testability and
        separation of concerns. The DuckDB adapter can optimize by
        overriding the same port contract with a single SQL JOIN; the service
        sees no difference.

        Args:
            mention_id: The MentionId to generate candidates for.

        Returns:
            ResolutionResult: Non-empty tuple of CandidateCluster objects,
                            sorted descending by score, pruned to top_n.
                            Always includes the mention's own cluster.
        """
        links = self._similarity_repo.find_for(mention_id)

        # Group by cluster and take the max score per cluster
        cluster_scores: dict[ClusterId, float] = {}
        for link in links:
            other_id = link.other(mention_id)
            cid = self._cluster_repo.find_cluster_of(other_id)
            cluster_scores[cid] = max(cluster_scores.get(cid, 0.0), link.score)

        # Always include the mention's own assigned cluster
        own_cluster_id = self._cluster_repo.find_cluster_of(mention_id)
        cluster_scores.setdefault(own_cluster_id, 0.0)

        # Sort by score (descending), prune to top_n, build candidates
        sorted_pairs = sorted(cluster_scores.items(), key=lambda x: x[1], reverse=True)
        candidates = tuple(
            CandidateCluster(cluster_id=cid, score=score)
            for cid, score in sorted_pairs[: self._config.top_n]
        )

        return ResolutionResult(candidates=candidates)


# -----------------------------------------------------------------------
# Public resolution API
# -----------------------------------------------------------------------


def resolve_to_result(
    entity_mention: EntityMention,
    resolver: EntityResolver,
    mapper: RDFMapper,
) -> ResolutionResult:
    """
    Core resolution pipeline: RDF parsing -> domain mapping -> resolver resolution.

    Args:
        entity_mention: EntityMention from erspec.
        resolver: EntityResolver instance (core algorithm).
        mapper: RDFMapper implementation for entity mention parsing.

    Returns:
        ResolutionResult: Domain object with (cluster_id, score) candidates.

    Raises:
        ValueError: If RDF parsing fails or entity type is unknown.
    """
    mention = mapper.map_entity_mention_to_domain(entity_mention)

    # Log properties after RDF mapping
    log.trace(
        "Entity resolver will use the following properties of %s: %s",
        entity_mention.identifiedBy.request_id,
        mention.attributes,
    )

    # Conflict guard: same mention_id, different content → reject
    resolver.check_conflict(mention)

    # Idempotency: if already resolved, return current state
    cached = resolver.find_cluster_for(mention.id)
    if cached is not None:
        log.trace("Returning result for already resolved mention: %s", mention.id.value)
        return cached

    return resolver.resolve(mention)


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

    def process_request(self, request: ERERequest) -> EREResponse:
        """
        Process a resolution request and return a response.

        Args:
            request: ERERequest (could be EntityMentionResolutionRequest or other type).

        Returns:
            EntityMentionResolutionResponse if request is EntityMentionResolutionRequest,
            EREErrorResponse for unknown request types or resolution errors.
        """
        now = datetime.now(timezone.utc)

        if not isinstance(request, EntityMentionResolutionRequest):
            log.error(
                "Unsupported request type: %s",
                type(request).__name__,
            )
            return EREErrorResponse(
                ere_request_id=getattr(request, "ere_request_id", "unknown"),
                error_type="UnsupportedRequestType",
                error_title="Unsupported request type",
                error_detail=f"EntityResolutionService does not handle {type(request).__name__}",
                timestamp=now,
            )

        try:
            entity_mention = request.entity_mention
            entity_type = entity_mention.identifiedBy.entity_type
            log.trace(
                "Mention of type %s submitted for resolution: %s",
                entity_type,
                entity_mention.identifiedBy.request_id,
            )

            resolution_outcome = resolve_to_result(
                entity_mention, self._resolver, self._mapper
            )

            # Log resolution result with candidates
            candidate_info = [
                (c.cluster_id.value, c.score, c.score)
                for c in resolution_outcome.candidates
            ]
            log.trace(
                "Resolution result for mention %s: %s",
                entity_mention.identifiedBy.request_id,
                candidate_info,
            )

            candidates = [
                ClusterReference(
                    cluster_id=c.cluster_id.value,
                    confidence_score=c.score,
                    similarity_score=c.score,
                )
                for c in resolution_outcome.candidates
            ]
            return EntityMentionResolutionResponse(
                entity_mention_id=entity_mention.identifiedBy,
                candidates=candidates,
                ere_request_id=request.ere_request_id,
                timestamp=now,
            )
        except (ValueError, ConflictError) as exc:
            log.error(  # NOSONAR - intentionally no traceback for controlled exceptions
                "Resolution error for mention %s: %s",
                request.ere_request_id,
                exc,
            )
            return EREErrorResponse(
                ere_request_id=request.ere_request_id,
                error_type=type(exc).__name__,
                error_title="Resolution error",
                error_detail=str(exc),
                timestamp=now,
            )
        except Exception:  # pylint: disable=broad-exception-caught
            log.exception(
                "Unexpected error for mention %s",
                request.ere_request_id,
            )
            return EREErrorResponse(
                ere_request_id=request.ere_request_id,
                error_type="InternalError",
                error_title="Unexpected error",
                error_detail="An unexpected error occurred",
                timestamp=now,
            )

    def __call__(self, request: ERERequest) -> EREResponse:
        """Make the service callable."""
        return self.process_request(request)
