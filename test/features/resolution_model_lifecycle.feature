Feature: Resolution model lifecycle across restarts

  As the operator of the ERE
  I want the search space and the trained similarity model to survive restarts
  So that a restart neither forgets stored mentions nor falls back to untrained scoring

  Spec: openspec/changes/memory-improvement/specs/resolution-model-lifecycle/spec.md

  Background:
    Given an on-disk ERE database in a temporary directory


  # ---------------------------------------------------------------------------
  # Search space survives restart
  # ---------------------------------------------------------------------------

  Scenario: A mention matches a mention stored before a restart
    Given the ERE is started
    And the ERE has resolved "ACME Industries GmbH" in "DEU" as mention "a"
    When the ERE is restarted
    And the ERE resolves "ACME Industries GmbH." in "DEU" as mention "b"
    Then the candidates of mention "b" include the cluster of mention "a"

  Scenario: A mention matches a mention stored just before it
    Given the ERE is started
    And the ERE has resolved "ACME Industries GmbH" in "DEU" as mention "a"
    When the ERE resolves "ACME Industries GmbH." in "DEU" as mention "b"
    Then the candidates of mention "b" include the cluster of mention "a"


  # ---------------------------------------------------------------------------
  # Training happens once and is frozen
  # ---------------------------------------------------------------------------

  Scenario Outline: Training starts once, at the default threshold
    Given a resolver with the default training threshold and a counting linker
    When <resolved> mentions are resolved
    Then the default training threshold is 200
    And training has started <trainings> times

    Examples:
      | resolved | trainings |
      | 199      | 0         |
      | 200      | 1         |
      | 1000     | 1         |

  Scenario: A bite crossing the threshold starts training once
    Given a resolver with the default training threshold and a counting linker
    When 190 mentions are resolved
    And a bite of 50 more mentions is resolved
    Then training has started 1 times


  # ---------------------------------------------------------------------------
  # Trained model is persisted and reloaded
  # ---------------------------------------------------------------------------

  Scenario: Restart with a trained model file
    Given a trained model file exists beside the database
    When the ERE is started
    Then the linker scores with the persisted model

  Scenario: Start-up past the threshold without a model file
    Given the database already stores 200 organisation mentions
    When the ERE is started
    Then the linker scores with a freshly trained model
    And a model file exists beside the database

  Scenario: A corrupt model file falls back to cold-start parameters
    Given a corrupt model file exists beside the database
    When the ERE is started
    Then the linker scores with cold-start parameters
    And an error about the model file is logged
    And the model file is unchanged

  Scenario: A training failure keeps cold-start parameters
    Given the ERE is started
    And model training will fail
    When training is triggered
    Then the linker scores with cold-start parameters
    And no model file exists beside the database
    And a warning about training is logged
