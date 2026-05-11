#!/usr/bin/env python3
"""
Demo: Indirect Redis client for ERE (Entity Resolution Engine).

This demo connects to ERE through the Redis queue infrastructure (no direct Python API).
It demonstrates:
1. Checking Redis connectivity
2. Sending EntityMentionResolutionRequest messages to the queue
3. Listening for EntityMentionResolutionResponse messages
4. Logging all interactions

The example uses 6 synthetic mentions from ALGORITHM.md that cluster into 2 groups:
  - Cluster 1: {1, 2, 5}  (organizations with high similarity)
  - Cluster 2: {3, 4, 6}  (different organizations, also highly similar)

⚠️  IMPORTANT: The ERE resolver persists state in a DuckDB database volume.
    Before running a fresh demo with different data, clear the old database:

    docker volume rm ere-local_ere-data
    make infra-rebuild

    Failure to do so will mix old mentions with new ones, corrupting demo results.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import redis

# Default data file path
DEFAULT_DATA_FILE = Path(__file__).parent / "data" / "org-tiny.json"

DELAY_BETWEEN_MESSAGES = (
    0  # seconds to wait between sending messages (set to >0 for sequential processing)
)
GLOBAL_TIMEOUT = 0  # seconds to wait for responses before giving up (0 = no timeout)


# ===============================================================================
# Configuration
# ===============================================================================


def load_env_file(env_path: str = None) -> dict:
    """Load configuration from .env or environment variables."""
    config = {}

    # Try to load from .env if it exists
    if env_path is None:
        env_path = Path(__file__).parent.parent / "infra" / ".env"

    if Path(env_path).exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    if "=" in line:
                        key, value = line.split("=", 1)
                        config[key.strip()] = value.strip()

    # Environment variables override .env
    config["REDIS_HOST"] = os.environ.get(
        "REDIS_HOST", config.get("REDIS_HOST", "localhost")
    )
    config["REDIS_PORT"] = int(
        os.environ.get("REDIS_PORT", config.get("REDIS_PORT", "6379"))
    )
    config["REDIS_DB"] = int(os.environ.get("REDIS_DB", config.get("REDIS_DB", "0")))
    config["REDIS_PASSWORD"] = os.environ.get(
        "REDIS_PASSWORD", config.get("REDIS_PASSWORD")
    )
    config["ERSYS_REQUEST_QUEUE"] = os.environ.get(
        "ERSYS_REQUEST_QUEUE", config.get("ERSYS_REQUEST_QUEUE", "ere_requests")
    )
    config["ERSYS_RESPONSE_QUEUE"] = os.environ.get(
        "ERSYS_RESPONSE_QUEUE", config.get("ERSYS_RESPONSE_QUEUE", "ere_responses")
    )

    return config


# ===============================================================================
# Logging Setup
# ===============================================================================

TRACE = 5


def setup_logging():
    """Configure logging with timestamps."""
    log_level_name = os.environ.get("ERE_LOG_LEVEL", "INFO").upper()

    # Handle custom TRACE level
    if log_level_name == "TRACE":
        log_level = TRACE
        logging.addLevelName(TRACE, "TRACE")
    else:
        log_level = getattr(logging, log_level_name, logging.INFO)

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger = logging.getLogger(__name__)
    logger.setLevel(log_level)
    logger.info(f"Logging configured at level {log_level_name}")

    return logger


# ===============================================================================
# Redis Connection
# ===============================================================================


def check_redis_connectivity(
    host: str, port: int, db: int, password: str
) -> redis.Redis:
    """
    Check Redis connectivity and return client.

    Attempts connection to specified host first, then fallback to localhost
    if configured host is "redis" (Docker).

    Raises:
        RuntimeError: If Redis is not accessible.
    """
    hosts_to_try = [host]

    # Fallback: if configured host is "redis" (Docker), also try localhost
    if host == "redis":
        hosts_to_try.append("localhost")

    last_error = None
    for try_host in hosts_to_try:
        try:
            logging.getLogger(__name__).info(
                f"Attempting Redis connection to {try_host}:{port}..."
            )
            client = redis.Redis(
                host=try_host,
                port=port,
                db=db,
                password=password,
                decode_responses=False,
            )
            client.ping()
            return client
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(
        f"Redis unavailable. Tried hosts: {hosts_to_try}, port: {port}, db: {db}"
    ) from last_error


# ===============================================================================
# Request/Response Handling
# ===============================================================================


def escape_turtle_string(value: str) -> str:
    """
    Escape a string for safe inclusion in Turtle RDF format.

    Handles special characters: backslash, double quotes, newlines, carriage returns, tabs.

    Args:
        value: String to escape

    Returns:
        Escaped string safe for use in Turtle string literals
    """
    if not value:
        return value

    # Escape backslash first (must be done before other escapes)
    value = value.replace("\\", "\\\\")
    # Escape double quotes
    value = value.replace('"', '\\"')
    # Escape newlines
    value = value.replace("\n", "\\n")
    # Escape carriage returns
    value = value.replace("\r", "\\r")
    # Escape tabs
    value = value.replace("\t", "\\t")

    return value


def create_entity_mention_request(
    request_id: str,
    source_id: str,
    entity_type: str,
    legal_name: str,
    country_code: str,
    nuts_code: str | None = None,
    post_code: str | None = None,
    post_name: str | None = None,
    thoroughfare: str | None = None,
) -> dict:
    """
    Create an EntityMentionResolutionRequest payload.

    Uses RDF/Turtle format with entity metadata including extended address fields.
    All string values are properly escaped for Turtle compatibility.

    Args:
        request_id: Unique request identifier
        source_id: Source system identifier
        entity_type: Entity type (e.g., ORGANISATION)
        legal_name: Legal name of the entity
        country_code: ISO 2-letter country code
        nuts_code: Optional NUTS regional code
        post_code: Optional postal code
        post_name: Optional city/locality name
        thoroughfare: Optional street address
    """
    # Escape all string values for Turtle safety
    legal_name_safe = escape_turtle_string(legal_name or "")
    country_code_safe = escape_turtle_string(country_code or "")

    # Build address properties dynamically
    address_props = [f'epo:hasCountryCode "{country_code_safe}"']
    if nuts_code:
        nuts_code_safe = escape_turtle_string(nuts_code)
        address_props.append(f'epo:hasNutsCode "{nuts_code_safe}"')
    if post_code:
        post_code_safe = escape_turtle_string(post_code)
        address_props.append(f'locn:postCode "{post_code_safe}"')
    if post_name:
        post_name_safe = escape_turtle_string(post_name)
        address_props.append(f'locn:postName "{post_name_safe}"')
    if thoroughfare:
        thoroughfare_safe = escape_turtle_string(thoroughfare)
        address_props.append(f'locn:thoroughfare "{thoroughfare_safe}"')

    address_content = " ;\n        ".join(address_props)

    content = f"""@prefix org: <http://www.w3.org/ns/org#> .
@prefix cccev: <http://data.europa.eu/m8g/> .
@prefix epo: <http://data.europa.eu/a4g/ontology#> .
@prefix locn: <http://www.w3.org/ns/locn#> .
@prefix epd: <http://data.europa.eu/a4g/resource/> .

epd:ent{request_id} a org:Organization ;
    epo:hasLegalName "{legal_name_safe}" ;
    cccev:registeredAddress [
        {address_content}
    ] .
"""

    return {
        "type": "EntityMentionResolutionRequest",
        "entity_mention": {
            "identifiedBy": {
                "request_id": request_id,
                "source_id": source_id,
                "entity_type": entity_type,
            },
            "content": content.strip(),
            "content_type": "text/turtle",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ere_request_id": f"{request_id}:01",
    }


def parse_response(response_bytes: bytes) -> dict:
    """Parse JSON response from Redis."""
    return json.loads(response_bytes.decode("utf-8"))


# ===============================================================================
# Demo Data Loading
# ===============================================================================


def load_demo_mentions(data_file: str | None = None) -> list[dict]:
    """
    Load demo mentions from a JSON file.

    Args:
        data_file: Path to JSON file containing mentions. If None, uses default.

    Returns:
        List of mention dicts with keys: request_id, source_id, entity_type,
                                         legal_name, country_code, description.

    Raises:
        FileNotFoundError: If data file does not exist.
        ValueError: If JSON is invalid or missing 'mentions' key.
    """
    if data_file is None:
        data_file = DEFAULT_DATA_FILE

    data_path = Path(data_file)
    if not data_path.exists():
        raise FileNotFoundError(f"Data file not found: {data_path}")

    with open(data_path) as f:
        data = json.load(f)

    if "mentions" not in data:
        raise ValueError(f"JSON must contain 'mentions' key")

    return data["mentions"]


# ===============================================================================
# Main Demo
# ===============================================================================


def main(data_file: str | None = None):
    """
    Run the Redis-based ERE demo.

    Args:
        data_file: Path to JSON file containing demo mentions.
                   If None, uses default (mentions_mixed_countries.json).
    """
    logger = setup_logging()

    # Load configuration
    logger.info("Loading configuration...")
    config = load_env_file()
    logger.info(
        f"Redis config: host={config['REDIS_HOST']}, "
        f"port={config['REDIS_PORT']}, db={config['REDIS_DB']}"
    )
    logger.info(
        f"Queue names: request={config['ERSYS_REQUEST_QUEUE']}, "
        f"response={config['ERSYS_RESPONSE_QUEUE']}"
    )

    # Load demo mentions from JSON
    try:
        demo_mentions = load_demo_mentions(data_file)
        logger.info(
            f"Loaded {len(demo_mentions)} mentions from {data_file or DEFAULT_DATA_FILE}"
        )
    except (FileNotFoundError, ValueError) as e:
        logger.error(f"Failed to load demo mentions: {e}")
        return 1

    # Check Redis connectivity
    logger.info("Checking Redis connectivity...")
    try:
        redis_client = check_redis_connectivity(
            host=config["REDIS_HOST"],
            port=config["REDIS_PORT"],
            db=config["REDIS_DB"],
            password=config["REDIS_PASSWORD"],
        )
        logger.info("✓ Redis is available")
    except RuntimeError as e:
        logger.error(f"✗ Redis check failed: {e}")
        return 1

    # Clear queues
    logger.info("Clearing request and response queues...")
    redis_client.delete(config["ERSYS_REQUEST_QUEUE"], config["ERSYS_RESPONSE_QUEUE"])

    # ⚠️  Check if DuckDB database is non-empty (stale from prior runs)
    # This guards against corrupting demo results by mixing old and new mentions
    duckdb_path = Path(os.environ.get("DUCKDB_PATH", "/data/app.duckdb"))
    if duckdb_path.exists() and duckdb_path.stat().st_size > 0:
        logger.warning(
            f"⚠️  WARNING: DuckDB database file exists and is non-empty!\n"
            f"      This may contain mentions from a prior run.\n"
            f"      This will CORRUPT demo results by mixing old and new data.\n"
            f"      \n"
            f"      To reset the database:\n"
            f"      1. docker volume rm ere-local_ere-data\n"
            f"      2. make infra-rebuild\n"
        )

    # Send demo requests
    logger.info(f"Sending {len(demo_mentions)} entity mentions...")
    request_ids = []

    for mention in demo_mentions:
        request = create_entity_mention_request(
            request_id=mention["request_id"],
            source_id=mention["source_id"],
            entity_type=mention["entity_type"],
            legal_name=mention["legal_name"],
            country_code=mention["country_code"],
            nuts_code=mention.get("nuts_code"),
            post_code=mention.get("post_code"),
            post_name=mention.get("post_name"),
            thoroughfare=mention.get("thoroughfare"),
        )

        message_json = json.dumps(request)
        if logger.isEnabledFor(TRACE):
            logger.log(TRACE, f"Full request message:\n{json.dumps(request, indent=2)}")

        message_bytes = message_json.encode("utf-8")
        redis_client.rpush(config["ERSYS_REQUEST_QUEUE"], message_bytes)
        request_ids.append(mention["request_id"])

        logger.info(
            f"  → Sent request {mention['request_id']}: "
            f"{mention['legal_name']} ({mention['country_code']}) "
            f"[{mention.get('description', '')}]"
        )

        # Wait 1 second between messages to ensure sequential processing
        if DELAY_BETWEEN_MESSAGES:
            time.sleep(1)

    logger.info("")
    logger.info("Listening for responses...")
    logger.info("-" * 80)

    # Track mentions for summary: map request_id → (legal_name, cluster_id)
    mention_tracking = {}
    for mention in demo_mentions:
        mention_tracking[mention["request_id"]] = {
            "legal_name": mention["legal_name"],
            "cluster_id": None,  # Will be filled in from response
        }

    # Listen for responses
    responses_received = {}
    start_time = time.time()

    while len(responses_received) < len(request_ids):
        elapsed = time.time() - start_time
        if GLOBAL_TIMEOUT > 0 and elapsed > GLOBAL_TIMEOUT:
            logger.warning(
                f"Timeout after {GLOBAL_TIMEOUT}s. Received {len(responses_received)}/{len(request_ids)} responses."
            )
            break

        # Try to get a response with short timeout
        result = redis_client.brpop(config["ERSYS_RESPONSE_QUEUE"], timeout=1)

        if result is not None:
            _, response_bytes = result
            response = parse_response(response_bytes)

            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE, f"Full response message:\n{json.dumps(response, indent=2)}"
                )

            req_id = response["entity_mention_id"]["request_id"]
            responses_received[req_id] = response

            logger.info(f"\n✓ Response received for {req_id}:")
            logger.info(f"  Type: {response['type']}")
            logger.info(f"  Timestamp: {response['timestamp']}")

            source_id = response["entity_mention_id"]["source_id"]
            entity_type = response["entity_mention_id"]["entity_type"]
            logger.info(f"  Mention: ({source_id}, {req_id}, {entity_type})")

            logger.info(f"  Candidates:")

            # Track the top cluster assignment (first candidate is the assignment)
            if response.get("candidates"):
                top_candidate = response["candidates"][0]
                assigned_cluster = top_candidate["cluster_id"]
                mention_tracking[req_id]["cluster_id"] = assigned_cluster
                logger.info(f"  → Assigned to cluster: {assigned_cluster}")

            for i, candidate in enumerate(response.get("candidates", []), 1):
                logger.info(
                    f"    {i}. Cluster {candidate['cluster_id']}: "
                    f"confidence={candidate['confidence_score']:.4f}, "
                    f"similarity={candidate['similarity_score']:.4f}"
                )

    logger.info("-" * 80)
    logger.info(
        f"\nDemo complete. Received {len(responses_received)}/{len(request_ids)} responses."
    )

    # Build clustering summary as single block
    summary_lines = []
    summary_lines.append("=" * 80)
    summary_lines.append("CLUSTERING SUMMARY")
    summary_lines.append("=" * 80)

    # Group mentions by assigned cluster
    clusters = {}
    unassigned = []

    for req_id in request_ids:
        tracking = mention_tracking.get(req_id)
        if tracking:
            cluster_id = tracking["cluster_id"]
            legal_name = tracking["legal_name"]

            if cluster_id is None:
                unassigned.append((req_id, legal_name))
            else:
                if cluster_id not in clusters:
                    clusters[cluster_id] = []
                clusters[cluster_id].append((req_id, legal_name))

    # Build cluster output
    if clusters:
        for cluster_id in sorted(clusters.keys()):
            members = clusters[cluster_id]
            summary_lines.append("")
            summary_lines.append(f"{cluster_id} ({len(members)} members):")
            for req_id, legal_name in members:
                summary_lines.append(f"  {req_id:4s} | {legal_name}")
    else:
        summary_lines.append("")
        summary_lines.append("(No clusters formed)")

    # Add unassigned mentions
    if unassigned:
        summary_lines.append("")
        summary_lines.append(f"Unassigned ({len(unassigned)} mentions):")
        for req_id, legal_name in unassigned:
            summary_lines.append(f"  {req_id:4s} | {legal_name}")

    summary_lines.append("=" * 80)

    # Print entire summary in one log call
    summary_block = "\n".join(summary_lines)
    logger.info(f"\n{summary_block}")

    # Summary
    if len(responses_received) == len(request_ids):
        logger.info("✓ All responses received successfully!")
        return 0
    else:
        logger.warning(
            f"✗ Missing {len(request_ids) - len(responses_received)} response(s)."
        )
        return 1


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Redis-based ERE demo with parametrized mentions data."
    )
    parser.add_argument(
        "--data",
        type=str,
        default=None,
        help=f"Path to JSON file with demo mentions (default: {DEFAULT_DATA_FILE})",
    )
    args = parser.parse_args()

    sys.exit(main(data_file=args.data))
