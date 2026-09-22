# S1 — Splink batch cost (2026-09-17)

`test/stress/spike_batch_cost.py --stored 25000`: 25,000 stored mentions (bulk insert), production linker with
name-similarity blocking, new records from `org-mid.csv` with random name prefixes. Median of 3 repeats; one-by-one
measured on up to 50 records per size. Local SSD; a slow corpus test ran in parallel (absolute ms slightly high,
ratios reliable).

| batch | batched ms/record | one-by-one ms/record | speed-up | links/record |
|---:|---:|---:|---:|---:|
| 1 | 71.5 | 72.6 | 1.0× | 5.0 |
| 10 | 9.5 | 72.3 | 7.6× | 4.4 |
| 50 | 3.8 | 72.3 | 19.0× | 3.1 |
| 100 | 3.3 | 71.9 | 22.0× | 3.6 |
| 500 | 2.6 | 72.1 | 27.9× | 3.1 |

- One call costs ~70 ms regardless of batch size: Splink pipeline construction and DuckDB planning dominate.
- The proceed criterion (ms/record at 100 ≤ ⅓ of ms at 1) is met by a wide margin: D-10 and D-11 proceed.
- Diminishing returns past ~100 records; `ERE_BATCH_MAX_MENTIONS` default 500 stays reasonable, the adaptive bite
  (2 s target) will settle wherever the full per-mention cost (scoring + persistence + responses) puts it.
