"""
ERE service launcher — entrypoint for local development & Docker.

Reads entity resolution requests from a Redis queue, logs them to stdout,
and produces responses back to another Redis queue.

Configuration is read from environment variables or CLI arguments.

Environment variables:
    ERSYS_REQUEST_QUEUE   Redis queue for inbound requests (default: ere_requests)
    ERSYS_RESPONSE_QUEUE  Redis queue for outbound responses (default: ere_responses)
    REDIS_HOST            Redis hostname (default: localhost)
    REDIS_PORT            Redis port (default: 6379)
    REDIS_DB              Redis DB index (default: 0)
    REDIS_PASSWORD        Redis authentication password (default: unset)
    REDIS_TLS             Enable TLS for Redis connection (default: false)
    ERE_LOG_LEVEL         Python log level name (default: INFO) — supports TRACE
    RDF_MAPPING_PATH      Path to rdf_mapping.yaml config file
    RESOLVER_CONFIG_PATH  Path to resolver.yaml config file
    DUCKDB_PATH           Path to persistent DuckDB file (overrides resolver.yaml)
    ERE_DUCKDB_STORAGE    DuckDB storage: disk (default) or memory (overrides resolver.yaml duckdb.type)
    ERE_DUCKDB_MEMORY_LIMIT  DuckDB memory limit, e.g. 2GB (default: DuckDB's own, sized from host RAM)
    ERE_DUCKDB_THREADS    DuckDB worker threads (default: DuckDB's own)
    ERE_DUCKDB_TEMP_DIR   DuckDB spill directory (default: <DUCKDB_PATH>.tmp on disk)
    ERE_SPLINK_MODEL_PATH Trained model file (default: <DUCKDB_PATH>.splink_model.json on disk)
    ERE_BATCH_TARGET_SECONDS  Target processing time of one bite of requests (default: 2)
    ERE_BATCH_MAX_MENTIONS    Most requests taken in one bite (default: 500)
    ERE_BATCH_MAX_BYTES       Most payload bytes taken in one bite (default: 50000000)
    ERE_BATCH_LINGER_MS       Wait for more requests when the queue drains mid-bite (default: 250)

CLI arguments:
    --log-level           Python log level name (overrides LOG_LEVEL env var)
    --rdf-mapping-path    Path to rdf_mapping.yaml config file
    --resolver-config-path Path to resolver.yaml config file
"""

import argparse
import logging
import os
import signal
import sys

from ere.adapters.redis_client import RedisConnectionConfig
from ere.entrypoints.bootstrap import (
    build_entity_resolution_service,
    build_entity_resolver,
    build_rdf_mapper,
    resolve_batch_settings,
)
from ere.entrypoints.queue_worker import RedisQueueWorker
from ere.utils.logging import configure_logging

log = logging.getLogger(__name__)


def main() -> None:
    """Main entry point: orchestrate service setup and run queue worker."""
    # Parse CLI arguments
    parser = argparse.ArgumentParser(
        description="ERE service: Entity Resolution Engine"
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Python log level name (DEBUG, INFO, WARNING, ERROR, CRITICAL, TRACE)",
    )
    parser.add_argument(
        "--rdf-mapping-path",
        default=None,
        help="Path to rdf_mapping.yaml config file",
    )
    parser.add_argument(
        "--resolver-config-path",
        default=None,
        help="Path to resolver.yaml config file",
    )
    args = parser.parse_args()

    configure_logging(log_level=args.log_level)
    log.info("ERE service starting")

    # Read configuration from environment or CLI
    redis_config = RedisConnectionConfig.from_env()
    request_queue = os.environ.get("ERSYS_REQUEST_QUEUE", "ere_requests")
    response_queue = os.environ.get("ERSYS_RESPONSE_QUEUE", "ere_responses")

    # Config file paths: CLI takes precedence over environment
    rdf_mapping_path = args.rdf_mapping_path or os.environ.get("RDF_MAPPING_PATH")
    resolver_config_path = args.resolver_config_path or os.environ.get(
        "RESOLVER_CONFIG_PATH"
    )
    duckdb_path = os.environ.get("DUCKDB_PATH")

    log.info(
        "Configuration: redis=%s:%d/%d, tls=%s, request_queue=%s, response_queue=%s",
        redis_config.host,
        redis_config.port,
        redis_config.db,
        redis_config.tls,
        request_queue,
        response_queue,
    )
    log.info(
        "Config paths: rdf_mapping=%s, resolver_config=%s",
        rdf_mapping_path or "(default)",
        resolver_config_path or "(default)",
    )

    # Connect to Redis
    try:
        client = redis_config.create_client()
        client.ping()
        log.info("Connected to Redis")
    except Exception:  # pylint: disable=broad-exception-caught
        log.exception("Failed to connect to Redis")
        sys.exit(1)

    # Build resolver, mapper, and service once before the loop
    resolver = None
    try:
        log.info("Building entity resolution components")
        resolver = build_entity_resolver(
            resolver_config_path=resolver_config_path,
            duckdb_path=duckdb_path,
        )
        mapper = build_rdf_mapper(rdf_mapping_path=rdf_mapping_path)
        service = build_entity_resolution_service(resolver, mapper)
        log.info("Entity resolution service ready")
    except Exception:  # pylint: disable=broad-exception-caught
        log.exception("Failed to build entity resolution service")
        sys.exit(1)

    try:
        batch_settings = resolve_batch_settings(os.environ)
    except ValueError:
        log.exception("Invalid batch settings")
        sys.exit(1)
    log.info("Batch settings: %s", batch_settings)

    # Create queue worker
    worker = RedisQueueWorker(
        redis_client=client,
        entity_resolution_service=service,
        request_queue=request_queue,
        response_queue=response_queue,
        batch_settings=batch_settings,
    )

    # Set up signal handling for graceful shutdown
    running = True

    def _handle_shutdown(sig, _frame):
        nonlocal running
        log.info("Received signal %s — stopping service", sig)
        running = False

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    # Main service loop
    log.info("ERE service ready, listening for requests")
    try:
        while running:
            worker.process_bite()
    except KeyboardInterrupt:
        log.info("Service interrupted")
    except Exception as e:  # pylint: disable=broad-exception-caught
        log.exception("Unexpected error in service loop: %s", e)
    finally:
        # Close DuckDB connection if it was created
        if resolver is not None:
            # Access the underlying connection through the repositories
            # TODO: expose via public API
            mention_repo = resolver._mention_repo  # pylint: disable=protected-access
            if hasattr(mention_repo, "_con"):
                mention_repo._con.close()  # pylint: disable=protected-access
                log.info("DuckDB connection closed")
        client.close()
        log.info("ERE service stopped")


if __name__ == "__main__":
    main()
