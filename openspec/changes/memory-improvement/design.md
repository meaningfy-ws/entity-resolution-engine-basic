> Parent: EPIC `memory-improvement` (`proposal.md`) — DEC-1 … DEC-10

## Context

Current request path (single thread, `entrypoints/app.py` → `RedisQueueWorker.process_single_message`):

```
BRPOP ere_requests
 └─ log.info(full raw RDF)                                   ← L8 (DEC-10)
 └─ get_request_from_message → EntityResolutionService.process_request
     └─ resolve_to_result
         ├─ mapper.map_entity_mention_to_domain (rdflib)
         ├─ check_conflict      → mentions scan            O(N), no index
         ├─ find_cluster_for    → clusters scan            O(N), no index
         └─ EntityResolver.resolve
             ├─ linker.find_matches      Splink: +1 predictions TABLE, +1 view  (leak, L1/L2)
             ├─ similarity_repo.save_all register("df_temp")                    (L6)
             ├─ cluster_repo.find_cluster_of(best)                O(N)
             ├─ cluster_repo.save
             ├─ cluster_repo.get_all_memberships()  O(N) + N Python objects      (L4)
             ├─ mention_repo.save
             ├─ linker.register_mention  pd.concat full copy + re-register     (L3)
             ├─ count == 50 → background EM on a new in-memory DuckDB           (L7: old con not closed)
             └─ _gen_cand: similarities scan + find_cluster_of × links   O(links·N)
LPUSH ere_responses
```

Three DuckDB instances today: the persistent resolver file, Splink's in-memory connection and one more per
training run. Splink's search space is a pandas `_tf_df` registered as a view (zero-copy), built empty
at start, so it is empty after every restart even though `mentions` is persisted. Evidence and benchmark:
`inputs/MEMORY-ANALYSIS.md`.

Constraints: one thread (DEC-1), cosmic-python layering enforced by import-linter (Splink/DuckDB stay in
`adapters/`, config parsing in `services/`), versions pinned in `src/pyproject.toml` (duckdb 1.5.2,
splink 4.0.16).

## Goals / Non-Goals

**Goals:**
- Flat RSS and flat per-mention latency from N = 1k to N = 300k stored mentions (DEC-9 budget ≤360 ms mean).
- No DuckDB catalog growth per request.
- On-disk DuckDB by default, configurable by environment (DEC-6), shared by the resolver and Splink (DEC-7).
- Warm start and one-time frozen training that survives a restart (DEC-4, DEC-5).
- Payload-free lifecycle tracing with per-stage timings (DEC-10).

**Non-Goals:** everything listed in the EPIC's No-gos.

## Decisions

- **D-1 Single DuckDB database, two cursors.** `services/factories.py` opens one `duckdb.connect(path,
  config=…)`. The repositories use the connection; the Splink `DuckDBAPI` gets `con.cursor()` (same
  database, same buffer pool, same `memory_limit`). *Alternative rejected:* a separate Splink scratch file.
  It doubles the buffer pools and needs cross-database reads of `mentions`. (DEC-7)
- **D-2 DuckDB settings object.** A `DuckDBSettings` value object (storage, path, memory_limit, threads,
  temp_directory) is resolved in `services/resolver_config.py`. Precedence is: environment variable > `resolver.yaml` >
  default. Constants name the env keys (no free strings). An adapter factory turns it into `duckdb.connect`
  config. `memory` storage opens `:memory:` with the same `memory_limit`/`temp_directory`. Default
  `temp_directory` = `<db_path>.tmp` (disk) or `ERE_DUCKDB_TEMP_DIR` (required writable path for memory mode on
  read-only filesystems). (DEC-6)
- **D-3 Search space = `mentions` table.** The Splink `Linker` is built on the table name `mentions`, and
  `__splink__df_concat_with_tf` is registered as a *view* over `mentions` with a constant
  `__splink_salt` column. `register_mention` becomes a no-op on the Splink side (the repository `INSERT`
  already happened). **Gated by spike T1.1**: if Splink materialises or caches the view contents, fall back
  to today's pandas registration and keep the other slices. (DEC-7)
- **D-4 Per-request artefact cleanup.** After `find_matches_to_new_records(...).as_pandas_dataframe()`,
  call `drop_table_from_database_and_remove_from_cache()` on the returned SplinkDataFrame, and drop
  the `__splink__df_new_records_*` view(s) plus their `_intermediate_table_cache` entries. Scope: only those two
  name patterns, never a broad cache sweep. `save_all` unregisters `df_temp` after the INSERT.
- **D-5 Candidates in one query.** `resolve()` passes the links it already holds into candidate
  generation. The DuckDB adapter adds a `ClusterRepository.clusters_for(mention_ids) -> dict` bulk lookup
  (one `WHERE mention_id IN (…)`/join). The idempotent path (`find_cluster_for`) keeps reading
  `similarities`, now indexed. The port contract stays in `adapters/repositories.py`; the in-memory stubs get the
  same method.
- **D-6 Indexes.** `init_schema` adds `CREATE INDEX IF NOT EXISTS` on `mentions(mention_id)`,
  `clusters(mention_id)`, `similarities(mention_id_l)`, `similarities(mention_id_r)`. It is idempotent on
  existing files (migration = start-up).
- **D-7 Training: once, persisted, frozen.** `auto_train_threshold` default is 200, trigger `count ==
  threshold` (as today, one shot). On success, save with `linker.misc.save_model_to_json(model_path,
  overwrite=True)`, where `model_path` = `<db_path>.splink_model.json` (env `ERE_SPLINK_MODEL_PATH` overrides).
  On start: if the model file exists, build the linker from it (no cold start, no training); else if `count ≥
  threshold`, train once synchronously at start on a sample of `threshold` mentions; else apply the cold-start parameters.
  Training runs on its own `con.cursor()` of the same database. After the swap nothing else needs closing,
  because cursors are released with the old linker. The `_training_in_progress` guard stays. (DEC-4, DEC-5)
- **D-8 Lifecycle tracing.** A small `MentionTrace` helper in `services/` (a timer plus a structured `extra`)
  emits events: `dequeued`, `parsed`, `guard`, `scored`, `clustered`, `persisted`, `responded`. Each event is
  one DEBUG line; one INFO summary line is emitted per request. Fields: `ere_request_id`, `mention_id`,
  `entity_type`, `payload_bytes`, `queue_wait_ms` (only when `request.timestamp` is set), `guard`
  (`conflict|idempotent|new`), `links`, `best_score`, `decision` (`join|new`), `cluster_id`, per-stage
  `*_ms`, `total_ms`. Event names and field keys are constants (see Test-pinned interfaces for stage ownership). (DEC-10)
- **D-9 TRACE hygiene.** Remove `get_all_memberships()` from `resolve()`. Wrap the remaining expensive TRACE
  arguments (`dict(row)`, `to_flat_dict()` in logs, blocking-rule stringification) in
  `if log.isEnabledFor(TRACE_LEVEL_NUM)`. Delete the `FIXME to be deleted` detail block.

### Test-pinned interfaces

The tests written ahead of implementation (marked `pending`) bind these names. Changing a name means updating
this section and the tests in one commit.

| Layer | Name | Contract |
|---|---|---|
| adapters | `ere.adapters.duckdb_connection.DuckDBStorage` | `StrEnum`: `DISK="disk"`, `MEMORY="memory"` |
| adapters | `ere.adapters.duckdb_connection.DuckDBSettings` | frozen: `storage`, `path: str`, `memory_limit: str\|None`, `threads: int\|None`, `temp_directory: str\|None` |
| adapters | `ere.adapters.duckdb_connection.open_duckdb(settings)` | returns a connection with the settings applied; disk default `temp_directory` = `<path>.tmp` |
| adapters | `ClusterRepository.clusters_for(mention_ids) -> dict[MentionId, ClusterId]` | bulk lookup; unknown ids omitted (DuckDB adapter + in-memory stub; the stub reads its dict directly, not via `find_cluster_of`) |
| adapters | `SpLinkSimilarityLinker(entity_fields, config, connection=None, model_path=None)` | keyword args; reads the search space from `mentions` on `connection` (`None` → private in-memory DB, as today); persists/loads model at `model_path` (`None` → no persistence) |
| adapters | `SpLinkSimilarityLinker.model_source` → `ere.adapters.splink_linker_impl.ModelSource` | `StrEnum`: `COLD_START`, `PERSISTED`, `TRAINED` |
| adapters | `SpLinkSimilarityLinker._release_request_artefacts(result)` | a failure is caught in `find_matches` and logged as WARNING with the exception text |
| services | `ere.services.resolver_config.DuckDBEnvVar` | `StrEnum`: `STORAGE`, `PATH` (`DUCKDB_PATH`), `MEMORY_LIMIT`, `THREADS`, `TEMP_DIR`, `MODEL_PATH` |
| services | `ere.services.resolver_config.resolve_duckdb_settings(config, env) -> DuckDBSettings` | env > yaml > default; `DuckDBConfig()` (no type) → `DISK`; invalid → `ValueError` naming the variable and allowed values |
| services | `ere.services.resolver_config.resolve_model_path(settings, env) -> str\|None` | env, else `<path>.splink_model.json` for disk, else `None` |
| services | `ResolverConfig.auto_train_threshold` default | `200`; same value in every `src/config/resolver*.yaml` |
| services | `ere.services.tracing.TraceEvent` | `StrEnum`: `DEQUEUED`, `PARSED`, `GUARD`, `SCORED`, `CLUSTERED`, `PERSISTED`, `RESPONDED`, `SUMMARY` |
| services | `ere.services.tracing.TraceField` | `StrEnum` of `LogRecord` attribute names: `EVENT="ere_event"`, `REQUEST_ID="ere_request_id"`, `MENTION_ID="mention_id"`, `PAYLOAD_BYTES="payload_bytes"`, `QUEUE_WAIT_MS="queue_wait_ms"`, `GUARD="guard"`, `LINKS="links"`, `BEST_SCORE="best_score"`, `DECISION="decision"`, `CLUSTER_ID="cluster_id"`, `STAGE_MS="stage_ms"`, `TOTAL_MS="total_ms"` |
| services | `ere.services.tracing.GuardOutcome` / `ClusterDecision` | `StrEnum`: `CONFLICT`/`IDEMPOTENT`/`NEW` and `JOIN`/`NEW` |

Stage ownership: the entrypoint emits `dequeued`, `responded`, `summary`; the service emits `parsed` (after RDF
mapping), `guard`, `scored`, `clustered`, `persisted`.

## Algorithm / approach

Target request path:

```
BRPOP ere_requests                          trace: dequeued(payload_bytes, queue_wait_ms)
 └─ parse RDF → Mention                     trace: parsed(rdf_ms)
 └─ guard: find_by_id / find_cluster_of     indexed O(log N)   trace: guard
 └─ find_matches                            Splink reads `mentions` view; blocking join O(N_country)
      └─ drop predictions table + new-records view               trace: scored(links, splink_ms)
 └─ similarities INSERT (unregister df_temp)
 └─ best link → clusters_for({best}) → join | new               trace: clustered(decision)
 └─ clusters INSERT, mentions INSERT        ← Splink sees it next request (view)
 └─ count == 200 → background train on cursor → save model JSON → swap
 └─ candidates from in-hand links + clusters_for(link ids)      trace: persisted
LPUSH ere_responses                         trace: responded(total_ms)  + INFO summary
```

Worked example (warm start after restart, N = 120k, model file present):
1. Start: open `/data/app.duckdb` (disk, memory_limit from env), `init_schema` (indexes exist), load the Splink
   model JSON, register the `mentions` view. No training.
2. Mention 120,001 (DEU): guard is 2 index lookups; Splink compares against ~22k DEU mentions in DuckDB;
   9 links; the per-request tables are dropped; one bulk cluster lookup; 3 inserts. Catalog size is the same as before
   the request.

Idempotency: replaying a request with the same `mention_id` and the same content returns the current candidates without
new rows (unchanged `find_cluster_for` path). With different content it returns a `ConflictError` response (unchanged).

### Anti-patterns
- ❌ Logging request bodies, RDF, or `mention.attributes` at INFO/DEBUG (DEC-10).
- ❌ Sweeping all `__splink__*` tables or clearing Splink's whole cache; this would drop the registered
  search space and model tables.
- ❌ Materialising `mentions` into pandas anywhere on the request path.
- ❌ Retraining on restart when a model file exists; periodic retraining (DEC-4).
- ❌ Reading `os.environ` outside `resolver_config.py`/`app.py`; free-string env keys.
- ❌ Sharing one DuckDB connection object across threads (the training thread uses its own cursor).
- ❌ Importing duckdb/splink in `services/` beyond the existing factory wiring (import-linter).

## Error matrix

| Failure mode | Expected handling |
|---|---|
| Invalid `ERE_DUCKDB_STORAGE` / unparsable memory limit | Fail at start with a clear error; process exits non-zero (existing build-failure path) |
| Temp dir not writable | DuckDB error at first spill → request gets an `EREErrorResponse`; start-up logs the resolved temp dir to make this diagnosable |
| DuckDB exceeds `memory_limit` and cannot spill | Request returns `EREErrorResponse` (InternalError); service keeps consuming |
| Dropping a per-request Splink artefact fails | Log WARNING with table name; response still sent (cleanup never fails a request) |
| Model JSON missing | Cold start, or one-time start-up training if N ≥ threshold (D-7) |
| Model JSON corrupt/incompatible | Log ERROR, fall back to cold-start parameters, **do not** overwrite the file |
| EM training fails | WARNING (as today); cold-start parameters remain; no model file written |
| Spike T1.1 negative (Splink caches view) | Keep pandas registration (today's behaviour) for the search space; D-4/D-5/D-6/D-8 still ship |
| `request.timestamp` absent | `queue_wait_ms` omitted from trace, no error |
| Duplicate delivery of a request | Idempotent path returns current candidates; no duplicate rows |

## Risks / Trade-offs

- [Splink private API for cleanup/cache] → pin version; regression test T2.4 fails if tables accumulate.
- [Indexes cost insert time and memory] → only 4 lookup columns; the profiling harness measures insert cost.
- [On-disk I/O slower than RAM for small N] → buffer pool keeps hot pages in memory; `ERE_DUCKDB_STORAGE=memory`
  remains available.
- [Search space via view changes Splink's column types/nulls] → view casts entity fields to VARCHAR and
  `COALESCE(field, '')`, matching today's `build_tf_df` normalisation; parity test T4.3 compares links on a
  fixture corpus.
- [Scores differ before/after training freeze across a restart] → accepted (DEC-3).
- [Local venv versions differ from pins] → T1.0 aligns them before any benchmark.

## Open Questions

- ~~Does `find_matches_to_new_records` re-read a registered *view* on every call, or cache its result?~~
  **Resolved by spike T1.1 (splink 4.0.16, duckdb 1.5.2): it re-reads.** Registering
  `__splink__df_concat_with_tf` as a `SplinkDataFrame` whose physical name is a view over `mentions` makes rows
  inserted after registration visible to the next call. D-3 proceeds. Spike findings that shape D-4:
  - Passing a *list of records* registers a new `__splink__df_new_records_<uid>` replacement view on the cursor per
    call. Passing a **fixed table name** instead (the adapter registers the single new record under one constant
    name, overwritten each request) avoids the per-request name and the cache entry.
  - `blocked_pairs` is already dropped by Splink; the `__splink__find_matches_predictions_*` table is dropped by
    `drop_table_from_database_and_remove_from_cache()`.
  - `executed_queries` and `queries_retrieved_from_cache` grow per call; reset them with
    `reset_executed_queries_tracker()` / `reset_queries_retrieved_from_cache_tracker()`.
- Is `linker.misc.save_model_to_json` + `Linker(settings=<json>)` round-trip lossless for the cold-start-modified
  comparison levels in Splink 4.0.16? (Covered by T5.2.)

---

# Part 2 — Throughput (design)

> Parent: EPIC `memory-improvement` Part 2 — DEC-11 … DEC-25. Part 1 decisions D-1 … D-9 stand except D-6
> (`similarities` indexes, superseded by D-19).

## Context (Part 2)

Measured in this session (local SSD, synthetic corpus cycling `org-mid.csv`):

| Measurement | Result |
|---|---|
| Part 1 profile, mean / p95 ms per mention | 87 / 94 at 2.5k → 115 / 151 at 10k → 188 / 600 at 17.5k; catalog flat (4) |
| Autocommitted single-row insert vs inside one transaction | 2.9 ms vs 0.11 ms |
| RDF parse + extraction (S3) | 0.4 ms @ 1.7 KB, 4 ms @ 20 KB, 39 ms @ 200 KB, 440 ms @ 2 MB |
| Links stored per mention (memo, 5k real mentions) | 6.3, rising with N |
| S5 sample, first 300 rows of `org-mid.csv` (**sample only**, too few reference pairs to judge recall) | country-only: 13.5 pairs/mention, 109 merged pairs of which 98 `unrelated`; A: 0.5 pairs/mention, 37 merged / 27 `unrelated`; both kept all 9 `exact` pairs |
| S5 round 1, full `org-mid.csv` (`inputs/s5-blocking-round1.md`) | country-only 267 pairs/mention, 93 % of merges unrelated, 1.45 M links; A 12.5 pairs/mention, all reference pairs admitted, 85 % unrelated merges, not faster at 5.5k (prefix recomputed per call); B 2.2 pairs/mention, 0.1 % unrelated, misses 1.5 % `exact` pairs (raw-name Jaro-Winkler) |
| S5 round 2 (`inputs/s5-blocking-round2.md`) | derived-key rules over-merge at every threshold (address-only pairs score > 0.9); B_norm (country + Jaro-Winkler ≥ 0.8 on normalised name): 2.4 pairs/mention, 100 % reference pairs admitted, 96.2 % `exact` clustered, 1–6 unrelated merges of ~5,700; removing the `country_code` comparison: links 8,509 → 6,744, unrelated 6 → 1 |
| S1 batch cost (`inputs/s1-batch-cost.md`, 25k stored, name-similarity blocking) | one Splink call ≈ 70 ms fixed; ms/record: 71.5 (1), 9.5 (10), 3.8 (50), 3.3 (100), 2.6 (500) |
| S2 commit cost | 2.9 ms per autocommitted insert vs 0.11 ms in one transaction (local SSD) |

Scoring cost per mention is dominated by pairs from the country block: blocking on `country_code` alone brings every
stored same-country mention into Jaro-Winkler comparisons on five fields, of which ~6 survive
`match_weight_threshold`. The rule `[country_code, nuts_code]` adds nothing because `country_code` alone already
matches those pairs.

## Measurements (DEC-18)

| Spike | Measures | Proceeds if | Otherwise |
|---|---|---|---|
| S6 diagnosis | per-stage ms (score / persist / rank / commit) and links per mention at 2.5k…25k, corpus without name cycles | — | reorders slices by the largest stage |
| S5 blocking and scoring | `test/stress/spike_blocking.py` on `org-mid.csv` in file order. Round 1: country-only, A, A without the region/town rule, B, and A at `match_weight_threshold` −10 / −5 / 0. Round 2 (chosen blocking): cluster `threshold` 0.2 / 0.5 / 0.7 / 0.9, each with and without the `country_code` comparison. Metrics per run: pairs admitted per mention (exact, SQL), share of `exact` and `variant` reference pairs admitted by blocking and ending in one cluster, merged pairs, `unrelated` merges, joins, stored links, ms per mention (runs in parallel: indicative only) | blocking: ≥ 99 % `exact` and ≥ 95 % `variant` admitted, ≥ 10× fewer pairs; threshold: lowest with `unrelated` < 5 % of merged pairs; comparison removed if `unrelated` drops and `exact` clustering does not | try B, then C (DEC-22); threshold provisional 0.7 |
| S1 batch cost | one Splink call with 1/10/50/100/500 records on a 25k search space, with the chosen blocking | ms per record at 100 ≤ ⅓ of ms at 1 | keep one request at a time; D-10/D-11 dropped |
| S2 commit cost | 1,000 inserts autocommit vs one transaction, local disk and NFS mount | ≥ 3× faster | D-12 kept anyway (no downside), lower priority — **done: 26× on local SSD; NFS not available locally** |
| S3 parse cost | done (Context table) | — | parse pool cancelled (DEC-12) |

## Decisions (Part 2)

- **D-10 Bite taking (DEC-11, DEC-16).** `RedisQueueWorker.take_bite(limit)`: `BRPOP` for the first request (blocking,
  `queue_timeout`), then `LMPOP 1 <queue> RIGHT COUNT <n>` (fallback `RPOP <queue> <n>`) for up to `limit − 1` waiting
  requests, trimmed at `ERE_BATCH_MAX_BYTES`; a request that would exceed the byte cap is pushed back with `RPUSH` so it is
  next; a single request larger than the cap forms a bite of one. If fewer than requested come back, poll every 50 ms until
  `ERE_BATCH_LINGER_MS` elapses or the limit is reached. `BiteSizer` (services) keeps an exponential moving average of
  mentions/second (α = 0.3) and returns `clamp(round(rate × target_seconds), 1, max_mentions)`; the first bite uses
  `max_mentions`. `BatchSettings` resolved from env in `resolver_config.py`, validated `> 0`, env keys as constants.
- **D-11 Resolving a bite (DEC-13, DEC-14, DEC-15, DEC-20).** `EntityResolutionService.process_batch(requests)`:
  1. per request, in order: map RDF, conflict guard, idempotency guard; guarded or failed requests are answered and leave
     the bite; a duplicate `mention_id` later in the bite follows the idempotent/conflict path against the earlier one.
  2. `EntityResolver.resolve_batch(mentions)` inside one unit of work: insert mentions → `linker.find_matches_batch` (one
     Splink call; the search space already contains the bite) → drop self-links and links to later bite members → rank
     candidates per mention from all its links (one bulk `clusters_for`, assignments of earlier bite members applied in
     memory) → assign clusters in arrival order → keep top `top_n` links per mention → bulk insert links and clusters.
  3. on any exception in step 2: rollback, then resolve each pending mention on the single path.
  4. training trigger: `before < threshold ≤ after`.
- **D-12 Unit of work (DEC-13).** `DuckDBUnitOfWork` (adapters): context manager issuing `BEGIN`/`COMMIT`/`ROLLBACK` on the
  shared connection; repositories and the linker's scoring cursor run inside it. The single path `resolve()` uses it too.
- **D-13 Columnar links (DEC-17).** Port `SimilarityLinker.find_matches_batch(mentions) -> LinkTable`; `LinkTable` (models)
  is a frozen dataclass of three parallel tuples (`left_ids`, `right_ids`, `scores`). The Splink adapter fills it from
  `predictions` with one Arrow fetch; `SimilarityRepository.save_table(link_table)` inserts through one registered Arrow
  view. `MentionLink` objects only for best links and candidates. `find_matches` delegates to the batch path.
- **D-16 Pipelined responses (DEC-16).** One `pipeline(transaction=False)` of `LPUSH` calls in request order per bite.
- **D-17 Batch tracing.** `MentionTrace` gains `batch_id` (uuid4 hex) and `batch_size`; `BatchTrace` emits one INFO `batch`
  event (`batch_size`, `payload_bytes`, `score_ms`, `persist_ms`, `total_ms`, `bite_limit`). Per-request `scored` carries
  the request's links and best score; stage ms are the bite's.
- **D-18 Name-similarity blocking (DEC-21, DEC-22).** `init_schema` adds `legal_name_norm TEXT` to `mentions`.
  `DuckDBMentionRepository.save` fills it in the same INSERT with the SQL expression
  `NULLIF(regexp_replace(strip_accents(lower(?)), '[^\p{L}\p{N}]', '', 'g'), '')` (one definition, constant
  `NORMALISED_NAME_SQL` in `adapters/duckdb_schema.py`). The search-space view exposes `legal_name_norm`; the adapter's
  new-record view computes it with the same expression. Scoring blocking rule (from `resolver.yaml`, rendered as SQL):
  `l.country_code = r.country_code AND jaro_winkler_similarity(l.legal_name_norm, r.legal_name_norm) >= 0.8`.
  Config shape: `blocking_rules: [{same: country_code, similar: {field: legal_name, min_jaro_winkler: 0.8}}]`, validated at
  start (unknown keys refuse to start). NULL names never block. The EM training rule is `block_on("country_code")`
  explicitly. Fallback C (name-token pre-block) only if 13.2 shows the name comparison dominating.
- **D-19 Top-k links, no `similarities` indexes (DEC-19).** Ranking uses all links of a mention; before insert, links per
  left mention are cut to the `top_n` highest scores. `init_schema` issues `DROP INDEX IF EXISTS` for the two
  `similarities` indexes and no longer creates them; the Part 1 test asserting four indexes changes to two.
- **D-20 Match-weight threshold (DEC-23).** Only changed in `resolver*.yaml` if S5 shows the response candidates the ERS
  uses (top candidate and those ≥ `threshold`) are identical at the higher value.
- **D-22 Reference pairs (S5).** No ground truth exists for the corpus, so S5 measures against pair sets computed in SQL
  on normalised names `regexp_replace(strip_accents(lower(legal_name)), '[^\p{L}\p{N}]', '', 'g')`:
  `exact` = same country and identical normalised name (5,464 pairs in `org-mid.csv`); `variant` = same country,
  different normalised name, Jaro-Winkler ≥ 0.95 and same `post_code`; `unrelated` = Jaro-Winkler < 0.8. A 0.92–0.95
  similarity band was rejected as a reference: sampled pairs were distinct organisations ("Département de la Savoie" /
  "Département de la Vendée").
- **D-23 Threshold and country comparison (DEC-26, DEC-27).** In all `resolver*.yaml`: `threshold: 0.7`; remove
  `country_code` from `splink.comparisons` and `splink.cold_start.comparisons`. Test fixtures relying on 0.20 pin their
  own threshold.
- **D-21 No migration (DEC-24).** Part 2 schema changes (new `legal_name_norm` column, dropped `similarities` indexes)
  apply to new databases; start-up refuses an existing `mentions` table without `legal_name_norm` with an error telling
  the operator to reset the database file.

### Test-pinned interfaces (Part 2, candidate scoring)

| Layer | Name | Contract |
|---|---|---|
| adapters | `ere.adapters.duckdb_schema.normalised_name_sql(expression: str) -> str` | SQL normalising a name expression: `NULLIF(regexp_replace(strip_accents(lower(expr)), '[^\p{L}\p{N}]', '', 'g'), '')` |
| adapters | `ere.adapters.duckdb_schema.NORMALISED_SUFFIX` | `"_norm"`; the stored column for `legal_name` is `legal_name_norm` |
| adapters | `ere.adapters.duckdb_schema.OutdatedSchemaError` | raised by `init_schema` when `mentions` exists without the normalised column |
| adapters | `ere.adapters.splink_linker_impl.BlockingRuleKey` | `StrEnum`: `SAME="same"`, `SIMILAR="similar"`, `FIELD="field"`, `MIN_JARO_WINKLER="min_jaro_winkler"` |
| adapters | `ere.adapters.splink_linker_impl.render_blocking_rules(rules: list) -> list[str]` | SQL over `l.`/`r.`; a `{same, similar}` entry → `l.<same> = r.<same> AND jaro_winkler_similarity(l.<field>_norm, r.<field>_norm) >= <min>`; a field name or list keeps today's equality rules; unknown keys → `ValueError` naming the key and allowed keys |
| adapters | `ere.adapters.splink_linker_impl.EM_TRAINING_FIELD` | `"country_code"`; `_get_em_training_rule()` blocks on it |
| services | `build_entity_resolver` on an outdated database | raises `OutdatedSchemaError` whose message names the database path and says to reset it |

### Test-pinned interfaces (Part 2, batching)

| Layer | Name | Contract |
|---|---|---|
| models | `ere.models.resolver.LinkTable` | frozen dataclass: `left_ids: tuple[str, ...]`, `right_ids: tuple[str, ...]`, `scores: tuple[float, ...]`; `__len__`; `links_for(mention_id) -> list[MentionLink]` |
| models | `SimilarityLinker.find_matches_batch(mentions: list[Mention]) -> LinkTable` | port method; the in-memory stub and the Splink adapter implement it |
| adapters | `SimilarityRepository.save_table(table: LinkTable) -> None` | DuckDB adapter and in-memory stub |
| adapters | `ere.adapters.duckdb_unit_of_work.DuckDBUnitOfWork(con)` | callable returning a context manager: `BEGIN` on enter, `COMMIT` on success, `ROLLBACK` on exception |
| services | `EntityResolver(..., unit_of_work: Callable[[], ContextManager] \| None = None)` | `None` → no transaction management (in-memory stubs) |
| services | `EntityResolver.resolve_batch(mentions: list[Mention]) -> list[ResolutionResult]` | results in input order; one `unit_of_work()` per call; intra-bite matching (D-11) |
| services | `EntityResolutionService.process_batch(requests: list[ERERequest], traces: list[MentionTrace] \| None = None) -> list[EREResponse]` | one response per request, input order; per-request guards; rollback → one-by-one fallback |
| services | `ere.services.resolver_config.BatchEnvVar` | `StrEnum`: `TARGET_SECONDS="ERE_BATCH_TARGET_SECONDS"`, `MAX_MENTIONS="ERE_BATCH_MAX_MENTIONS"`, `MAX_BYTES="ERE_BATCH_MAX_BYTES"`, `LINGER_MS="ERE_BATCH_LINGER_MS"` |
| services | `ere.services.resolver_config.BatchSettings` + `resolve_batch_settings(env) -> BatchSettings` | frozen: `target_seconds=2.0`, `max_mentions=500`, `max_bytes=50_000_000`, `linger_ms=250`; non-numeric or ≤ 0 → `ValueError` naming the variable |
| services | `ere.services.bite_sizer.BiteSizer(settings)` | `limit() -> int` (first: `max_mentions`); `observe(mentions: int, seconds: float)` updates an EMA (α = 0.3) of mentions/s; `limit = clamp(round(rate × target_seconds), 1, max_mentions)` |
| services | `ere.services.tracing.TraceEvent.BATCH`, `TraceField.BATCH_ID`, `BATCH_SIZE`, `BITE_LIMIT`; `BatchTrace(logger, bite_limit)` | per-bite INFO `batch` event; `BatchTrace.request_trace(request_id)` returns a `MentionTrace` carrying `batch_id`/`batch_size` |
| entrypoints | `RedisQueueWorker(..., batch_settings: BatchSettings \| None = None)` | `None` → defaults |
| entrypoints | `RedisQueueWorker.take_bite(limit: int) -> list[bytes]` | `BRPOP` then multi-pop up to `limit − 1`, byte cap with `RPUSH` push-back, linger (D-10) |
| entrypoints | `RedisQueueWorker.process_bite() -> int` | takes a bite with the sizer's limit, resolves, pushes responses in one pipeline; returns requests processed (0 on timeout) |

### Implementation notes (apply session 2)

- **Scoring runs on the shared connection, not a cursor (amends D-1).** A DuckDB cursor is a separate connection with its
  own transaction, so it cannot see the bite's mentions inserted earlier in the same unit of work; intra-bite matching
  failed silently with stubs passing. The scoring linker now uses the shared connection; EM training still uses its own
  cursor. Covered by the Splink-backed scenario "Similar mentions in one bite match with the production configuration".
- **Trained parameters are handed over, not swapped in from the training thread (amends D-7).** Building a scoring linker
  runs queries; doing that from the training thread would use the shared connection from two threads. Training stores
  the trained settings; the resolver thread rebuilds the scoring linker before its next scoring call.
- **pandas instead of Arrow (amends D-13).** `pyarrow` is not a project dependency; new records are registered as a
  pandas frame and links are fetched with one `SELECT` from the predictions table into a `LinkTable`.
- **Link orientation.** `LinkTable` and `MentionLink` from the linker point from the new mention (left) to the scored
  mention (right); `similarities.mention_id_l` is now the mention whose resolution produced the link.
- **`process_batch` signature.** `process_batch(requests, traces: list[MentionTrace] | None)`: the worker creates one trace
  per request from a `BatchTrace`; `process_request` and `resolve` are bites of one.
- **Worker compatibility.** `process_single_message()` remains as a bite of one; `app.py` loops on `process_bite()`.

### Review findings (architecture lens L3, 2026-09-17)

No blockers; layers and import direction hold. Should-fix: (1) factory depended on the Splink class for the start-up
training decision — **fixed** via `SimilarityLinker.needs_training()`; (2) blocking-rule validation and the raw config dict
live in the Splink adapter — task 14.3; (3) `NORMALISED_FIELDS` and `EM_TRAINING_FIELD` hard-coded in adapters — task 14.3;
(4) model-lifecycle logging in the adapter — task 14.4; (5) exception text and tracebacks could carry data values —
**fixed** (type at ERROR/WARNING, traceback at DEBUG; Splink cleanup warnings keep table names). Nice-to-have: ports split
between `models/ports` and `adapters/` (#6), composition root in `services/` (#8), queue mechanics in the entrypoint (#9) —
task 14.7. Also fixed: `ere.models` added to the import-linter layers; `train()` check-then-set race replaced by a
non-blocking lock.

### Review findings (adversarial correctness, clean code and tests, 2026-09-17)

Correctness (L2/L4) — fixed test-first: **B1** non-string `ere_request_id` crashed the worker loop and dropped the bite
(now answered with `unknown`); **B2** one failing mention turned its whole bite into errors (the service now owns the
fallback and resolves each request of a failed bite on its own, per-request errors); **S1** a failed bite emitted trace
events twice (resolver stages are buffered and emitted only after commit); **S2** trained parameters were lost if the
scoring-linker rebuild failed (cleared only after success); **S3** non-finite batch settings accepted (`math.isfinite`);
**S4** blocking-rule validation gaps (missing keys, `nan`/out-of-range similarity, non-identifier field names now refused);
N1 linger overshoot and `ERE_BATCH_LINGER_MS=0` rejected; N2 negative queue wait; N3 one unserialisable response lost the
pipeline. Open nice-to-haves: N5 guard tagged union, N6 remaining free strings, N7 unused `save_all`, N8 long legacy
methods, N9 per-mention dict merge (removed while fixing S1).

Tests — mutation probes showed 7 of 16 injected bugs undetected. Added or strengthened: push-back order, exact byte cap,
linger lower bound, all-unparsable bite, responses before next bite, EMA pinned, candidates from all links (clusters
reached only by links below top_n), top_n ties, start-up training branches, name normalisation (accents, Cyrillic, Greek,
punctuation, NULL), partial outdated schema, NULL blocking keys, reloaded and trained parameters actually used for scoring,
empty/invalid model files, catalog **and** Splink cache flat against the pre-request baseline, batch event stage timings,
summary content, conflict guard, poisoned mention in a bite, duplicate ids in a bite, Splink-backed "bites equal one at a
time" on 400 reference organisations (`@integration`). Removed the "stored similarities are not recomputed" scenario: no
code path can rewrite stored links, so the test could not fail.

Finding: with training enabled, runs of the same input can cluster differently because background training finishes at a
different moment; with training disabled, bites and one-at-a-time give identical clusters (400 organisations). Accepted
under DEC-3.

### Refactors 14.3 and 14.4 (2026-09-17)

- **Typed blocking settings (models).** Blocking rules are domain values (`EqualityRule`, `NameSimilarityRule`,
  `BlockingSettings`) in `models/resolver/blocking.py` — the Splink adapter cannot import `services/` (import-linter), so
  typed rules live in `models/` and are validated there: known keys, required keys, identifiers, fields among
  `entity_fields`, similarity field among the normalised fields, `0 ≤ min_jaro_winkler ≤ 1` and finite. `ResolverConfig`
  parses them at start-up (`splink.blocking_rules`, optional `splink.normalised_fields` default `[legal_name]`, optional
  `splink.em_blocking_field` default `country_code`). The adapter renders SQL from typed rules only; `init_schema` and
  `DuckDBMentionRepository` take the normalised fields as a parameter. The linker still reads Splink comparisons and
  cold-start parameters from the raw section (adapter-specific; not in scope).
- **Model lifecycle reported, not logged, by the adapter.** `ModelSource`, `ModelStatus`, `TrainingStatus`,
  `TrainingOutcome` in `models/resolver/model_state.py`; the linker port returns `train() -> TrainingOutcome` and exposes
  `model_status()`. `services/model_lifecycle.py` logs them (unusable model file → ERROR with file and error type; failed
  training → WARNING with error type; success → INFO with sample size and persistence; skipped → DEBUG).
  `EntityResolver.train()` and the start-up path in `factories.py` call it. Remaining adapter logging: DEBUG, and the
  per-request cleanup WARNING required by `resolution-resource-bounds`.
- **Finding (pre-existing, out of scope):** `src/config/resolver_compound.yaml` and `resolver_multirule.yaml` have no
  `entity_fields` and reference a `city` field no RDF mapping provides, so `ResolverConfig.from_dict` could never load them
  (true before this change). Decide to fix or delete them in a separate change.

### Clean-architecture alignment (task 14.7, 2026-09-17)

```
entrypoints/  app.py (loop) · queue_worker.py (bites, parsing, tracing, formatting) · bootstrap.py (composition root:
              environment + resolver.yaml → DuckDB, Splink, repositories, unit of work, RDF mapper, services)
services/     entity_resolution_service · model_lifecycle (logging + start-up training rule) · bite_sizer · tracing ·
              resolver_config (typed resolver.yaml only; no environment, no adapter types)
adapters/     duckdb_* · splink_linker_impl · rdf_mapper* · redis_client · redis_request_queue (take bites, push responses)
models/       resolver/ (domain values incl. blocking settings and model state) · ports/ (linker, repositories,
              rdf_mapper, resolver) · exceptions
```

Enforced by import-linter: layers `entrypoints > services > adapters > models`, and `ere.services` may not import
`ere.adapters`. Decisions: ports live with the domain (`models/ports`) so services depend only on abstractions; the
"train at start-up if due" rule is a use-case decision and stays in services (`model_lifecycle.train_on_start_if_due`),
while reading the environment and choosing concrete adapters is composition and moved to `entrypoints/bootstrap.py`;
Redis list mechanics (blocking pop, multi-pop with RPOP fallback, byte cap with push-back, linger, pipelined push) are
infrastructure (`adapters/redis_request_queue.py`), the worker keeps sizing, parsing, service calls, formatting and
tracing. Test layout mirrors it (`test/unit/entrypoints/test_bootstrap_*`, `test/unit/adapters/test_redis_request_queue.py`).

**Configuration fix (task 15.1).** `resolver_compound.yaml` and `resolver_multirule.yaml` could not start the ERE
(`KeyError: 'entity_fields'`), blocked on a `city` field the RDF mapping does not provide (NULL never matches, so that
rule never fired) and used a prior of 0.3 (30 % of random pairs assumed to match). Fixed: `entity_fields` added,
`city` → `post_name`, prior 0.003; every shipped configuration is now built and exercised by a unit test.

## Algorithm (Part 2)

```
loop
  bite     = take_bite(limit = sizer.limit())                # BRPOP + LMPOP, byte cap, linger
  answered, pending = parse_and_guard_each(bite)            # conflict / idempotent / errors
  try:
    with unit_of_work():                                     # one COMMIT
      insert mentions(pending)
      links = linker.find_matches_batch(pending)            # one Splink call, country + name similarity
      keep links to stored or earlier-in-bite mentions
      rank candidates + assign clusters in arrival order
      insert top_n links per mention; insert clusters
  except: rollback; results = [resolve(m) for m in pending]
  push responses(answered ∪ results) in request order        # one pipeline
  sizer.observe(len(bite), elapsed)
```

Worked example: 1,000 queued, target 2 s, max 500, rate 180/s → limit 360 → take 360 (7.2 MB) → 3 idempotent answered →
357 inserted → one Splink call: name-similarity blocking scores ~3 pairs per mention instead of every same-country mention →
2,900 links → drop 357 self-links, 41 links to later members → rank and assign (the 12th mention links to the 5th and
joins its cluster) → 2,480 links stored (none over `top_n`) → one commit → 360 responses in one pipeline → rate updates.

Idempotency: a replayed request in a later bite takes the idempotent path (DEC-25). A bite is all-or-nothing in the
database, so the one-by-one fallback starts from a clean state.

### Anti-patterns (Part 2)
- ❌ Taking "everything that arrived in the last N seconds".
- ❌ Recomputing the normalised name per request, or a second normalisation definition in Python.
- ❌ Building `MentionLink` / pandas rows for every link of a bite.
- ❌ One commit per statement inside a bite; repositories opening their own transactions.
- ❌ Failing the whole bite because one request is malformed.
- ❌ Changing blocking without the S5 recall numbers recorded.

## Error matrix (Part 2)

| Failure mode | Expected handling |
|---|---|
| One payload unparsable in a bite | `ProcessingError` response for that request; the rest proceeds |
| Splink or DuckDB error while resolving a bite | Rollback; each pending mention resolved on the single path; WARNING with `batch_id` and error type |
| Request larger than `ERE_BATCH_MAX_BYTES` | Bite of one |
| `LMPOP` unsupported | `RPOP key count`; logged once at start |
| Pipeline push fails | Exception logged once per bite; no retry (DEC-2) |
| Invalid batch env value | Refuse to start; error names the variable |
| Unknown key in a `blocking_rules` entry | Refuse to start; error names the key and the allowed ones |
| Existing database without `legal_name_norm` | Refuse to start; error tells the operator to reset the database file (DEC-24) |

## Risks / Trade-offs (Part 2)

- [Recall loss: organisations renamed or abbreviated beyond Jaro-Winkler 0.8 are no longer matched] → accepted (DEC-3); S5: 100 % of reference pairs admitted.
- [Name comparison over the whole country block at 300k] → stored normalised name; 13.2 measures; fallback C.
- [Burst responses to ERS] → each bite ≤ target seconds, far inside the 30 s client budget.
- [Intra-bite semantics differ from one-at-a-time] → "same result as one at a time" scenario on a fixture corpus.
- [Larger Splink pair tables per call] → bounded by `max_mentions` and much smaller blocks.

## Open Questions (Part 2)

- ~~Final blocking rule set, thresholds, `country_code` comparison~~ — decided by S5 rounds 1–2 (DEC-21, DEC-23, DEC-26, DEC-27).
- Does name-similarity blocking stay cheap at 300k (DEU ≈ 54k per call)? Answered by 13.2; fallback C otherwise.
- Does `find_matches_to_new_records` with many records keep a per-call fixed cost (S1)?
