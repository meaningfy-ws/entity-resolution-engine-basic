# EPIC: rc8-review-findings — suspected defects and open questions found in the 1.1.0-rc.8 review

## Appetite

**Small** for the investigation (≈3 days): confirm or dismiss each finding with a failing test or a
written explanation. The fixes are shaped separately, per confirmed finding, once the real cause is known.

## Why

A documentation review of ERSys 1.1.0-rc.8 compared the use cases, ADRs and the ERS–ERE Technical
Contract with the code and tests at tag `1.1.0-rc.8`. Most differences were intended decisions and the
documentation was updated. The findings below were not: they look like defects or undecided behaviour
in the Basic ERE, or in how the ERE and the ERS interact. They come from static analysis of code, tests
and commit history only; none has been reproduced at runtime. Each needs further analysis to determine
the real issue before any fix is designed. Evidence per finding is in `inputs/findings.md`.

## Solution outline

Outcome: every finding below is either **confirmed** (reproduced by a failing unit or BDD test, with the
real cause identified) or **dismissed** (with the reason recorded in this change). Confirmed findings
then get their own shaped fix, as specs deltas in this change or in follow-up changes.

Findings to investigate:

1. **F-1 `candidates[0]` is not always the assigned cluster.** When a mention is placed in a new
   singleton cluster but has links below the clustering threshold, candidate ranking can put another
   cluster first. The contract requires `candidates[0]` to be the ERE's assignment, and the ERS records
   `candidates[0]` as the placement, so the two components may disagree. Related: top-N pruning can
   drop the assigned cluster, and the idempotent path re-ranks from current links.
2. **F-2 Mention data in error responses and debug logs.** A `ConflictError` response carries the
   existing and incoming mention attributes on the response queue, and unexpected errors are logged
   with `exc_info` at DEBUG. Check against the rule that mention content is never logged.
3. **F-3 Curator recommendations have no effect.** `proposed_cluster_ids` and `excluded_cluster_ids`
   are not read; a request for an already-resolved mention is answered from stored state. Combined with
   the ERS rule "one curator action per placement", decisions can become permanently non-curatable
   (see the ERS change of the same name).
4. **F-4 Message loss.** Requests are popped without acknowledgement and failed response pushes are
   only logged. This is an accepted decision (no crash recovery), but its system effect (a provisional
   identifier that is never corrected) should be confirmed as acceptable.
5. **F-5 Store reset orphans ERS identifiers.** After a database reset (required by the 1.1.0-rc.8
   upgrade), cluster identifiers held by the ERS are unknown to the ERE, and new mentions of the same
   entities form new clusters. Decide whether a rebuild or migration path is needed.
6. **F-6 Test pins behaviour against the contract.** The BDD scenario "Below-threshold match creates
   singleton, own cluster appears alongside candidates" asserts the ordering questioned in F-1.

## Key decisions

- **DEC-1**: Investigate before fixing — the findings come from static analysis; each is confirmed by a
  failing test or dismissed with a reason before any code changes.
- **DEC-2**: The ERS–ERE Technical Contract is the reference: where code and contract disagree, the
  default assumption is that the code is wrong, unless the investigation shows the contract should change
  (then the change is proposed in entity-resolution-docs).

## Rabbit-holes

- Re-designing the clustering algorithm (greedy, online) — out of scope; F-1 concerns only the order of
  the response.
- Implementing recommendation handling (F-3) inside the investigation — first decide whether the Basic
  ERE should honour recommendations at all or whether the ERS should change.

## No-gos

- No behaviour change in this change until each finding is confirmed.
- No change to the ERS–ERE message models (entity-resolution-spec).
- No performance or memory work (covered by earlier changes).

---

## What Changes

- Investigation only at this stage: failing tests for confirmed findings, recorded conclusions for
  dismissed ones. Fix scope is added after the investigation.

## Capabilities

### New Capabilities
<!-- To be determined after the investigation. -->

### Modified Capabilities
<!-- To be determined after the investigation; likely candidate ranking (F-1) and error responses (F-2). -->

## Impact

- `src/ere/services/entity_resolution_service.py` (candidate ranking, idempotent path, error responses),
  `src/ere/models/exceptions.py`, `src/ere/adapters/redis_request_queue.py`, `src/ere/entrypoints/queue_worker.py`.
- `test/features/entity_resolution_algorithm.feature` and related unit tests.
- ERS: outcome integration relies on `candidates[0]` (see the ERS change `rc8-review-findings`).
