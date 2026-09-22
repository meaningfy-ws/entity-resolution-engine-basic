# S5 round 2 — threshold and country comparison (2026-09-17)

Same harness and corpus as round 1; eight runs in parallel (ms indicative). `-cc` = without the `country_code` comparison.
"merged" = pairs placed in one cluster; "unrelated" = merged pairs whose normalised names have Jaro-Winkler < 0.8.

| run | pairs/mention | block exact | block variant | clustered exact | clustered variant | merged | unrelated | links | ms/mention |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| country-only, t 0.2 (today) | 266.9 | 100.0 % | 100.0 % | 98.0 % | 100.0 % | 105,871 | 98,613 | 1,451,459 | 91.7 |
| A, t 0.2 | 12.5 | 100.0 % | 100.0 % | 97.8 % | 96.2 % | 41,588 | 35,429 | 63,663 | 108.5 |
| A, t 0.7 | 12.5 | 100.0 % | 100.0 % | 97.6 % | 96.2 % | 40,335 | 34,402 | 63,663 | 107.7 |
| A, t 0.5 -cc | 12.5 | 100.0 % | 100.0 % | 97.6 % | 96.2 % | 40,196 | 34,326 | 54,554 | 104.7 |
| A, t 0.7 -cc | 12.5 | 100.0 % | 100.0 % | 97.6 % | 96.2 % | 40,068 | 34,226 | 54,554 | 105.0 |
| A, t 0.9 -cc | 12.5 | 100.0 % | 100.0 % | 96.9 % | 96.2 % | 37,599 | 31,826 | 54,554 | 104.9 |
| B_norm, t 0.7 | 2.4 | 100.0 % | 100.0 % | 96.3 % | 96.2 % | 5,847 | 6 | 8,509 | 89.6 |
| B_norm, t 0.5 -cc | 2.4 | 100.0 % | 100.0 % | 96.2 % | 96.2 % | 5,718 | 4 | 6,744 | 85.9 |
| **B_norm, t 0.7 -cc** | **2.4** | **100.0 %** | **100.0 %** | **96.2 %** | **96.2 %** | **5,700** | **1** | **6,744** | **85.5** |
| B_norm, t 0.9 -cc | 2.4 | 100.0 % | 100.0 % | 94.8 % | 96.2 % | 5,477 | 1 | 6,744 | 86.1 |

Mentions that joined an existing cluster / largest clusters: A t 0.2 → 3,333 / 155, 95, 82; A t 0.9 -cc → 3,109 / 150, 95, 82;
B_norm t 0.5 -cc → 1,569 / 55, 26, 22; B_norm t 0.7 -cc → 1,558 / 55, 26, 22.

## Findings

1. **Raising the threshold does not fix A**: even at 0.9 without the country comparison, 85 % of merged pairs have unrelated
   names. The trained model gives same-postcode or same-region pairs match probabilities above 0.9 regardless of name
   (training warnings: several `legal_name` levels not trained). Any blocking that scores address-only pairs over-merges.
2. **B on normalised names meets every bar at every threshold**: admits 100 % of `exact` and `variant` reference pairs,
   111× fewer pairs than country-only, unrelated merges 1–6 out of ~5,700 (≤ 0.1 %), fastest runs.
3. **Removing the `country_code` comparison** lowers stored links (8,509 → 6,744) and unrelated merges (6 → 1) at 0.7 with
   no measurable recall change (96.3 % → 96.2 % `exact` clustered). DEC-27 confirmed.
4. **Threshold**: by the DEC-26 rule (lowest threshold with unrelated < 5 %) every value passes, so the rule selects 0.5.
   0.7 has identical recall and 1 instead of 4 unrelated merges; 0.9 loses `exact` recall (94.8 %).
5. **`clustered exact` 96.2 % vs 98.0 % for country-only**: B splits some identical-name pairs into separate clusters
   (greedy order: a mention joins the best-scoring earlier cluster, identical names may already sit in two clusters).
6. B compares every same-country mention by name on each call (Jaro-Winkler + normalisation). At 5.5k this is cheap; at
   300k (DEU ≈ 54k) the per-call regular-expression normalisation over the block is the risk → store the normalised name.
7. Spike caveat: the script's `joins` column counts top candidates, not cluster joins; cluster-based counts above are correct.
