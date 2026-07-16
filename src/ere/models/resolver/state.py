"""Resolver state domain model: introspection snapshot."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .ids import ClusterId, MentionId


class ResolverState(BaseModel):
    """
    Introspection snapshot returned by EntityResolver.state().

    Provides high-level counts and detailed cluster membership mapping.
    """

    model_config = ConfigDict(frozen=True)

    mention_count: int
    similarity_count: int
    cluster_count: int
    cluster_membership: dict[ClusterId, list[MentionId]]

    def as_dict(self) -> dict:
        """
        Return backward-compatible dict form.

        Current state() API contract:
        {
            "mentions": int,
            "similarities": int,
            "clusters": int,
            "cluster_membership": {cluster_id_str: [mention_id_str, ...], ...}
        }
        """
        return {
            "mentions": self.mention_count,
            "similarities": self.similarity_count,
            "clusters": self.cluster_count,
            "cluster_membership": {
                k.value: [m.value for m in v]
                for k, v in self.cluster_membership.items()
            },
        }
