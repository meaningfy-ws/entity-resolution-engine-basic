> Derived from EPIC `memory-improvement` (proposal.md) · design: design.md

## 0. Acceptance tests (written ahead, marked `pending`)

- [x] 0.1 Gherkin features + steps: `test/features/resolution_resource_bounds.feature`, `resolution_model_lifecycle.feature`, `mention_lifecycle_tracing.feature` (shared steps in `test/features/steps/conftest.py`)
- [x] 0.2 Unit tests: `test/unit/services/test_resolver_config_duckdb.py`, `test/unit/adapters/test_duckdb_connection.py`, `test/unit/adapters/test_duckdb_lookups.py`, `test/unit/entrypoints/test_queue_worker_consumption.py`
- [x] 0.3 `pending` marker → strict xfail in `test/conftest.py`; each slice below removes the marker from the tests it satisfies

## 1. Baseline and spike

- [x] 1.0 Align local venv to pinned duckdb 1.5.2 / splink 4.0.16; confirm `make test-unit` green (design Risks)
- [x] 1.1 Spike: Splink `Linker` over a `mentions`-backed view — verify `find_matches_to_new_records` sees rows inserted after registration without re-registering; record outcome in design.md Open Questions (decides D-3)
- [x] 1.2 Extend `test/stress/` into a profiling harness (RSS, catalog table count, mean/p95 ms per mention, sample at configurable N) and record the baseline at N ≈ 25k — *harness: `test/stress/profile_resolution.py`; pre-change baseline is the memo benchmark in `inputs/MEMORY-ANALYSIS.md` §4 (the old implementation is replaced, not kept side by side)*

## 2. Leaks and hygiene (spec: resolution-resource-bounds — no per-request artefacts)

- [x] 2.1 Drop the Splink predictions table and new-records view after each match, warning on cleanup failure (D-4)
- [x] 2.2 Unregister `df_temp` after `similarities` insert (D-4)
- [x] 2.3 Remove `get_all_memberships()` from `resolve()`; guard expensive TRACE arguments; delete the FIXME detail block (D-9)
- [x] 2.4 Regression test: catalog table/view count flat across 1,000 resolutions; cleanup-failure scenario

## 3. Per-request reads independent of N (spec: resolution-resource-bounds — per-request work)

- [x] 3.1 Add the four lookup indexes in `init_schema`, idempotent on existing files (D-6)
- [x] 3.2 Add `ClusterRepository.clusters_for(mention_ids)` to port, DuckDB adapter and in-memory stub, with unit tests (D-5)
- [x] 3.3 Build candidates in `resolve()` from in-hand links via `clusters_for`; existing `_gen_cand` tests stay green (D-5)

## 4. DuckDB configuration and shared instance (spec: resolution-resource-bounds — storage by environment)

- [x] 4.1 `DuckDBSettings` value object + env-key constants + precedence (env > yaml > default) in `resolver_config.py`, with unit tests incl. invalid storage (D-2)
- [x] 4.2 Factory opens one DuckDB with settings; Splink `DuckDBAPI` uses `con.cursor()`; start-up logs resolved settings (D-1)
- [x] 4.3 Splink search space as a view over `mentions` (or pandas fallback per 1.1); link-parity test on a fixture corpus vs current behaviour (D-3) — *done: existing Splink integration tests and same-cluster BDD scenarios pass unchanged on the view-backed search space; no side-by-side run against the replaced pandas implementation*
- [x] 4.4 Document new env vars in `app.py` docstring, `infra/.env.example`, compose files (add `mem_limit`) and `src/config/README.md`

## 5. Training lifecycle (spec: resolution-model-lifecycle)

- [x] 5.1 Default `auto_train_threshold` 200 in all `resolver*.yaml`; persist model JSON after successful training (D-7)
- [x] 5.2 Load persisted model at start; round-trip test (save → load → identical scores on fixture pairs); corrupt-file fallback (D-7)
- [x] 5.3 Start-up path: no model and N ≥ threshold → train once on a 200-mention sample, then consume (D-7)
- [x] 5.4 Training uses its own cursor; remove the unclosed replacement connection (D-7, L7)
- [x] 5.5 BDD feature: match across restart, no retraining after threshold, restart with model (steps in `test/features/steps/`)

## 6. Lifecycle tracing (spec: mention-lifecycle-tracing)

- [x] 6.1 Event-name and field-key constants + `MentionTrace` helper in `services/` with unit tests (D-8)
- [x] 6.2 Entrypoint: replace raw-body INFO log with `dequeued` (payload_bytes, queue_wait_ms) and `responded`; summary line at INFO (D-8, DEC-10)
- [x] 6.3 Service: `parsed`, `guard`, `scored`, `clustered`, `persisted` events with timings (D-8)
- [x] 6.4 Tests: no RDF/attribute values in captured logs at INFO and TRACE; event order; idempotent path; single INFO summary

## 7. Verification and hand-off

- [ ] 7.1 Run profiling harness at N ≈ 25k / 150k / 300k; record results against the 360 ms budget and baseline in `inputs/` (DEC-9) — *25k recorded in `inputs/profile-part1-25k.md`: catalog flat, latency not flat (87 → 352 ms mean); 150k/300k deferred until Part 2 blocking lands (a 300k run at today's cost would take > 24 h)*
- [x] 7.2 Update `inputs/MEMORY-ANALYSIS.md` §8 stale note and the installation-manual erratum list (temp dir, env vars)
- [x] 7.3 `make lint`, `make test-unit`, `make check-architecture`, `make check-specs` green

## 8. Part 2 — measurements (DEC-18)

- [x] 8.0 S3 parse cost: 0.4 ms @ 1.7 KB, 4 ms @ 20 KB, 39 ms @ 200 KB, 440 ms @ 2 MB → parse pool cancelled (DEC-12)
- [x] 8.1 S6 diagnosis: profile harness reports per-stage ms and links per mention; run 2.5k…25k on a corpus without name cycles; record in design.md Context — *`inputs/profile-part2-25k.md`: latency flat (65.6 → 70.3 ms over 25k); Splink call ≈ 89 % of a request*
- [x] 8.2 S5 round 1 `test/stress/spike_blocking.py`: country-only, A, A without region/town rule, B, A at `match_weight_threshold` −10/−5/0 on `org-mid.csv`; record reference-pair metrics (D-22) in design.md; fix the blocking rule set in D-18 — *recorded in `inputs/s5-blocking-round1.md`; rule set decided after round 2 (A vs B_norm)*
- [x] 8.5 S5 round 2 with the chosen blocking: cluster `threshold` 0.2/0.5/0.7/0.9, with and without the `country_code` comparison; record; fix `threshold` (DEC-26) and the comparison decision (DEC-27); update `candidate-scoring` scenarios with the chosen values — *recorded in `inputs/s5-blocking-round2.md`: country + Jaro-Winkler ≥ 0.8 on normalised name, threshold 0.7 (owner's choice; rule gave 0.5), `country_code` comparison removed*
- [x] 8.3 S1 batch cost spike with the chosen blocking; record ms/record at 1/10/50/100/500 (decides D-10, D-11) — *`inputs/s1-batch-cost.md`: 72 ms/record one-by-one vs 3.3 at 100 (22×), 2.6 at 500 (28×); batching proceeds*
- [x] 8.4 S2 commit cost on local disk and an NFS mount; record in design.md (D-12 priority) — *local SSD: 2.9 ms per autocommitted insert vs 0.11 ms inside one transaction (26×); no NFS/EFS mount available in the dev environment, so the EFS factor is not measured; D-12 proceeds (gain on SSD alone justifies it)*

## 9. Part 2 — pending acceptance tests

> Execution order (apply session 2): 9.1, 9.4 (candidate scoring) → 10.x → 8.1, 8.3, 8.4 on the new blocking → 9.2, 9.3, 9.4 (batching) → 11 → 12 → 13.

- [x] 9.1 `test/features/candidate_scoring.feature` + steps for every `candidate-scoring` scenario, tagged `@pending` (reference-corpus scenarios as a marked slow test) — *the corpus clustering scenario is tagged `@integration` (≈10 min)*
- [x] 9.2 `test/features/batched_resolution.feature` + steps for every `batched-resolution` scenario, tagged `@pending`
- [x] 9.3 Update Part 1 scenarios: consumption (bite of 100 leaves 900), "bite crosses threshold", "bite context in traces", four → two indexes; tagged `@pending`
- [x] 9.4 Unit tests (pending): blocking-rule config rendering and validation, name normalisation SQL (accents, Cyrillic, punctuation-only), old-schema refusal, `BatchSettings`, `BiteSizer`, `take_bite` byte cap/linger/fallback, `LinkTable` save, unit of work rollback, top-k cut

## 10. Part 2 — score fewer pairs, store less

- [x] 10.1 `legal_name_norm` column filled at insert, exposed in the search-space and new-record views; name-similarity blocking rendered from config with validation; explicit `country_code` EM rule; old-schema refusal (D-18, D-21)
- [x] 10.2 Apply to `src/config/resolver*.yaml` and `test/resources/resolver.yaml`: name-similarity blocking, `threshold: 0.7`, remove `country_code` comparison and its cold-start entry; pin the threshold in fixtures whose scenarios rely on 0.20 (D-23)
- [x] 10.3 Top-k link cut before insert; drop `similarities` indexes in `init_schema` (D-19)

## 11. Part 2 — one commit and columnar links

- [x] 11.1 `DuckDBUnitOfWork`; single-path `resolve()` runs inside it (D-12)
- [x] 11.2 `LinkTable`, `find_matches_batch` port + Splink adapter via Arrow, `SimilarityRepository.save_table` (D-13) — *pandas instead of Arrow: `pyarrow` is not a dependency*
- [x] 11.3 Single path uses the batch code path with a bite of one

## 12. Part 2 — micro-batching (S1: proceed, 22× at 100)

- [x] 12.1 `BatchSettings` + env constants + validation (D-10)
- [x] 12.2 `BiteSizer` (D-10)
- [x] 12.3 `RedisQueueWorker.take_bite` (BRPOP + LMPOP/RPOP fallback, byte cap with push-back, linger) (D-10)
- [x] 12.4 `EntityResolver.resolve_batch`: insert-then-score, arrival-order assignment, training on crossing (D-11, DEC-20) — *found and fixed: scoring must use the shared connection (a cursor does not see the bite's uncommitted inserts); background training now hands trained parameters to the resolver thread instead of building the scoring linker on its own thread*
- [x] 12.5 `EntityResolutionService.process_batch`: per-request guards, one-by-one fallback on rollback (D-11)
- [x] 12.6 Pipelined response push in request order (D-16)
- [x] 12.7 Batch tracing: `batch_id`, `batch_size`, per-bite `batch` event (D-17)
- [x] 12.8 `app.py` loop uses bites; docs for new env vars and the database-reset upgrade note (D-21)

## 13. Part 2 — verification

- [x] 13.1 Remove `@pending` from Part 2 tests as slices land; unit + BDD + Splink integration green
- [x] 13.2 Backlog profile: 25k queued requests through the worker; record mentions/s, p95 bite time, per-stage ms vs Part 1 in `inputs/` — *`inputs/profile-part2-25k.md`: 368 → 316 mentions/s over 25k, bites of 500 in ~1.5 s*
- [x] 13.4 Architecture and clean-code review of the whole change (Part 1 and Part 2) against the Meaningfy principles (`cosmic-python` catalogue): layer placement and import direction, no free strings, function size and SRP, tests per layer and BDD per use case, observability placement; record findings and fixes in design.md — *done via 14.1, 14.2, 14.5, 14.6; follow-ups 14.3, 14.4, 14.7*
- [x] 13.3 `make lint`, `make test-unit`, `make check-architecture`, `make check-clean-code`, `make check-specs` green

## 14. Review and hardening (requested 2026-09-17)

- [x] 14.1 Architecture review (L3 lens) of the whole change; findings recorded in design.md "Review findings"
- [x] 14.2 Fix L3 should-fix #1 (factory depends on the port: `SimilarityLinker.needs_training()`), #5 (no exception text or tracebacks with data at INFO+), #7 (`ere.models` added to import-linter layers), and the reported training race (non-blocking lock)
- [x] 14.3 L3 should-fix #2 and #3: typed blocking rules, normalised fields and EM blocking field in `ResolverConfig`, validated at start; linker receives typed config instead of the raw dict — *`BlockingSettings`/`EqualityRule`/`NameSimilarityRule` in `models/resolver/blocking.py`, parsed and validated in `ResolverConfig.from_dict`; `splink.normalised_fields` and `splink.em_blocking_field` configurable; adapter only renders SQL. The Splink comparisons/cold-start section is still read from the raw dict (adapter-specific)*
- [x] 14.4 L3 should-fix #4: model-lifecycle logging moves from the Splink adapter to services (training outcome returned by the port); parameter dump becomes debug-only — *`TrainingOutcome`/`ModelStatus`/`ModelSource` in `models/resolver/model_state.py`; port `train() -> TrainingOutcome` and `model_status()`; logging in `services/model_lifecycle.py`; the adapter keeps DEBUG only plus the per-request cleanup warning required by the spec*
- [x] 14.5 Adversarial code review, remaining lenses: L2 correctness and thread safety (bite fallback, trained-settings hand-over, byte cap push-back order, duplicate ids in a bite, empty bites), L4 clean code (free strings, function size, duplication), L1/L5; record findings, fix blockers and should-fix items
- [x] 14.6 Adversarial test review: for every spec requirement check positive, negative and edge cases (empty bite, bite of one, all requests unparsable, duplicate mention in one bite with same and different content, byte cap exactly reached, LMPOP returning fewer than asked, training threshold crossed by exactly one mention, names that normalise to NULL, NULL country, top_n smaller than links with score ties, corrupt and empty model files, outdated schema with partial columns); strengthen weak assertions, remove tests that cannot fail
- [x] 14.7 Nice-to-have follow-ups from L3 (#6 ports into `models/ports`, #8 composition root out of `services/`, #9 Redis request-queue adapter) — decide keep/defer with the owner, record in proposal — *done on owner request: ports in `models/ports` (`repositories`, `rdf_mapper`, `resolver`, `linker`); composition root `entrypoints/bootstrap.py` (replaces `services/factories.py`, `adapters/factories.py`, and the environment parsing formerly in `services/resolver_config.py`); `adapters/redis_request_queue.py`; import-linter contract `ere.services` must not import `ere.adapters`*
- [x] 14.8 Integration tests with Redis: `make infra-up` (project stack only, never another project's Redis), run `make test-integration` and the e2e suite, fix fallout, `make infra-down` — *24 passed, 2 skipped (need a running ERE service container). Host port 6379 is taken by a local Redis that belongs to no project here, and `make infra-up` would also start an ERE container consuming the test queue, so tests ran against a throw-away `redis:7.4.4-alpine` on 127.0.0.1:6380 (`REDIS_PORT=6380`), removed afterwards*

## 15. Clean-architecture alignment, config fixes, final verification (owner request 2026-09-17)

- [x] 15.1 Fix `resolver_compound.yaml` / `resolver_multirule.yaml`: add `entity_fields`, replace the non-existent `city` field with `post_name`, prior 0.3 → 0.003; test that every shipped configuration builds a resolver and resolves a mention
- [x] 15.2 Restore CRLF line endings on files that had them in HEAD (review-diff hygiene) — *28 files; diff 3399/2951 → 1727/1279 changed lines; formatter-only changes reverted in 4 files this change does not otherwise touch*
- [x] 15.3 Redis integration and e2e tests after the refactors — *24 passed, 2 skipped; with the ERE service started against the test Redis the same file runs 6 passed, so the two long-skipped tests now execute*
- [x] 15.4 Profiles at 25k (single path and backlog) compared with the Part 2 run in one table (time box: 60 min) — *`inputs/profile-post-refactor-25k.md`; refactors are performance-neutral: backlog within ±1.5 % at every checkpoint, single path 1–5 % faster (70.3 → 66.9 ms mean at 25k), RSS within 9 MB, catalog constant at 4*

## Roadmap

- [x] 0.1 · [x] 0.2 · [x] 0.3 · [x] 1.0 · [x] 1.1 · [x] 1.2 · [x] 2.1 · [x] 2.2 · [x] 2.3 · [x] 2.4 · [x] 3.1 · [x] 3.2 · [x] 3.3 · [x] 4.1 · [x] 4.2 · [x] 4.3 · [x] 4.4 · [x] 5.1 · [x] 5.2 · [x] 5.3 · [x] 5.4 · [x] 5.5 · [x] 6.1 · [x] 6.2 · [x] 6.3 · [x] 6.4 · [ ] 7.1 · [x] 7.2 · [x] 7.3 · [x] 8.0 · [x] 8.1 · [x] 8.2 · [x] 8.3 · [x] 8.4 · [x] 8.5 · [x] 9.1 · [x] 9.2 · [x] 9.3 · [x] 9.4 · [x] 10.1 · [x] 10.2 · [x] 10.3 · [x] 11.1 · [x] 11.2 · [x] 11.3 · [x] 12.1 · [x] 12.2 · [x] 12.3 · [x] 12.4 · [x] 12.5 · [x] 12.6 · [x] 12.7 · [x] 12.8 · [x] 13.1 · [x] 13.2 · [x] 13.3 · [x] 13.4 · [x] 14.1 · [x] 14.2 · [x] 14.3 · [x] 14.4 · [x] 14.5 · [x] 14.6 · [x] 14.7 · [x] 14.8 · [x] 15.1 · [x] 15.2 · [x] 15.3 · [x] 15.4

## Verification

Part 1: unit + BDD suites green with no `pending` markers left, flat-catalog regression test, profiling harness within 360 ms mean at N = 300k, `openspec validate memory-improvement --strict`, clarity gate ≥ 9/10 before `/opsx:apply`. Part 2: spikes S1–S4 recorded, backlog profile shows mentions/s gain vs Part 1, same gates.
