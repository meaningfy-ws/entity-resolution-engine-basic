## ADDED Requirements

### Requirement: Only similarly named organisations of the same country are scored
The ERE SHALL score a new mention only against stored mentions with the same `country_code` whose normalised legal
names (lower-case, accents stripped, letters and digits only) have a Jaro-Winkler similarity of at least 0.8. The
normalised name SHALL be computed once when a mention is stored.

#### Scenario: Reference pairs admitted on the reference corpus
- **WHEN** the configured blocking is evaluated on `test/stress/data/org-mid.csv`
- **THEN** it admits at least 99 % of the `exact` reference pairs (same country, identical normalised legal name) and at least 95 % of the `variant` reference pairs (same country, normalised names with Jaro-Winkler similarity ≥ 0.95, same post code)

#### Scenario: Fewer pairs scored
- **WHEN** the same corpus is evaluated
- **THEN** the configured blocking admits at least 10 times fewer pairs per mention than blocking on `country_code` alone

#### Scenario: Spelling variants are scored
- **WHEN** a stored mention "Acme Industries GmbH" (DEU) exists and a new mention "ACME-Industries GmbH." (DEU) is resolved
- **THEN** the pair is scored

#### Scenario: Accented and non-Latin names are compared by their letters
- **WHEN** a stored mention "Комисия за защита на конкуренцията" (BGR) exists and a new mention "КОМИСИЯ ЗА ЗАЩИТА НА КОНКУРЕНЦИЯТА" (BGR) is resolved
- **THEN** the pair is scored

#### Scenario: Different names at the same address are not scored
- **WHEN** a stored mention "Bäckerei Schmidt" and a new mention "Acme Industries GmbH" share country, post code, town and region
- **THEN** the pair is not scored

#### Scenario: Same name in another country is not scored
- **WHEN** a stored mention "Acme Industries GmbH" (AUT) exists and a new mention "Acme Industries GmbH" (DEU) is resolved
- **THEN** the pair is not scored

#### Scenario: Names without letters or digits never block together
- **WHEN** two mentions of the same country have legal names consisting only of punctuation
- **THEN** the pair is not scored

#### Scenario: Invalid blocking configuration
- **WHEN** a `blocking_rules` entry contains an unknown key
- **THEN** the ERE SHALL refuse to start and log an error naming the key and the allowed keys

### Requirement: Cluster joins need strong evidence
The ERE SHALL join a new mention to an existing cluster only when its best link reaches the configured `threshold`
(default 0.7), and SHALL NOT include a comparison that agrees for every scored pair by construction of the blocking.

#### Scenario: Same organisation joins
- **WHEN** a stored mention "Acme Industries GmbH" and a new mention "ACME Industries GmbH." share country, post code, town and region
- **THEN** the new mention joins the stored mention's cluster

#### Scenario: Unrelated merges on the reference corpus
- **WHEN** `test/stress/data/org-mid.csv` is resolved in file order with the configured rules and threshold
- **THEN** fewer than 5 % of the pairs placed in the same cluster have normalised names with Jaro-Winkler similarity below 0.8, and at least 95 % of the `exact` reference pairs are placed in the same cluster

#### Scenario: No always-agreeing comparison
- **WHEN** the shipped resolver configurations are loaded
- **THEN** none compares `country_code`

### Requirement: Existing databases without the normalised name are refused
The ERE SHALL refuse to start on a database whose `mentions` table lacks the normalised-name column, with an error
telling the operator to reset the database file.

#### Scenario: Database from before the change
- **WHEN** the ERE starts on a database file created before this change
- **THEN** it logs an error naming the database file and the reset instruction, and exits non-zero

### Requirement: Training blocking is unchanged
EM training SHALL block on `country_code` regardless of the scoring blocking rules.

#### Scenario: Training with name-similarity scoring rules
- **WHEN** training runs with the configured name-similarity blocking
- **THEN** the expectation-maximisation step blocks on `country_code`

### Requirement: Only the best links are stored
The ERE SHALL store at most `top_n` links per newly resolved mention, keeping the highest scores, while building that
mention's response candidates from all links computed for it.

#### Scenario: Dense group of near-duplicates
- **WHEN** a new mention produces 250 links and `top_n` is 100
- **THEN** 100 links with the highest scores are stored and the response candidates are identical to those built from all 250 links

#### Scenario: Few links
- **WHEN** a new mention produces 7 links
- **THEN** all 7 links are stored
