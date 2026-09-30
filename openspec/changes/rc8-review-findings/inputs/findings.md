# Findings — evidence (static analysis at tag 1.1.0-rc.8)

Source: documentation review of ERSys 1.1.0-rc.8 (2026-09-30), comparing the use cases, ADRs and the
ERS–ERE Technical Contract (entity-resolution-docs) with the code, tests and commit history.
Nothing below was reproduced at runtime. Line numbers refer to tag `1.1.0-rc.8`.

## F-1 `candidates[0]` is not always the assigned cluster

- Contract: `ERS-ERE-Contract/interface.adoc` — "`candidates[0]` MUST be the ERE's top proposed cluster
  assignment … ERS always selects `candidates[0]`"; a new singleton outcome is "a single candidate" with
  scores 0.0.
- Code: `_rank_candidates` (`src/ere/services/entity_resolution_service.py` ~388-411) keeps the best score
  per linked cluster, adds the mention's own cluster with score 0.0, sorts by score descending and cuts
  to `top_n`. `_assign_cluster` (~237-244) places the mention in a new singleton when the best link is
  below the threshold (default 0.7). Any link above 0 but below 0.7 then ranks above the own cluster.
- ERS side: `outcome_integration_service.py:89` stores `response.candidates[0]` as the current placement.
- Suspected effect: ERS records a cluster the ERE did not assign; the ERE keeps the mention in its singleton.
- Related: with ≥ `top_n` linked clusters scoring above 0, the own cluster (0.0) is pruned. The idempotent
  path (`_gen_cand`) re-ranks from links as they are now, so later links can change which cluster is first.
- History: the scenario and unit test asserting this order were migrated from the POC (2026-02-27); the
  normative ordering and singleton rules were added to the contract on 2026-04-17; the tests were not revisited.
- To analyse: is the order ever intended to differ from the assignment? What does DEC-26 ("response
  candidates are unaffected") imply for the ERS?

## F-2 Mention data in error responses and debug logs

- `check_conflict` (~326-345) returns an `EREErrorResponse` with `error_type="ConflictError"`; its
  `error_detail` (`src/ere/models/exceptions.py`) includes the existing and incoming mention attributes,
  sent on the response queue.
- `_unexpected_error` (~692-696) logs `exc_info` at DEBUG.
- Rule in question: mention content is never logged at any level (memory-improvement DEC-10).
- To analyse: whether the ERS logs `error_detail` (it currently logs only `ere_request_id` and
  `error_type`), and whether the response queue counts as a log for this rule.

## F-3 Curator recommendations have no effect

- `proposed_cluster_ids` / `excluded_cluster_ids` are not read anywhere in `src/ere`.
- A request for an already-resolved mention id goes through the idempotent path and returns the stored
  assignment with candidates rebuilt from stored links.
- ERS: every curator action (Accept, Assign, Reject) re-sends the mention with recommendations; the ERS
  blocks further actions on a decision until the ERE returns a changed outcome.
- Suspected effect: with the Basic ERE, a curated decision can become permanently non-curatable.
  A float32/float64 difference between stored and in-memory scores may make the first re-query differ
  by accident (`duckdb_schema.py`: similarity scores stored as `REAL`), so the block may appear only
  from the second action.
- To analyse: whether the Basic ERE should honour recommendations, or the ERS should release its guard
  on any curator-triggered response.

## F-4 Message loss

- Requests: destructive pop (`redis_request_queue.py` ~42, 87-96), no processing list or acknowledgement
  (memory-improvement DEC-1, DEC-2).
- Responses: a failed push is logged and dropped (`queue_worker.py` ~176-179).
- Effect: the ERS keeps a provisional identifier that no ERE outcome corrects, until the mention is re-submitted.
- To analyse: confirm this remains acceptable; the documentation now states "no delivery guarantee".

## F-5 Store reset orphans ERS identifiers

- The 1.1.0-rc.8 upgrade requires deleting the DuckDB database (DEC-24); `ERE_DUCKDB_STORAGE=memory`
  resets on every restart.
- The ERS answers replays from its Decision Store and never re-sends decided mentions, so the ERE cannot
  be rebuilt from the ERS. Cluster identifiers held by the ERS become unknown to the ERE; new mentions of
  the same entities form new clusters beside them; the Basic ERE does not merge them.
- Greedy online clustering makes a replay in another order produce different cluster memberships.
- To analyse: whether a rebuild path (for example a re-submission tool) or a migration is needed for
  future schema changes.

## F-6 Test pins behaviour against the contract

- `test/features/entity_resolution_algorithm.feature:44-53` "Below-threshold match creates singleton,
  own cluster appears alongside candidates" and `test_entity_resolution_service.py:150-159` assert
  candidate 0 = another cluster while the assignment is the own singleton.
- To analyse together with F-1.
