"""Domain model: named, typed concepts for entity resolution."""

from .ids import ClusterId, MentionId
from .mention import Mention
from .similarity import MentionLink
from .cluster import CandidateCluster, ClusterMembership, ResolutionResult
from .state import ResolverState

__all__ = [
    "MentionId",
    "ClusterId",
    "Mention",
    "MentionLink",
    "ClusterMembership",
    "CandidateCluster",
    "ResolutionResult",
    "ResolverState",
]
