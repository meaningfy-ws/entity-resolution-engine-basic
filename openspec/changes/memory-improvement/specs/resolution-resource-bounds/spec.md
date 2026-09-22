## ADDED Requirements

### Requirement: No per-request database artefacts are retained
The ERE SHALL NOT retain any database table, view, or cache entry created solely to process a single
resolution request after that request's response has been produced.

#### Scenario: Catalog stays flat across many requests
- **WHEN** the ERE resolves 1,000 distinct mentions in sequence
- **THEN** the number of tables and views in the DuckDB catalog after the last request equals the number after the first request

#### Scenario: Cleanup failure does not fail the request
- **WHEN** dropping a per-request artefact raises an error
- **THEN** the ERE SHALL still send the resolution response and log a warning naming the artefact

### Requirement: Per-request work is independent of cluster membership size
The ERE SHALL NOT load the full cluster membership, nor scan the full `clusters` or `mentions` tables, nor read
`similarities`, while resolving a new mention.

#### Scenario: Resolving a mention with a large corpus
- **WHEN** a new mention is resolved with 100,000 mentions already stored
- **THEN** mention and cluster lookups use indexed access, `similarities` is only written, and the full membership map is never built

#### Scenario: Candidates built from computed links
- **WHEN** a new mention produces links during scoring
- **THEN** its cluster candidates are derived from those links with a single bulk cluster lookup, with the same candidates, scores and order as the current algorithm

### Requirement: Flat memory and latency as stored mentions grow
The ERE SHALL keep process memory bounded and mean per-mention processing time at or below 360 ms, measured
from N = 1,000 up to N = 300,000 stored mentions on the reference profiling harness.

#### Scenario: Profiling at increasing corpus size
- **WHEN** the profiling harness resolves mentions at N ≈ 25k, 150k and 300k
- **THEN** mean processing time per mention is ≤ 360 ms at every sample point and RSS does not grow proportionally to N

### Requirement: DuckDB storage is configurable by environment with on-disk default
The ERE SHALL open DuckDB according to `ERE_DUCKDB_STORAGE` (`disk` or `memory`), `ERE_DUCKDB_MEMORY_LIMIT`,
`ERE_DUCKDB_THREADS` and `ERE_DUCKDB_TEMP_DIR`. Environment values SHALL override `resolver.yaml`, and storage
SHALL default to `disk`. The same database instance SHALL serve both the resolver repositories and the similarity
linker.

#### Scenario: Default configuration
- **WHEN** the ERE starts with no DuckDB environment variables set
- **THEN** it opens a file-backed DuckDB database with its temp directory beside the database file, and the linker uses that same database

#### Scenario: Memory limit from environment
- **WHEN** `ERE_DUCKDB_MEMORY_LIMIT=2GB` is set
- **THEN** `current_setting('memory_limit')` reports 2 GB for the connection used by the resolver and the linker

#### Scenario: In-memory mode on request
- **WHEN** `ERE_DUCKDB_STORAGE=memory` is set
- **THEN** the ERE opens an in-memory database and applies the configured memory limit and temp directory

#### Scenario: Invalid storage value
- **WHEN** `ERE_DUCKDB_STORAGE=ram` is set
- **THEN** the ERE SHALL refuse to start and log an error naming the variable and allowed values

### Requirement: Single-threaded consumption at processing rate
The ERE SHALL resolve with a single thread and SHALL take at most one bite of requests at a time (see
`batched-resolution`), so requests beyond the current bite remain in the queue until its responses are sent.

#### Scenario: Backlog stays in Redis
- **WHEN** 1,000 requests are queued and the bite limit is 100
- **THEN** 900 requests remain in the request queue while the bite is processed
