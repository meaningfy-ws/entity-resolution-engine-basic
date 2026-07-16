"""Resolver configuration: typed extraction from YAML."""

from pydantic import BaseModel, ConfigDict, Field


class DuckDBConfig(BaseModel):
    """DuckDB database configuration."""

    type: str = "in-memory"  # "in-memory" or "persistent"
    path: str = (
        ":memory:"  # Database path: ":memory:" for in-memory, file path for persistent
    )


class ResolverConfig(BaseModel):
    """
    Typed resolver configuration extracted from YAML dict.

    Attributes:
        threshold: Cluster assignment probability cutoff (0.0-1.0).
                   A mention joins an existing cluster only if match_probability >= threshold.
        match_weight_threshold: Splink output pre-filter (log-odds).
                                Controls which scored pairs are stored in the similarities table.
                                -10 includes pairs with match_probability >= ~0.001.
        top_n: Maximum number of cluster references returned per resolution request.
        cache_strategy: Strategy for maintaining Splink search space cache.
                       Default: "tf_incremental" (incremental cache updates).
        auto_train_threshold: Number of mentions at which to trigger background training.
                             Default: 50 (0 = disabled).
        entity_fields: List of entity field names to extract from RDF (e.g. ["legal_name", "country_code"]).
                      Must match fields defined in rdf_mapping.yaml.
        duckdb: DuckDB database configuration (type and path).
               Default: in-memory database.
    """

    model_config = ConfigDict(frozen=True)

    threshold: float
    match_weight_threshold: float
    top_n: int
    entity_fields: list[str]
    cache_strategy: str = "tf_incremental"
    auto_train_threshold: int = 50
    duckdb: DuckDBConfig = Field(default_factory=DuckDBConfig)

    @classmethod
    def from_dict(cls, d: dict) -> "ResolverConfig":
        """
        Load configuration from YAML-parsed dict.

        Args:
            d: Dict with keys: threshold, match_weight_threshold, top_n, entity_fields,
                              cache_strategy (optional), auto_train_threshold (optional),
                              duckdb (optional).

        Returns:
            ResolverConfig instance.

        Raises:
            ValidationError: If required keys are missing or values are invalid.
        """
        duckdb_config_dict = d.get("duckdb", {})
        duckdb_config = DuckDBConfig(
            type=duckdb_config_dict.get("type", "in-memory"),
            path=duckdb_config_dict.get("path", ":memory:"),
        )

        return cls(
            threshold=d["threshold"],
            match_weight_threshold=d["match_weight_threshold"],
            top_n=d["top_n"],
            entity_fields=d["entity_fields"],
            cache_strategy=d.get("cache_strategy", "tf_incremental"),
            auto_train_threshold=d.get("auto_train_threshold", 50),
            duckdb=duckdb_config,
        )
