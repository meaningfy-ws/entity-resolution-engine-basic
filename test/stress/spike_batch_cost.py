"""Spike S1 (memory-improvement Part 2): does one Splink call with many records cost less per record?

Fills a search space by bulk insert (no scoring), then times `find_matches_to_new_records` for batches of new records
through the production linker (shared DuckDB, name-similarity blocking), against scoring the same records one call
each. Prints ms per record per batch size.

    cd src && poetry run python ../test/stress/spike_batch_cost.py --stored 25000
"""

import argparse
import csv
import random
import statistics
import string
import tempfile
import time
from pathlib import Path

import duckdb
import yaml

from ere.adapters.duckdb_repositories import DuckDBMentionRepository
from ere.adapters.duckdb_schema import init_schema
from ere.adapters.splink_linker_impl import SpLinkSimilarityLinker
from ere.models.resolver import Mention, MentionId

REPO_ROOT = Path(__file__).parents[2]
CORPUS = REPO_ROOT / "test" / "stress" / "data" / "org-mid.csv"
CONFIG = REPO_ROOT / "src" / "config" / "resolver.yaml"
BATCH_SIZES = (1, 10, 50, 100, 500)
REPEATS = 3
PREFIX_LETTERS = 8


def corpus_mentions(count: int, fields: list[str], seed: int) -> list[Mention]:
    with CORPUS.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    words = random.Random(seed)
    mentions = []
    for index in range(count):
        cycle, row = divmod(index, len(rows))
        record = rows[row]
        attributes = {field: record.get(field) or None for field in fields}
        if cycle or seed:
            prefix = "".join(words.choices(string.ascii_lowercase, k=PREFIX_LETTERS))
            attributes["legal_name"] = f"{prefix} {record['legal_name']}"
        mentions.append(
            Mention(
                id=MentionId(value=f"s{seed}-{record['mention_id']}-{cycle}"),
                attributes=attributes,
            )
        )
    return mentions


def score_batch(linker: SpLinkSimilarityLinker, mentions: list[Mention]) -> int:
    """One Splink call for all mentions, through the production batch path."""
    return len(linker.find_matches_batch(mentions))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stored", type=int, default=25000)
    args = parser.parse_args()

    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    fields = raw["entity_fields"]
    con = duckdb.connect(str(Path(tempfile.mkdtemp(prefix="ere-s1-")) / "app.duckdb"))
    init_schema(con, fields)
    repo = DuckDBMentionRepository(con, fields)
    con.execute("BEGIN")
    for mention in corpus_mentions(args.stored, fields, seed=0):
        repo.save(mention)
    con.execute("COMMIT")
    linker = SpLinkSimilarityLinker(fields, raw, connection=con)
    new_mentions = corpus_mentions(max(BATCH_SIZES), fields, seed=1)
    score_batch(linker, new_mentions[:1])  # warm-up

    print(f"stored={args.stored}")
    print(
        f"{'batch':>6} {'batched ms/record':>18} {'one-by-one ms/record':>21} {'speed-up':>9} {'links/record':>13}"
    )
    for size in BATCH_SIZES:
        batch = new_mentions[:size]
        batched, single, links = [], [], 0
        for _ in range(REPEATS):
            started = time.perf_counter()
            links = score_batch(linker, batch)
            batched.append((time.perf_counter() - started) * 1000 / size)
            started = time.perf_counter()
            for mention in batch[: min(size, 50)]:
                score_batch(linker, [mention])
            single.append((time.perf_counter() - started) * 1000 / min(size, 50))
        batched_ms, single_ms = statistics.median(batched), statistics.median(single)
        print(
            f"{size:>6} {batched_ms:>18.2f} {single_ms:>21.2f} {single_ms / batched_ms:>8.1f}× {links / size:>13.1f}"
        )


if __name__ == "__main__":
    main()
