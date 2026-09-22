"""Spike S5 (memory-improvement Part 2): blocking recall and cost on a real corpus.

Resolves the corpus in file order through the production resolver (shared on-disk DuckDB, Splink view search space)
with a given blocking variant and match-weight threshold. Training runs synchronously at the configured threshold so
variants are comparable. Writes cluster assignments and counters to JSON. The corpus has no ground truth, so
`--compare` measures each run against reference pair sets computed in SQL: `exact` (same country, identical
normalised name) and `variant` (near-identical name, same postcode) — how many the blocking admits and how many end
in one cluster — plus merges of `unrelated` names and the number of candidate pairs each rule set admits.

    cd src
    poetry run python ../test/stress/spike_blocking.py --variant country --out /tmp/s5/country.json
    poetry run python ../test/stress/spike_blocking.py --variant A --out /tmp/s5/A.json
    poetry run python ../test/stress/spike_blocking.py --compare /tmp/s5/*.json
"""

import argparse
import csv
import json
import tempfile
import time
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import duckdb
import yaml

from ere.adapters.duckdb_repositories import (
    DuckDBClusterRepository,
    DuckDBMentionRepository,
    DuckDBSimilarityRepository,
)
from ere.adapters.duckdb_schema import init_schema
from ere.adapters.splink_linker_impl import SpLinkSimilarityLinker
from ere.models.resolver import Mention, MentionId
from ere.services.entity_resolution_service import EntityResolver
from ere.services.resolver_config import ResolverConfig

REPO_ROOT = Path(__file__).parents[2]
DEFAULT_CORPUS = REPO_ROOT / "test" / "stress" / "data" / "org-mid.csv"
DEFAULT_CONFIG = REPO_ROOT / "src" / "config" / "resolver.yaml"
COUNTRY_FIELD = "country_code"

NORMALISED = "REGEXP_REPLACE(STRIP_ACCENTS(LOWER({side}legal_name)), '[^\\p{{L}}\\p{{N}}]', '', 'g')"
NAME_PREFIX = "NULLIF(LEFT(REGEXP_REPLACE(STRIP_ACCENTS(LOWER({side}legal_name)), '[^\\p{{L}}\\p{{N}}]', '', 'g'), 4), '')"


def _prefix(side: str) -> str:
    return NAME_PREFIX.format(side=side)


# Each variant is a list of SQL blocking rules over l./r. (OR-ed by Splink). NULL never equals NULL.
VARIANTS = {
    "country": ["l.country_code = r.country_code"],
    "A": [
        f"l.country_code = r.country_code AND {_prefix('l.')} = {_prefix('r.')}",
        "l.country_code = r.country_code AND l.post_code = r.post_code",
        "l.country_code = r.country_code AND l.nuts_code = r.nuts_code AND l.post_name = r.post_name",
    ],
    "A_name_post": [
        f"l.country_code = r.country_code AND {_prefix('l.')} = {_prefix('r.')}",
        "l.country_code = r.country_code AND l.post_code = r.post_code",
    ],
    "B": [
        "l.country_code = r.country_code AND jaro_winkler_similarity(l.legal_name, r.legal_name) >= 0.8",
    ],
    "B_norm": [
        "l.country_code = r.country_code AND jaro_winkler_similarity("
        + NORMALISED.format(side="l.")
        + ", "
        + NORMALISED.format(side="r.")
        + ") >= 0.8",
    ],
}


def load_mentions(corpus: Path, entity_fields: list[str]) -> list[Mention]:
    with corpus.open(encoding="utf-8") as f:
        return [
            Mention(
                id=MentionId(value=row["mention_id"]),
                attributes={field: row.get(field) or None for field in entity_fields},
            )
            for row in csv.DictReader(f)
        ]


def run(  # pylint: disable=too-many-arguments,too-many-locals  # spike CLI: one knob per argument
    variant: str,
    match_weight_threshold: float,
    corpus: Path,
    config_path: Path,
    out: Path,
    threshold: float | None = None,
    drop_country_comparison: bool = False,
) -> None:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["match_weight_threshold"] = match_weight_threshold
    if threshold is not None:
        raw["threshold"] = threshold
    if drop_country_comparison:
        raw["splink"]["comparisons"] = [
            c for c in raw["splink"]["comparisons"] if c["field"] != COUNTRY_FIELD
        ]
        raw["splink"].get("cold_start", {}).get("comparisons", {}).pop(
            COUNTRY_FIELD, None
        )
    train_at = raw.get("auto_train_threshold", 200)
    raw["auto_train_threshold"] = 0  # trained synchronously below, for comparable runs
    config = ResolverConfig.from_dict(raw)
    fields = config.entity_fields

    workdir = Path(tempfile.mkdtemp(prefix=f"ere-s5-{variant}-"))
    con = duckdb.connect(str(workdir / "app.duckdb"))
    init_schema(con, fields)
    linker = SpLinkSimilarityLinker(
        fields, raw, connection=con, training_sample_size=train_at
    )
    rules = VARIANTS[variant]
    linker._get_blocking_rules = lambda: list(rules)  # pylint: disable=protected-access  # spike: override rules only
    resolver = EntityResolver(
        DuckDBMentionRepository(con, fields),
        DuckDBSimilarityRepository(con),
        DuckDBClusterRepository(con),
        linker,
        config,
    )

    mentions = load_mentions(corpus, fields)
    joins, top_candidates, per_mention_ms = 0, {}, []
    started = time.perf_counter()
    for position, mention in enumerate(mentions, start=1):
        t0 = time.perf_counter()
        result = resolver.resolve(mention)
        per_mention_ms.append((time.perf_counter() - t0) * 1000)
        top_candidates[mention.id.value] = [
            (c.cluster_id.value, round(c.score, 6))
            for c in result.candidates
            if c.score >= config.threshold
        ]
        if position == train_at:
            linker.train()
    elapsed = time.perf_counter() - started

    clusters = dict(
        con.execute("SELECT mention_id, cluster_id FROM clusters").fetchall()
    )
    joins = sum(
        1 for mention_id, cluster_id in clusters.items() if mention_id != cluster_id
    )
    stored_links = con.execute("SELECT count(*) FROM similarities").fetchone()[0]
    last = per_mention_ms[-1000:]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "variant": variant,
                "match_weight_threshold": match_weight_threshold,
                "threshold": config.threshold,
                "country_comparison": not drop_country_comparison,
                "mentions": len(mentions),
                "model_source": str(linker.model_source),
                "joins": joins,
                "stored_links": stored_links,
                "seconds": round(elapsed, 1),
                "mean_ms": round(elapsed * 1000 / len(mentions), 1),
                "mean_ms_last_1000": round(sum(last) / len(last), 1),
                "clusters": clusters,
                "top_candidates": top_candidates,
            }
        ),
        encoding="utf-8",
    )
    print(
        f"{variant} mwt={match_weight_threshold}: joins={joins} links={stored_links} {elapsed:.0f}s"
    )


def co_clustered_pairs(clusters: dict[str, str]) -> set[tuple[str, str]]:
    members = defaultdict(list)
    for mention_id, cluster_id in clusters.items():
        members[cluster_id].append(mention_id)
    return {
        tuple(sorted(p)) for group in members.values() for p in combinations(group, 2)
    }


NORMALISED_NAME = "REGEXP_REPLACE(STRIP_ACCENTS(LOWER({side}.legal_name)), '[^\\p{{L}}\\p{{N}}]', '', 'g')"
REFERENCE_SETS = {
    # Same organisation with high confidence: identical normalised name in the same country.
    "exact": "l.country_code = r.country_code AND {nl} = {nr}",
    # Likely the same organisation spelled differently: near-identical name and the same postcode.
    "variant": (
        "l.country_code = r.country_code AND {nl} <> {nr} "
        "AND jaro_winkler_similarity({nl}, {nr}) >= 0.95 AND l.post_code = r.post_code"
    ),
    # Names not even similar: a merge of such a pair is very likely wrong.
    "unrelated": "jaro_winkler_similarity({nl}, {nr}) < 0.8",
}


class CorpusPairs:
    """Exact pair sets over the corpus, computed in DuckDB."""

    def __init__(self, corpus: Path):
        self._con = duckdb.connect()
        self._con.execute(
            f"CREATE TABLE t AS SELECT row_number() OVER () AS rn, * FROM read_csv_auto('{corpus}', all_varchar=true)"
        )

    def _condition(self, template: str) -> str:
        return template.format(
            nl=NORMALISED_NAME.format(side="l"), nr=NORMALISED_NAME.format(side="r")
        )

    def pairs(self, condition: str) -> set[tuple[str, str]]:
        rows = self._con.execute(
            f"SELECT l.mention_id, r.mention_id FROM t l JOIN t r ON l.rn < r.rn AND ({condition})"
        ).fetchall()
        return {tuple(sorted(row)) for row in rows}

    def reference(self, name: str) -> set[tuple[str, str]]:
        return self.pairs(self._condition(REFERENCE_SETS[name]))

    def admitted(self, rules: list[str]) -> set[tuple[str, str]]:
        return self.pairs(" OR ".join(f"({rule})" for rule in rules))

    def unrelated(self, candidates: set[tuple[str, str]]) -> int:
        if not candidates:
            return 0
        self._con.execute("CREATE OR REPLACE TEMP TABLE c (a VARCHAR, b VARCHAR)")
        self._con.executemany("INSERT INTO c VALUES (?, ?)", list(candidates))
        condition = self._condition(REFERENCE_SETS["unrelated"])
        return self._con.execute(
            f"SELECT count(*) FROM c JOIN t l ON l.mention_id = c.a JOIN t r ON r.mention_id = c.b WHERE {condition}"
        ).fetchone()[0]


def compare(run_paths: list[Path], corpus: Path) -> None:
    corpus_pairs = CorpusPairs(corpus)
    exact, variant = corpus_pairs.reference("exact"), corpus_pairs.reference("variant")
    print(f"reference pairs: exact={len(exact)} variant={len(variant)}")
    print(
        f"{'run':30} {'pairs/mention':>13} {'block exact':>11} {'block var':>9} {'clust exact':>11} "
        f"{'clust var':>9} {'merged':>7} {'unrelated':>9} {'joins':>6} {'links':>8} {'ms/m':>6} {'ms last1k':>9}"
    )
    admitted_cache: dict[str, set] = {}
    for path in run_paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        name = data["variant"]
        if name not in admitted_cache:
            admitted_cache[name] = corpus_pairs.admitted(VARIANTS[name])
        admitted = admitted_cache[name]
        clustered = co_clustered_pairs(data["clusters"])
        country = "" if data.get("country_comparison", True) else " -cc"
        label = f"{name} mwt={data['match_weight_threshold']:g} t={data.get('threshold', 0.2):g}{country}"
        print(
            f"{label:30} {len(admitted) / data['mentions']:>13.1f} "
            f"{len(admitted & exact) / len(exact):>11.1%} {len(admitted & variant) / max(len(variant), 1):>9.1%} "
            f"{len(clustered & exact) / len(exact):>11.1%} {len(clustered & variant) / max(len(variant), 1):>9.1%} "
            f"{len(clustered):>7} {corpus_pairs.unrelated(clustered):>9} {data['joins']:>6} {data['stored_links']:>8} "
            f"{data['mean_ms']:>6} {data['mean_ms_last_1000']:>9}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--variant", choices=sorted(VARIANTS))
    parser.add_argument("--match-weight-threshold", type=float, default=-10)
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="cluster threshold; default from config",
    )
    parser.add_argument("--drop-country-comparison", action="store_true")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--compare", nargs="+", type=Path, help="run JSON files to compare"
    )
    args = parser.parse_args()
    if args.compare:
        compare(args.compare, args.corpus)
    else:
        run(
            args.variant,
            args.match_weight_threshold,
            args.corpus,
            args.config,
            args.out,
            args.threshold,
            args.drop_country_comparison,
        )


if __name__ == "__main__":
    main()
