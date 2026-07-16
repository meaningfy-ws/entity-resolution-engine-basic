#!/usr/bin/env python3
"""
Unified Stress Test for Entity Resolver

Standalone stress test runner (not pytest-managed) for performance testing
of the entity resolver with configurable datasets and parameters.

Usage:
    python test/stress/stress_test.py \
        --dataset test/stress/data/org-small.csv \
        --output /tmp/stress_result.json

    python test/stress/stress_test.py \
        --dataset test/stress/data/org-mid.csv \
        --seed 200 \
        --records 500 \
        --config src/config/resolver.yaml \
        --output /tmp/stress_mid.json
"""

import argparse
import csv
import json
import logging
import sys
import time
import traceback
import tracemalloc
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, stdev

import duckdb
import yaml

# Import resolver components
from ere.adapters.duckdb_repositories import (
    DuckDBClusterRepository,
    DuckDBMentionRepository,
    DuckDBSimilarityRepository,
)
from ere.adapters.duckdb_schema import init_schema
from ere.adapters.splink_linker_impl import SpLinkSimilarityLinker
from ere.models.resolver import Mention
from ere.services.entity_resolution_service import EntityResolver
from ere.services.resolver_config import ResolverConfig

logger = logging.getLogger(__name__)


# =============================================================================
# Data Models
# =============================================================================


@dataclass
class RequestMetric:
    """Per-request latency and context."""

    record_idx: int
    mention_id: str
    latency_ms: float
    cluster_id: str
    n_candidates: int
    score: float


@dataclass
class ExperimentResult:
    """Aggregated stress test results."""

    name: str
    dataset_path: str
    n_mentions: int
    n_records_stressed: int
    n_seed: int
    n_clusters: int
    mean_latency_ms: float
    median_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    min_latency_ms: float
    max_latency_ms: float
    stdev_latency_ms: float
    peak_memory_mb: float
    total_time_sec: float
    metrics: list[RequestMetric]


# =============================================================================
# Core Functions
# =============================================================================


def load_mentions(csv_path: str) -> list[Mention]:
    """
    Load mentions from CSV file.

    Expected columns: mention_id, legal_name, country_code (and other optional attributes).
    """
    mentions = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Use flat dict form; Mention validator handles conversion
            mentions.append(Mention(**row))
    return mentions


def create_resolver(
    entity_fields: list[str], config_path: str
) -> tuple[EntityResolver, dict, duckdb.DuckDBPyConnection]:
    """
    Create fresh EntityResolver instance with in-memory DuckDB.

    Returns:
        (resolver, raw_config_dict, connection)
    """
    # Load config
    with open(config_path) as f:
        raw_config = yaml.safe_load(f)

    # Create in-memory DB and init schema
    con = duckdb.connect(":memory:")
    init_schema(con, entity_fields)

    # Wire up repositories and linker
    mention_repo = DuckDBMentionRepository(con, entity_fields)
    similarity_repo = DuckDBSimilarityRepository(con)
    cluster_repo = DuckDBClusterRepository(con)
    linker = SpLinkSimilarityLinker(entity_fields, raw_config)

    # Create resolver
    resolver_config = ResolverConfig.from_dict(raw_config)
    resolver = EntityResolver(
        mention_repo, similarity_repo, cluster_repo, linker, resolver_config
    )

    return resolver, raw_config, con


def seed_and_train(
    resolver: EntityResolver,
    mentions: list[Mention],
    n_seed: int,
    skip_train: bool = False,
):
    """
    Seed resolver with first n_seed mentions and optionally trigger training.

    Args:
        resolver: EntityResolver instance
        mentions: List of mentions to seed with
        n_seed: Number of mentions to seed (0 = cold-start, no seeding)
        skip_train: If True, skip training (pure cold-start with parameters only)

    This warms up the resolver and establishes initial clusters for the
    stress test phase. With skip_train=True, tests cold-start performance
    using only the Splink cold-start parameters (no EM training).
    """
    if n_seed > 0:
        logger.info(f"Seeding with {n_seed} mentions...")
        for i in range(min(n_seed, len(mentions))):
            mention = mentions[i]
            try:
                resolver.resolve(mention)
            except Exception as e:
                logger.warning(f"Seed error at record {i}: {e}")
    else:
        logger.info("Cold-start: skipping seed phase")

    if not skip_train:
        logger.info("Training linker...")
        resolver.train()
        logger.info("Seeding and training complete")
    else:
        logger.info("Cold-start: skipping training (using cold-start parameters only)")


def stress_loop(
    resolver: EntityResolver,
    mentions: list[Mention],
    start_idx: int,
    exit_strategy: str,
    exit_value: float | int,
) -> list[RequestMetric]:
    """
    Run stress test loop with latency tracking.

    Args:
        resolver: EntityResolver instance
        mentions: List of mentions to process
        start_idx: Starting index in mentions list
        exit_strategy: "records" (process N records) or "time" (run for N seconds)
        exit_value: Value for exit strategy (record count or seconds)

    Returns:
        List of RequestMetric for each resolved mention
    """
    metrics = []
    start_time = time.perf_counter()

    if exit_strategy == "records":
        n_stress = int(exit_value)
        end_idx = min(start_idx + n_stress, len(mentions))
    elif exit_strategy == "time":
        end_idx = len(mentions)  # Process all, stop by time
        timeout_sec = float(exit_value)
    else:
        raise ValueError(f"Unknown exit_strategy: {exit_strategy}")

    logger.info(
        f"Starting stress loop: {exit_strategy}={exit_value}, "
        f"processing mentions[{start_idx}:{end_idx}]"
    )

    for i in range(start_idx, end_idx):
        mention = mentions[i]

        # Check time-based exit
        if exit_strategy == "time":
            elapsed = time.perf_counter() - start_time
            if elapsed > timeout_sec:
                logger.info(f"Time limit reached: {elapsed:.1f}s")
                break

        # Time the resolve call
        t0 = time.perf_counter()
        try:
            result = resolver.resolve(mention)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            # Extract metrics
            metric = RequestMetric(
                record_idx=i,
                mention_id=mention.id.value,
                latency_ms=elapsed_ms,
                cluster_id=result.top.cluster_id.value if result.top else "NONE",
                n_candidates=len(result.candidates),
                score=result.top.score if result.top else 0.0,
            )
            metrics.append(metric)

            if i % 50 == 0:
                logger.debug(
                    f"Record {i}: {elapsed_ms:.1f}ms, "
                    f"cluster={metric.cluster_id}, "
                    f"candidates={metric.n_candidates}"
                )

        except Exception as e:
            logger.error(f"Stress loop error at record {i}: {e}")
            logger.debug(traceback.format_exc())

    total_time = time.perf_counter() - start_time
    logger.info(
        f"Stress loop complete: {len(metrics)} records in {total_time:.1f}s "
        f"({len(metrics) / total_time:.1f} rec/s)"
    )

    return metrics


def run_experiment(
    name: str,
    dataset_path: str,
    config_path: str,
    seed_count: int = 200,
    exit_strategy: str = "records",
    exit_value: int | float = 200,
    skip_train: bool = False,
) -> ExperimentResult:
    """
    Run full stress test experiment.

    Args:
        name: Experiment name
        dataset_path: Path to CSV dataset
        config_path: Path to resolver config YAML
        seed_count: Number of mentions to seed with (0 = cold-start)
        exit_strategy: "records" or "time"
        exit_value: Record count or seconds (depending on strategy)
        skip_train: If True, skip training (cold-start with parameters only)

    Returns:
        ExperimentResult with full metrics
    """
    logger.info(f"=== Experiment: {name} ===")

    # Load data
    logger.info(f"Loading {dataset_path}...")
    mentions = load_mentions(dataset_path)
    logger.info(f"Loaded {len(mentions)} mentions")

    # Determine entity fields from config
    with open(config_path) as f:
        raw_config = yaml.safe_load(f)
    entity_fields = [
        comp["field"] for comp in raw_config.get("splink", {}).get("comparisons", [])
    ]

    # Create resolver
    resolver, _, con = create_resolver(entity_fields, config_path)

    try:
        # Seed and train (or cold-start)
        seed_and_train(resolver, mentions, seed_count, skip_train=skip_train)

        # Run stress loop
        tracemalloc.start()
        start_idx = seed_count
        metrics = stress_loop(resolver, mentions, start_idx, exit_strategy, exit_value)
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Aggregate metrics
        if not metrics:
            logger.error("No metrics collected!")
            return None

        latencies = [m.latency_ms for m in metrics]
        latencies_sorted = sorted(latencies)

        # Resolved cluster count
        resolved_clusters = Counter(m.cluster_id for m in metrics)

        result = ExperimentResult(
            name=name,
            dataset_path=str(dataset_path),
            n_mentions=len(mentions),
            n_records_stressed=len(metrics),
            n_seed=seed_count,
            n_clusters=len(resolved_clusters),
            mean_latency_ms=mean(latencies),
            median_latency_ms=latencies_sorted[len(latencies_sorted) // 2],
            p95_latency_ms=latencies_sorted[int(0.95 * len(latencies_sorted))],
            p99_latency_ms=latencies_sorted[int(0.99 * len(latencies_sorted))],
            min_latency_ms=min(latencies),
            max_latency_ms=max(latencies),
            stdev_latency_ms=stdev(latencies) if len(latencies) > 1 else 0.0,
            peak_memory_mb=peak / (1024 * 1024),
            total_time_sec=sum(m.latency_ms for m in metrics) / 1000,
            metrics=metrics,
        )

        return result
    finally:
        # Ensure DuckDB connection is properly closed
        try:
            con.close()
        except Exception as e:
            logger.warning(f"Error closing DuckDB connection: {e}")


# =============================================================================
# Reporting
# =============================================================================


def print_summary(result: ExperimentResult):
    """Print human-readable summary to stdout."""
    print(f"\n{'=' * 70}")
    print(f"Experiment: {result.name}")
    print(f"{'=' * 70}")
    print(f"Dataset: {result.dataset_path}")
    print(f"Mentions: {result.n_mentions} total, {result.n_records_stressed} stressed")
    print(f"Seeding: {result.n_seed} mentions")
    print()
    print(f"Resolved clusters: {result.n_clusters}")
    print()
    print("Latency (ms):")
    print(f"  Mean:   {result.mean_latency_ms:8.2f}")
    print(f"  Median: {result.median_latency_ms:8.2f}")
    print(f"  Std:    {result.stdev_latency_ms:8.2f}")
    print(f"  Min:    {result.min_latency_ms:8.2f}")
    print(f"  P95:    {result.p95_latency_ms:8.2f}")
    print(f"  P99:    {result.p99_latency_ms:8.2f}")
    print(f"  Max:    {result.max_latency_ms:8.2f}")
    print()
    print(f"Memory: {result.peak_memory_mb:.1f} MB (peak)")
    print(f"Total time: {result.total_time_sec:.1f} sec")
    print(f"{'=' * 70}\n")


def save_result_json(result: ExperimentResult, output_path: str):
    """Save result to JSON file."""
    # Convert metrics to dicts for JSON serialization
    result_dict = asdict(result)
    result_dict["metrics"] = [asdict(m) for m in result.metrics]

    with open(output_path, "w") as f:
        json.dump(result_dict, f, indent=2)

    logger.info(f"Saved result to {output_path}")


# =============================================================================
# CLI
# =============================================================================


def main():
    """Parse CLI arguments and run experiment."""
    parser = argparse.ArgumentParser(
        description="Unified stress test for entity resolver"
    )
    parser.add_argument(
        "--dataset",
        required=True,
        help="Path to CSV dataset (org-small.csv, org-mid.csv, etc.)",
    )
    parser.add_argument(
        "--config",
        default="src/config/resolver.yaml",
        help="Path to resolver config YAML",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=200,
        help="Number of mentions to seed before stress loop",
    )
    parser.add_argument(
        "--records",
        type=int,
        default=None,
        help="Number of records to process (default: all remaining)",
    )
    parser.add_argument(
        "--time",
        type=float,
        default=None,
        help="Run for N seconds instead of fixed record count",
    )
    parser.add_argument(
        "--output",
        default="/tmp/stress_result.json",
        help="Output JSON file path",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Experiment name (default: dataset basename)",
    )
    parser.add_argument(
        "--no-train",
        action="store_true",
        help="Skip training; use cold-start parameters only (implies --seed 0)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Cold-start mode implies seed=0 and skip_train=True
    if args.no_train:
        args.seed = 0
        skip_train = True
    else:
        skip_train = False

    # Determine exit strategy
    if args.time:
        exit_strategy = "time"
        exit_value = args.time
    elif args.records:
        exit_strategy = "records"
        exit_value = args.records
    else:
        # Default: process all remaining records
        exit_strategy = "records"
        exit_value = 999999  # Effectively unlimited

    # Experiment name
    exp_name = args.name or Path(args.dataset).stem
    if args.no_train:
        exp_name += "_coldstart"

    # Run experiment
    try:
        result = run_experiment(
            name=exp_name,
            dataset_path=args.dataset,
            config_path=args.config,
            seed_count=args.seed,
            exit_strategy=exit_strategy,
            exit_value=exit_value,
            skip_train=skip_train,
        )

        if result:
            print_summary(result)
            save_result_json(result, args.output)
            logger.info(f"✅ Experiment complete")
            # Flush output streams before exiting
            sys.stdout.flush()
            sys.stderr.flush()
            return 0
        else:
            logger.error("❌ Experiment failed")
            return 1

    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        logger.debug(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
