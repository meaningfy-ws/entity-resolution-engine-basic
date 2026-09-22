Feature: Resolution stays within resource bounds

  As the operator of a long-running ERE
  I want memory and per-request work to stay flat as mentions accumulate
  So that ERE uptime and corpus size do not slow down the nightly pipeline

  Spec: openspec/changes/memory-improvement/specs/resolution-resource-bounds/spec.md


  # ---------------------------------------------------------------------------
  # No per-request database artefacts are retained
  # ---------------------------------------------------------------------------

  Scenario Outline: The database catalog stays flat across many requests
    Given a Splink-backed resolver on a fresh on-disk database
    When <count> distinct organisation mentions are resolved in sequence
    Then the database catalog and Splink's cache hold as much as before the first request

    Examples:
      | count |
      | 30    |
      | 80    |

  Scenario: A cleanup failure does not fail the request
    Given a Splink-backed resolver on a fresh on-disk database
    And releasing per-request artefacts fails with "cannot drop __splink__find_matches_predictions_x"
    When 2 distinct organisation mentions are resolved in sequence
    Then every resolution returned at least one candidate
    And a warning mentions "__splink__find_matches_predictions_x"


  # ---------------------------------------------------------------------------
  # Per-request work is independent of cluster membership size
  # ---------------------------------------------------------------------------

  Scenario: Candidates are built from the links just computed
    Given a resolver that refuses full membership loads, link re-reads and per-link cluster lookups
    And mentions "m2" and "m1" have similarity 0.66
    And mentions "m2" and "m3" have similarity 0.95
    When mentions "m1", "m3" and "m2" are resolved in that order
    Then the last result has 2 candidates
    And candidate 0 is cluster "m3" with score 0.95
    And candidate 1 is cluster "m1" with score 0.66


  # ---------------------------------------------------------------------------
  # DuckDB storage is configurable by environment with on-disk default
  # ---------------------------------------------------------------------------

  Scenario: Default configuration opens an on-disk database
    Given no DuckDB environment variables are set
    When the ERE database is opened
    Then the database is file-backed
    And its temp directory is beside the database file

  Scenario Outline: DuckDB limits come from the environment
    Given the DuckDB environment variables storage "<storage>" and memory limit "<memory_limit>"
    When the ERE database is opened
    Then the database reports a memory limit of "<reported_limit>"

    Examples:
      | storage | memory_limit | reported_limit |
      | disk    | 2GiB         | 2.0 GiB        |
      | memory  | 4GiB         | 4.0 GiB        |

  Scenario: An invalid storage value stops the ERE from starting
    Given the DuckDB environment variables storage "ram" and memory limit "1GiB"
    When the ERE database settings are resolved
    Then start-up is refused with an error naming "ERE_DUCKDB_STORAGE" and the values "disk" and "memory"


  # ---------------------------------------------------------------------------
  # Single-threaded consumption at processing rate
  # ---------------------------------------------------------------------------

  Scenario: Unprocessed requests stay in the request queue
    Given 1000 resolution requests are waiting in the request queue
    When the ERE processes one bite with limit 100
    Then 900 requests remain in the request queue
    And 100 response is in the response queue
