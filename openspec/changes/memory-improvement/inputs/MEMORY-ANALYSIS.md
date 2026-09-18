# ERE Memory Analysis — DuckDB / Splink OOM Investigation

**Date:** 2026-09-07
**Branch:** release/1.1.0
**Scope:** Why the ERE service runs out of memory in long-running operation.
**Method:** static code analysis + instrumented A/B benchmark (5000 real mentions per arm).

---

## Executive summary

The service leaks **~373 KB of DuckDB memory per Redis request**, permanently.

Every call to `find_matches` creates a new physical DuckDB table that is never dropped.
After 5000 requests, **83% of process RSS is leaked Splink tables**. The leak is a function
of uptime, not of load — the service will OOM at ~100k requests regardless of throughput.

| Requests | Leaked memory (projected) |
|---|---|
| 10,000 | 3.6 GB |
| 100,000 | **35.6 GB** |
| 1,000,000 | 356 GB |

A targeted fix (drop the per-request tables) reduces RSS **5.05×** at 5000 requests
and holds it flat, while producing byte-identical results and cutting latency growth
from +119% to +33%.

---

## 1. How many DuckDB instances are there?

**Three**, not one. They share nothing.

| # | Created in | Kind | Holds |
|---|---|---|---|
| 1 | `services/factories.py:70-77` | **persistent** file (`/data/app.duckdb`) | `mentions`, `similarities`, `clusters` |
| 2 | `adapters/splink_linker_impl.py:96` (`self._splink_con`) | **in-memory** | everything Splink touches |
| 3 | `adapters/splink_linker_impl.py` `_train_safe()` (`splink_con_new`) | **in-memory**, one more per training run | a second full copy during EM |

Instance #2 is where the memory goes. During EM training you transiently hold #2 and #3
simultaneously, plus a full `_tf_df.copy()`.

Note: `_train_safe` swaps `self._splink_con` without calling `.close()` on the old
connection — it relies on GC to reclaim it.

---

## 2. Root cause

Splink's `find_matches_to_new_records` registers the incoming record under a **random**
table name:

```python
# splink/internals/linker_components/inference.py:522-525
uid = ascii_uid(8)
new_records_tablename = f"__splink__df_new_records_{uid}"
```

Splink caches materialised results keyed on `sha256(sql + cache_uid)`
(`splink/internals/database_api.py`, `sql_to_splink_dataframe_checking_cache`). Because the
random table name is embedded in the SQL, **every request produces a different hash** →
guaranteed cache miss → a fresh `CREATE TABLE` on every request, retained for the life of
the process.

Splink's cache is designed for a batch workflow (one `predict()` per process), not a
long-lived request server. Nothing in ERE's own code is wrong here — the mismatch is
architectural.

### These are real tables, not rows

Catalog dump of the Splink in-memory DuckDB after **5** requests:

```
TABLES
  __splink__find_matches_predictions_119eb8d31   cols=23
  __splink__find_matches_predictions_31a23ce2c   cols=23
  __splink__find_matches_predictions_42dbaeb91   cols=23
  __splink__find_matches_predictions_8029c6c36   cols=23
  __splink__find_matches_predictions_c230a2655   cols=23
VIEWS
  __splink__df_new_records_3hq6r74j   (x5, one per request)
  __splink__df_concat_with_tf_vdnwl4hl
  __splink__input_table_0
```

Five requests → five distinct 23-column tables **and** five distinct views.

### Why each leaked table is so expensive

Isolation experiment — 1000 synthetic 23-column tables in a bare DuckDB:

| rows per table | RSS cost | per table |
|---|---|---|
| 0 rows | +31.3 MB | **32 KB** |
| 1 row | +341.0 MB | **349 KB** |
| 10 rows | +338.7 MB | **347 KB** |

One row costs the same as ten. The cost is DuckDB's **fixed per-column block allocation**,
not the data volume. A leaked prediction table costs ~32 KB while empty and jumps ~10× to
~350 KB the moment it holds a single match.

This predicts a **knee**: early requests find no matches (cheap empty tables); as the corpus
fills in, more requests match and per-request cost rises ~10× toward the ~350 KB asymptote.
**The benchmark confirmed this** (section 4).

---

## 3. Static analysis — state that survives a request

| State | Location | Δ per request | Bounded? |
|---|---|---|---|
| `__splink__find_matches_predictions_<hash>` table | Splink DuckDB catalog | +1 (23 cols) | **no** |
| `__splink__df_new_records_<uid>` view | Splink DuckDB catalog | +1 | **no** |
| `_intermediate_table_cache` | `database_api.py:186` | +1 entry | **no** |
| `executed_queries` list | `cache_dict_with_logging.py:17` | +2 `SplinkDataFrame` | **no** |
| `queries_retrieved_from_cache` | same | +1 per cache hit | **no** |
| `_tf_df` | linker | +1 row, **full copy** via `pd.concat` | O(N) resident |
| `mentions` / `clusters` rows | main DuckDB | +1 | O(N), by design |
| `similarities` rows | main DuckDB | +#links | O(N·blocksize) |

### Transient but O(N) per request — runs even at INFO level

- `get_all_memberships()` at `services/entity_resolution_service.py:137` is
  **unconditional** — it materialises the entire cluster membership map as Python objects
  purely to build a `log.trace` string.
- `dict(row)` per link at `adapters/splink_linker_impl.py:199`.
- `log.trace` guards with `isEnabledFor` *inside* `_trace` (`utils/logging.py:15`), so
  **arguments are evaluated at the call site** regardless of log level. The guard buys nothing.
- The blocking join scans all N rows of `concat_with_tf` per request → O(N²) cumulative.

---

## 4. Benchmark

**Harness:** live RSS from `/proc/self/statm` (not the `ru_maxrss` watermark), DuckDB's own
per-connection accounting via `duckdb_memory()`, Python heap via `tracemalloc`, `_tf_df`
measured separately. Dataset `test/stress/data/org-mid.csv` (5497 rows, country-skewed:
DEU 997, FRA 830). Training disabled (`auto_train_threshold=0`) to isolate the request path.

**Arms:** BASELINE = current code. FIX = drop the per-request table + view after each
`find_matches`.

```
    n  RSS_base  RSS_fix  ratio  splinkDB  KB/table  marg KB/req  ms_base  ms_fix
  250     248.5    217.2   1.14      14.8      60.6         60.6      196     193
 1000     402.2    244.3   1.65     124.3     127.3        184.2      234     206
 2000     640.4    269.0   2.38     360.4     184.5        269.1      284     223
 3000     937.4    289.9   3.23     641.5     219.0        290.9      331     230
 4000    1264.7    290.2   4.36     967.6     247.7        339.4      373     244
 5000    1582.8    313.5   5.05    1319.7     270.3        373.3      430     257
```

### Findings

1. **1582.8 MB vs 313.5 MB at 5000 requests — 5.05× and still diverging.**
2. **Attribution is direct, not inferred:** DuckDB's buffer manager reports 1319.7 MB of the
   1582.8 MB baseline RSS. 83% of the process is leaked Splink tables.
3. **The predicted knee is real.** Marginal cost climbs 61 → 373 KB/request and saturates at
   the ~350 KB/table fixed cost measured in isolation. Growth is superlinear while the corpus
   fills in, then linear at ~373 KB/request forever.
4. **The leak also costs latency.** Baseline 196 → 430 ms (+119%); fixed 193 → 257 ms (+33%).
   Roughly two-thirds of latency drift is the planner walking a catalog full of junk tables.
5. **Identical results.** `simrows` matches exactly across both arms at every sample point.
6. **The fix holds flat:** `tables=0`, `cache=1`, `splinkDB=0.00 MB` across all 5000 requests.
   Residual RSS growth (146 → 313 MB) is allocator retention from `pd.concat` churn and
   plateaus from n=2500 onward. Bounded, not leaking.

### Not fixed by this, still growing by design

`similarities` reaches 31,636 rows at 5000 mentions (6.3/mention and rising, because country
blocking makes block size grow with N). On-disk and modest today, but O(N²) — it needs a
retention policy before it becomes the next problem.

---

## 5. DuckDB configuration — defaults and what is controllable

Measured defaults (duckdb 1.4.4 in the local venv; note `src/pyproject.toml` pins **1.5.2**,
so the local environment is behind):

```
memory_limit             = 49.8 GiB     (80% of HOST RAM)
temp_directory           = .tmp         (in-memory DB -> CWD!)
temp_directory           = <db>.tmp     (file-backed DB)
max_temp_directory_size  = 90% of available disk
threads                  = 20
```

Spilling to disk **is** enabled by default. Two things defeat it in Docker:

1. **`memory_limit` is sized from host RAM, not the cgroup limit.** In a container capped at
   2 GB, DuckDB still believes it has ~50 GB and never decides to spill — the kernel
   OOM-kills the container first.
   *Verify inside the running container:* `SELECT current_setting('memory_limit')`.
2. For the in-memory Splink connection, `temp_directory` is `.tmp` relative to **CWD**
   (`/app`), not the `/data` volume. Spill files land in the container overlay.

### What is controllable, and where

**DuckDB has no environment variables.** Configuration is only via
`duckdb.connect(config={...})` or `SET`. **This codebase sets none of it** — both connections
are created bare (`services/factories.py:72,76`; `adapters/splink_linker_impl.py:96`).

| Setting | Why it matters |
|---|---|
| `memory_limit` | must be set explicitly in containers; ~60–70% of the cgroup limit |
| `temp_directory` | point at the `/data` volume so spilling actually works |
| `max_temp_directory_size` | bound the spill |
| `threads` | 20 threads × per-thread buffers inside a small container is its own OOM |

**Splink has no memory knob** — it delegates entirely to DuckDB. What Splink does control:
blocking rules (the real lever; `country_code` alone means every mention from a large country
blocks against all the others) and `max_pairs=1e6` in `estimate_u_using_random_sampling`.

**Existing ERE env vars** (`entrypoints/app.py` docstring): `DUCKDB_PATH`, `RDF_MAPPING_PATH`,
`RESOLVER_CONFIG_PATH`, `ERE_LOG_LEVEL`, `REDIS_*`, `ERSYS_*_QUEUE`.

`infra/compose.dev.yaml` sets no `mem_limit` and no DuckDB tuning at all.

---

## 6. Recommended fix order

1. **Drop the Splink result table + new-records view after each `find_matches`.**
   Confirmed: turns superlinear growth into flat, 5.05× RSS reduction at 5000 requests,
   and cuts latency growth by two-thirds. Target the two specific artefacts
   (`find_matches_predictions_*`, `df_new_records_*`) rather than sweeping the whole cache.
2. **Set `memory_limit` / `temp_directory` / `threads`** on both connections from
   `resolver.yaml`, plus a `mem_limit` in compose so the service fails loudly instead of
   eating the host.
3. **Guard the two unconditional O(N) logging paths** behind `log.isEnabledFor`, or drop the
   arguments — they run at INFO today.
4. **Add a regression test** asserting `duckdb_tables()` stays flat across N requests. This is
   what stops the leak silently returning on a Splink upgrade.
5. **Then** consider `_tf_df` (O(N) resident, O(N²) cumulative via `pd.concat`) and a
   retention policy for `similarities`.

---

## 7. Caveats

- Single process, **training disabled** to isolate the request path. Real EM training
  transiently adds a second full in-memory DuckDB plus a `_tf_df` copy on top of the above.
- No Redis and no RDF parsing in the measured loop — those add per-request allocation but not
  retention.
- Absolute MB/request depends on the country mix; `org-mid.csv` is skewed, which is realistic
  for procurement data but not universal.
- Local venv has duckdb 1.4.4 / splink 4.0.15; `pyproject.toml` pins 1.5.2 / 4.0.16.
  Re-confirm the per-table cost after aligning versions.

---

## 8. Stale documentation found during this investigation

The agent memory file describes `AbstractPubSubResolutionService` with a
`ThreadPoolExecutor(max_workers=os.cpu_count())`. **That no longer exists.** `entrypoints/app.py`
runs a single-threaded `while running: worker.process_single_message()` loop; the only extra
thread is the daemon EM-training thread. This should be corrected before it misleads future work.

---

## 9. Status after change `memory-improvement` (2026-09-17)

- §8 is resolved: the stale agent-memory note about a `ThreadPoolExecutor` was corrected; the service is
  single-threaded by decision (EPIC DEC-1).
- §6 items 1–4 are implemented: per-request Splink artefacts are released, DuckDB settings come from
  `ERE_DUCKDB_*`, the O(N) logging paths are gone, and a BDD scenario asserts the catalog stays flat.
- §6 item 5: `_tf_df` no longer exists — Splink reads the persisted `mentions` table through a view.
  `similarities` keeps growing by design (EPIC DEC-8); its reads are indexed and no longer on the new-mention path.

### Installation-manual errata this change feeds (outside this repository)

1. DuckDB spill directory: now `<DUCKDB_PATH>.tmp` on the data volume by default, or `ERE_DUCKDB_TEMP_DIR`;
   no write under the working directory, which removes the `Read-only file system` `.tmp` errors.
2. New environment variables: `ERE_DUCKDB_STORAGE`, `ERE_DUCKDB_MEMORY_LIMIT` (set to ~60% of the task
   memory), `ERE_DUCKDB_THREADS`, `ERE_DUCKDB_TEMP_DIR`, `ERE_SPLINK_MODEL_PATH`.
3. The trained model file lives next to the database on the data volume and must persist across deployments;
   deleting it forces one retraining on the next start.
4. Restart behaviour: a restart keeps the search space and the trained model (reset policy to be documented
   in the OP installation and testing manuals).
