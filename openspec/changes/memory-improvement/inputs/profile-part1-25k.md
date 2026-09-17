# Part 1 profile — 25k mentions (2026-09-17)

Harness: `test/stress/profile_resolution.py --mentions 25000 --window 2500`, production factory, on-disk DuckDB
(no memory limit set), local SSD, resolver only (no Redis, no RDF), corpus `org-mid.csv` cycled with a numeric
suffix on the legal name after 5,497 rows (creates near-duplicates). Raw samples: `profile-part1-25k.json`.

|      n | rss_mb | catalog | mean_ms | p95_ms |
|-------:|-------:|--------:|--------:|-------:|
|  2,500 |    403 |       4 |    87.2 |   94.2 |
|  5,000 |    493 |       4 |    93.8 |  110.4 |
|  7,500 |    438 |       4 |   103.0 |  123.5 |
| 10,000 |    514 |       4 |   115.4 |  150.9 |
| 12,500 |    625 |       4 |   132.9 |  222.0 |
| 15,000 |    731 |       4 |   156.9 |  412.5 |
| 17,500 |    797 |       4 |   187.9 |  600.2 |
| 20,000 |    798 |       4 |   229.2 |  999.6 |
| 22,500 |  1,070 |       4 |   278.5 | 1,317.3 |
| 25,000 |  1,196 |       4 |   351.8 | 1,753.0 |

- **Leak fixed:** catalog constant at 4 objects (3 tables + search-space view) across 25k requests.
- **Not flat:** mean latency ×4 and p95 ×19 from 2.5k to 25k; at 25k the mean reaches the 360 ms budget (DEC-9).
  Growth is super-linear after the corpus starts cycling, consistent with near-duplicates enlarging per-mention
  links and with country-only blocking scoring every same-country mention. Diagnosed in Part 2 S6; addressed by
  derived-key blocking (DEC-21) and top-k storage (DEC-19).
- **RSS** grows 403 → 1,196 MB; DuckDB had no `memory_limit` (defaults to 80 % of host RAM), so buffer-pool growth
  and leak cannot be separated from this run; S6 repeats with `ERE_DUCKDB_MEMORY_LIMIT` set.
