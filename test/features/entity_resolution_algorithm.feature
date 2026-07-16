Feature: Entity Resolution Algorithm

  Entity resolution algorithm for matching and clustering mentions.
  Based on ALGORITHM.md canonical examples with configurable threshold.

  Scenario: First mention always creates a singleton
    Given an entity resolution service with threshold 0.8
    When I resolve mention "m1"
    Then mention "m1" is in cluster "m1" with score 0.0
    And the result has 1 candidate clusters

  Scenario: Strong match joins the best match's cluster
    Given an entity resolution service with threshold 0.8
    When I resolve mention "m1"
    And I set similarity between "m1" and "m2" to 0.95
    And I resolve mention "m2"
    Then mention "m2" is in cluster "m1" with score 0.95
    And the result has 1 candidate clusters

  Scenario: New mention joins cluster of best match, not best match itself
    Given an entity resolution service with threshold 0.8
    When I resolve mention "m1"
    And I set similarity between "m1" and "m2" to 0.95
    And I resolve mention "m2"
    And I set similarity between "m3" and "m2" to 0.92
    And I resolve mention "m3"
    Then mention "m3" is in cluster "m1" with score 0.92
    And the result has 1 candidate clusters

  Scenario: Strong match joins cluster, below-threshold link also surfaces as candidate
    Given an entity resolution service with threshold 0.8
    When I resolve mention "m1"
    Then mention "m1" is in cluster "m1" with score 0.0
    When I resolve mention "m3"
    Then mention "m3" is in cluster "m3" with score 0.0
    When I set similarity between "m2" and "m1" to 0.66
    And I set similarity between "m2" and "m3" to 0.95
    And I resolve mention "m2"
    Then mention "m2" is in cluster "m3" with score 0.95
    And the result has 2 candidate clusters
    And candidate 0 is cluster "m3" with score 0.95
    And candidate 1 is cluster "m1" with score 0.66

  Scenario: Below-threshold match creates singleton, own cluster appears alongside candidates
    Given an entity resolution service with threshold 0.8
    When I resolve mention "m1"
    Then mention "m1" is in cluster "m1" with score 0.0
    When I set similarity between "m1" and "m2" to 0.60
    And I resolve mention "m2"
    Then the result has 2 candidate clusters
    And candidate 0 is cluster "m1" with score 0.60
    And candidate 1 is cluster "m2" with score 0.0
    And the cluster assignment for mention "m2" is "m2"
