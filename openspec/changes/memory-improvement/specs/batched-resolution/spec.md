## ADDED Requirements

### Requirement: Bites are sized by processing capacity, not by arrival
The ERE SHALL take requests from the request queue in bites. After a blocking wait for the first request, it
SHALL take only requests already waiting, up to a bite limit equal to its recent processing rate (mentions per
second) multiplied by `ERE_BATCH_TARGET_SECONDS`, never exceeding `ERE_BATCH_MAX_MENTIONS` requests or
`ERE_BATCH_MAX_BYTES` of payload. It SHALL NOT take the next bite before every response of the current bite is sent.

#### Scenario: Backlog larger than the bite limit
- **WHEN** 10,000 requests are queued and the bite limit is 400
- **THEN** the ERE takes 400 requests and 9,600 remain in the request queue until those 400 responses are sent

#### Scenario: Byte cap reached before the count cap
- **WHEN** 300 requests of 2 MB each are queued and `ERE_BATCH_MAX_BYTES` is 50 MB
- **THEN** the bite holds at most 25 requests

#### Scenario: Quiet queue adds at most the linger
- **WHEN** a single request arrives on an empty queue and no other request arrives
- **THEN** its processing starts no later than `ERE_BATCH_LINGER_MS` after it is taken

#### Scenario: Bite limit adapts to processing speed
- **WHEN** recent bites were processed at 50 mentions per second and `ERE_BATCH_TARGET_SECONDS` is 2
- **THEN** the next bite limit is 100 requests (within the configured maxima)

#### Scenario: Invalid batch setting
- **WHEN** `ERE_BATCH_MAX_MENTIONS=0` is set
- **THEN** the ERE SHALL refuse to start and log an error naming the variable

### Requirement: Mentions in the same bite can match each other
The ERE SHALL score each mention of a bite against stored mentions and against mentions earlier in the same
bite, and SHALL assign clusters in arrival order.

#### Scenario: Two similar mentions in one bite
- **WHEN** mentions "a" and "b" with near-identical names in the same country arrive in the same bite, "a" first
- **THEN** the candidates of "b" include the cluster of "a"

#### Scenario: Same result as one at a time
- **WHEN** the same ordered sequence of mentions is resolved once in bites of 50 and once one at a time
- **THEN** every mention is assigned to the same cluster in both runs

### Requirement: One response per request, in request order
The ERE SHALL send exactly one response per request of a bite, in the order the requests were taken, pushed in a
single pipelined call.

#### Scenario: Bite of mixed outcomes
- **WHEN** a bite holds a new mention, an idempotent re-submission, a conflicting re-submission and an unparsable message
- **THEN** four responses are sent in request order: a resolution, a resolution, a conflict error and a processing error

### Requirement: Failures are isolated per request
The ERE SHALL decide parsing, conflict and idempotency per request. If scoring or persisting a bite fails, it SHALL
roll back the bite's writes and resolve the bite's requests one at a time.

#### Scenario: Scoring a bite fails
- **WHEN** scoring a bite of 20 new mentions raises an error
- **THEN** no partial rows of that bite remain, the 20 requests are resolved one at a time, and 20 responses are sent

### Requirement: One database commit per bite
The ERE SHALL persist all mentions, similarities and cluster assignments of a bite in a single transaction.

#### Scenario: Commits per bite
- **WHEN** a bite of 100 new mentions is resolved
- **THEN** exactly one DuckDB transaction is committed for the bite's writes
