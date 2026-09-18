"""Step definitions for candidate_scoring.feature."""

import csv
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pytest
import yaml
from assertpy import assert_that
from pytest_bdd import given, parsers, scenarios, then, when
from test.unit.adapters.stubs import (
    InMemoryClusterRepository,
    InMemoryMentionRepository,
    InMemorySimilarityRepository,
)

from ere.adapters.duckdb_repositories import DuckDBMentionRepository
from ere.adapters.duckdb_schema import (
    OutdatedSchemaError,
    init_schema,
    normalised_name_sql,
)
from ere.adapters.splink_linker_impl import (
    SpLinkSimilarityLinker,
    render_blocking_rules,
)
from ere.entrypoints.bootstrap import build_entity_resolver
from ere.models.resolver import (
    BlockingSettings,
    ClusterId,
    ClusterMembership,
    LinkTable,
    Mention,
    MentionId,
    MentionLink,
)
from ere.services.entity_resolution_service import EntityResolver
from ere.services.resolver_config import ResolverConfig

scenarios("../candidate_scoring.feature")

REPO_ROOT = Path(__file__).parents[3]
SHIPPED_CONFIG = REPO_ROOT / "src" / "config" / "resolver.yaml"
SHIPPED_CONFIGS = sorted((REPO_ROOT / "src" / "config").glob("resolver*.yaml"))
REFERENCE_CORPUS = REPO_ROOT / "test" / "stress" / "data" / "org-mid.csv"
COUNTRY_ONLY_RULE = "l.country_code = r.country_code"
NAME_FIELD = "legal_name"
NORMALISED_NAME = f"{NAME_FIELD}_norm"
UNRELATED_BELOW = 0.8
VARIANT_AT_LEAST = 0.95
SCORE_EVERYTHING = (
    -1000
)  # match-weight floor low enough that every scored pair is returned


# ---------------------------------------------------------------------------
# Reference corpus helpers
# ---------------------------------------------------------------------------


class ReferenceCorpus:
    """The corpus as a DuckDB table with normalised names, plus the reference pair sets."""

    def __init__(self, con: duckdb.DuckDBPyConnection | None = None):
        self.con = con or duckdb.connect()
        self.con.execute(
            f"CREATE OR REPLACE TABLE corpus AS SELECT row_number() OVER () AS rn, *, "
            f"{normalised_name_sql(NAME_FIELD)} AS {NORMALISED_NAME} "
            f"FROM read_csv_auto('{REFERENCE_CORPUS}', all_varchar=true)"
        )
        self.size = self.con.execute("SELECT count(*) FROM corpus").fetchone()[0]

    def pairs(self, condition: str) -> set[tuple[str, str]]:
        rows = self.con.execute(
            f"SELECT l.mention_id, r.mention_id FROM corpus l JOIN corpus r ON l.rn < r.rn AND ({condition})"
        ).fetchall()
        return {tuple(sorted(row)) for row in rows}

    def exact(self) -> set[tuple[str, str]]:
        return self.pairs(
            f"l.country_code = r.country_code AND l.{NORMALISED_NAME} = r.{NORMALISED_NAME}"
        )

    def variant(self) -> set[tuple[str, str]]:
        return self.pairs(
            f"l.country_code = r.country_code AND l.{NORMALISED_NAME} <> r.{NORMALISED_NAME} "
            f"AND jaro_winkler_similarity(l.{NORMALISED_NAME}, r.{NORMALISED_NAME}) >= {VARIANT_AT_LEAST} "
            f"AND l.post_code = r.post_code"
        )

    def unrelated(self, pairs: set[tuple[str, str]]) -> int:
        self.con.execute(
            "CREATE OR REPLACE TEMP TABLE clustered (a VARCHAR, b VARCHAR)"
        )
        self.con.executemany("INSERT INTO clustered VALUES (?, ?)", list(pairs))
        return self.con.execute(
            f"SELECT count(*) FROM clustered JOIN corpus l ON l.mention_id = clustered.a "
            f"JOIN corpus r ON r.mention_id = clustered.b "
            f"WHERE jaro_winkler_similarity(l.{NORMALISED_NAME}, r.{NORMALISED_NAME}) < {UNRELATED_BELOW}"
        ).fetchone()[0]


@dataclass
class BlockingEvaluation:
    admitted: set
    exact: set
    variant: set
    per_mention: float
    country_only_per_mention: float


@pytest.fixture(scope="module")
def reference_corpus() -> ReferenceCorpus:
    return ReferenceCorpus()


def _shipped_raw_config() -> dict:
    return yaml.safe_load(SHIPPED_CONFIG.read_text(encoding="utf-8"))


def _organisation(
    mention_id: str,
    name: str,
    country: str,
    post_code: str,
    post_name: str | None,
    fields: list[str],
):
    attributes = {field: None for field in fields}
    attributes.update(
        {
            "legal_name": name,
            "country_code": country,
            "post_code": post_code,
            "post_name": post_name,
        }
    )
    return Mention(id=MentionId(value=mention_id), attributes=attributes)


# ---------------------------------------------------------------------------
# Blocking on the reference corpus
# ---------------------------------------------------------------------------


@given("the shipped blocking rules", target_fixture="blocking_sql")
def shipped_blocking_rules() -> list[str]:
    raw = _shipped_raw_config()
    return render_blocking_rules(
        BlockingSettings.from_config(
            raw["splink"]["blocking_rules"], entity_fields=raw["entity_fields"]
        ).rules
    )


@when(
    "they are evaluated on the reference corpus", target_fixture="blocking_evaluation"
)
def evaluate_blocking(
    blocking_sql: list[str], reference_corpus: ReferenceCorpus
) -> BlockingEvaluation:
    admitted = reference_corpus.pairs(" OR ".join(f"({rule})" for rule in blocking_sql))
    country_only = reference_corpus.pairs(COUNTRY_ONLY_RULE)
    return BlockingEvaluation(
        admitted=admitted,
        exact=reference_corpus.exact(),
        variant=reference_corpus.variant(),
        per_mention=len(admitted) / reference_corpus.size,
        country_only_per_mention=len(country_only) / reference_corpus.size,
    )


@then(
    parsers.parse(
        "they admit at least {percent:d} percent of the exact reference pairs"
    )
)
def admits_exact(percent: int, blocking_evaluation: BlockingEvaluation):
    share = len(blocking_evaluation.admitted & blocking_evaluation.exact) / len(
        blocking_evaluation.exact
    )
    assert_that(share * 100).is_greater_than_or_equal_to(percent)


@then(
    parsers.parse(
        "they admit at least {percent:d} percent of the variant reference pairs"
    )
)
def admits_variant(percent: int, blocking_evaluation: BlockingEvaluation):
    share = len(blocking_evaluation.admitted & blocking_evaluation.variant) / len(
        blocking_evaluation.variant
    )
    assert_that(share * 100).is_greater_than_or_equal_to(percent)


@then(
    parsers.parse(
        "they admit at least {factor:d} times fewer pairs per mention than blocking on country alone"
    )
)
def admits_fewer_pairs(factor: int, blocking_evaluation: BlockingEvaluation):
    assert_that(blocking_evaluation.per_mention * factor).is_less_than_or_equal_to(
        blocking_evaluation.country_only_per_mention
    )


# ---------------------------------------------------------------------------
# Which stored mentions are scored
# ---------------------------------------------------------------------------


@dataclass
class ScoringSetup:
    con: duckdb.DuckDBPyConnection
    linker: SpLinkSimilarityLinker
    mention_repo: DuckDBMentionRepository
    fields: list[str]


@given(
    "a Splink-backed resolver with name-similarity blocking",
    target_fixture="scoring_setup",
)
def name_similarity_linker(tmp_path):
    raw = _shipped_raw_config()
    raw["match_weight_threshold"] = SCORE_EVERYTHING
    fields = raw["entity_fields"]
    con = duckdb.connect(str(tmp_path / "app.duckdb"))
    init_schema(con, fields)
    linker = SpLinkSimilarityLinker(fields, raw, connection=con)
    yield ScoringSetup(
        con=con,
        linker=linker,
        mention_repo=DuckDBMentionRepository(con, fields),
        fields=fields,
    )
    con.close()


@given(
    parsers.parse('a stored mention "{name}" in "{country}" at post code "{post_code}"')
)
def stored_mention(
    name: str, country: str, post_code: str, scoring_setup: ScoringSetup
):
    scoring_setup.mention_repo.save(
        _organisation("stored", name, country, post_code, None, scoring_setup.fields)
    )


@when(
    parsers.parse(
        'a new mention "{name}" in "{country}" at post code "{post_code}" is scored'
    ),
    target_fixture="scored_links",
)
def score_new_mention(
    name: str, country: str, post_code: str, scoring_setup: ScoringSetup
):
    return scoring_setup.linker.find_matches(
        _organisation("new", name, country, post_code, None, scoring_setup.fields)
    )


@then("the stored mention is scored")
def stored_is_scored(scored_links):
    assert_that(
        [link.other(MentionId(value="new")).value for link in scored_links]
    ).contains("stored")


@then("the stored mention is not scored")
def stored_is_not_scored(scored_links):
    assert_that(scored_links).is_empty()


@given(
    parsers.parse('a blocking rule with the unknown key "{key}"'),
    target_fixture="blocking_config",
)
def blocking_rule_with_unknown_key(key: str):
    return [
        {"same": "country_code", key: {"field": NAME_FIELD, "min_jaro_winkler": 0.8}}
    ]


@when("the blocking rules are loaded", target_fixture="loading_error")
def load_blocking_rules(blocking_config):
    with pytest.raises(ValueError) as error:
        BlockingSettings.from_config(
            blocking_config, entity_fields=_shipped_raw_config()["entity_fields"]
        )
    return str(error.value)


@then(parsers.parse('loading fails with an error naming "{key}" and the allowed keys'))
def loading_fails(key: str, loading_error: str):
    assert_that(loading_error).contains(key, "same", "similar")


# ---------------------------------------------------------------------------
# Cluster joins need strong evidence
# ---------------------------------------------------------------------------


@given(
    "the ERE is started with the shipped configuration",
    target_fixture="shipped_resolver",
)
def start_with_shipped_configuration(tmp_path):
    resolver = build_entity_resolver(
        resolver_config_path=SHIPPED_CONFIG, duckdb_path=str(tmp_path / "app.duckdb")
    )
    yield resolver
    resolver._mention_repo._con.close()  # pylint: disable=protected-access  # mirrors app.py shutdown


@given(
    parsers.parse(
        'it has resolved "{name}" in "{country}" at post code "{post_code}" in "{post_name}" as mention "{mention_id}"'
    )
)
@when(
    parsers.parse(
        'it resolves "{name}" in "{country}" at post code "{post_code}" in "{post_name}" as mention "{mention_id}"'
    )
)
def resolve_with_shipped(
    name, country, post_code, post_name, mention_id, shipped_resolver: EntityResolver
):
    fields = shipped_resolver._config.entity_fields  # pylint: disable=protected-access
    shipped_resolver.resolve(
        _organisation(mention_id, name, country, post_code, post_name, fields)
    )


@then(parsers.parse('mention "{joined}" is in the cluster of mention "{founder}"'))
def in_cluster_of(joined: str, founder: str, shipped_resolver: EntityResolver):
    clusters = shipped_resolver._cluster_repo  # pylint: disable=protected-access
    assert_that(clusters.find_cluster_of(MentionId(value=joined))).is_equal_to(
        clusters.find_cluster_of(MentionId(value=founder))
    )


@when(
    "the reference corpus is resolved in file order", target_fixture="clustered_pairs"
)
def resolve_reference_corpus(shipped_resolver: EntityResolver):
    fields = shipped_resolver._config.entity_fields  # pylint: disable=protected-access
    with REFERENCE_CORPUS.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            shipped_resolver.resolve(
                Mention(
                    id=MentionId(value=row["mention_id"]),
                    attributes={field: row.get(field) or None for field in fields},
                )
            )
    memberships = shipped_resolver._cluster_repo.get_all_memberships()  # pylint: disable=protected-access
    return {
        tuple(sorted((a.value, b.value)))
        for members in memberships.values()
        for i, a in enumerate(members)
        for b in members[i + 1 :]
    }


@then(
    parsers.parse(
        "fewer than {percent:d} percent of the pairs placed in the same cluster have unrelated names"
    )
)
def few_unrelated(percent: int, clustered_pairs, reference_corpus: ReferenceCorpus):
    assert_that(clustered_pairs).is_not_empty()
    assert_that(
        reference_corpus.unrelated(clustered_pairs) * 100 / len(clustered_pairs)
    ).is_less_than(percent)


@then(
    parsers.parse(
        "at least {percent:d} percent of the exact reference pairs are placed in the same cluster"
    )
)
def exact_clustered(percent: int, clustered_pairs, reference_corpus: ReferenceCorpus):
    exact = reference_corpus.exact()
    assert_that(
        len(clustered_pairs & exact) * 100 / len(exact)
    ).is_greater_than_or_equal_to(percent)


@when(
    "the shipped resolver configurations are loaded", target_fixture="shipped_configs"
)
def load_shipped_configs():
    return {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in SHIPPED_CONFIGS
    }


@then(parsers.parse('none of them compares "{field}"'))
def none_compares(field: str, shipped_configs: dict):
    for name, raw in shipped_configs.items():
        compared = [comparison["field"] for comparison in raw["splink"]["comparisons"]]
        assert_that(compared).described_as(name).does_not_contain(field)


@then(parsers.parse("all of them use a cluster threshold of {threshold:f}"))
def all_use_threshold(threshold: float, shipped_configs: dict):
    for name, raw in shipped_configs.items():
        assert_that(raw["threshold"]).described_as(name).is_equal_to(threshold)


# ---------------------------------------------------------------------------
# Existing databases, training, storage
# ---------------------------------------------------------------------------


@given("a database file created before this change", target_fixture="old_database")
def old_database(tmp_path) -> Path:
    path = tmp_path / "old.duckdb"
    con = duckdb.connect(str(path))
    columns = ", ".join(
        f"{field} TEXT" for field in _shipped_raw_config()["entity_fields"]
    )
    con.execute(f"CREATE TABLE mentions (mention_id TEXT, {columns})")
    con.close()
    return path


@when("the ERE is started on it", target_fixture="startup_error")
def start_on_old_database(old_database: Path):
    with pytest.raises(OutdatedSchemaError) as error:
        build_entity_resolver(
            resolver_config_path=SHIPPED_CONFIG, duckdb_path=str(old_database)
        )
    return str(error.value)


@then("start-up fails with an error naming the database file and telling to reset it")
def startup_refused(startup_error: str, old_database: Path):
    assert_that(startup_error).contains(str(old_database))
    assert_that(startup_error.lower()).contains("reset")


@then(parsers.parse('its training blocking rule compares "{field}" only'))
def training_rule(field: str, scoring_setup: ScoringSetup):
    rule_sql = (
        scoring_setup.linker._get_em_training_rule()
        .get_blocking_rule("duckdb")
        .blocking_rule_sql
    )  # pylint: disable=protected-access
    assert_that(rule_sql).is_equal_to(f'l."{field}" = r."{field}"')


class _ProducingLinker:
    """Returns a fixed list of links for the next mention; nothing else."""

    def __init__(self, links: list[MentionLink]):
        self._links = links

    def find_matches(self, mention):  # pylint: disable=unused-argument
        return list(self._links)

    def find_matches_batch(self, mentions):  # pylint: disable=unused-argument
        return LinkTable(
            left_ids=tuple(link.left_id.value for link in self._links),
            right_ids=tuple(link.right_id.value for link in self._links),
            scores=tuple(link.score for link in self._links),
        )

    def register_mention(self, mention):  # pylint: disable=unused-argument
        return None

    def train(self):
        return None


@given(
    parsers.parse(
        "a resolver whose linker returns {produced:d} links for the next mention"
    ),
    target_fixture="top_k_setup",
)
def resolver_producing_links(produced: int):
    new_id = MentionId(value="new")
    cluster_repo = InMemoryClusterRepository()
    links = []
    for index in range(produced):
        other = MentionId(value=f"m{index}")
        cluster_repo.save(
            ClusterMembership(mention_id=other, cluster_id=ClusterId(value=f"c{index}"))
        )
        links.append(
            MentionLink(
                left_id=new_id,
                right_id=other,
                score=round(0.001 + index / (produced + 1), 6),
            )
        )
    return {
        "links": links,
        "cluster_repo": cluster_repo,
        "similarity_repo": InMemorySimilarityRepository(),
    }


@when(
    parsers.parse("that mention is resolved with top_n {top_n:d}"),
    target_fixture="top_k_result",
)
def resolve_with_top_n(top_n: int, top_k_setup: dict):
    resolver = EntityResolver(
        mention_repo=InMemoryMentionRepository(),
        similarity_repo=top_k_setup["similarity_repo"],
        cluster_repo=top_k_setup["cluster_repo"],
        linker=_ProducingLinker(top_k_setup["links"]),
        config=ResolverConfig(
            threshold=0.99,
            match_weight_threshold=-10,
            top_n=top_n,
            entity_fields=["legal_name"],
            auto_train_threshold=0,
        ),
    )
    result = resolver.resolve(
        Mention(id=MentionId(value="new"), attributes={"legal_name": "New"})
    )
    return {"result": result, "top_n": top_n}


@then(parsers.parse("{stored:d} links are stored for it"))
def links_stored(stored: int, top_k_setup: dict):
    stored_links = top_k_setup["similarity_repo"].find_for(MentionId(value="new"))
    assert_that(stored_links).is_length(stored)
    highest = sorted((link.score for link in top_k_setup["links"]), reverse=True)[
        :stored
    ]
    assert_that(
        sorted((link.score for link in stored_links), reverse=True)
    ).is_equal_to(highest)


@then(parsers.parse("its candidates equal those built from all {produced:d} links"))
def candidates_from_all_links(produced: int, top_k_setup: dict, top_k_result: dict):
    links = top_k_setup["links"]
    assert_that(links).is_length(produced)
    by_cluster = {f"c{index}": link.score for index, link in enumerate(links)}
    by_cluster.setdefault("new", 0.0)
    expected = sorted(by_cluster.items(), key=lambda item: item[1], reverse=True)[
        : top_k_result["top_n"]
    ]
    actual = [(c.cluster_id.value, c.score) for c in top_k_result["result"].candidates]
    assert_that(actual).is_equal_to(expected)
