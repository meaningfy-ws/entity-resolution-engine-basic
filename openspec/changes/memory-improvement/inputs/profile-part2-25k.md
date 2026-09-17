# Part 2 profiles — 25k mentions (2026-09-17)

Local SSD, production factory (on-disk DuckDB, name-similarity blocking, threshold 0.7, one transaction per bite,
training at 200), `org-mid.csv` cycled with a random word prefixed to the name after 5,497 rows. RDF parsing excluded
(measured separately: 4 ms for an average 20 KB mention). Raw samples: `profile-part2-backlog-25k.json`,
`profile-part2-single-25k.json`.

## Backlog through the worker (`test/stress/profile_backlog.py`, task 13.2)

25,000 requests queued up front, drained by `process_bite()` with default batch settings.

| processed | mentions/s | mean bite ms | p95 bite ms | bite size | RSS MB |
|---:|---:|---:|---:|---:|---:|
| 2,500 | 367.8 | 1,359 | 1,324 | 500 | 219 |
| 5,000 | 373.7 | 1,338 | 1,348 | 500 | 230 |
| 10,000 | 351.4 | 1,423 | 1,426 | 500 | 240 |
| 15,000 | 332.6 | 1,503 | 1,523 | 500 | 250 |
| 20,000 | 327.7 | 1,526 | 1,536 | 500 | 257 |
| 25,000 | 316.3 | 1,581 | 1,586 | 500 | 266 |

- 25k drained in ≈ 73 s; bites stay at the 500 cap (≈ 1.5 s, under the 2 s target).
- Throughput drops 14 % from 2.5k to 25k stored mentions; RSS grows 47 MB.
- Per mention ≈ 2.7–3.2 ms, against 87–352 ms one at a time before Part 2.

## One request at a time (`test/stress/profile_resolution.py`, task 8.1)

`ERE_DUCKDB_MEMORY_LIMIT=4GB`. Per-stage columns of this run were invalid (the harness still timed the pre-batching
methods; fixed afterwards) — latency columns are valid.

| n | RSS MB | catalog | mean ms | p95 ms |
|---:|---:|---:|---:|---:|
| 2,500 | 343 | 4 | 65.6 | 69.5 |
| 10,000 | 370 | 4 | 67.3 | 71.0 |
| 17,500 | 381 | 4 | 69.6 | 74.0 |
| 25,000 | 392 | 4 | 70.3 | 75.3 |

- Flat: +7 % mean over 25k (Part 1 with country-only blocking: 87 → 352 ms mean, p95 up to 1,753 ms).
- Catalog constant; RSS +49 MB with a 4 GB DuckDB limit.

## Against the budget (DEC-9)

Budget ≤ 360 ms mean per mention (20k in 2 h). Met by ≈ 5× one at a time and by ≈ 110× in bites at 25k. Projection
to 300k is not measured (task 7.1): the remaining growth driver is the name comparison over each country's stored
mentions (DEC-22 scaling guard).

## Stage split (task 8.1, 6k mentions, one request at a time, `ERE_DUCKDB_MEMORY_LIMIT=4GB`)

| n | mean ms | p95 ms | links/mention | score ms | links ms | cluster lookup ms | cluster save ms | mention insert ms |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1,500 | 64.2 | 67.9 | 0.3 | 57.6 | 0.18 | 0.06 | 0.19 | 2.16 |
| 3,000 | 65.3 | 69.1 | 1.1 | 58.2 | 0.43 | 0.17 | 0.20 | 2.21 |
| 4,500 | 66.5 | 70.4 | 1.8 | 59.2 | 0.56 | 0.22 | 0.21 | 2.27 |
| 6,000 | 66.5 | 70.8 | 1.4 | 59.2 | 0.46 | 0.18 | 0.21 | 2.30 |

- The Splink call is ~89 % of a single request (≈ 58 ms, almost constant) — the fixed per-call overhead S1 measured —
  so batching, not the database, is the lever. Persistence stages together stay under 3.5 ms.
