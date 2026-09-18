Feature: Tracing a mention through the ERE

  As an operator investigating ERE throughput
  I want every request traced from queue to response with timings, without payloads
  So that I can see where time goes and how long requests waited in the queue

  Spec: openspec/changes/memory-improvement/specs/mention-lifecycle-tracing/spec.md

  Background:
    Given an ERE worker backed by a resolution service


  # ---------------------------------------------------------------------------
  # Request payloads are never logged
  # ---------------------------------------------------------------------------

  Scenario Outline: Payloads never reach the log
    When request "r1" is processed at log level "<level>"
    Then no log line contains the RDF content or attribute values
    And the log carries request id "r1" and its payload size

    Examples:
      | level |
      | INFO  |
      | DEBUG |
      | TRACE |


  # ---------------------------------------------------------------------------
  # Mention lifecycle events with timings
  # ---------------------------------------------------------------------------

  Scenario: Lifecycle events of a new mention at DEBUG
    When request "r1" is processed at log level "DEBUG"
    Then the lifecycle events are "dequeued, parsed, guard, scored, clustered, persisted, responded" in that order
    And every lifecycle event carries request id "r1" and a stage duration

  Scenario: A conflicting re-submission is reported by the guard
    Given request "r1" was already processed
    When request "r1" with different content is processed at log level "DEBUG"
    Then the guard event reports "conflict"
    And no "scored" event is logged
    And no "clustered" event is logged

  Scenario: An idempotent re-submission skips scoring
    Given request "r1" was already processed
    When request "r1" is processed at log level "DEBUG"
    Then the guard event reports "idempotent"
    And no "scored" event is logged
    And no "clustered" event is logged

  Scenario Outline: Scoring outcome is reported
    Given request "a" was already processed
    And mentions "a" and "b" have similarity <score>
    When request "b" is processed at log level "DEBUG"
    Then the scored event reports 1 links with best score <score>
    And the clustered event reports decision "<decision>" into cluster "<cluster>"

    Examples:
      | score | decision | cluster |
      | 0.95  | join     | a       |
      | 0.10  | new      | b       |


  # ---------------------------------------------------------------------------
  # One summary line per request at INFO
  # ---------------------------------------------------------------------------

  Scenario: One summary line at INFO
    When request "r1" is processed at log level "INFO"
    Then exactly one summary line is logged for request "r1"
    And no per-stage lifecycle events are logged
    And the summary for request "r1" reports mention "r1", guard "new", decision "new", its payload size and every stage duration


  # ---------------------------------------------------------------------------
  # Queue wait is reported when measurable
  # ---------------------------------------------------------------------------

  Scenario: Queue wait of a timestamped request
    When request "r1" created 11 seconds ago is processed at log level "DEBUG"
    Then the dequeued event and the summary report a queue wait between 11000 and 15000 ms

  Scenario: A request without timestamp is still processed
    When request "r1" without timestamp is processed at log level "DEBUG"
    Then no event reports a queue wait
    And exactly one summary line is logged for request "r1"
    And 1 response is in the response queue


  # ---------------------------------------------------------------------------
  # Bite context in traces
  # ---------------------------------------------------------------------------

  Scenario: Requests of one bite share the bite context
    Given requests "r1", "r2" and "r3" are waiting in the request queue
    When one bite is processed at log level "INFO"
    Then 3 summary lines share one batch id with batch size 3
    And exactly one batch event reports batch size 3 with its stage timings
