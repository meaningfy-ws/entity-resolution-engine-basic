# Stress Test Documentation

Unified stress test runner for the entity resolver. This document describes usage patterns, parameters, and interpretation of results.

## Quick Start

### Small dataset smoke test (~30 seconds)

```bash
poetry run python3 test/stress/stress_test.py \
  --dataset test/stress/data/org-small.csv \
  --seed 20 \
  --records 30 \
  --output /tmp/results.json
```

### Mid-size dataset baseline (2-3 minutes)

```bash
poetry run python3 test/stress/stress_test.py \
  --dataset test/stress/data/org-mid.csv \
  --seed 200 \
  --records 500 \
  --output /tmp/baseline.json
```

### Cold-start test (no training)

```bash
poetry run python3 test/stress/stress_test.py \
  --dataset test/stress/data/org-small.csv \
  --no-train \
  --records 30 \
  --output /tmp/coldstart.json
```

## CLI Parameters

### Required

**`--dataset PATH`**
- Path to CSV file with stress test data
- Available: `test/stress/data/org-small.csv`, `test/stress/data/org-mid.csv`

### Optional

**`--config PATH`**
- Path to resolver config YAML (default: `config/resolver.yaml`)
- Determines blocking rules, thresholds, and Splink settings

**`--seed N`**
- Number of mentions to seed resolver with before stress loop (default: 200)
- Higher seed = warmer start, more stable latency
- Lower seed = cold-start behavior, variable latency

**`--records N`**
- Number of records to process in stress loop
- If omitted, processes all remaining records (after seed)

**`--time SECONDS`**
- Instead of fixed record count, run stress loop for N seconds
- Mutually exclusive with `--records`
- Useful for capacity planning: "How many records in 60 seconds?"

**`--output PATH`**
- JSON file to save results (default: `/tmp/stress_result.json`)

**`--name STR`**
- Experiment name (default: dataset basename, e.g., `org-small`)

**`--no-train`**
- Skip training; use cold-start parameters only (forces `--seed 0`)
- Tests resolver behavior with only Splink cold-start probabilities
- No EM training occurs; model uses hard-coded m/u values from config
- Useful for measuring pure latency baseline without training overhead

## Understanding Results

### Summary Output

```
======================================================================
Experiment: org-small
======================================================================
Dataset: test/stress/data/org-small.csv
Mentions: 100 total, 50 stressed
Seeding: 20 mentions

Resolved clusters: 25
Latency (ms):
  Mean:     145.32
  Median:   143.87
  Std:       12.45
  Min:      121.03
  P95:      168.19
  P99:      171.02
  Max:      175.45

Memory: 1.2 MB (peak)
Total time: 7.3 sec
======================================================================
```

### Key Metrics

**Resolved clusters**
- Number of distinct clusters created by the resolver during stress test
- Indicates clustering behavior and diversity of matches

**Latency (ms)**
- **Mean**: Average per-request time (typical case)
- **Median**: 50th percentile (robust to outliers)
- **Std**: Standard deviation (variability)
- **P95, P99**: 95th and 99th percentile (tail behavior)
- **Min, Max**: Range (watch for outliers suggesting GC or I/O stalls)

**Memory**
- Peak memory used during stress loop (MB)
- In-memory DuckDB + Splink DataFrame size
- Should remain stable; growth suggests memory leak

**Total time**
- Wall-clock seconds for stress loop
- Includes I/O, GC, all overhead
- Throughput = records / time

### JSON Schema

The JSON output has this structure:

```json
{
  "name": "experiment_name",
  "dataset_path": "test/stress/data/org-small.csv",
  "n_mentions": 100,
  "n_records_stressed": 50,
  "n_seed": 20,
  "n_clusters": 25,
  "mean_latency_ms": 145.32,
  "median_latency_ms": 143.87,
  "p95_latency_ms": 168.19,
  "p99_latency_ms": 171.02,
  "min_latency_ms": 121.03,
  "max_latency_ms": 175.45,
  "stdev_latency_ms": 12.45,
  "peak_memory_mb": 1.2,
  "total_time_sec": 7.3,
  "metrics": [
    {
      "record_idx": 20,
      "mention_id": "d00001234",
      "latency_ms": 145.67,
      "cluster_id": "cl000042",
      "n_candidates": 5,
      "score": 0.92
    },
    ...
  ]
}
```

## Stress Test Datasets

Organization datasets for entity resolution testing across varied scales.

### org-small.csv — Small Organization Dataset
- **Size**: 100 organizations (12 KB)
- **Use Case**: Quick testing and validation, smoke testing with realistic EU organization data
- **Expected latency**: ~15-25ms per request
- **Geography**: European organizations with country codes (ISO 3166-1 alpha-3)

### org-mid.csv — Mid-Size Organization Dataset
- **Size**: 5,497 organizations (456 KB)
- **Use Case**: Performance baseline testing, representative dataset for entity resolution evaluation
- **Expected latency**: ~100-200ms per request (scaling effects)
- **Geography**: European organizations with country codes (ISO 3166-1 alpha-3)

### CSV Schema

```
mention_id,legal_name,country_code,nuts_code,post_code,post_name,thoroughfare
d000001,"SNAGA, družba za ravnanje z odpadki in druge komunalne storitve, d.o.o.",SVN,SI,2000,Maribor,Nasipna ulica 64
d000002,Zavod Republike Slovenije za transfuzijsko medicino,SVN,SI,1000,Ljubljana,Šlajmerjeva ulica 6
d000003,Universitair Ziekenhuis Gent,BEL,BE234,9000,Gent,Corneel Heymanslaan 10
...
```

**Fields**:
- `mention_id`: Unique mention identifier (e.g., `d000001`)
- `legal_name`: Organization name (company/institution name, may contain special characters and formatting)
- `country_code`: ISO 3166-1 alpha-3 code (European countries)
- `nuts_code`: NUTS (Nomenclature of Territorial Units for Statistics) region code
- `post_code`: Postal code (may be empty)
- `post_name`: City or postal locality name
- `thoroughfare`: Street address or location (may be empty)

## Cold-Start Testing

### What is Cold-Start?

Cold-start means resolving mentions **without prior training**. The resolver uses only:
- Cold-start m/u probabilities from config (hardcoded)
- No EM training
- No seeding (resolver empty)

Useful for:
- Measuring "out-of-the-box" latency (no training overhead)
- Baseline performance before any warm data
- Testing Splink linker startup cost

### Running Cold-Start Tests

```bash
# Pure cold-start: no seeding, no training
poetry run python3 test/stress/stress_test.py \
  --dataset test/stress/data/org-small.csv \
  --no-train \
  --records 30
```

The `--no-train` flag:
- Forces `--seed 0` (no seeding)
- Skips EM training
- Uses cold-start parameters from config YAML

### Expected Behavior

Cold-start results typically show:
- **Higher latency** than trained (no optimized parameters)
- **More variable latency** (P99 >> Mean, indicating higher uncertainty)
- **Faster startup** (no EM training overhead)

Example output:
```
Experiment: org-small_coldstart
Seeding: 0 mentions
Latency (ms):
  Mean:     226.54
  Median:   225.72
  P95:      272.27
  P99:      272.27
```

vs. trained (for comparison):
```
Experiment: org-small
Seeding: 20 mentions
Latency (ms):
  Mean:     218.76
  Median:   219.37
  P95:      238.11
```

### Cold-Start vs Warm-Start Comparison

```bash
# Warm-start baseline
poetry run python3 test/stress/stress_test.py \
  --dataset test/stress/data/org-small.csv \
  --seed 50 \
  --records 50 \
  --output /tmp/warm.json

# Cold-start equivalent
poetry run python3 test/stress/stress_test.py \
  --dataset test/stress/data/org-small.csv \
  --no-train \
  --records 50 \
  --output /tmp/cold.json
```

Compare `mean_latency_ms` in both JSON files to measure training benefit.

## Exit Strategies

### Record-based (default)

Process a fixed number of records:

```bash
# Process exactly 100 records after seeding
python3 test/stress/stress_test.py \
  --dataset test/stress/data/org-mid.csv \
  --seed 200 \
  --records 100
```

**Pros**:
- Deterministic (same input = same output)
- Good for regression testing and comparisons
- Reproducible across runs

**Cons**:
- May not reflect real-world time constraints

### Time-based

Process records for a fixed duration:

```bash
# Run for 60 seconds, process as many records as possible
python3 test/stress/stress_test.py \
  --dataset test/stress/data/org-mid.csv \
  --seed 200 \
  --time 60
```

**Pros**:
- Reflects real-world SLA constraints
- Good for capacity planning
- Shows throughput under time pressure

**Cons**:
- Non-deterministic (latency affects record count)
- Harder to compare across runs

## Assumptions & Constraints

1. **In-memory DuckDB**: All data fits in RAM
   - Suitable for POC/testing (< 1GB)
   - Not for production (use file-backed DB or distributed)

2. **Single-threaded**: No parallelization
   - Conservative latency measurement (no contention)
   - Useful for baseline, not production throughput

3. **Cold Splink linker**: No pre-trained model
   - Uses cold-start parameters from config
   - EM training happens during seed phase
   - Latency may stabilize after first N records

4. **Single config**: All experiments use one resolver config
   - To test different configs, run separate experiments
   - Results not comparable if configs differ

## Troubleshooting

**"ModuleNotFoundError: No module named 'ere'"**
- Run with `poetry run`: `poetry run python3 test/stress/stress_test.py`

**"No such file: test/stress/data/org-small.csv"**
- Check dataset path is correct
- Datasets must be in `/home/greg/PROJECTS/ERS/ere-basic/test/stress/data/`

**"Your model is not yet fully trained" warnings**
- Normal with small seed or sparse data
- Splink uses cold-start parameters for untrained levels
- More seed data improves training (try `--seed 100`)

**Latency spikes (P99 >> Mean)**
- May indicate GC pauses or I/O stalls
- Try on quieter system or increase seed size for stability
- Use `--verbose` to see detailed timing

**Memory grows over time**
- Check `peak_memory_mb` in JSON output
- If > 1GB with 5k records, investigate for leaks
- Consider smaller seed or fewer records
