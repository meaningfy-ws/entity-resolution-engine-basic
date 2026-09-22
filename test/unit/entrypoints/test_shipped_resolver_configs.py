"""Unit tests: every shipped resolver configuration starts a resolver that resolves a mention (composition root).

Guards against configurations that cannot be loaded (e.g. missing `entity_fields`, fields unknown to the RDF mapping).
"""

from pathlib import Path

import pytest
import yaml

from ere.entrypoints.bootstrap import build_entity_resolver
from ere.models.resolver import Mention, MentionId

CONFIG_DIR = Path(__file__).parents[3] / "src" / "config"
SHIPPED_CONFIGS = sorted(CONFIG_DIR.glob("resolver*.yaml"))


def _rdf_mapping_fields() -> set[str]:
    mapping = yaml.safe_load(
        (CONFIG_DIR / "rdf_mapping.yaml").read_text(encoding="utf-8")
    )
    return {
        field
        for entity in mapping["entity_types"].values()
        for field in entity["fields"]
    }


@pytest.mark.parametrize("config_file", SHIPPED_CONFIGS, ids=lambda p: p.name)
def test_entity_fields_exist_in_the_rdf_mapping(config_file):
    raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))

    assert set(raw["entity_fields"]) <= _rdf_mapping_fields()


@pytest.mark.parametrize("config_file", SHIPPED_CONFIGS, ids=lambda p: p.name)
def test_shipped_configuration_builds_a_resolver_that_resolves(config_file, tmp_path):
    resolver = build_entity_resolver(
        resolver_config_path=config_file, duckdb_path=str(tmp_path / "app.duckdb")
    )
    fields = resolver._config.entity_fields  # pylint: disable=protected-access
    try:
        first, second = (
            Mention(
                id=MentionId(value=mention_id),
                attributes={
                    field: {
                        "legal_name": name,
                        "country_code": "DEU",
                        "post_name": "Berlin",
                    }.get(field)
                    for field in fields
                },
            )
            for mention_id, name in (
                ("a", "Acme Industries GmbH"),
                ("b", "ACME Industries GmbH"),
            )
        )
        resolver.resolve(first)
        result = resolver.resolve(second)
    finally:
        resolver._mention_repo._con.close()  # pylint: disable=protected-access

    assert result.candidates
