# EPIC: memory-improvement — flat memory and latency for a long-running ERE

## Appetite

**Medium** (≈3 weeks). Single-threaded, no algorithm change. If a slice exceeds its budget, cut the
later slices (on-disk search space, profiling at 300k), never the leak fixes.

## Why

In OP's acceptance environment the ERE gets slower during the nightly run: processing grows from 0.2 s to
0.9 s per request, and queue waits grow from 11 s to 47 min. Memory also grows with uptime (vault
assessment *Debugging session 2026-09-17 conclusions*). The static and benchmark analysis
(`inputs/MEMORY-ANALYSIS.md`) shows a per-request DuckDB/Splink leak (~373 KB/request). It also shows
several per-request steps whose cost grows with the number of stored mentions (N), so the service
cannot keep up with a normal day (~25k mentions, <500 MB of RDF) and certainly not with a backlog
(1–6 GB, up to ~300k mentions). Separately, a restart empties the Splink search space, while the mentions stay
in DuckDB. That is why "a restart makes it fast".

## Solution outline

Outcome: **per-mention latency and process memory stay flat as N grows**, up to ~300k stored mentions, on
one thread, consuming from Redis at the rate the ERE can process.

1. **Stop the leaks.** Drop Splink's per-request prediction tables and new-record views, release
   registered DataFrames, and close replaced connections. Remove O(N) work that exists only to build log strings.
2. **Make per-request reads independent of N.** Index the lookup columns, and build cluster candidates in one
   join from the links already computed instead of re-reading `similarities` and scanning `clusters`
   once per link.
3. **One on-disk store, configurable by environment.** DuckDB runs on disk by default for both the resolver
   and Splink, with memory limit, threads and temp directory configurable by environment. Splink reads the search
   space from the persisted `mentions` table instead of a pandas copy, which also gives a warm start after
   a restart.
4. **Train once, then freeze.** EM training runs once at 200 mentions. The trained model is persisted
   and reloaded on start. It never retrains, and stored similarities are never recomputed.
5. **Trace a mention through the ERE.** Structured lifecycle log events per mention (dequeued with queue wait,
   parsed, guard outcome, scored, clustered, persisted, responded), with timings. No payload content is logged.
6. **Prove it.** A profiling harness at N ≈ 25k / 150k / 300k, plus a regression test that the DuckDB
   catalog and RSS stay flat across requests.

## Key decisions

- **DEC-1**: **Single-threaded consumer.** Keep one `BRPOP` at a time. Splink's
  `find_matches_to_new_records` mutates shared settings, and a DuckDB connection is not shareable across
  threads. Backpressure is already native: unconsumed requests stay in Redis. Parallelism is revisited
  only if profiling (DEC-9) shows one thread cannot meet the budget.
- **DEC-2**: **No crash-recovery work.** The ERE is assumed to slow down, not crash. No processing list or ack.
- **DEC-3**: **No clustering-correctness guarantee.** Cluster ids may be arbitrary but should look
  realistic. Greedy online assignment stays as is, and order dependence is accepted.
- **DEC-4**: **Train once at 200 mentions, then freeze.** Change `auto_train_threshold` from 50 to 200,
  with no retraining afterwards. Training learns the m/u parameters of the similarity function. Stored
  similarities are never recomputed, so the first ~200 keep their starting-parameter scores. Rationale:
  EM over `country_code` blocking costs Σ(mentions per country)², which is unbounded if repeated.
- **DEC-5**: **Persist the trained model** (Splink model JSON next to the DuckDB file) and load it on start.
  If there is no model and N ≥ 200, train once at start on a 200-mention sample. Without this, a restarted
  ERE would score with starting parameters forever.
- **DEC-6**: **DuckDB on disk by default, configurable by environment** for both connections:
  `ERE_DUCKDB_STORAGE` (`disk`|`memory`, default `disk`), `ERE_DUCKDB_MEMORY_LIMIT`,
  `ERE_DUCKDB_THREADS`, `ERE_DUCKDB_TEMP_DIR` (default: beside the DB file). Environment variables override
  `resolver.yaml`. DuckDB otherwise sizes memory from host RAM (not the container limit) and writes temp files to the
  current working directory, which is read-only on Fargate.
- **DEC-7**: **Splink shares the resolver's DuckDB instance** through a cursor and reads the `mentions` table as
  its search space. One copy of the corpus and one buffer pool, registration is the existing `INSERT`,
  and the warm start comes for free.
- **DEC-8**: **No retention policy for `similarities`.** Disk space is not a concern. Its performance cost
  comes only from reads, which step 2 removes from the per-request path. Blocking stays at `country_code`
  (narrower blocking is not available in the data).
- **DEC-9**: **Measure before optimising further.** The budget is ≤360 ms per mention on average
  (20k mentions in 2 h) at N = 300k. The harness decides whether any further step (e.g. parallel lanes) is needed.
- **DEC-10**: **RDF content is never written to logs** at any level. Logs carry ids, sizes and timings only.

## Rabbit-holes

- **Splink caching a live table.** Splink caches `__splink__df_concat_with_tf`. It must see newly inserted
  rows without re-registration or a materialised snapshot. Spike this first, and fall back to the current
  registration if Splink cannot, while keeping the leak fixes.
- **Splink internals are private API.** Dropping per-request artefacts relies on table-name patterns and
  `_intermediate_table_cache`. Pin the Splink version and add a regression test so an upgrade that
  re-introduces the leak fails CI.
- **Local vs pinned versions.** The venv has duckdb 1.4.4 / splink 4.0.15, but `pyproject` pins 1.5.2 / 4.0.16.
  Align them before benchmarking.
- **Large outlier mentions (~2 MB RDF).** Parsing time is accepted; avoid any copy or log of the payload.
- **DuckDB indexes** (ART) slow inserts slightly and live in memory. Measure; do not index speculatively
  beyond the four lookup columns.

## No-gos

- Multithreading, multiprocessing or sharded lanes (DEC-1).
- Crash recovery, processing lists, dead-letter queues (DEC-2).
- Re-clustering, cluster merges, or recomputing stored similarities (DEC-3, DEC-4).
- Periodic or repeated EM training (DEC-4).
- Changing blocking rules, comparison levels or thresholds.
- Retention or pruning of `similarities`, `mentions` or `clusters` (DEC-8).
- Changes to the ERS–ERE contract or the response format.
- Documenting reset/restart policy. That goes to OP's installation and testing manuals, outside this change.
- OpenTelemetry instrumentation. Structured logs only.

---

## What Changes

- Drop Splink per-request artefacts after each match. Unregister temporary DataFrames. Close
  replaced connections.
- Remove the unconditional `get_all_memberships()` from `resolve()`. Guard expensive TRACE arguments.
- **BREAKING (logs)**: stop logging the raw request body at INFO. Replace it with lifecycle events carrying
  ids, sizes and timings.
- Add indexes on `mentions(mention_id)`, `clusters(mention_id)`, `similarities(mention_id_l)`,
  `similarities(mention_id_r)`. Compute candidates in a single join from the in-hand links.
- New DuckDB environment variables (DEC-6), default on-disk. Apply the same config to the Splink connection.
- Splink search space backed by the `mentions` table. Warm start after a restart.
- **BREAKING (behaviour)**: `auto_train_threshold` defaults to 200. The trained model is persisted and reloaded,
  with no retraining.
- Profiling harness and flat-memory regression test.

## Capabilities

### New Capabilities
- `resolution-resource-bounds`: per-mention memory and latency stay flat as stored mentions grow; no
  per-request artefacts retained; DuckDB storage and limits configurable by environment, default on-disk.
- `resolution-model-lifecycle`: one-time training at a mention threshold, persisted and reloaded model,
  warm-started search space after a restart, no recomputation of stored similarities.
- `mention-lifecycle-tracing`: structured, payload-free log events tracing a mention from dequeue to
  response, including queue wait and per-stage timings.

### Modified Capabilities
<!-- none — openspec/specs/ is empty -->

## Impact

- **Code:** `adapters/splink_linker_impl.py`, `adapters/duckdb_repositories.py`, `adapters/duckdb_schema.py`,
  `services/factories.py`, `services/entity_resolution_service.py`, `services/resolver_config.py`,
  `entrypoints/queue_worker.py`, `entrypoints/app.py`, `utils/logging.py`, `config/resolver*.yaml`.
- **Tests:** unit tests per layer, BDD for resolution with warm start and frozen training, and a stress/profiling harness
  under `test/stress/`.
- **Infra/docs:** `infra/compose*.yaml` (a memory limit and the new environment variables), environment variable docs in `app.py` and the
  installation manual (new variables, writable temp dir — closes manual erratum 8.1).
- **Operations:** existing deployments keep their DuckDB file. On first start after upgrade, the ERE trains once
  (DEC-5) and warm-starts from the stored mentions.
- **Source inputs:** `inputs/MEMORY-ANALYSIS.md`; vault assessment *Debugging session 2026-09-17 conclusions*.

---

# Part 2 — Throughput (extension, 2026-09-17)

> Extends this EPIC instead of opening a new one: same bet (the ERE must not be the bottleneck), second
> lever. Part 1 made the ERE stop leaking and warm-start; Part 2 makes per-mention cost small and independent of
> corpus size. Part 1 decisions stand except where a Part 2 decision explicitly amends one.

## Appetite (Part 2)

**Medium** (≈3 weeks), gated by short measurements (DEC-18). A slice whose measurement shows no gain is dropped,
not built.

## Why (Part 2)

Part 1 profiling (synthetic corpus, local SSD) shows the catalog stays flat but latency still grows with N:
87 ms mean / 94 ms p95 at 2.5k stored mentions, 188 ms / 600 ms at 17.5k. For every new mention Splink scores
**every stored mention of the same country** (blocking on `country_code` alone; the second rule
`country_code + nuts_code` is redundant) and then discards almost all of them (~6 links per mention survive at 5k),
so scoring work grows with the country block (~54k mentions for DEU at 300k). On top, each mention pays a fixed
cost: one Splink SQL pipeline per call, ~3 autocommits (measured locally: 2.9 ms per autocommitted insert vs
0.11 ms inside a transaction; OP's DuckDB is on EFS) and one Redis round-trip each way. RDF parsing was measured
and is not a bottleneck: 4 ms for an average 20 KB mention, 440 ms for a 2 MB outlier.
A backlog of up to ~300k mentions (1–6 GB of RDF) must clear in a couple of hours.

## Solution outline (Part 2)

Outcome: **per-mention cost that does not track the size of the country block, several-fold higher throughput
under backlog, no added latency when the queue is quiet, bites still sized to what the ERE can digest.**

1. **Find where the time goes.** Per-stage timings and links per mention in the profile harness, on a corpus
   without artificial near-duplicates.
2. **Score fewer, plausible pairs.** Block on country plus name similarity over a stored normalised name; raise the
   cluster threshold to 0.7; drop the score-inflating `country_code` comparison (all measured in S5).
3. **Store less.** Keep only each mention's best `top_n` links; drop the `similarities` indexes.
4. **Pay fixed costs once per bite.** Adaptive micro-batching: one Splink call, one transaction, one pipelined
   response push per bite; mentions in a bite can match each other.
5. **Keep links columnar.** No per-link Python objects between Splink and `similarities`.

## Key decisions (Part 2)

- **DEC-11**: **Bite = processing-time budget, not arrival window.** A pure time window takes everything already
  queued when there is a backlog, so its size would be unbounded exactly when the ERE is overloaded, and a bite that
  outlasts the ERS client budget (30 s) turns every answer into a provisional one. The bite limit is: mentions
  processed per second (moving average of recent bites) × `ERE_BATCH_TARGET_SECONDS` (default 2 s), bounded by
  `ERE_BATCH_MAX_MENTIONS` (default 500) and `ERE_BATCH_MAX_BYTES` (default 50 MB). No waiting while requests are
  queued; when the queue drains mid-bite, linger up to `ERE_BATCH_LINGER_MS` (default 250 ms).
- **DEC-12**: **RDF parsing stays on the resolver thread.** Measured at 4 ms per average mention (2–5 % of
  resolution time); a worker pool would add failure modes for no material gain. Part 1 DEC-1 stands unchanged.
- **DEC-13**: **One DuckDB transaction per bite** (and per request on the single-mention path).
- **DEC-14**: **Intra-bite matching by insert-then-score.** Mentions of the bite are inserted before scoring;
  self-links and links to later bite members are dropped; greedy assignment follows arrival order (DEC-3 applies).
- **DEC-15**: **Failure isolation.** Parse, conflict and idempotency are decided per request; if scoring or
  persisting a bite fails, the transaction rolls back and the bite is resolved one request at a time.
- **DEC-16**: **Pipelined Redis I/O.** One multi-pop per bite; responses pushed in one pipeline, in request order.
- **DEC-17**: **Columnar links.** Links stay as Arrow/DuckDB data from Splink to `similarities`; domain objects only
  for best matches and returned candidates. Ranking logic stays in `services/`.
- **DEC-18**: **Measure first.** S1 batch cost (Splink call with 1/10/50/100/500 records), S2 commit cost
  (autocommit vs one transaction, local disk and an NFS mount), S3 parse cost (**done**: 4 ms avg / 440 ms at 2 MB →
  DEC-12), S5 blocking recall and speed on `test/stress/data/org-mid.csv`, S6 per-stage diagnosis of latency growth.
- **DEC-19**: **Top-k link storage, no `similarities` indexes.** Only each new mention's `top_n` highest-scoring links
  are stored; candidates in the response are still built from all links computed for the request, so responses do
  not change. The `similarities` ART indexes are removed; `mentions`/`clusters` indexes stay. Impact expected small
  (typically far fewer than `top_n` links per mention); it caps dense near-duplicate groups and insert cost.
- **DEC-20**: **Training trigger on crossing.** Training starts when a bite moves the stored count from below to at or
  above the threshold.
- **DEC-21 (amends Part 1 No-go "changing blocking rules")**: **Name-similarity blocking** (decided by S5,
  `inputs/s5-blocking-round1.md`, `inputs/s5-blocking-round2.md`). A pair is scored only if both mentions share
  `country_code` **and** their normalised legal names have Jaro-Winkler similarity ≥ 0.8. The normalised name
  (lower-case, accents stripped, Unicode letters and digits only, empty → NULL) is stored in `mentions` at insert, so it is
  not recomputed per request. Result on `org-mid.csv`: 2.4 pairs scored per mention (111× fewer than country-only),
  100 % of `exact` and `variant` reference pairs admitted, ≤ 0.1 % unrelated merges. Rejected: derived-key rules
  (name prefix OR postcode OR region + town) — they admit address-only pairs, which the trained model scores above
  0.9 regardless of name, so 85 % of merges stayed unrelated at every threshold. Measured against reference pairs,
  not against country-only blocking (which merges mostly unrelated organisations).
- **DEC-22**: **Scaling guard for name-similarity blocking.** It still compares every same-country mention by name (cheap
  Jaro-Winkler on a stored column). If the Part 2 backlog profile (13.2) shows that comparison dominating at large N,
  add a name-token pre-block (fallback C) — not built unless measured.
- **DEC-23**: **`match_weight_threshold` stays −10.** S5: −5 and 0 store ~15 % fewer links but do not change merges; with
  name-similarity blocking and top-k storage the saving is not needed.
- **DEC-24**: **No schema migration; a database reset on upgrade is acceptable.** Part 2 adds a stored normalised-name
  column to `mentions` and drops the `similarities` indexes; old database files are reset, not migrated. Integer
  surrogate keys stay out of scope (their remaining benefit is unmeasured Splink speed).
- **DEC-25**: **Idempotent re-submissions keep today's behaviour**: answered immediately from stored links without
  re-scoring (rare; the ERS already filters duplicates).
- **DEC-26 (amends Part 1 No-go "changing thresholds")**: **Cluster `threshold` 0.7** (was 0.20), owner's choice from
  S5 round 2 with name-similarity blocking: 96.2 % of `exact` pairs clustered, 1 unrelated merge of 5,700. The pre-set rule
  (lowest threshold with unrelated merges < 5 %) selected 0.5 with the same recall and 4 unrelated merges; 0.7 was preferred
  for margin on larger corpora. 0.9 loses recall (94.8 %). Response candidates are unaffected; only cluster assignment changes.
- **DEC-27 (amends Part 1 No-go "changing comparison levels")**: **Remove the `country_code` comparison.** Every scored
  pair shares `country_code` by blocking, so the comparison only inflated match odds (m/u ≈ 10). Removing it lowered
  stored links 8,509 → 6,744 and unrelated merges 6 → 1 at equal recall (S5 round 2). EM training still blocks on
  `country_code`.
- **DEC-28**: **Known model weakness, not fixed here.** Training at 200 mentions leaves several `legal_name` levels
  untrained and lets address agreement dominate scores. Name-similarity blocking neutralises this for clustering; retraining
  strategy stays a No-go (DEC-4).

## Rabbit-holes (Part 2)

- **Name normalisation.** Keep it trivial and in one SQL expression (lower-case, strip accents, Unicode letters and digits);
  an ASCII-only pattern would collapse Cyrillic or Greek names to empty strings. No normalisation library, no legal-form
  stripping.
- **EM training rule.** Training derived its rule from the first blocking rule; it must now be set explicitly to
  `country_code` (the scoring rule is a SQL expression).
- **Rate estimate oscillation.** Moving average and a floor of 1 for the bite limit.
- **Arrow/pandas type drift on empty or all-null columns** (known issue in this codebase): cast explicitly.
- **`LMPOP` needs Redis ≥ 7.** OP runs 7.1; fallback `RPOP key count` (Redis ≥ 6.2).
- **Synthetic profile corpus exaggerates growth** (suffix-cycled names create near-duplicates that pass name
  similarity): S6 and 13.2 use a corpus without cycles, or separate the effect.

## No-gos (Part 2)

- Threads or processes for parsing, scoring or persistence (DEC-12, Part 1 DEC-1).
- Fixed arrival windows longer than the linger (DEC-11).
- Integer surrogate keys and schema migrations (DEC-24).
- Freezing idempotent answers (DEC-25).
- Physically ordering `mentions` by country (evaluated: gain only at very large N, small after batching).
- Changing comparison levels or cold-start parameters beyond DEC-26 (threshold) and DEC-27 (`country_code` comparison).
- Retraining or a different training sample to fix the model weakness (DEC-28).
- Crash recovery, re-clustering, retraining, retention by age (Part 1 no-gos stand).

## What Changes (Part 2)

- Profile harness reports per-stage timings and links per mention (S6).
- **BREAKING (behaviour)**: blocking becomes country + name similarity on a stored normalised name (DEC-21); pairs
  with dissimilar names are no longer scored, so weak-tail candidates disappear from responses and clusters form only
  among similarly named organisations.
- Only the top `top_n` links per mention are stored; `similarities` indexes removed (DEC-19).
- **BREAKING (behaviour)**: several waiting requests are taken at once; the Part 1 "one request at a time"
  requirement becomes "one bite at a time".
- New `EntityResolver.resolve_batch` with intra-bite matching (DEC-14) inside one transaction (DEC-13); the single
  path also commits once.
- Columnar link hand-over between the Splink adapter and the service (DEC-17); pipelined responses (DEC-16).
- New env vars: `ERE_BATCH_TARGET_SECONDS`, `ERE_BATCH_MAX_MENTIONS`, `ERE_BATCH_MAX_BYTES`, `ERE_BATCH_LINGER_MS`.
- Tracing: per-request events carry `batch_id` and `batch_size`; one `batch` event per bite.
- **BREAKING (behaviour)**: cluster `threshold` 0.20 → 0.7 (DEC-26) and `country_code` comparison removed (DEC-27).
- **BREAKING (schema)**: `mentions` gains a normalised-name column; existing database files are reset (DEC-24).
- Upgrade note: existing database files may be reset (DEC-24).

## Capabilities (Part 2)

### New Capabilities
- `batched-resolution`: bite sizing, intra-bite matching, failure isolation, one commit per bite, pipelined I/O.
- `candidate-scoring`: country + name-similarity blocking with a reference-pair bar, cluster joins that need strong
  evidence (threshold 0.7, no inflating comparison), top-k link storage.

### Modified Capabilities (within this change)
- `resolution-resource-bounds`: consumption becomes bite-based.
- `mention-lifecycle-tracing`: batch fields and a per-bite event.
- `resolution-model-lifecycle`: training trigger on crossing (DEC-20).

## Impact (Part 2)

- **Code:** `entrypoints/queue_worker.py`, `entrypoints/app.py`, `services/entity_resolution_service.py`,
  `services/tracing.py`, `services/resolver_config.py`, `adapters/splink_linker_impl.py`,
  `adapters/duckdb_repositories.py`, `adapters/duckdb_schema.py`, `models/ports/linker.py`, `adapters/repositories.py`.
- **Config:** blocking rules, `threshold`, `country_code` comparison and its cold-start entry in `src/config/resolver*.yaml`
  and `test/resources/resolver.yaml`; tests asserting joins at 0.20 may need their fixture threshold pinned.
- **Tests:** BDD `batched_resolution.feature`, `candidate_scoring.feature`; updated Part 1 consumption scenario;
  spikes and the extended profile harness under `test/stress/`.
- **Operations:** Redis ≥ 6.2; new env vars documented; responses reach the ERS in bursts per bite; database reset
  acceptable on upgrade.
