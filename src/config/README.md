# Entity resolver Configuration

The Entity Resolver is the core component of the Basic ERE (Entity Resolution Engine) service.
It is responsible for identifying and linking entities across different data sources,
ensuring consistency and accuracy in entity identification and consolidation.

This page provides configuration details and guidelines for the Entity Resolver component.
 

Configuration files control the entity resolution algorithm behavior, including similarity matching, blocking rules, thresholds, and statistical priors. This directory contains two primary configuration files.

---

## Overview

### resolver.yaml
The main configuration file for the entity resolver. Defines:
- **Entity fields** to extract from RDF (legal name, address components)
- **Database settings** (DuckDB type and location)
- **Clustering thresholds** (when a mention joins an existing cluster)
- **Output limits** (max candidate clusters per resolution)
- **Blocking rules** (which mention pairs to compare)
- **Statistical models** (Splink comparisons, cold-start priors, EM training)

### rdf_mapping.yaml
Maps RDF entity types to extraction rules. Defines:
- **Supported entity types** (e.g., ORGANISATION)
- **RDF type discriminator** (which RDF class identifies an organization)
- **Field property paths** (how to traverse RDF to extract attributes)

The entity fields defined in `resolver.yaml` must match the field names in `rdf_mapping.yaml`.

---

## Environment Variables

ERE is configured at runtime through environment variables. The service reads directly from the process environment; it does not load `.env` files itself. When running via Docker Compose, the `env_file` directive in `compose.dev.yaml` applies `src/infra/.env` to the container. When running the service directly, export the variables in your shell beforehand.

### Configuration groups

**Logging** — Controls verbosity of the ERE service log output. `ERE_LOG_LEVEL` accepts standard Python logging level strings (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) plus the custom `TRACE` level (below `DEBUG`) defined in `src/ere/utils/logging.py`.

**Queues** — Redis list keys used to exchange messages with ERS. `ERSYS_REQUEST_QUEUE` is the inbound queue ERE reads resolution requests from; `ERSYS_RESPONSE_QUEUE` is the queue ERE writes results back to. Both must match the corresponding values set on the ERS side.

**Redis** — Connection settings for the Redis message broker. The four connection variables (`REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, `REDIS_PASSWORD`) must point to the same Redis instance used by ERS. Set `REDIS_TLS=true` to require a TLS-encrypted connection (e.g. AWS ElastiCache with in-transit encryption).

**Bites** — The ERE takes waiting requests from Redis in bites and resolves each bite with one scoring call and one database commit. The bite size follows recent processing speed (`ERE_BATCH_TARGET_SECONDS`), so a backlog stays in Redis instead of being taken in at once.

**Upgrade note** — Databases created before the name-similarity blocking change lack the `legal_name_norm` column; the ERE refuses to start on them. Delete the database file (and its `.splink_model.json`) and restart: the search space rebuilds from new requests.

**Storage** — Paths to the DuckDB database and the YAML configuration files that drive entity resolution behaviour. When unset, `RESOLVER_CONFIG_PATH` and `RDF_MAPPING_PATH` fall back to the bundled files baked into the Docker image at `/app/config/`. `DUCKDB_PATH` is optional — when unset, the path defined inside `resolver.yaml` is used, else `data/app.duckdb`. DuckDB runs on disk by default; `ERE_DUCKDB_*` variables override `resolver.yaml` and set the memory limit, threads and spill directory. The trained similarity model is saved next to the database file and reloaded on start.

### Variable reference

| Name | Group | Description | Default | Mandatory |
| :--- | :--- | :--- | :--- | :---: |
| `DUCKDB_PATH` | Storage | Path to the DuckDB database file. Leave unset to use the path defined in `resolver.yaml`. | *(from resolver.yaml)* | No |
| `ERE_BATCH_LINGER_MS` | Bites | How long to wait for more requests when the queue drains in the middle of taking a bite. | `250` | No |
| `ERE_BATCH_MAX_BYTES` | Bites | Most request payload bytes taken in one bite; a single larger request forms a bite of one. | `50000000` | No |
| `ERE_BATCH_MAX_MENTIONS` | Bites | Most requests taken in one bite. | `500` | No |
| `ERE_BATCH_TARGET_SECONDS` | Bites | Target processing time of one bite; the bite size adapts to recent processing speed so each bite takes about this long. | `2` | No |
| `ERE_DUCKDB_MEMORY_LIMIT` | Storage | DuckDB memory limit (e.g. `2GB`). Set it in containers to ~60% of the container memory limit; DuckDB otherwise sizes it from host RAM. | *(DuckDB default: 80% of host RAM)* | No |
| `ERE_DUCKDB_STORAGE` | Storage | `disk` or `memory`. Overrides `duckdb.type` in `resolver.yaml`. | `disk` | No |
| `ERE_DUCKDB_TEMP_DIR` | Storage | Writable directory DuckDB spills to when over its memory limit. Required for `memory` storage on a read-only filesystem. | `<DUCKDB_PATH>.tmp` (disk) | No |
| `ERE_DUCKDB_THREADS` | Storage | Number of DuckDB worker threads. | *(DuckDB default: CPU count)* | No |
| `ERE_LOG_LEVEL` | Logging | Python logging level for the ERE service. Accepts `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`, and the custom `TRACE` level. | `INFO` | No |
| `ERSYS_REQUEST_QUEUE` | Queues | Redis list key ERE reads inbound resolution requests from. Must match the ERS-side queue name. | `ere_requests` | No |
| `ERSYS_RESPONSE_QUEUE` | Queues | Redis list key ERE writes resolution responses to. Must match the ERS-side queue name. | `ere_responses` | No |
| `RDF_MAPPING_PATH` | Storage | Path to the RDF field mapping config YAML. Defines namespace bindings and field extraction rules for each entity type. When unset, falls back to the bundled file at `/app/config/rdf_mapping.yaml` inside the Docker image. | *(bundled `/app/config/rdf_mapping.yaml`)* | No |
| `ERE_SPLINK_MODEL_PATH` | Storage | File the trained similarity model is saved to and reloaded from. | `<DUCKDB_PATH>.splink_model.json` (disk); not persisted in memory mode | No |
| `REDIS_DB` | Redis | Redis database index. | `0` | No |
| `REDIS_HOST` | Redis | Redis server hostname or endpoint. | `localhost` | No |
| `REDIS_PASSWORD` | Redis | Redis authentication password. Leave empty if Redis AUTH is not configured. | | No |
| `REDIS_PORT` | Redis | Redis server port. | `6379` | No |
| `REDIS_TLS` | Redis | Enable TLS-encrypted connections to Redis. Set to `true` when the Redis endpoint requires TLS (e.g. AWS ElastiCache with in-transit encryption). | `false` | No |
| `RESOLVER_CONFIG_PATH` | Storage | Path to the Splink resolver config YAML. Defines comparisons, blocking rules, and similarity thresholds. When unset, falls back to the bundled file at `/app/config/resolver.yaml` inside the Docker image. | *(bundled `/app/config/resolver.yaml`)* | No |

---

## Configuration Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| **entity_fields** | list | `[legal_name, country_code, nuts_code, post_code, post_name, thoroughfare]` | RDF attributes to extract and use for similarity computation |
| **duckdb.type** | string | `persistent` | Database mode: `persistent` (file-based) or `in-memory` (test/ephemeral) |
| **duckdb.path** | string | `data/app.duckdb` | Database file location (only for persistent mode; overridden by DUCKDB_PATH env var) |
| **cache_strategy** | string | `tf_incremental` | Search space caching: `tf_incremental` (term-frequency incremental) |
| **threshold** | float (0.0–1.0) | `0.7` | Minimum match probability for cluster assignment. 0.7 measured on `test/stress/data/org-mid.csv`: 0.2 merged mostly unrelated organisations |
| **top_n** | int | `100` | Maximum cluster candidates returned per mention (pruning limit) |
| **match_weight_threshold** | float | `-10` | Pre-filter on Splink match weight; `-10` captures below-threshold links needed for full candidate output |
| **auto_train_threshold** | int | `200` | Mention count at which EM training runs once on that many mentions; the model is then frozen, saved and reloaded on restart (0 = disabled) |
| **probability_two_random_records_match** | float (0.0–1.0) | `0.003` | Fellegi-Sunter prior λ: baseline probability any two records match (affects all m/u probability ratios) |

---

## Splink Comparisons

The `splink.comparisons` section defines similarity functions and their thresholds. Each comparison produces gamma levels (discrete similarity buckets).

| Field | Type | Thresholds | Purpose |
|-------|------|-----------|---------|
| **legal_name** | jaro_winkler | [0.9, 0.8] | Primary identifier; primary signal for match determination |
| **nuts_code** | exact_match | — | EU regional code; exact match or missing data |
| **post_code** | jaro_winkler | [0.95, 0.85] | Postal/ZIP code; typo-tolerant with high thresholds |
| **post_name** | jaro_winkler | [0.90, 0.80] | City name; captures spelling variants and abbreviations |
| **thoroughfare** | jaro_winkler | [0.95, 0.85] | Street address; highly specific, typos uncommon |

**Interpretation:** Each threshold defines a gamma level. For example, `legal_name` with thresholds `[0.9, 0.8]` produces three levels:
- Gamma 2: JW ≥ 0.9 (highest match)
- Gamma 1: 0.8 ≤ JW < 0.9 (medium match)
- Gamma 0: JW < 0.8 (no match)

---

## Blocking Rules

Pairs are compared only if **at least one** blocking rule matches. This reduces computation significantly.

**Current configuration:**
```yaml
blocking_rules:
  - same: country_code
    similar:
      field: legal_name
      min_jaro_winkler: 0.8
```

**Semantics:**
- A pair is scored only if both mentions have the same `country_code` **and** their normalised legal names
  (lower-case, accents stripped, letters and digits only; stored in `mentions.legal_name_norm`) have
  Jaro-Winkler similarity ≥ 0.8.
- A rule may also be a field name or a list of field names (all equal).
- Rules are validated at start-up: unknown or missing keys, field names that are not entity fields, similarity on a
  field without a normalised copy, and thresholds outside 0–1 refuse start-up.
- `splink.normalised_fields` (default `[legal_name]`) lists the fields stored with a normalised copy;
  `splink.em_blocking_field` (default `country_code`) is the field EM training blocks on.
- `country_code` is not a comparison: blocking guarantees it agrees, so comparing it would only inflate scores.
- EM training blocks on `splink.em_blocking_field`, independent of these rules.

**Effect (measured on `test/stress/data/org-mid.csv`):** 2.4 pairs scored per mention instead of 267 with
country-only blocking; all identical-name pairs still scored; < 0.1 % of merged pairs have unrelated names.

---

## Cold-Start Priors

Before EM training, Splink uses cold-start m/u probabilities. These are empirically tuned defaults:

- **m_probability**: Likelihood of observing this gamma level **given the records match**
- **u_probability**: Likelihood of observing this gamma level **given the records are independent** (random pair)

Example (legal_name):
```yaml
m_probabilities: [0.9, 0.6, 0.025, 0.005]      # Gamma levels 2, 1, 0, (implicit no-match)
u_probabilities: [0.00001, 0.0004, 0.004, 0.99559]
```

- If records match, high JW (gamma 2) is very likely (m=0.9)
- If records are random, high JW is very unlikely (u=0.00001)
- Likelihood ratio: 0.9 / 0.00001 = 90,000× evidence for match

**Note:** Once EM training completes (at `auto_train_threshold`), these are replaced by empirically learned parameters.

---

## Fine-Tuning the Resolver

### Precision vs. Recall

- **Increase recall** (catch more matches): Lower `threshold` or lower `m_probabilities` for weak signals
- **Increase precision** (reduce false positives): Raise `threshold` or raise `u_probabilities`

### Field Contribution

Fields with higher m/u ratios have stronger influence. To emphasize address over name:
- Increase `m_probabilities` for address fields (post_code, thoroughfare)
- Decrease `m_probabilities` for weaker signals (post_name)

### Blocking Granularity

- **Tighter blocking** (fewer comparisons): Add more rules, require more fields to match
- **Looser blocking** (more comparisons, slower): Fewer rules, match-any semantics

Example: To enable cross-country matches within same organization parent:
```yaml
blocking_rules:
  - legal_name  # Only compare if legal names are very similar
```

### EM Training

When the stored mentions reach `auto_train_threshold`, EM estimation runs once in the background on that many mentions and replaces the cold-start m/u parameters. The trained model is then frozen: it is saved next to the database (`ERE_SPLINK_MODEL_PATH`), reloaded on restart, and never retrained. Stored similarity scores are not recomputed. If the service starts with at least `auto_train_threshold` mentions and no model file, it trains once before consuming requests. Delete the model file to force a new training on the next start.

To disable: Set `auto_train_threshold: 0`

---

## References

- **Splink documentation**: [Splink — Entity resolution at scale](https://moj-analytical-services.github.io/splink/)
  - Blocking rules: https://moj-analytical-services.github.io/splink/blocking.html
  - Comparisons: https://moj-analytical-services.github.io/splink/comparison_library.html
  - EM training: https://moj-analytical-services.github.io/splink/em_help.html

- **Fellegi-Sunter model**: [The Fellegi-Sunter model in Splink](https://moj-analytical-services.github.io/splink/theory/fellegi_sunter.html)

- **ERE algorithm**: See `../../docs/algorithm.md` for detailed explanation of the online greedy clustering approach.

---

## Troubleshooting

**Too many false positives (precision too low):**
- Increase `threshold` (e.g., 0.20 → 0.35)
- Increase `u_probabilities` for weak signals (less evidence needed)
- Tighten blocking rules (fewer comparisons, more selective)

**Missing matches (recall too low):**
- Decrease `threshold` (e.g., 0.20 → 0.10)
- Decrease `m_probabilities` for strong signals (more generous)
- Loosen blocking rules (more comparisons, more chances to match)

**Slow performance:**
- Reduce `entity_fields` (fewer attributes to compare)
- Tighten blocking rules (fewer pairs to evaluate)
- Decrease `match_weight_threshold` to filter more low-confidence pairs before storing

**Training not converging:**
- Increase `auto_train_threshold` (collect more data before training)
- Manually inspect cluster quality to ensure ground truth is reasonable
- Consider adjusting cold-start priors as fallback
