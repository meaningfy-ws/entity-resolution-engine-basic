# memory-improvement — benchmark results

Every performance and memory measurement taken for this change, in one place. Raw samples and per-run notes live in
`inputs/`; this file collects them per implementation stage and compares them.

| stage | source | date | conditions |
|---|---|---|---|
| **Before Part 1** (baseline, leaking) | `inputs/MEMORY-ANALYSIS.md` §4 | 2026-09-07 | branch `release/1.1.0`, 5k requests, training disabled, `org-mid.csv` (5,497 rows) |
| **Before Part 2** (after Part 1) | `inputs/profile-part1-25k.md` | 2026-09-17 | leak fixed, country-only blocking, no DuckDB `memory_limit` |
| **After Part 2** | `inputs/profile-part2-25k.md` | 2026-09-17 | name-similarity blocking, threshold 0.7, batched resolution, `ERE_DUCKDB_MEMORY_LIMIT=4GB` |
| **After Part 2, post-refactor** | `inputs/profile-post-refactor-25k.md` | 2026-09-17 | same as Part 2, after the review refactors (tasks 14.3, 14.4, 14.7) |

All runs: local SSD, `org-mid.csv` cycled past 5,497 rows, resolver only (no Redis, no RDF parsing; RDF parsing
measured separately at ≈ 4 ms for a 20 KB mention). Budget (DEC-9): ≤ 360 ms mean per mention.

## Summary — one request at a time

| metric | before Part 1 | before Part 2 | after Part 2 (post-refactor) |
|---|---:|---:|---:|
| RSS at 5k | 1,583 MB | 493 MB | 359 MB |
| RSS at 25k | ≈ 9 GB (projected, 373 KB/request) | 1,196 MB | **396 MB** |
| mean ms at 5k | 430 | 93.8 | 66.1 |
| mean ms at 25k | not measured | 351.8 | **66.9** |
| p95 ms at 25k | not measured | 1,753 | **72.2** |
| DuckDB catalog objects | +1 per request (leak) | 4 | 4 |
| mean vs 360 ms budget | exceeded at ≈ 4k | at the limit at 25k | ≈ 5× under |
| backlog throughput | — | — | 319 mentions/s (≈ 110× under budget) |

## 1. Before Part 1 — leaking baseline (≤ 5k)

A/B benchmark: BASELINE = code at the time; FIX = drop the per-request Splink table and view (the Part 1 fix, prototyped).
`splinkDB` = memory held by leaked Splink tables per DuckDB's own accounting.

| n | RSS MB base | RSS MB fix | ratio | splinkDB MB | marginal KB/request | mean ms base | mean ms fix |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 250 | 248.5 | 217.2 | 1.14 | 14.8 | 60.6 | 196 | 193 |
| 1,000 | 402.2 | 244.3 | 1.65 | 124.3 | 184.2 | 234 | 206 |
| 2,000 | 640.4 | 269.0 | 2.38 | 360.4 | 269.1 | 284 | 223 |
| 3,000 | 937.4 | 289.9 | 3.23 | 641.5 | 290.9 | 331 | 230 |
| 4,000 | 1,264.7 | 290.2 | 4.36 | 967.6 | 339.4 | 373 | 244 |
| 5,000 | 1,582.8 | 313.5 | 5.05 | 1,319.7 | 373.3 | 430 | 257 |

- ≈ 373 KB leaked per request; 83 % of RSS at 5k is leaked Splink tables.
- Projected leak: 3.6 GB at 10k, 35.6 GB at 100k, 356 GB at 1M requests.
- Latency +119 % over 5k (base) vs +33 % (fix); results identical in both arms.

## 2. Before Part 2 — after Part 1 (25k, one request at a time)

Harness: `test/stress/profile_resolution.py --mentions 25000 --window 2500`, production factory, on-disk DuckDB,
no `memory_limit` (DuckDB defaults to 80 % of host RAM). Raw: `inputs/profile-part1-25k.json`.

| n | RSS MB | catalog | mean ms | p95 ms |
|---:|---:|---:|---:|---:|
| 2,500 | 403 | 4 | 87.2 | 94.2 |
| 5,000 | 493 | 4 | 93.8 | 110.4 |
| 7,500 | 438 | 4 | 103.0 | 123.5 |
| 10,000 | 514 | 4 | 115.4 | 150.9 |
| 12,500 | 625 | 4 | 132.9 | 222.0 |
| 15,000 | 731 | 4 | 156.9 | 412.5 |
| 17,500 | 797 | 4 | 187.9 | 600.2 |
| 20,000 | 798 | 4 | 229.2 | 999.6 |
| 22,500 | 1,070 | 4 | 278.5 | 1,317.3 |
| 25,000 | 1,196 | 4 | 351.8 | 1,753.0 |

- Leak fixed: catalog constant at 4 objects (3 tables + search-space view).
- Latency super-linear: mean ×4, p95 ×19 from 2.5k to 25k; the 25k mean reaches the 360 ms budget.
- RSS 403 → 1,196 MB; with no `memory_limit`, buffer-pool growth cannot be separated from a leak in this run.

## 3. After Part 2 (25k)

### One request at a time — Part 2 → post-refactor

Harness: `test/stress/profile_resolution.py`, `ERE_DUCKDB_MEMORY_LIMIT=4GB`.
Raw: `inputs/profile-part2-single-25k.json`, `inputs/profile-post-refactor-single-25k.json`.

| n | RSS MB | catalog | mean ms | p95 ms |
|---:|---:|---:|---:|---:|
| 2,500 | 343 → 349 | 4 | 65.6 → 64.7 | 69.5 → 68.2 |
| 5,000 | 355 → 359 | 4 | 66.6 → 66.1 | 70.3 → 70.0 |
| 7,500 | 360 → 364 | 4 | 67.8 → 66.7 | 72.7 → 70.0 |
| 10,000 | 370 → 375 | 4 | 67.3 → 65.8 | 71.0 → 69.6 |
| 12,500 | 377 → 382 | 4 | 68.1 → 66.2 | 72.3 → 70.5 |
| 15,000 | 379 → 383 | 4 | 68.7 → 66.6 | 72.5 → 70.9 |
| 17,500 | 381 → 385 | 4 | 69.6 → 66.8 | 74.0 → 71.4 |
| 20,000 | 391 → 396 | 4 | 68.9 → 66.4 | 73.1 → 71.1 |
| 22,500 | 392 → 397 | 4 | 69.9 → 66.6 | 74.8 → 71.5 |
| 25,000 | 392 → 396 | 4 | 70.3 → 66.9 | 75.3 → 72.2 |

Mean grows +7 % (Part 2) / +3.4 % (post-refactor) over 25k; RSS +47 MB and flat from 20k.

### Backlog through the worker — Part 2 → post-refactor

Harness: `test/stress/profile_backlog.py`, 25,000 requests queued up front, drained by `process_bite()` with default
batch settings. Raw: `inputs/profile-part2-backlog-25k.json`, `inputs/profile-post-refactor-backlog-25k.json`.

| processed | mentions/s | mean bite ms | p95 bite ms | bite size | RSS MB |
|---:|---:|---:|---:|---:|---:|
| 2,500 | 367.8 → 367.0 | 1,359 → 1,363 | 1,324 → 1,335 | 500 | 219 → 221 |
| 5,000 | 373.7 → 376.9 | 1,338 → 1,327 | 1,348 → 1,337 | 500 | 230 → 232 |
| 7,500 | 354.7 → 356.5 | 1,410 → 1,402 | 1,421 → 1,428 | 500 | 235 → 242 |
| 10,000 | 351.4 → 346.0 | 1,423 → 1,445 | 1,426 → 1,467 | 500 | 240 → 249 |
| 12,500 | 339.7 → 336.4 | 1,472 → 1,486 | 1,484 → 1,507 | 500 | 244 → 252 |
| 15,000 | 332.6 → 335.3 | 1,503 → 1,491 | 1,523 → 1,498 | 500 | 250 → 256 |
| 17,500 | 329.2 → 328.0 | 1,519 → 1,525 | 1,544 → 1,552 | 500 | 251 → 263 |
| 20,000 | 327.7 → 329.4 | 1,526 → 1,518 | 1,536 → 1,528 | 500 | 257 → 264 |
| 22,500 | 321.7 → 322.8 | 1,554 → 1,549 | 1,561 → 1,565 | 500 | 261 → 267 |
| 25,000 | 316.3 → 319.0 | 1,581 → 1,568 | 1,586 → 1,575 | 500 | 266 → 268 |

- 25k drains in 73–78 s; ≈ 3 ms per mention; bites pinned at the 500 cap, ≈ 1.5 s (under the 2 s target).
- Throughput −14 % from 2.5k to 25k; RSS +47 MB (≈ 1.5 MB per 1k mentions, plateau not yet shown).

### Stage split — post-refactor, one request at a time

| n | score ms | links ms | cluster lookup ms | cluster save ms | mention insert ms | links/mention |
|---:|---:|---:|---:|---:|---:|---:|
| 2,500 | 57.9 | 0.27 | 0.10 | 0.20 | 2.19 | 0.5 |
| 5,000 | 58.8 | 0.55 | 0.21 | 0.21 | 2.26 | 1.8 |
| 10,000 | 58.8 | 0.22 | 0.08 | 0.21 | 2.33 | 0.4 |
| 15,000 | 59.3 | 0.31 | 0.12 | 0.21 | 2.39 | 0.7 |
| 20,000 | 59.0 | 0.38 | 0.15 | 0.21 | 2.39 | 0.8 |
| 25,000 | 59.4 | 0.42 | 0.17 | 0.21 | 2.42 | 0.9 |

The Splink call is 89 % of a request and grows 2.5 % over 25k; persistence stays under 3.3 ms and flat (DEC-22).
The Part 2 run's stage columns were invalid (harness timed the pre-batching methods); a separate 6k run gave the
same split (score ≈ 58 ms, persistence < 3.5 ms).

## 4. Design spikes (Part 2)

### S1 — Splink batch cost (`inputs/s1-batch-cost.md`, 25k stored mentions)

| batch | batched ms/record | one-by-one ms/record | speed-up | links/record |
|---:|---:|---:|---:|---:|
| 1 | 71.5 | 72.6 | 1.0× | 5.0 |
| 10 | 9.5 | 72.3 | 7.6× | 4.4 |
| 50 | 3.8 | 72.3 | 19.0× | 3.1 |
| 100 | 3.3 | 71.9 | 22.0× | 3.6 |
| 500 | 2.6 | 72.1 | 27.9× | 3.1 |

≈ 70 ms fixed cost per Splink call regardless of batch size; diminishing returns past ≈ 100 records.

### S5 — blocking and threshold (`inputs/s5-blocking-round1.md`, `inputs/s5-blocking-round2.md`, 5.5k mentions)

Runs in parallel, so ms per mention is indicative only. "unrelated" = merged pairs whose normalised names have
Jaro-Winkler < 0.8.

| run | pairs/mention | clustered exact | clustered variant | merged | unrelated | stored links | ms/mention |
|---|---:|---:|---:|---:|---:|---:|---:|
| country-only, t 0.2 (before Part 2) | 266.9 | 98.0 % | 100.0 % | 105,871 | 98,613 (93 %) | 1,451,459 | 91.7 |
| A (derived keys), t 0.2 | 12.5 | 97.8 % | 96.2 % | 41,588 | 35,429 (85 %) | 63,663 | 108.5 |
| A, t 0.9 -cc | 12.5 | 96.9 % | 96.2 % | 37,599 | 31,826 | 54,554 | 104.9 |
| B (raw-name JW ≥ 0.8), t 0.2 | 2.2 | 95.1 % | 94.3 % | 5,555 | 7 (0.1 %) | 6,247 | 82.8 |
| B_norm, t 0.7 | 2.4 | 96.3 % | 96.2 % | 5,847 | 6 | 8,509 | 89.6 |
| **B_norm, t 0.7 -cc (chosen)** | **2.4** | **96.2 %** | **96.2 %** | **5,700** | **1** | **6,744** | **85.5** |
| B_norm, t 0.9 -cc | 2.4 | 94.8 % | 96.2 % | 5,477 | 1 | 6,744 | 86.1 |

`-cc` = without the `country_code` comparison (DEC-27). The chosen setup scores 111× fewer pairs than country-only,
admits 100 % of reference pairs into the block and stores 215× fewer links.

## Caveats and open measurements

- The before-Part-1 run used a different harness (training disabled, 5k requests, no p95): compare trends, not
  absolute milliseconds. Its 25k figure is a projection, not a measurement.
- Before Part 2 ran without a DuckDB `memory_limit`; after Part 2 ran with 4 GB. Part of the RSS drop is the limit.
- None of the runs used a container with a hard memory cap; set `ERE_DUCKDB_MEMORY_LIMIT` to ≈ 60–70 % of the
  cgroup limit (`inputs/MEMORY-ANALYSIS.md`).
- Nothing beyond 25k is measured: 150k / 300k remains task 7.1. The backlog RSS creep (≈ 1.5 MB per 1k) and the
  per-call name normalisation over large country blocks (DEU ≈ 54k at 300k) are the things to watch there.
