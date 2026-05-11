# Basic Entity Resolution Engine (Basic ERE)

[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=meaningfy-ws_entity-resolution-engine-basic&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=meaningfy-ws_entity-resolution-engine-basic)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=meaningfy-ws_entity-resolution-engine-basic&metric=coverage)](https://sonarcloud.io/summary/new_code?id=meaningfy-ws_entity-resolution-engine-basic)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/downloads/)

> A basic implementation of the ERE component of the Entity Resolution System (ERSys).

## Overview

The **Basic Entity Resolution Engine (Basic ERE)** is an asynchronous microservice that implements entity resolution for predefined entity types. It supports incremental clustering with stable cluster identifiers.

Its primary purpose is to interact with the Entity Resolution System (ERSys). It adheres to the [ERS–ERE Technical Contract](docs/ERS-ERE-System-Technical-Contract.pdf), which establishes the communication protocol between ERE and ERS (part of ERSys) via a message queue (Redis). It also provides a foundation for other ERE implementations.

### Capabilities

* **Entity mention resolution**: Accepts a structured entity mention and returns one or more cluster candidates with similarity and confidence scores

* **Cluster lifecycle management**: Creates new singleton clusters for unknown entities; assigns known entities to the best-matching cluster

* **Canonical identifier derivation**: Derives cluster IDs deterministically: `SHA256(concat(source_id, request_id, entity_type))`

* **Idempotent processing**: Re-submitting the same request (same identifier triad) returns the same clustering outcome

* **Cold-start and incremental resolution**: Builds cluster structure organically without prior training data and doesn't require global reclustering.

* **RDF data ingestion**: Accepts RDF (Turtle) entity data with configurable field mapping and extraction

* **Declarative entity type support**: Arbitrary entity types specified via configuration files (no hardcoding)

* **Automatic probabilistic model training**: Trains the entity resolution model on-the-fly as the mention database grows (based on statistical distribution and not human-in-the-loop; uses Expectation-Maximisation); 


For detailed documentation, see:
- [Architecture](docs/architecture.md) - description of the applied architecture
- [Algorithm](docs/algorithm.md) - incremental probabilistic entity linking
- [Configuration](src/config/README.md) - field mapping, model tuning, Splink setup
- [ERS–ERE Technical Contract v0.2](docs/ERS-ERE-System-Technical-Contract.pdf)


### Dependencies

ERE relies on **ers-spec** (from [entity-resolution-spec](https://github.com/OP-TED/entity-resolution-spec)), which provides:
- **Shared domain models** - Common entity types and concepts across the ERSys ecosystem
- **ERE contract message models** - Standardized request/response structures for ERE–ERS communication (`EntityMentionResolutionRequest`, `EntityMentionResolutionResponse`, `EREErrorResponse`)

This ensures type-safe, versioned communication between ERE and other ERSys components.

#### External Infrastructure Dependencies
To function, the ERE service requires the following external infrastructure:
- **Redis**: Used as the message broker for the request/response queues (`ere_requests` and `ere_responses`).
- **Docker**: Required for containerized deployment and local development.
- **Python 3.12**: The runtime environment for the engine.


## Getting Started

### Prerequisites

- Python 3.12+
- [Poetry](https://python-poetry.org/) 2.x
- Docker + Docker Compose

### 1. Clone and install

```bash
git clone https://github.com/meaningfy-ws/entity-resolution-engine-basic.git
cd entity-resolution-engine-basic
make install
```

### 2. Configure the environment

```bash
cp src/infra/.env.example src/infra/.env
```

The defaults work for local development. Notable variables in `src/infra/.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_HOST` | `ersys-redis` | Redis host (shared network `ersys-local`) |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_PASSWORD` | `changeme` | Redis password — **must match ERS** |
| `REDIS_DB` | `0` | Redis database index |
| `REDIS_TLS` | `false` | Enable TLS-encrypted Redis connection — set to `true` when the Redis endpoint requires TLS |
| `ERSYS_REQUEST_QUEUE` | `ere_requests` | Inbound request queue name — **must match ERS** |
| `ERSYS_RESPONSE_QUEUE` | `ere_responses` | Outbound response queue name — **must match ERS** |
| `ERE_LOG_LEVEL` | `INFO` | Log level |

### 3. Start the stack

```bash
make infra-up     # start ERE + Redis + RedisInsight
make infra-logs   # follow service logs
make infra-down   # stop all services

Note: `make infra-up` creates a shared external network `ersys-local` used for cross-component communication.
To remove it manually: `docker network rm ersys-local`
```

| Service | URL / Port |
|---------|-----------|
| Redis | `localhost:6379` |
| RedisInsight | `http://localhost:5540` |

### What this stack does NOT include

This repo starts ERE and its own Redis instance. It does **not** include the ERS backend or the web UI.

ERE communicates exclusively through Redis queues — it has no HTTP API. Without ERS publishing requests to `ere_requests`, ERE will start and listen but process nothing.

- To add ERS: follow the Getting Started section in [entity-resolution-service](https://github.com/meaningfy-ws/entity-resolution-service#getting-started).
- To add the web UI: follow the Getting Started section in [entity-resolution-service-webapp](https://github.com/meaningfy-ws/entity-resolution-service-webapp#getting-started).

#### Running ERE alongside ERS (shared Redis)

ERS starts its own Redis on port 6379. ERE also starts Redis on port 6379 by default — running both simultaneously causes a port conflict.

**Solution**: let ERS own Redis, point ERE at it:

1. In `src/infra/.env`, set `REDIS_HOST=ersys-redis`
2. Comment out the `ersys-redis` service block in `src/infra/compose.dev.yaml`
3. Start ERS first (`make up` in the ERS repo), then ERE (`make infra-up`)

Queue names and `REDIS_PASSWORD` must match between both `.env` files (defaults already align).

### 4. Run the demo

With ERE running (`make infra-up`), launch the demo script to observe end-to-end resolution:

```bash
cd src && poetry run python demo/demo.py                          # 8 mentions, 2 clusters (default)
cd src && poetry run python demo/demo.py --data demo/data/org-small.json  # 100 mentions
```

> The demo connects directly to Redis (`localhost:6379`). Set `REDIS_HOST=localhost` in `src/infra/.env` before running.

```bash
make infra-logs   # inspect ERE service logs
make infra-down   # stop when done
```

See [`src/demo/README.md`](src/demo/README.md) for datasets, configuration, and example output.

---


## Usage

ERE has no HTTP API. It communicates exclusively through Redis message queues:
- **Request queue**: `ere_requests` - ERS publishes `EntityMentionResolutionRequest` messages
- **Response queue**: `ere_responses` - ERE publishes `EntityMentionResolutionResponse` or `EREErrorResponse` messages


### Make targets
Available targets (`make help`):
```
  Development:
    install              - Install project dependencies via Poetry
    install-poetry       - Install Poetry if not present
    build                - Build the package distribution

  Testing:
    test                 - Run all tests
    test-unit            - Run unit tests with coverage (fast, your venv)
    test-integration     - Run integration tests only
    test-coverage        - Generate HTML coverage report

  Code Quality (Developer):
    format               - Format code with Ruff
    lint                 - Run pylint checks (your venv, fast)
    lint-fix             - Auto-fix with Ruff

  Code Quality (CI/Isolated):
    check-clean-code     - Clean-code checks: pylint + radon + xenon (tox)
    check-architecture   - Validate layer contracts (tox)
    all-quality-checks   - Run all quality checks
    ci                   - Full CI pipeline for GitHub Actions

  Infrastructure (Docker):
    infra-build          - Build the ERE Docker image
    infra-up             - Start services (docker compose up -d)
    infra-down           - Stop and remove stack containers and networks
    infra-down-volumes   - Stop services and remove volumes (clean slate)
    infra-rebuild        - Rebuild images and start services
    infra-rebuild-clean  - Rebuild from scratch (no cache) and start
    infra-logs           - Follow service logs
    infra-watch          - Start services with file watching (sync src/ and src/config/)

  Utilities:
    clean                - Remove build artifacts and caches
    help                 - Display this help message
```

### Configuration (Resolver and Mapper)

Entity resolution behaviour is configured via two YAML files:
- **Resolver configuration** ([resolver.yaml](./src/config/resolver.yaml)): Splink comparisons, cold-start parameters, similarity thresholds
- **RDF mapping** ([rdf_mapping.yaml](./src/config/rdf_mapping.yaml)): RDF namespace bindings, field extraction rules, entity type definitions

For detailed configuration options and tuning, see the [configuration page](./src/config/README.md).

### Examples

A working demo is available that demonstrates ERE as a black-box service communicating through Redis queues.

```bash
# Prerequisites: Redis must be running, ERE service must be listening
python src/demo/demo.py                                   # Uses org-tiny.json (8 mentions, 2 clusters)
python src/demo/demo.py --data src/demo/data/org-small.json  # 100 mentions, realistic clustering
```

The demo:
- Loads entity mentions from JSON datasets stored in `src/demo/data/`
- Sends mentions to the request queue via RDF Turtle messages
- Listens for resolution responses with cluster assignments
- Logs all interactions with timestamps and outputs a clustering summary

**Datasets**: Multiple datasets available:
- `org-tiny.json` (default) — 8 organization mentions
- `org-small.json` — 100 organization mentions (corresponds to `test/stress/data/org-small.csv`)
- `org-mid.json` — 1,000 organization mentions (corresponds to `test/stress/data/org-mid.csv`)

Note: For practical reasons (Turtle syntax is more verbose and less popular than JSON), the `demo.py` script accepts JSON files of a fixed structure and constructs RDF payloads from them on the fly.

See [`src/demo/README.md`](src/demo/README.md) for datasets, configuration, logging, prerequisites, troubleshooting, and example output.


## Project

### Repository Layout

This repository places the self-contained Python project (source code, dependencies, and tooling config) under `src/`. The canonical `Makefile` lives at the repo root and owns all build logic. Recipes invoke `cd src &&` internally so that Poetry, Ruff, and pytest all resolve correctly against the `src/` project. All `make` targets are run from the repo root — no need to `cd src` first.

### Structure

ERE follows a **Cosmic Python layered architecture** that enforces clear separation of concerns and testability. The `src/ere/` directory contains four layers: domain models (pure business logic), services (use-case orchestration), adapters (infrastructure integrations), and entrypoints (external drivers). Test suites mirror this structure with unit, integration, and BDD scenarios, while documentation covers architecture decisions and implementation tasks. `src/demo/` provides working examples with sample datasets, and `src/infra/` contains containerisation and configuration for local development.

```
src/
├── ere/                  # Python package
│   ├── adapters/         # Redis client, cluster store, resolver implementations
│   ├── entrypoints/      # Redis pub/sub consumer
│   ├── models/           # Domain models (entities, value objects, exceptions)
│   └── services/         # Resolution use-case orchestration
├── config/
│   ├── resolver.yaml     # Splink comparisons, blocking rules, thresholds
│   ├── rdf_mapping.yaml  # RDF namespace bindings, field extraction rules
│   └── README.md         # Configuration documentation
├── demo/
│   ├── demo.py           # Entity resolution demonstration script
│   ├── data/             # Sample datasets (derived from TED procurement data)
│   └── README.md         # Demo usage and configuration guide
├── infra/
│   ├── Dockerfile        # ERE service image definition
│   ├── compose.dev.yaml  # Docker Compose for local development
│   └── .env.example      # Environment variable template
├── pyproject.toml        # Project metadata and dependencies
└── poetry.lock

test/
├── features/        # Gherkin BDD feature files
├── steps/           # pytest-bdd step definitions
├── integration/     # Integration tests (full stack)
├── e2e/             # End-to-end tests (Redis queue flows)
├── test_data/       # RDF test fixtures (Turtle)
└── conftest.py      # Shared fixtures and test configuration

docs/
├── ERS-ERE-System-Technical-Contract.pdf
└── *.md             # Architecture, algorithm, glossary
```

### Tooling

| Category | Tools |
|---|---|
| Language | Python 3.12+ |
| Entity resolution engine | Splink (probabilistic record linkage) |
| Data storage | DuckDB (embedded) |
| Message broker | Redis |
| Package management | Poetry |
| Build & task runner | Make |
| Containerisation | Docker + Docker Compose |
| Test runner | pytest, pytest-bdd (Gherkin) |
| Code quality | Ruff (formatting, linting), Pylint (style/SOLID) |
| Architecture enforcement | importlinter (dependency validation) |

### Data Sources

The datasets stored in `demo/data/` and `test/` directories have been derived from public procurement data published by the European Commission at [TED (Tenders Electronic Daily)](https://ted.europa.eu/en/). These datasets are used for demonstration, testing, and benchmarking the entity resolution engine. The derived datasets maintain the character of the original procurement data while being tailored for the specific purposes of validating ERE functionality across realistic entity resolution scenarios.



## Testing

ERE has several test layers aligned with its Cosmic Python architecture.

| Test Type | Location | Purpose |
|---|---|---|
| **Unit Tests (adapters)** | `test/unit/adapters/` | Verify individual adapter components (DuckDB repositories, RDF mapper, Splink linker) in isolation |
| **Unit Tests (services)** | `test/unit/services/` | Validate service-layer use-case orchestration; entity resolution workflow |
| **Integration Tests** | `test/integration/` | Test EntityResolver with all real adapters (DuckDB, Splink); full entity mention flow with clustering |
| **BDD Scenarios** | `test/features/` + `test/features/steps/` | Gherkin feature files + pytest-bdd step definitions; document resolution algorithm behaviour; verify clustering rules and thresholds |
| **End-to-End Tests** | `test/e2e/` | Full service startup; Redis queue integration; request/response payload structure validation |
| **Stress Tests** | `test/stress/` | Load testing and performance profiling; throughput and latency benchmarks |

**Stress Test Datasets**: Committed to `test/stress/data/`.
See [Stress Test & Datasets README](test/stress/README.md) for dataset descriptions and usage.

### Running Tests

```bash
# All tests (unit + integration; requires Docker)
make test

# Unit tests only (no Docker required)
make test-unit

# Integration tests (requires Docker)
make test-integration

# Code formatting and linting
make format             # Auto-format with Ruff
make lint               # Lint without modifying files
make lint-fix           # Lint with auto-fix
```

### Key Testing Practices

- **TDD by default** - write failing tests before implementing features
- **Layer isolation** - each layer tests its own responsibility only
- **Fixture-driven setup** - reusable fixtures in `conftest.py` for service/mapper creation


## Contributing

Contributions are welcome. Please open an issue before submitting a pull request.

- Follow the existing code style (run `make lint-check` before pushing)
- Write tests for new behaviour (BDD features or unit tests)
- Keep commits small and well-described
- Branch naming: `feature/<ticket>/<short-description>` (e.g. `feature/ERS1-124/conflict-detection`)

For active tasks and current work, edit [WORKING.md](WORKING.md).
For development workflow and architecture guidelines, see [CLAUDE.md](.claude/CLAUDE.md).
