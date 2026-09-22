## ADDED Requirements

### Requirement: Request payloads are never logged
The ERE SHALL NOT write request bodies, RDF content, or mention attribute values to logs at any log level.

#### Scenario: Request received at INFO
- **WHEN** a request is dequeued with `ERE_LOG_LEVEL=INFO`
- **THEN** the log contains the request id and payload size in bytes, and no RDF content

#### Scenario: Request received at TRACE
- **WHEN** a request is dequeued with `ERE_LOG_LEVEL=TRACE`
- **THEN** no log line contains the RDF content or attribute values of the mention

### Requirement: Mention lifecycle events with timings
The ERE SHALL emit a structured log event for each lifecycle stage of a request (`dequeued`, `parsed`, `guard`,
`scored`, `clustered`, `persisted`, `responded`). Every event SHALL carry the `ere_request_id` and, once known,
the `mention_id`, together with the elapsed milliseconds of that stage.

#### Scenario: New mention at DEBUG
- **WHEN** a new mention is resolved with `ERE_LOG_LEVEL=DEBUG`
- **THEN** the seven lifecycle events are logged in order, each with the request id and stage duration

#### Scenario: Idempotent re-submission
- **WHEN** an already-resolved mention is submitted again
- **THEN** the `guard` event reports `idempotent`, and no `scored` or `clustered` events are emitted

#### Scenario: Scoring outcome
- **WHEN** scoring completes for a new mention
- **THEN** the `scored` event reports the number of links and the best score, and the `clustered` event reports `join` or `new` with the cluster id

### Requirement: One summary line per request at INFO
The ERE SHALL emit exactly one INFO summary line per processed request, containing the request id, mention id,
guard outcome, decision, payload size, per-stage durations and total duration.

#### Scenario: Summary at INFO
- **WHEN** a request is processed with `ERE_LOG_LEVEL=INFO`
- **THEN** exactly one summary line for that request is logged and no per-stage events are logged

### Requirement: Queue wait is reported when measurable
The ERE SHALL report the time between the request's `timestamp` and its dequeue as `queue_wait_ms` when the
request carries a timestamp.

#### Scenario: Request with timestamp
- **WHEN** a request with a timestamp 11 seconds in the past is dequeued
- **THEN** the `dequeued` event and the summary line report `queue_wait_ms` of about 11,000

#### Scenario: Request without timestamp
- **WHEN** a request without a timestamp is dequeued
- **THEN** `queue_wait_ms` is omitted and processing continues normally

### Requirement: Bite context in traces
Every lifecycle event and summary line SHALL carry the `batch_id` and `batch_size` of the bite the request was part of,
and the ERE SHALL emit one INFO `batch` event per bite with its size, payload bytes, the milliseconds spent parsing
the bite's messages, resolving them and sending the responses, the total milliseconds, and the bite limit used.

#### Scenario: Bite of three requests
- **WHEN** three requests are processed in one bite at INFO
- **THEN** three summary lines share one `batch_id` with `batch_size` 3, and one `batch` event reports size 3 with parse, resolve, respond and total milliseconds
