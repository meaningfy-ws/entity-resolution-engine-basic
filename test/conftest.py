"""
Pytest configuration file, which the framework picks up at startup.

[Details here](https://docs.pytest.org/en/stable/reference/fixtures.html)
"""

import os
import logging.config
from pathlib import Path

import pytest
import redis
import yaml

# Path constants — single source of truth for test directory structure
TEST_RESOURCES_DIR = Path(__file__).parent / "resources"
TEST_DATA_DIR = Path(__file__).parent / "test_data"


def pytest_configure(config: pytest.Config):
    """
    Configures various pytest settings:

    * markers for tests
    * Logging
    """

    config.addinivalue_line("markers", "integration: Integration test marker.")

    # Setup logging from YAML config file
    cfg_path = str(TEST_RESOURCES_DIR / "logging-test.yml")
    with open(cfg_path, encoding="utf-8") as f:
        logging_cfg = yaml.safe_load(f)
    logging.config.dictConfig(logging_cfg)


# ============================================================================
# Helper: Load RDF content by relative path
# ============================================================================


def load_rdf(relative_path: str) -> str:
    """
    Load RDF content from test_data directory.

    Args:
        relative_path: Path relative to test_data/, e.g., "organizations/group1/661238-2023.ttl"

    Returns:
        str: Full RDF/Turtle content

    Raises:
        FileNotFoundError: If file does not exist
    """
    file_path = TEST_DATA_DIR / relative_path
    if not file_path.exists():
        raise FileNotFoundError(f"Test data file not found: {file_path}")
    return file_path.read_text(encoding="utf-8")


# ============================================================================
# Organizations Test Data Fixtures
# ============================================================================


@pytest.fixture(scope="session")
def org_group1_file1() -> str:
    """Organizations group1, file 1."""
    return load_rdf("organizations/group1/661238-2023.ttl")


@pytest.fixture(scope="session")
def org_group1_file2() -> str:
    """Organizations group1, file 2."""
    return load_rdf("organizations/group1/662860-2023.ttl")


@pytest.fixture(scope="session")
def org_group1_file3() -> str:
    """Organizations group1, file 3."""
    return load_rdf("organizations/group1/663653-2023.ttl")


@pytest.fixture(scope="session")
def org_group2_file1() -> str:
    """Organizations group2, file 1."""
    return load_rdf("organizations/group2/661197-2023.ttl")


@pytest.fixture(scope="session")
def org_group2_file2() -> str:
    """Organizations group2, file 2."""
    return load_rdf("organizations/group2/663952-2023.ttl")


# ============================================================================
# Procedures Test Data Fixtures
# ============================================================================


@pytest.fixture(scope="session")
def proc_group1_file1() -> str:
    """Procedures group1, file 1."""
    return load_rdf("procedures/group1/662861-2023.ttl")


@pytest.fixture(scope="session")
def proc_group1_file2() -> str:
    """Procedures group1, file 2."""
    return load_rdf("procedures/group1/663131-2023.ttl")


@pytest.fixture(scope="session")
def proc_group1_file3() -> str:
    """Procedures group1, file 3."""
    return load_rdf("procedures/group1/664733-2023.ttl")


@pytest.fixture(scope="session")
def proc_group2_file1() -> str:
    """Procedures group2, file 1."""
    return load_rdf("procedures/group2/661196-2023.ttl")


@pytest.fixture(scope="session")
def proc_group2_file2() -> str:
    """Procedures group2, file 2."""
    return load_rdf("procedures/group2/663262-2023.ttl")


# ============================================================================
# Path Fixtures — inject test file paths to avoid inline Path constructions
# ============================================================================


@pytest.fixture(scope="session")
def resolver_config_path():
    """Path to test resolver.yaml (from test/resources)."""
    return TEST_RESOURCES_DIR / "resolver.yaml"


@pytest.fixture(scope="session")
def rdf_mapping_path():
    """Path to test rdf_mapping.yaml (from test/resources)."""
    return TEST_RESOURCES_DIR / "rdf_mapping.yaml"


# ============================================================================
# Entity Resolution Service Fixture
# ============================================================================


@pytest.fixture
def entity_resolution_service(resolver_config_path, rdf_mapping_path):  # pylint: disable=redefined-outer-name  # pytest fixture params intentionally shadow outer scope names
    """
    Fresh EntityResolver instance per test (core resolver).

    Creates isolated resolver with in-memory DuckDB for test scenario isolation.
    Uses test-specific config files to ensure reproducibility and independence.
    """
    import duckdb
    from ere.adapters.duckdb_repositories import (
        DuckDBMentionRepository,
        DuckDBSimilarityRepository,
        DuckDBClusterRepository,
    )
    from ere.adapters.duckdb_schema import init_schema
    from ere.adapters.splink_linker_impl import SpLinkSimilarityLinker
    from ere.services.entity_resolution_service import EntityResolver
    from ere.services.resolver_config import ResolverConfig

    # Load resolver config from injected fixture path
    with open(resolver_config_path, encoding="utf-8") as f:
        raw_config = yaml.safe_load(f)

    # Entity fields are the source of truth from config
    entity_fields = raw_config.get("entity_fields", ["legal_name", "country_code"])

    resolver_config = ResolverConfig.from_dict(raw_config)
    con = duckdb.connect(":memory:")
    try:
        init_schema(con, entity_fields)

        mention_repo = DuckDBMentionRepository(con, entity_fields)
        similarity_repo = DuckDBSimilarityRepository(con)
        cluster_repo = DuckDBClusterRepository(con)
        linker = SpLinkSimilarityLinker(entity_fields, raw_config)

        yield EntityResolver(
            mention_repo, similarity_repo, cluster_repo, linker, resolver_config
        )
    finally:
        con.close()


# ============================================================================
# RDF Mapper Fixture
# ============================================================================


@pytest.fixture
def rdf_mapper(rdf_mapping_path):  # pylint: disable=redefined-outer-name  # pytest fixture params intentionally shadow outer scope names
    """
    Fresh RDFMapper instance per test.

    Returns a concrete TurtleRDFMapper implementation using test-specific config.
    Uses injected rdf_mapping_path fixture to ensure reproducibility and independence.
    """
    from ere.adapters.rdf_mapper_impl import TurtleRDFMapper

    return TurtleRDFMapper(rdf_mapping_path)


# ============================================================================
# Redis fixture
# ============================================================================


@pytest.fixture(scope="module")
def redis_client():
    """
    Connect to Redis and verify it's available.
    Tries configured host first, then fallback to localhost if configured host is "redis".
    Raises: RuntimeError if Redis is not accessible.
    """
    hosts_to_try = []

    # Primary: configured host (from .env or environment)
    configured_host = os.environ.get("REDIS_HOST", "localhost")
    hosts_to_try.append(configured_host)

    # Fallback: if configured host is a Docker service name, also try localhost
    if configured_host in ("redis", "ersys-redis"):
        hosts_to_try.append("localhost")

    port = int(os.environ.get("REDIS_PORT", "6379"))
    db = int(os.environ.get("REDIS_DB", "0"))
    password = os.environ.get("REDIS_PASSWORD")

    client = None
    last_error = None
    for host in hosts_to_try:
        try:
            client = redis.Redis(
                host=host,
                port=port,
                db=db,
                password=password,
                decode_responses=False,
            )
            client.ping()
            last_error = None
            break
        except redis.RedisError as e:
            last_error = e
            client = None
            continue

    if last_error is not None:
        raise RuntimeError("Redis test service cannot be detected.") from last_error

    # Propagate the host that actually worked so that any test reading REDIS_HOST from
    # os.environ (e.g. to wire main() or another subprocess) gets a resolvable address.
    if host != configured_host:
        os.environ["REDIS_HOST"] = host

    # Verify connection
    try:
        client.ping()
        print(f"\n✓ Connected to Redis at {host}:{port}")
    except redis.RedisError as e:
        pytest.skip(f"Redis not available at {host}:{port} — {e}")

    # Flush entire database to start clean
    try:
        client.flushdb()
        print(f"✓ Flushed Redis DB {db}")
    except redis.RedisError as e:
        print(f"Warning: Could not flush database: {e}")

    yield client

    # Cleanup after test
    try:
        client.flushdb()
    except redis.RedisError as e:
        print(f"Warning: Could not cleanup after test: {e}")

    return client
