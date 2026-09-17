"""Profile resolution memory and latency as stored mentions grow (memory-improvement, DEC-9).

Resolves mentions through the production factory (on-disk DuckDB, shared Splink connection) and
prints, per window: stored mentions, process RSS, DuckDB catalog size, mean / p95 ms per mention, links per mention
and mean ms per stage (score = Splink find_matches, links = similarity insert, clusters = cluster lookup + insert,
mention = mention insert). The corpus cycles `org-mid.csv`; later cycles prefix the legal name with a random word
per row, so copies behave like distinct organisations instead of near-duplicates.

    cd src && poetry run python ../test/stress/profile_resolution.py --mentions 25000 --window 2500
"""

import argparse
import csv
import json
import os
import random
import statistics
import string
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from ere.entrypoints.bootstrap import DuckDBEnvVar, build_entity_resolver
from ere.models.resolver import Mention, MentionId

REPO_ROOT = Path(__file__).parents[2]
DEFAULT_CORPUS = REPO_ROOT / "test" / "stress" / "data" / "org-mid.csv"
DEFAULT_CONFIG = REPO_ROOT / "src" / "config" / "resolver.yaml"
PAGE_SIZE_BYTES = os.sysconf("SC_PAGE_SIZE")
BYTES_PER_MB = 1024 * 1024
P95 = 0.95
PREFIX_LETTERS = 8
STAGES = {
    "score": ("_linker", "find_matches_batch"),
    "links": ("_similarity_repo", "save_table"),
    "clusters_lookup": ("_cluster_repo", "clusters_for"),
    "clusters_save": ("_cluster_repo", "save"),
    "mention": ("_mention_repo", "save"),
}


def rss_mb() -> float:
    """Live resident set size (not the high-water mark)."""
    with open("/proc/self/statm", encoding="utf-8") as statm:
        resident_pages = int(statm.read().split()[1])
    return round(resident_pages * PAGE_SIZE_BYTES / BYTES_PER_MB, 1)


def synthetic_mentions(corpus: Path, count: int, entity_fields: list[str]):
    with corpus.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    words = random.Random(42)
    for index in range(count):
        cycle, row = divmod(index, len(rows))
        record = rows[row]
        attributes = {field: record.get(field) or None for field in entity_fields}
        if cycle:
            prefix = "".join(words.choices(string.ascii_lowercase, k=PREFIX_LETTERS))
            attributes["legal_name"] = f"{prefix} {record['legal_name']}"
        yield Mention(
            id=MentionId(value=f"{record['mention_id']}-{cycle}"), attributes=attributes
        )


def catalog_size(resolver) -> int:
    con = resolver._mention_repo._con  # pylint: disable=protected-access  # profiling reads the shared connection
    tables = con.execute("SELECT count(*) FROM duckdb_tables()").fetchone()[0]
    views = con.execute(
        "SELECT count(*) FROM duckdb_views() WHERE NOT internal"
    ).fetchone()[0]
    return tables + views


def instrument(
    resolver, stage_ms: dict[str, list[float]], link_counts: list[int]
) -> None:
    """Wrap the resolver's collaborators to time each stage and count links per mention."""
    for stage, (attribute, method_name) in STAGES.items():
        target = getattr(resolver, attribute)
        original = getattr(target, method_name)

        def timed(*args, _original=original, _stage=stage, **kwargs):
            started = time.perf_counter()
            result = _original(*args, **kwargs)
            stage_ms[_stage].append((time.perf_counter() - started) * 1000)
            if _stage == "score":
                link_counts.append(
                    len(result)
                )  # LinkTable rows for the mention (bite of one)
            return result

        setattr(target, method_name, timed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mentions", type=int, default=25000)
    parser.add_argument("--window", type=int, default=2500)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="keeps the database; default: temp dir",
    )
    parser.add_argument(
        "--output", type=Path, default=None, help="write samples as JSON"
    )
    args = parser.parse_args()

    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="ere-profile-"))
    os.environ[DuckDBEnvVar.PATH] = str(workdir / "app.duckdb")
    resolver = build_entity_resolver(resolver_config_path=args.config)
    entity_fields = resolver._config.entity_fields  # pylint: disable=protected-access

    stage_ms: dict[str, list[float]] = defaultdict(list)
    link_counts: list[int] = []
    instrument(resolver, stage_ms, link_counts)
    samples, window_ms = [], []
    print(
        f"{'n':>8} {'rss_mb':>8} {'catalog':>8} {'mean_ms':>8} {'p95_ms':>8} {'links':>6} "
        + " ".join(f"{s:>15}" for s in STAGES)
    )
    for position, mention in enumerate(
        synthetic_mentions(args.corpus, args.mentions, entity_fields), start=1
    ):
        started = time.perf_counter()
        resolver.resolve(mention)
        window_ms.append((time.perf_counter() - started) * 1000)
        if position % args.window == 0 or position == args.mentions:
            ordered = sorted(window_ms)
            sample = {
                "n": position,
                "rss_mb": rss_mb(),
                "catalog": catalog_size(resolver),
                "mean_ms": round(statistics.fmean(window_ms), 1),
                "p95_ms": round(ordered[int(len(ordered) * P95) - 1], 1),
                "links_per_mention": round(statistics.fmean(link_counts), 1)
                if link_counts
                else 0.0,
                **{
                    f"{stage}_ms": round(sum(stage_ms[stage]) / len(window_ms), 2)
                    for stage in STAGES
                },
            }
            samples.append(sample)
            stages = " ".join(f"{sample[f'{stage}_ms']:>15}" for stage in STAGES)
            print(
                f"{sample['n']:>8} {sample['rss_mb']:>8} {sample['catalog']:>8} {sample['mean_ms']:>8} "
                f"{sample['p95_ms']:>8} {sample['links_per_mention']:>6} {stages}",
                flush=True,
            )
            window_ms = []
            link_counts.clear()
            stage_ms.clear()

    if args.output:
        args.output.write_text(json.dumps(samples, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
