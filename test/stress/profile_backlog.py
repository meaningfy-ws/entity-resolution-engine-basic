"""Profile draining a backlog through the worker in bites (memory-improvement Part 2, task 13.2).

Queues N requests in an in-memory stand-in for Redis, then runs the production worker loop (`process_bite`) with the
production factory (on-disk DuckDB, name-similarity blocking, one transaction per bite) until the queue is empty.
Mentions come from `org-mid.csv`; later cycles prefix the name with a random word so copies behave like distinct
organisations. RDF parsing is replaced by a mapper reading the organisation from the request content (parsing was
measured separately: 4 ms for an average 20 KB mention). Prints, per reporting window: mentions processed, mentions/s,
mean and p95 bite duration, mean bite size, RSS.

    cd src && poetry run python ../test/stress/profile_backlog.py --mentions 25000
"""

import argparse
import csv
import json
import logging
import os
import random
import statistics
import string
import tempfile
import time
from pathlib import Path

from erspec.models.core import EntityMention, EntityMentionIdentifier
from erspec.models.ere import EntityMentionResolutionRequest
from linkml_runtime.dumpers import JSONDumper

from ere.entrypoints.bootstrap import (
    DuckDBEnvVar,
    build_entity_resolution_service,
    build_entity_resolver,
)
from ere.entrypoints.queue_worker import RedisQueueWorker
from ere.models.ports.rdf_mapper import RDFMapper
from ere.models.resolver import Mention, MentionId
from ere.services.resolver_config import BatchSettings

REPO_ROOT = Path(__file__).parents[2]
CORPUS = REPO_ROOT / "test" / "stress" / "data" / "org-mid.csv"
CONFIG = REPO_ROOT / "src" / "config" / "resolver.yaml"
REQUEST_QUEUE = "ere_requests"
RESPONSE_QUEUE = "ere_responses"
PREFIX_LETTERS = 8
P95 = 0.95
PAGE_SIZE_BYTES = os.sysconf("SC_PAGE_SIZE")
BYTES_PER_MB = 1024 * 1024


class InMemoryQueues:
    """The Redis list commands the worker uses, backed by Python lists (oldest request at the end)."""

    def __init__(self):
        self.lists: dict[str, list[bytes]] = {REQUEST_QUEUE: [], RESPONSE_QUEUE: []}

    def brpop(self, name, timeout=0):  # pylint: disable=unused-argument
        queue = self.lists[name]
        return (name.encode(), queue.pop()) if queue else None

    def lmpop(self, num_keys, *names, direction, count=1):  # pylint: disable=unused-argument
        queue = self.lists[names[0]]
        taken = [queue.pop() for _ in range(min(count, len(queue)))]
        return [names[0].encode(), taken] if taken else None

    def rpush(self, name, *values):
        self.lists[name].extend(values)

    def pipeline(self, transaction=True):  # pylint: disable=unused-argument
        return _Pipeline(self)


class _Pipeline:
    def __init__(self, queues: InMemoryQueues):
        self._queues = queues
        self._count = 0

    def lpush(self, name, value):  # pylint: disable=unused-argument
        self._count += 1  # responses are counted, not kept

    def execute(self):
        return [self._count]


class CsvRowMapper(RDFMapper):
    """Reads the organisation fields from the request content (JSON) instead of parsing RDF."""

    def map_entity_mention_to_domain(self, entity_mention: EntityMention) -> Mention:
        return Mention(
            id=MentionId(value=entity_mention.identifiedBy.request_id),
            attributes=json.loads(entity_mention.content),
        )


def queue_requests(queues: InMemoryQueues, count: int, fields: list[str]) -> None:
    with CORPUS.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    words, dumper = random.Random(42), JSONDumper()
    for index in range(count):
        cycle, row = divmod(index, len(rows))
        record = rows[row]
        attributes = {field: record.get(field) or None for field in fields}
        if cycle:
            attributes["legal_name"] = (
                f"{''.join(words.choices(string.ascii_lowercase, k=PREFIX_LETTERS))} {record['legal_name']}"
            )
        request_id = f"{record['mention_id']}-{cycle}"
        request = EntityMentionResolutionRequest(
            entity_mention=EntityMention(
                identifiedBy=EntityMentionIdentifier(
                    request_id=request_id,
                    source_id="profile",
                    entity_type="ORGANISATION",
                ),
                content_type="application/json",
                content=json.dumps(attributes),
            ),
            ere_request_id=request_id,
        )
        queues.lists[REQUEST_QUEUE].insert(0, dumper.dumps(request).encode("utf-8"))


def rss_mb() -> float:
    with open("/proc/self/statm", encoding="utf-8") as statm:
        return round(int(statm.read().split()[1]) * PAGE_SIZE_BYTES / BYTES_PER_MB, 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mentions", type=int, default=25000)
    parser.add_argument("--window", type=int, default=2500)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    logging.getLogger("ere").setLevel(logging.WARNING)

    os.environ[DuckDBEnvVar.PATH] = str(
        Path(tempfile.mkdtemp(prefix="ere-backlog-")) / "app.duckdb"
    )
    resolver = build_entity_resolver(resolver_config_path=CONFIG)
    queues = InMemoryQueues()
    queue_requests(queues, args.mentions, resolver._config.entity_fields)  # pylint: disable=protected-access
    worker = RedisQueueWorker(
        redis_client=queues,
        entity_resolution_service=build_entity_resolution_service(
            resolver, CsvRowMapper()
        ),
        request_queue=REQUEST_QUEUE,
        response_queue=RESPONSE_QUEUE,
        batch_settings=BatchSettings(),
    )

    print(
        f"{'processed':>9} {'mentions/s':>10} {'bite_ms':>8} {'p95_bite':>8} {'bite_size':>9} {'rss_mb':>7}"
    )
    samples, processed, next_report = [], 0, args.window
    bite_ms, bite_sizes, window_started = [], [], time.perf_counter()
    while queues.lists[REQUEST_QUEUE]:
        started = time.perf_counter()
        taken = worker.process_bite()
        bite_ms.append((time.perf_counter() - started) * 1000)
        bite_sizes.append(taken)
        processed += taken
        if processed >= next_report or not queues.lists[REQUEST_QUEUE]:
            seconds = time.perf_counter() - window_started
            ordered = sorted(bite_ms)
            sample = {
                "processed": processed,
                "mentions_per_s": round(sum(bite_sizes) / seconds, 1),
                "mean_bite_ms": round(statistics.fmean(bite_ms), 1),
                "p95_bite_ms": round(ordered[max(int(len(ordered) * P95) - 1, 0)], 1),
                "mean_bite_size": round(statistics.fmean(bite_sizes), 1),
                "rss_mb": rss_mb(),
            }
            samples.append(sample)
            print(
                f"{sample['processed']:>9} {sample['mentions_per_s']:>10} {sample['mean_bite_ms']:>8} "
                f"{sample['p95_bite_ms']:>8} {sample['mean_bite_size']:>9} {sample['rss_mb']:>7}",
                flush=True,
            )
            next_report += args.window
            bite_ms, bite_sizes, window_started = [], [], time.perf_counter()
    if args.output:
        args.output.write_text(json.dumps(samples, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
