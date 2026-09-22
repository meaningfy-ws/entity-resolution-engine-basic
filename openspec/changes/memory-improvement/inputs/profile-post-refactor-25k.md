# Post-refactor profiles — 25k mentions (2026-09-17, task 15.4)

Same harnesses, same corpus and same machine as `profile-part2-25k.md`, re-run after the review refactors
(tasks 14.3, 14.4, 14.7) and the config fixes. Purpose: confirm the refactors cost no performance.
Raw samples: `profile-post-refactor-backlog-25k.json`, `profile-post-refactor-single-25k.json`.

## Backlog through the worker (`test/stress/profile_backlog.py`)

| processed | mentions/s (before → after) | mean bite ms | p95 bite ms | RSS MB |
|---:|---:|---:|---:|---:|
| 2,500 | 367.8 → 367.0 | 1,359 → 1,363 | 1,324 → 1,335 | 219 → 221 |
| 5,000 | 373.7 → 376.9 | 1,338 → 1,327 | 1,348 → 1,337 | 230 → 232 |
| 10,000 | 351.4 → 346.0 | 1,423 → 1,445 | 1,426 → 1,467 | 240 → 249 |
| 15,000 | 332.6 → 335.3 | 1,503 → 1,491 | 1,523 → 1,498 | 250 → 256 |
| 20,000 | 327.7 → 329.4 | 1,526 → 1,518 | 1,536 → 1,528 | 257 → 264 |
| 25,000 | 316.3 → 319.0 | 1,581 → 1,568 | 1,586 → 1,575 | 266 → 268 |

Bite size stays pinned at the 500 cap throughout. Throughput within ±1.5 % at every checkpoint (25k drains in
≈ 78 s either way); RSS within 9 MB. No regression.

## One request at a time (`test/stress/profile_resolution.py`, `ERE_DUCKDB_MEMORY_LIMIT=4GB`)

| n | mean ms (before → after) | p95 ms | RSS MB |
|---:|---:|---:|---:|
| 2,500 | 65.6 → 64.7 | 69.5 → 68.2 | 343 → 349 |
| 10,000 | 67.3 → 65.8 | 71.0 → 69.6 | 370 → 375 |
| 17,500 | 69.6 → 66.8 | 74.0 → 71.4 | 381 → 385 |
| 25,000 | 70.3 → 66.9 | 75.3 → 72.2 | 392 → 396 |

Mean is 1–5 % faster and growth over 25k is now +3.4 % (was +7 %). Catalog constant at 4 objects — no leak.

## Stage split, now valid for the whole run

The pre-batching timing bug in the harness is fixed, so the per-stage columns are trustworthy at every checkpoint:

| n | score ms | links ms | cluster lookup ms | cluster save ms | mention insert ms | links/mention |
|---:|---:|---:|---:|---:|---:|---:|
| 2,500 | 57.9 | 0.27 | 0.10 | 0.20 | 2.19 | 0.5 |
| 10,000 | 58.8 | 0.22 | 0.08 | 0.21 | 2.33 | 0.4 |
| 17,500 | 59.5 | 0.32 | 0.12 | 0.21 | 2.41 | 0.5 |
| 25,000 | 59.4 | 0.42 | 0.17 | 0.21 | 2.42 | 0.9 |

The Splink call is 89 % of a request and grows 2.5 % over 25k; all persistence stages together stay under 3.3 ms
and are flat. Confirms DEC-22: the remaining scaling driver is the name comparison inside the blocked candidate set,
not the database.

## Verdict

The refactors are performance-neutral (single path marginally better), and the budget margin of `profile-part2-25k.md`
is unchanged: ≈ 5× under the 360 ms mean budget one at a time, ≈ 110× in bites. Projection to 150k/300k remains
unmeasured (task 7.1).
