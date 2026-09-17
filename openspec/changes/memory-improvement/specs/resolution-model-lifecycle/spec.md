## ADDED Requirements

### Requirement: Search space survives restart
The similarity linker SHALL compare each new mention against all mentions persisted in the ERE database,
including mentions stored before the most recent restart.

#### Scenario: Match across a restart
- **WHEN** mention A is resolved, the ERE is restarted, and a mention B with a near-identical legal name and the same country is resolved
- **THEN** B's candidates include A's cluster

#### Scenario: Newly stored mention is visible to the next request
- **WHEN** mention A is resolved and mention B similar to A is resolved immediately afterwards without restart
- **THEN** B's candidates include A's cluster

### Requirement: Training happens once and is frozen
The ERE SHALL estimate similarity-model parameters exactly once, when the number of stored mentions reaches
the configured training threshold (default 200), and SHALL NOT retrain afterwards.

#### Scenario: Training at threshold
- **WHEN** the 200th mention is stored and no trained model exists
- **THEN** training starts once in the background and scoring continues with the current parameters until training completes

#### Scenario: A bite crosses the threshold
- **WHEN** 190 mentions are stored and a bite of 50 new mentions is resolved
- **THEN** training starts once

#### Scenario: No retraining after threshold
- **WHEN** mentions 201 through 10,000 are resolved
- **THEN** no further training is started

#### Scenario: Stored similarities are not recomputed
- **WHEN** training completes
- **THEN** similarity rows stored before training keep their original scores

### Requirement: Trained model is persisted and reloaded
The ERE SHALL persist the trained model after successful training and SHALL load it at start-up when present,
without retraining.

#### Scenario: Restart with a trained model
- **WHEN** the ERE restarts and a trained model file exists
- **THEN** the linker scores with the persisted parameters and no training runs

#### Scenario: Restart past threshold without a model
- **WHEN** the ERE starts with at least 200 stored mentions and no trained model file
- **THEN** it trains once on a sample of 200 stored mentions, persists the model, and then starts consuming requests

#### Scenario: Corrupt model file
- **WHEN** the model file exists but cannot be loaded
- **THEN** the ERE logs an error, scores with cold-start parameters, and does not overwrite the file

#### Scenario: Training failure
- **WHEN** training raises an error
- **THEN** cold-start parameters remain in use, a warning is logged, and no model file is written
