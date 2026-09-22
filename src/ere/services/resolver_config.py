"""Resolver configuration: typed extraction from resolver.yaml (no environment access, no adapter types)."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ere.models.resolver import BlockingSettings

DEFAULT_AUTO_TRAIN_THRESHOLD = 200


class DuckDBConfig(BaseModel):
    """DuckDB database configuration from resolver.yaml; unset values fall back to env or defaults."""

    type: str | None = None  # "in-memory" or "persistent"; None means not configured
    path: str | None = None  # database file for persistent storage


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
        auto_train_threshold: Number of mentions at which training runs once; the model is then frozen.
                             Default: 200 (0 = disabled).
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
    auto_train_threshold: int = DEFAULT_AUTO_TRAIN_THRESHOLD
    duckdb: DuckDBConfig = Field(default_factory=DuckDBConfig)
    blocking: BlockingSettings | None = (
        None  # None only for configurations without a `splink` section (tests)
    )

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
            type=duckdb_config_dict.get("type"),
            path=duckdb_config_dict.get("path"),
        )

        return cls(
            threshold=d["threshold"],
            match_weight_threshold=d["match_weight_threshold"],
            top_n=d["top_n"],
            entity_fields=d["entity_fields"],
            cache_strategy=d.get("cache_strategy", "tf_incremental"),
            auto_train_threshold=d.get(
                "auto_train_threshold", DEFAULT_AUTO_TRAIN_THRESHOLD
            ),
            duckdb=duckdb_config,
            blocking=_blocking_from(d),
        )


class SplinkConfigKey(StrEnum):
    """Keys of the `splink` section of resolver.yaml read by the resolver configuration."""

    SECTION = "splink"
    BLOCKING_RULES = "blocking_rules"
    NORMALISED_FIELDS = "normalised_fields"
    EM_BLOCKING_FIELD = "em_blocking_field"


def _blocking_from(d: dict) -> BlockingSettings | None:
    """Validated blocking settings from the `splink` section; refuses start-up on any invalid rule or field."""
    splink = d.get(SplinkConfigKey.SECTION)
    if splink is None:
        return None
    optional = {
        key: splink[config_key]
        for key, config_key in (
            ("normalised_fields", SplinkConfigKey.NORMALISED_FIELDS),
            ("em_blocking_field", SplinkConfigKey.EM_BLOCKING_FIELD),
        )
        if config_key in splink
    }
    return BlockingSettings.from_config(
        splink.get(SplinkConfigKey.BLOCKING_RULES, []),
        entity_fields=d["entity_fields"],
        **optional,
    )


class BatchSettings(BaseModel):
    """How much work one bite may hold and how long a draining queue is waited for."""

    model_config = ConfigDict(frozen=True)

    target_seconds: float = Field(default=2.0, gt=0)
    max_mentions: int = Field(default=500, gt=0)
    max_bytes: int = Field(default=50_000_000, gt=0)
    linger_ms: int = Field(default=250, ge=0)
