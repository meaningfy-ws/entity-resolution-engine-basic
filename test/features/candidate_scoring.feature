Feature: Scoring only plausible candidates

  As the operator of the ERE
  I want each new mention scored only against similarly named organisations of its country
  So that per-mention cost does not track the country's size and unrelated organisations are not merged

  Spec: openspec/changes/memory-improvement/specs/candidate-scoring/spec.md


  # ---------------------------------------------------------------------------
  # Only similarly named organisations of the same country are scored
  # ---------------------------------------------------------------------------

  Scenario: Reference pairs are admitted on the reference corpus
    Given the shipped blocking rules
    When they are evaluated on the reference corpus
    Then they admit at least 99 percent of the exact reference pairs
    And they admit at least 95 percent of the variant reference pairs
    And they admit at least 10 times fewer pairs per mention than blocking on country alone

  Scenario Outline: Which stored mentions are scored against a new one
    Given a Splink-backed resolver with name-similarity blocking
    And a stored mention "<stored_name>" in "<stored_country>" at post code "<stored_post>"
    When a new mention "<new_name>" in "<new_country>" at post code "<new_post>" is scored
    Then the stored mention <outcome>

    Examples:
      | stored_name                        | stored_country | stored_post | new_name                           | new_country | new_post | outcome           |
      | Acme Industries GmbH               | DEU            | 10115       | ACME-Industries GmbH.              | DEU         | 80331    | is scored         |
      | Комисия за защита на конкуренцията | BGR            | 1000        | КОМИСИЯ ЗА ЗАЩИТА НА КОНКУРЕНЦИЯТА | BGR         | 1000     | is scored         |
      | Bäckerei Schmidt                   | DEU            | 10115       | Acme Industries GmbH               | DEU         | 10115    | is not scored     |
      | Acme Industries GmbH               | AUT            | 1010        | Acme Industries GmbH               | DEU         | 10115    | is not scored     |
      | ...                                | DEU            | 10115       | --- ---                            | DEU         | 10115    | is not scored     |

  Scenario: An unknown key in a blocking rule stops the ERE from starting
    Given a blocking rule with the unknown key "similar_to"
    When the blocking rules are loaded
    Then loading fails with an error naming "similar_to" and the allowed keys


  # ---------------------------------------------------------------------------
  # Cluster joins need strong evidence
  # ---------------------------------------------------------------------------

  Scenario: The same organisation at the same address joins its cluster
    Given the ERE is started with the shipped configuration
    And it has resolved "Acme Industries GmbH" in "DEU" at post code "10115" in "Berlin" as mention "a"
    When it resolves "ACME Industries GmbH." in "DEU" at post code "10115" in "Berlin" as mention "b"
    Then mention "b" is in the cluster of mention "a"

  @integration
  Scenario: Few unrelated merges on the reference corpus
    Given the ERE is started with the shipped configuration
    When the reference corpus is resolved in file order
    Then fewer than 5 percent of the pairs placed in the same cluster have unrelated names
    And at least 95 percent of the exact reference pairs are placed in the same cluster

  Scenario: No shipped configuration compares the country
    When the shipped resolver configurations are loaded
    Then none of them compares "country_code"
    And all of them use a cluster threshold of 0.7


  # ---------------------------------------------------------------------------
  # Existing databases, training, storage
  # ---------------------------------------------------------------------------

  Scenario: A database from before the change is refused
    Given a database file created before this change
    When the ERE is started on it
    Then start-up fails with an error naming the database file and telling to reset it

  Scenario: Training blocks on country regardless of scoring rules
    Given a Splink-backed resolver with name-similarity blocking
    Then its training blocking rule compares "country_code" only

  Scenario Outline: Only the best links are stored
    Given a resolver whose linker returns <produced> links for the next mention
    When that mention is resolved with top_n 100
    Then <stored> links are stored for it
    And its candidates equal those built from all <produced> links

    Examples:
      | produced | stored |
      | 250      | 100    |
      | 7        | 7      |
