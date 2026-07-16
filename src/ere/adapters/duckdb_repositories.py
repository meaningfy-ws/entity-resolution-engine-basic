"""DuckDB-backed repository implementations for service layer."""

import duckdb
import pandas as pd

from ere.models.resolver import (
    ClusterId,
    ClusterMembership,
    Mention,
    MentionId,
    MentionLink,
)
from ere.adapters.repositories import (
    ClusterRepository,
    MentionRepository,
    SimilarityRepository,
)


class DuckDBMentionRepository(MentionRepository):
    """DuckDB-backed mention repository."""

    def __init__(self, con: duckdb.DuckDBPyConnection, entity_fields: list[str]):
        """
        Initialize with a DuckDB connection and entity field names.

        Args:
            con: DuckDB connection (must have mentions table already created).
            entity_fields: List of field names (e.g. ["legal_name", "country_code"]).
        """
        self._con = con
        self._entity_fields = entity_fields

    def save(self, mention: Mention) -> None:
        """
        Persist a mention to storage.

        Uses parameterized INSERT with dynamic columns based on entity_fields.
        """
        flat_dict = mention.to_flat_dict()
        # Extract mention_id and entity field values in order
        values = [flat_dict["mention_id"]] + [
            flat_dict.get(f) for f in self._entity_fields
        ]
        placeholders = ",".join(["?"] * (1 + len(self._entity_fields)))
        col_names = ",".join(["mention_id"] + self._entity_fields)

        self._con.execute(
            f"INSERT INTO mentions ({col_names}) VALUES ({placeholders})", values
        )

    def load_all(self) -> list[Mention]:
        """
        Retrieve all persisted mentions.

        Reconstructs Mention objects from flat database rows.
        """
        col_list = ", ".join(["mention_id"] + self._entity_fields)
        rows = self._con.execute(f"SELECT {col_list} FROM mentions").fetchall()
        mentions = []
        for row in rows:
            # Reconstruct flat dict: {"mention_id": ..., "legal_name": ..., ...}
            flat_dict = {"mention_id": row[0]}
            for i, field in enumerate(self._entity_fields):
                flat_dict[field] = row[i + 1]
            mentions.append(Mention(**flat_dict))
        return mentions

    def count(self) -> int:
        """Return the total number of mentions in storage."""
        row = self._con.execute("SELECT COUNT(*) FROM mentions").fetchone()
        return row[0]

    def find_by_id(self, mention_id: MentionId) -> Mention | None:
        """
        Retrieve a single mention by ID.

        Returns:
            The Mention object if found, None otherwise.
        """
        col_list = ", ".join(["mention_id"] + self._entity_fields)
        rows = self._con.execute(
            f"SELECT {col_list} FROM mentions WHERE mention_id = ?",
            [mention_id.value],
        ).fetchall()
        if not rows:
            return None
        row = rows[0]
        # Reconstruct flat dict: {"mention_id": ..., "legal_name": ..., ...}
        flat_dict = {"mention_id": row[0]}
        for i, field in enumerate(self._entity_fields):
            flat_dict[field] = row[i + 1]
        return Mention(**flat_dict)


class DuckDBSimilarityRepository(SimilarityRepository):
    """DuckDB-backed similarity repository."""

    def __init__(self, con: duckdb.DuckDBPyConnection):
        """
        Initialize with a DuckDB connection.

        Args:
            con: DuckDB connection (must have similarities table already created).
        """
        self._con = con

    def save_all(self, links: list[MentionLink]) -> None:
        """
        Persist multiple mention-links using vectorized INSERT.

        Skips if empty. Uses pandas DataFrame + temporary table registration for efficiency
        (DuckDB optimizes vectorized INSERT SELECT operations).
        """
        if not links:
            return

        # Build DataFrame with columns: mention_id_l, mention_id_r, match_probability
        rows = [
            {
                "mention_id_l": link.left_id.value,
                "mention_id_r": link.right_id.value,
                "match_probability": link.score,
            }
            for link in links
        ]
        df = pd.DataFrame(rows)

        # Register DataFrame as temporary table and insert (optimized by DuckDB for vectorized operations)
        self._con.register("df_temp", df)
        self._con.execute("INSERT INTO similarities SELECT * FROM df_temp")

    def count(self) -> int:
        """Return the total number of mention-links in storage."""
        row = self._con.execute("SELECT COUNT(*) FROM similarities").fetchone()
        return row[0]

    def find_for(self, mention_id: MentionId) -> list[MentionLink]:
        """
        Retrieve all mention-links involving the given mention.

        Returns all links where this mention appears on either side
        (left_id or right_id).
        """
        mention_id_str = mention_id.value
        rows = self._con.execute(
            """
            SELECT mention_id_l, mention_id_r, match_probability
            FROM similarities
            WHERE mention_id_l = ? OR mention_id_r = ?
            """,
            [mention_id_str, mention_id_str],
        ).fetchall()

        links = []
        for left_str, right_str, score in rows:
            links.append(
                MentionLink(
                    left_id=MentionId(value=left_str),
                    right_id=MentionId(value=right_str),
                    score=score,
                )
            )
        return links


class DuckDBClusterRepository(ClusterRepository):
    """DuckDB-backed cluster repository."""

    def __init__(self, con: duckdb.DuckDBPyConnection):
        """
        Initialize with a DuckDB connection.

        Args:
            con: DuckDB connection (must have clusters table already created).
        """
        self._con = con

    def save(self, membership: ClusterMembership) -> None:
        """Persist a cluster membership assignment."""
        self._con.execute(
            "INSERT INTO clusters VALUES (?, ?)",
            [membership.mention_id.value, membership.cluster_id.value],
        )

    def find_cluster_of(self, mention_id: MentionId) -> ClusterId:
        """
        Look up the cluster a mention belongs to.

        Raises KeyError if the mention has no cluster assignment.
        """
        row = self._con.execute(
            "SELECT cluster_id FROM clusters WHERE mention_id = ?",
            [mention_id.value],
        ).fetchone()

        if row is None:
            raise KeyError(f"No cluster assignment for mention {mention_id}")

        return ClusterId(value=row[0])

    def count(self) -> int:
        """Return the total number of distinct clusters in storage."""
        row = self._con.execute(
            "SELECT COUNT(DISTINCT cluster_id) FROM clusters"
        ).fetchone()
        return row[0]

    def get_all_memberships(self) -> dict[ClusterId, list[MentionId]]:
        """
        Retrieve the full cluster membership mapping.

        Returns dict mapping ClusterId -> list of MentionIds in that cluster,
        sorted for determinism.
        """
        rows = self._con.execute(
            """
            SELECT cluster_id, array_agg(mention_id ORDER BY mention_id) AS members
            FROM clusters
            GROUP BY cluster_id
            ORDER BY cluster_id
            """
        ).fetchall()

        memberships: dict[ClusterId, list[MentionId]] = {}
        for cluster_id_str, members_array in rows:
            cluster_id = ClusterId(value=cluster_id_str)
            member_ids = [MentionId(value=m) for m in members_array]
            memberships[cluster_id] = member_ids

        return memberships
