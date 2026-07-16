# ERE Demo - Indirect Redis Client

This demo demonstrates the Entity Resolution Engine (ERE) as an indirect client communicating through Redis queues.

## Overview

The demo:
- Connects to Redis (checking connectivity first)
- Creates 6 synthetic entity mentions
- Sends them as `EntityMentionResolutionRequest` messages to the request queue
- Listens for `EntityMentionResolutionResponse` messages from the response queue
- Logs all interactions with timestamps

The demo treats ERE as a black box service accessible only through Redis message queues. This is useful for:
- Testing the queue-based infrastructure in isolation
- Demonstrating service-to-service communication patterns


## Configuration

Configuration is loaded from `infra/.env` (or environment variables):

| Variable | Default | Purpose |
|----------|---------|---------|
| `REDIS_HOST` | `redis` | Redis hostname (use `localhost` for local testing) |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_DB` | `0` | Redis database number |
| `REDIS_PASSWORD` | `changeme` | Redis password |
| `ERSYS_REQUEST_QUEUE` | `ere_requests` | Queue name for incoming requests |
| `ERSYS_RESPONSE_QUEUE` | `ere_responses` | Queue name for outgoing responses |

The script tries the configured host first, then falls back to `localhost` if the host is `redis` (Docker), making it work both locally and in Docker.

## Prerequisites

1. **Redis must be running** on the configured host/port
2. **ERE service must be running** (or at least the queue worker must be processing messages)
3. **Project dependencies installed** via Poetry: `poetry install`

## Running the Demo

### 1. With Docker Compose (recommended)

Start the full stack including Redis and ERE:

```bash
make infra-rebuild
```

Wait for services to be ready (check logs):

```bash
make infra-logs
```

### 2. Locally (development)

If you're running Redis locally (e.g., Docker container on `localhost:6379`):

```bash
# Ensure Redis is running
redis-cli ping  # should return "PONG"

# Run the demo
cd /home/greg/PROJECTS/ERS/ere-basic
python3 src/demo/demo.py
```

Or with Poetry:

```bash
poetry run python3 src/demo/demo.py
```

**Runtime**: Approximately 5-35 seconds (5s sending + up to 30s waiting for responses).
The demo sends messages with 1-second delays between them, then waits for responses.

### Using Different Datasets

By default, the demo loads `src/demo/data/org-tiny.json`. Specify a different dataset with the `--data` parameter:

```bash
# Use mentions dataset
poetry run python3 src/demo/demo.py --data src/demo/data/mentions_100b.json

# Use larger dataset
poetry run python3 src/demo/demo.py --data src/demo/data/org-mid.json
```

Available datasets in `src/demo/data/`:
- `org-tiny.json` (default) — 8 organization mentions, 2 clusters
- `org-small.json` — Small (100 mentions) organization dataset
- `org-mid.json` — Mid-size (1000 mentions) organization dataset

## Example Output

The demo logs all interactions with timestamps and provides a clustering summary at the end:

```
2026-03-01 12:34:56 [INFO] Loading configuration...
2026-03-01 12:34:56 [INFO] Redis config: host=localhost, port=6379, db=0
2026-03-01 12:34:56 [INFO] Queue names: request=ere_requests, response=ere_responses
2026-03-01 12:34:56 [INFO] Checking Redis connectivity...
2026-03-01 12:34:56 [INFO] ✓ Redis is available
2026-03-01 12:34:56 [INFO] Clearing request and response queues...
2026-03-01 12:34:56 [INFO] Sending 8 entity mentions...
2026-03-01 12:34:56 [INFO]   → Sent request m1: Stadt Osnabrück [Mention 1]
2026-03-01 12:34:57 [INFO]   → Sent request m2: Stadt Osnabrück — Fachdienst Öffentliche Aufträge [Mention 2]
...
2026-03-01 12:34:56 [INFO] Listening for responses...
2026-03-01 12:34:56 [INFO] ✓ Response received for m1:
2026-03-01 12:34:56 [INFO]   Type: EntityMentionResolutionResponse
2026-03-01 12:34:56 [INFO]   Timestamp: 2026-03-01T12:34:56.123456+00:00
2026-03-01 12:34:56 [INFO]   Candidates:
2026-03-01 12:34:56 [INFO]     1. Cluster 8cf6eabbf0edb0fe58fb0c346a7fc3c78ef4939518b1a6f349548c2d6a9953c2: confidence=0.95, similarity=0.95
...
2026-03-01 12:35:00 [INFO] Demo complete. Received 8/8 responses.

================================================================================
CLUSTERING SUMMARY
================================================================================

8cf6eabbf0edb0fe58fb0c346a7fc3c78ef4939518b1a6f349548c2d6a9953c2 (3 members):
  m1   | Stadt Osnabrück
  m2   | Stadt Osnabrück — Fachdienst Öffentliche Aufträge
  m5   | Stadt Osnabrück, Zentrale

914d738331f965d12ca7a0bb964473ce53876d308862b4f46e242de4a3ff6348 (3 members):
  m3   | Conseil départemental Haute-Garonne
  m4   | Conseil départemental Haute-Garonne Service Public
  m6   | Conseil Haute-Garonne

================================================================================
2026-03-01 12:35:00 [INFO] ✓ All responses received successfully!
```

The demo logs:
- **Request tracking**: Each sent mention with descriptive details
- **Response logging**: Received cluster candidates with confidence/similarity scores
- **Clustering summary**: Final cluster assignments with member organizations (by default, saved to `src/demo/log/`)
- **Extended logging**: Trace-level logging for detailed resolution diagnostics

## Demo Data

Datasets are stored in `src/demo/data/` (JSON format with RDF Turtle content).

### Dataset Correspondence to Stress Tests

Demo JSON datasets map to CSV datasets in `test/stress/data/` for reproducible benchmarking.

## Message Format

### Request (EntityMentionResolutionRequest)

```json
{
  "type": "EntityMentionResolutionRequest",
  "entity_mention": {
    "identifiedBy": {
      "request_id": "m1",
      "source_id": "DEMO",
      "entity_type": "ORGANISATION"
    },
    "content": "@prefix org: <http://www.w3.org/ns/org#> ...",
    "content_type": "text/turtle"
  },
  "timestamp": "2026-03-01T12:34:56.123456+00:00",
  "ere_request_id": "m1:01"
}
```

### Response (EntityMentionResolutionResponse)

```json
{
  "type": "EntityMentionResolutionResponse",
  "entity_mention_id": {
    "request_id": "m1",
    "source_id": "DEMO",
    "entity_type": "ORGANISATION"
  },
  "candidates": [
    {
      "cluster_id": "m1",
      "confidence_score": 0.0,
      "similarity_score": 0.0
    }
  ],
  "timestamp": "2026-03-01T12:34:56.234567+00:00",
  "ere_request_id": "m1:01"
}
```

## Troubleshooting

### "Redis unavailable" error

**Check Redis connectivity:**
```bash
redis-cli -h localhost -p 6379 ping
```

If it returns `PONG`, Redis is running. If not:

- **Docker**: `docker run -d -p 6379:6379 redis:latest`
- **Local Redis**: `brew install redis && brew services start redis` (macOS)
- **Docker Compose**: Ensure the service is running: `make infra-up`

### Timeout waiting for responses

**Possible causes:**
- ERE service is not running (no worker to process requests)
- Request queue name doesn't match ERE's configured queue name
- ERE worker crashed or stopped processing

**Check ERE logs:**
```bash
make infra-logs
```

### Password authentication fails

**Edit Redis connection parameters:**

Option 1: Modify `infra/.env`:
```bash
REDIS_PASSWORD=your_password
```

Option 2: Set environment variable:
```bash
export REDIS_PASSWORD=your_password
python3 src/demo/demo.py
```

## Design Notes

- **No direct Python API**: The demo uses Redis as the sole communication channel
- **Message logging**: Every request sent and response received is logged with timestamp
- **Connectivity check**: The demo verifies Redis is accessible before sending messages
- **Queue cleanup**: Request and response queues are cleared at the start of the demo
- **Timeout handling**: The demo waits up to 30 seconds for responses, then reports the count received
- **Docker fallback**: If the configured Redis host is "redis" (Docker), the demo tries localhost as a fallback for local development

## Logging

The demo logs all activity to:
- **Console**: INFO-level messages (requests, responses, clustering summary)
- **Log file**: `src/demo/log/demo_YYYYMMDD-HHMM--DATASETNAME.log` with TRACE-level diagnostics
  - Trace logs include detailed resolution diagnostics (field extraction, similarity scoring, etc.)
  - Clustering summary included at the end of each log file

Configure logging via environment variable:
```bash
export LOG_LEVEL=TRACE  # TRACE, DEBUG, INFO, WARNING, ERROR
python3 src/demo/demo.py
```
