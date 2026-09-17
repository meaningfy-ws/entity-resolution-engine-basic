# S5 round 1 — blocking on `org-mid.csv` (2026-09-17)

`test/stress/spike_blocking.py`, 5,497 organisations in file order, production resolver (shared on-disk DuckDB),
training synchronously at 200, cluster `threshold` 0.2, `country_code` comparison present. Six runs in parallel:
ms per mention is indicative only. Reference pairs (design D-22): `exact` = 5,464, `variant` = 53.

| run | pairs/mention | block exact | block variant | clustered exact | clustered variant | merged pairs | unrelated merges | joins | stored links | ms/mention |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| country-only, mwt −10 | 266.9 | 100.0 % | 100.0 % | 98.0 % | 100.0 % | 105,871 | 98,613 (93 %) | 5,303 | 1,451,459 | 91.7 |
| A, mwt −10 | 12.5 | 100.0 % | 100.0 % | 97.8 % | 96.2 % | 41,588 | 35,429 (85 %) | 3,725 | 63,663 | 108.5 |
| A without region/town rule, mwt −10 | 9.0 | 100.0 % | 100.0 % | 97.9 % | 96.2 % | 24,462 | 18,203 (74 %) | 2,908 | 45,011 | 99.1 |
| B (Jaro-Winkler ≥ 0.8 on raw name), mwt −10 | 2.2 | 98.5 % | 98.1 % | 95.1 % | 94.3 % | 5,555 | 7 (0.1 %) | 1,688 | 6,247 | 82.8 |
| A, mwt −5 | 12.5 | 100.0 % | 100.0 % | 97.8 % | 96.2 % | 41,588 | 35,429 (85 %) | 3,352 | 53,923 | 108.3 |
| A, mwt 0 | 12.5 | 100.0 % | 100.0 % | 97.6 % | 96.2 % | 40,497 | 34,474 (85 %) | 3,277 | 53,160 | 108.1 |

## Findings

1. **Country-only blocking over-merges massively at threshold 0.2**: 93 % of pairs placed in one cluster have unrelated
   names (clusters snowball: 5,303 of 5,497 mentions join something). It also stores 264 links per mention —
   the memo's 6.3 links/mention does not hold with the 6-field config.
2. **A scores 21× fewer pairs and keeps every reference pair admissible**, but still over-merges (85 % unrelated): the
   over-merging comes from scoring and threshold, not from blocking.
3. **A is not faster at 5.5k mentions** (108 vs 92 ms): its name-prefix key is recomputed with a regular expression over
   the search space on every call. At this corpus size the country block is still small, so the saving does not show
   yet; a stored key column (computed once at insert) removes the recompute cost.
4. **B (name pre-filter) is the only variant that stops unrelated merges** (7 of 5,555) and is fastest, but its blocking
   misses 1.5 % of `exact` pairs — Jaro-Winkler on the raw name is sensitive to case and punctuation. Round 2 uses B on
   normalised names (`B_norm`). B still compares every same-country mention (cheaply) on each call.
5. **`match_weight_threshold` −5 or 0 saves ~15 % of stored links and does not change merges materially.**
6. Top-k storage (DEC-19) matters more than estimated where blocking is wide (country-only: 264 links/mention).

Round 2 (running): A and B_norm × cluster `threshold` 0.5 / 0.7 / 0.9 without the `country_code` comparison, and 0.7 with it.
