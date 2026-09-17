"""Unit tests for services.factories: construction of resolver and service."""

from pathlib import Path

import pytest
import yaml

from ere.services.entity_resolution_service import EntityResolutionService, EntityResolver
from ere.services.factories import build_entity_resolution_service, build_entity_resolver
from test.unit.adapters.stubs import StubRDFMapper

TEST_RESOLVER_CONFIG = Path(__file__).parent.parent.parent / "resources" / "resolver.yaml"


def test_build_entity_resolver_returns_entity_resolver():
    resolver = build_entity_resolver(resolver_config_path=TEST_RESOLVER_CONFIG)
    assert isinstance(resolver, EntityResolver)


def test_build_entity_resolver_uses_default_config_when_no_path_given():
    resolver = build_entity_resolver()
    assert isinstance(resolver, EntityResolver)


def test_build_entity_resolver_with_explicit_entity_fields():
    resolver = build_entity_resolver(
        entity_fields=["legal_name"],
        resolver_config_path=TEST_RESOLVER_CONFIG,
    )
    assert isinstance(resolver, EntityResolver)


def test_build_entity_resolver_with_persistent_duckdb(tmp_path):
    db_file = str(tmp_path / "test.duckdb")
    with open(TEST_RESOLVER_CONFIG, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw["duckdb"] = {"type": "persistent", "path": db_file}
    config = tmp_path / "persistent.yaml"
    config.write_text(yaml.dump(raw), encoding="utf-8")

    resolver = build_entity_resolver(resolver_config_path=config, duckdb_path=db_file)
    assert isinstance(resolver, EntityResolver)


def test_build_entity_resolver_raises_on_invalid_duckdb_type(tmp_path):
    bad_config = tmp_path / "bad.yaml"
    bad_config.write_text(
        "threshold: 0.8\n"
        "match_weight_threshold: -10\n"
        "top_n: 10\n"
        "entity_fields: [legal_name]\n"
        "duckdb:\n"
        "  type: invalid_type\n"
        "  path: ':memory:'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Invalid duckdb type"):
        build_entity_resolver(resolver_config_path=bad_config)


def test_build_entity_resolution_service_returns_service():
    resolver = build_entity_resolver(resolver_config_path=TEST_RESOLVER_CONFIG)
    mapper = StubRDFMapper()

    service = build_entity_resolution_service(resolver, mapper)

    assert isinstance(service, EntityResolutionService)
