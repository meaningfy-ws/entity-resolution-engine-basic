Feature: Resolving requests in bites

  As the operator of the ERE
  I want waiting requests taken and resolved together, sized by what the ERE can process
  So that fixed per-call costs are paid once per bite without taking more than the ERE can digest

  Spec: openspec/changes/memory-improvement/specs/batched-resolution/spec.md


  # ---------------------------------------------------------------------------
  # Bites are sized by processing capacity, not by arrival
  # ---------------------------------------------------------------------------

  Scenario: A backlog larger than the bite limit stays in the queue
    Given 10000 resolution requests are waiting in the request queue
    When the ERE takes a bite with limit 400
    Then the bite holds 400 requests
    And 9600 requests remain in the request queue

  Scenario: The byte cap limits a bite of large requests
    Given 30 resolution requests of 2 MB each are waiting in the request queue
    And the bite byte cap is 50 MB
    When the ERE takes a bite with limit 500
    Then the bite holds at most 25 requests
    And the requests not taken remain in the request queue

  Scenario: A lone request waits at most the linger
    Given 1 resolution requests are waiting in the request queue
    And the bite linger is 250 ms
    When the ERE takes a bite with limit 500
    Then the bite holds 1 requests
    And taking the bite took less than 750 ms

  Scenario Outline: The bite limit follows processing speed
    Given a bite sizer with target <target> seconds and at most <max> mentions
    When bites of <mentions> mentions were each processed in <seconds> seconds
    Then the next bite limit is <limit>

    Examples:
      | target | max | mentions | seconds | limit |
      | 2      | 500 | 50       | 1       | 100   |
      | 2      | 500 | 400      | 1       | 500   |
      | 2      | 500 | 1        | 10      | 1     |

  Scenario: An invalid batch setting stops the ERE from starting
    Given the environment variable "ERE_BATCH_MAX_MENTIONS" is "0"
    When the batch settings are resolved
    Then resolving fails with an error naming "ERE_BATCH_MAX_MENTIONS"


  # ---------------------------------------------------------------------------
  # Mentions in the same bite can match each other
  # ---------------------------------------------------------------------------

  Scenario: Two similar mentions in one bite end in one cluster
    Given a resolver with threshold 0.8 whose similarity between "a" and "b" is 0.95
    When mentions "a" and "b" are resolved in one bite
    Then the candidates of "b" include the cluster of "a"
    And mention "b" is in the cluster of "a"

  Scenario: Similar mentions in one bite match with the production configuration
    Given the ERE is started with the shipped configuration
    When "Acme Industries GmbH" and "ACME Industries GmbH." in "DEU" at post code "10115" in "Berlin" are resolved in one bite as "p" and "q"
    Then production mention "q" is in the cluster of "p"

  Scenario: Bites give the same clusters as one request at a time
    Given a sequence of 200 mentions with random pairwise similarities
    When the sequence is resolved once in bites of 50 and once one at a time
    Then every mention is in the same cluster in both runs

  @integration
  Scenario: Bites give the same clusters as one request at a time with the production configuration
    When the first 400 reference organisations are resolved once in bites of 50 and once one at a time with the shipped configuration and training disabled
    Then every organisation is in the same cluster in both runs


  # ---------------------------------------------------------------------------
  # One response per request, failure isolation, one commit
  # ---------------------------------------------------------------------------

  Scenario: A bite of mixed outcomes answers every request in order
    Given an ERE worker with a stored mention "seen"
    And the request queue holds, in order, a new mention "fresh", a re-submission of "seen", a conflicting re-submission of "seen" and an unparsable message
    When the ERE processes one bite
    Then 4 responses are sent in request order
    And the responses are a resolution, a resolution, a "ConflictError" error and a "ProcessingError" error

  Scenario: A failing bite is rolled back and resolved one request at a time
    Given a DuckDB-backed resolver whose next batch scoring fails
    When a bite of 20 new mention requests is processed
    Then 20 responses are returned without errors
    And every mention is stored exactly once with exactly one cluster assignment

  Scenario: One unresolvable mention does not fail its bite
    Given a DuckDB-backed resolver that cannot score mention "bad"
    When the requests "a", "bad" and "c" are processed as one bite
    Then the responses for "a" and "c" are resolutions and the response for "bad" is an "InternalError" error
    And only the mentions "a" and "c" are stored

  Scenario: A bite commits once
    Given a DuckDB-backed resolver counting transactions
    When a bite of 100 new mentions is resolved
    Then exactly 1 transaction was committed
