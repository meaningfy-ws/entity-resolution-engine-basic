"""Composition root: reads the environment and configuration files and wires concrete adapters into services.

Only entrypoints know concrete adapter classes; services receive ports.
"""

import logging
import math
import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

import yaml

from ere.adapters.duckdb_connection import (
    TEMP_DIRECTORY_SUFFIX,
    DuckDBSettings,
    DuckDBStorage,
    open_duckdb,
)
from ere.adapters.duckdb_repositories import (
    DuckDBClusterRepository,
    DuckDBMentionRepository,
    DuckDBSimilarityRepository,
)
from ere.adapters.duckdb_schema import OutdatedSchemaError, init_schema
from ere.adapters.duckdb_unit_of_work import DuckDBUnitOfWork
from ere.adapters.rdf_mapper_impl import TurtleRDFMapper
from ere.adapters.splink_linker_impl import SpLinkSimilarityLinker
from ere.models.ports.rdf_mapper import RDFMapper
from ere.models.resolver import BlockingSettings
from ere.services.entity_resolution_service import (
    EntityResolutionService,
    EntityResolver,
)
from ere.services.model_lifecycle import log_model_status, train_on_start_if_due
from ere.services.resolver_config import (
    BatchSettings,
    DuckDBConfig,
    ResolverConfig,
    SplinkConfigKey,
)

log = logging.getLogger(__name__)

DEFAULT_DUCKDB_PATH = "data/app.duckdb"
MODEL_FILE_SUFFIX = ".splink_model.json"


class DuckDBType(StrEnum):
    """Database type values accepted in resolver.yaml (`duckdb.type`)."""

    IN_MEMORY = "in-memory"
    PERSISTENT = "persistent"


YAML_TYPE_TO_STORAGE = {
    DuckDBType.IN_MEMORY: DuckDBStorage.MEMORY,
    DuckDBType.PERSISTENT: DuckDBStorage.DISK,
}


class DuckDBEnvVar(StrEnum):
    """Environment variables that override the DuckDB section of resolver.yaml."""

    STORAGE = "ERE_DUCKDB_STORAGE"
    PATH = "DUCKDB_PATH"
    MEMORY_LIMIT = "ERE_DUCKDB_MEMORY_LIMIT"
    THREADS = "ERE_DUCKDB_THREADS"
    TEMP_DIR = "ERE_DUCKDB_TEMP_DIR"
    MODEL_PATH = "ERE_SPLINK_MODEL_PATH"


def _storage_from(config: DuckDBConfig, env: Mapping[str, str]) -> DuckDBStorage:
    env_value = env.get(DuckDBEnvVar.STORAGE)
    if env_value is not None:
        try:
            return DuckDBStorage(env_value)
        except ValueError as exc:
            allowed = ", ".join(storage.value for storage in DuckDBStorage)
            raise ValueError(
                f"Invalid {DuckDBEnvVar.STORAGE.value}={env_value!r}; allowed values: {allowed}"
            ) from exc
    if config.type is None:
        return DuckDBStorage.DISK
    try:
        return YAML_TYPE_TO_STORAGE[DuckDBType(config.type)]
    except ValueError as exc:
        raise ValueError(
            f"Invalid duckdb type: {config.type}. Must be 'in-memory' or 'persistent'."
        ) from exc


def _threads_from(env: Mapping[str, str]) -> int | None:
    env_value = env.get(DuckDBEnvVar.THREADS)
    if env_value is None:
        return None
    try:
        return int(env_value)
    except ValueError as exc:
        raise ValueError(
            f"Invalid {DuckDBEnvVar.THREADS.value}={env_value!r}; expected a positive integer"
        ) from exc


def resolve_duckdb_settings(
    config: DuckDBConfig, env: Mapping[str, str]
) -> DuckDBSettings:
    """Resolve DuckDB settings with precedence: environment > resolver.yaml > defaults (on-disk)."""
    storage = _storage_from(config, env)
    path = env.get(DuckDBEnvVar.PATH) or config.path or DEFAULT_DUCKDB_PATH
    temp_directory = env.get(DuckDBEnvVar.TEMP_DIR)
    if temp_directory is None and storage == DuckDBStorage.DISK:
        temp_directory = f"{path}{TEMP_DIRECTORY_SUFFIX}"
    return DuckDBSettings(
        storage=storage,
        path=path,
        memory_limit=env.get(DuckDBEnvVar.MEMORY_LIMIT),
        threads=_threads_from(env),
        temp_directory=temp_directory,
    )


def resolve_model_path(settings: DuckDBSettings, env: Mapping[str, str]) -> str | None:
    """Where the trained similarity model is persisted; `None` means it is not persisted."""
    env_value = env.get(DuckDBEnvVar.MODEL_PATH)
    if env_value:
        return env_value
    if settings.storage == DuckDBStorage.DISK:
        return f"{settings.path}{MODEL_FILE_SUFFIX}"
    return None


class BatchEnvVar(StrEnum):
    """Environment variables sizing the bites taken from the request queue (DEC-11)."""

    TARGET_SECONDS = "ERE_BATCH_TARGET_SECONDS"
    MAX_MENTIONS = "ERE_BATCH_MAX_MENTIONS"
    MAX_BYTES = "ERE_BATCH_MAX_BYTES"
    LINGER_MS = "ERE_BATCH_LINGER_MS"


# variable → (settings field, parser, smallest allowed value, whether the smallest value itself is allowed)
_BATCH_FIELDS = {
    BatchEnvVar.TARGET_SECONDS: ("target_seconds", float, 0, False),
    BatchEnvVar.MAX_MENTIONS: ("max_mentions", int, 0, False),
    BatchEnvVar.MAX_BYTES: ("max_bytes", int, 0, False),
    BatchEnvVar.LINGER_MS: ("linger_ms", int, 0, True),
}


def resolve_batch_settings(env: Mapping[str, str]) -> BatchSettings:
    """Batch settings from the environment; unset variables keep their defaults, invalid ones refuse to start."""
    values = {}
    for variable, (
        field_name,
        parse,
        minimum,
        minimum_allowed,
    ) in _BATCH_FIELDS.items():
        raw = env.get(variable)
        if raw is None:
            continue
        expected = f"a finite number {'>=' if minimum_allowed else '>'} {minimum}"
        try:
            value = parse(raw)
        except ValueError as exc:
            raise ValueError(
                f"Invalid {variable.value}={raw!r}: expected {expected}"
            ) from exc
        too_small = value < minimum if minimum_allowed else value <= minimum
        if not math.isfinite(value) or too_small:
            raise ValueError(f"Invalid {variable.value}={raw!r}: expected {expected}")
        values[field_name] = value
    return BatchSettings(**values)


def build_entity_resolver(
    entity_fields: list[str] = None,
    resolver_config_path: str | Path = None,
    duckdb_path: str = None,
) -> EntityResolver:
    """
    Factory: construct EntityResolver with all concrete adapter dependencies.

    This factory instantiates DuckDB repositories and Splink linker, wiring them
    together with configuration. The service layer never directly instantiates
    these concrete types; it receives them pre-built via dependency injection.

    Args:
        entity_fields: Field names for entity attributes (e.g. ["legal_name", "country_code"]).
                      If None, reads from resolver.yaml config.
        resolver_config_path: Path to resolver.yaml config file.
                             If None, uses default path.
        duckdb_path: Path to DuckDB file (overrides DUCKDB_PATH and resolver.yaml duckdb.path).
                    DuckDB storage, limits and model path follow resolve_duckdb_settings().

    Returns:
        Fully-constructed EntityResolver with DuckDB backend and Splink linker.
    """
    if resolver_config_path is None:
        config_path = Path(__file__).parent.parent.parent / "config" / "resolver.yaml"
    else:
        config_path = Path(resolver_config_path)

    with open(config_path, encoding="utf-8") as f:
        raw_config = yaml.safe_load(f)

    resolver_config = ResolverConfig.from_dict(raw_config)

    # Use entity_fields from config; parameter overrides config if provided
    if entity_fields is None:
        entity_fields = resolver_config.entity_fields

    env = dict(os.environ)
    if duckdb_path:
        env[DuckDBEnvVar.PATH] = duckdb_path
    duckdb_settings = resolve_duckdb_settings(resolver_config.duckdb, env)  # pylint: disable=no-member  # Pydantic model attribute
    model_path = resolve_model_path(duckdb_settings, env)
    blocking = resolver_config.blocking or BlockingSettings.from_config(
        raw_config[SplinkConfigKey.SECTION][SplinkConfigKey.BLOCKING_RULES],
        entity_fields,
    )
    log.info(
        "DuckDB: storage=%s path=%s memory_limit=%s threads=%s temp_directory=%s model_path=%s",
        duckdb_settings.storage,
        duckdb_settings.path,
        duckdb_settings.memory_limit or "(duckdb default)",
        duckdb_settings.threads or "(duckdb default)",
        duckdb_settings.temp_directory or "(duckdb default)",
        model_path or "(not persisted)",
    )
    con = open_duckdb(duckdb_settings)
    try:
        init_schema(con, entity_fields, blocking.normalised_fields)
    except OutdatedSchemaError as exc:
        con.close()
        raise OutdatedSchemaError(f"Database {duckdb_settings.path}: {exc}") from exc

    mention_repo = DuckDBMentionRepository(
        con, entity_fields, blocking.normalised_fields
    )
    similarity_repo = DuckDBSimilarityRepository(con)
    cluster_repo = DuckDBClusterRepository(con)
    linker = SpLinkSimilarityLinker(
        entity_fields,
        raw_config,
        connection=con,
        model_path=model_path,
        training_sample_size=resolver_config.auto_train_threshold or None,
        blocking=blocking,
        match_weight_threshold=resolver_config.match_weight_threshold,
    )
    log_model_status(linker.model_status(), model_path)
    train_on_start_if_due(
        linker, mention_repo.count(), resolver_config.auto_train_threshold
    )

    return EntityResolver(
        mention_repo,
        similarity_repo,
        cluster_repo,
        linker,
        resolver_config,
        unit_of_work=DuckDBUnitOfWork(con),
    )


def build_entity_resolution_service(
    resolver: EntityResolver, mapper: RDFMapper
) -> EntityResolutionService:
    """
    Factory: construct EntityResolutionService with pre-built resolver and mapper.

    This factory wires the core resolver and RDF mapper together into the public
    API service, avoiding repeated instantiation on every request.

    Args:
        resolver: EntityResolver instance (pre-built core resolver).
        mapper: RDFMapper implementation (pre-built).

    Returns:
        Fully-constructed EntityResolutionService ready for request processing.
    """
    return EntityResolutionService(resolver, mapper)


def build_rdf_mapper(rdf_mapping_path: str | Path = None) -> RDFMapper:
    """
    Factory: construct RDFMapper for entity mention parsing.

    Args:
        rdf_mapping_path: Path to rdf_mapping.yaml config file.
                         If None, uses default path.

    Returns:
        Fully-constructed RDFMapper implementation (TurtleRDFMapper).
    """
    return TurtleRDFMapper(rdf_mapping_path)
