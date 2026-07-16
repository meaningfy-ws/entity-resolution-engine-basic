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

**Storage** — Paths to the DuckDB database and the YAML configuration files that drive entity resolution behaviour. When unset, `RESOLVER_CONFIG_PATH` and `RDF_MAPPING_PATH` fall back to the bundled files baked into the Docker image at `/app/config/`. `DUCKDB_PATH` is optional — when unset, the path defined inside `resolver.yaml` is used.

### Variable reference

| Name | Group | Description | Default | Mandatory |
| :--- | :--- | :--- | :--- | :---: |
| `DUCKDB_PATH` | Storage | Path to the DuckDB database file. Leave unset to use the path defined in `resolver.yaml`. | *(from resolver.yaml)* | No |
| `ERE_LOG_LEVEL` | Logging | Python logging level for the ERE service. Accepts `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`, and the custom `TRACE` level. | `INFO` | No |
| `ERSYS_REQUEST_QUEUE` | Queues | Redis list key ERE reads inbound resolution requests from. Must match the ERS-side queue name. | `ere_requests` | No |
| `ERSYS_RESPONSE_QUEUE` | Queues | Redis list key ERE writes resolution responses to. Must match the ERS-side queue name. | `ere_responses` | No |
| `RDF_MAPPING_PATH` | Storage | Path to the RDF field mapping config YAML. Defines namespace bindings and field extraction rules for each entity type. When unset, falls back to the bundled file at `/app/config/rdf_mapping.yaml` inside the Docker image. | *(bundled `/app/config/rdf_mapping.yaml`)* | No |
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
| **threshold** | float (0.0–1.0) | `0.20` | Minimum match probability for cluster assignment. Lower = more sensitive (higher recall, lower precision); higher = more selective |
| **top_n** | int | `100` | Maximum cluster candidates returned per mention (pruning limit) |
| **match_weight_threshold** | float | `-10` | Pre-filter on Splink match weight; `-10` captures below-threshold links needed for full candidate output |
| **auto_train_threshold** | int | `50` | Mention count at which to trigger background EM training (0 = disabled) |
| **probability_two_random_records_match** | float (0.0–1.0) | `0.003` | Fellegi-Sunter prior λ: baseline probability any two records match (affects all m/u probability ratios) |

---

## Splink Comparisons

The `splink.comparisons` section defines similarity functions and their thresholds. Each comparison produces gamma levels (discrete similarity buckets).

| Field | Type | Thresholds | Purpose |
|-------|------|-----------|---------|
| **legal_name** | jaro_winkler | [0.9, 0.8] | Primary identifier; primary signal for match determination |
| **country_code** | exact_match | — | Blocking rule only (not used in comparison); preserves EM flexibility |
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
  - country_code                    # Primary: same country (strict)
  - [country_code, nuts_code]       # Secondary: same country AND same EU region
```

**Semantics:**
- Rule 1: Pair must have matching country codes
- Rule 2: Pair must have matching country codes **AND** matching NUTS codes (if both present)
- At least one rule must fire for comparison to occur

**Effect:** Drastically reduces comparison volume while preserving global comparisons within country.

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

Once `auto_train_threshold` is reached, background EM estimation updates m/u parameters based on your data. This is more accurate than cold-start but requires representative data (ideally ~500+ mentions).

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
