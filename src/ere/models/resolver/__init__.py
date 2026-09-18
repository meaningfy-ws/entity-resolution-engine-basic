"""Domain model: named, typed concepts for entity resolution."""

from .blocking import BlockingRule, BlockingSettings, EqualityRule, NameSimilarityRule
from .cluster import CandidateCluster, ClusterMembership, ResolutionResult
from .ids import ClusterId, MentionId
from .mention import Mention
from .model_state import ModelSource, ModelStatus, TrainingOutcome, TrainingStatus
from .similarity import LinkTable, MentionLink
from .state import ResolverState

__all__ = [
    "ModelSource",
    "ModelStatus",
    "TrainingOutcome",
    "TrainingStatus",
    "BlockingRule",
    "BlockingSettings",
    "EqualityRule",
    "NameSimilarityRule",
    "MentionId",
    "ClusterId",
    "Mention",
    "MentionLink",
    "LinkTable",
    "ClusterMembership",
    "CandidateCluster",
    "ResolutionResult",
    "ResolverState",
]
