"""Unit tests: blocking settings resolved from resolver.yaml at start-up (services).

Spec: candidate-scoring — "Invalid blocking configuration", "Training blocking is unchanged".
"""

from pathlib import Path

import pytest
import yaml

from ere.models.resolver import EqualityRule, NameSimilarityRule
from ere.services.resolver_config import ResolverConfig

SHIPPED_CONFIGS = sorted(
    (Path(__file__).parents[3] / "src" / "config").glob("resolver*.yaml")
)
BASE = {
    "threshold": 0.7,
    "match_weight_threshold": -10,
    "top_n": 100,
    "entity_fields": ["legal_name", "country_code", "nuts_code"],
}
NAME_RULE = {
    "same": "country_code",
    "similar": {"field": "legal_name", "min_jaro_winkler": 0.8},
}


def _config(splink: dict) -> ResolverConfig:
    return ResolverConfig.from_dict({**BASE, "splink": splink})


def test_blocking_rules_are_typed_with_defaults():
    config = _config({"blocking_rules": [NAME_RULE, "nuts_code"]})

    assert config.blocking.rules == (
        NameSimilarityRule(
            same="country_code", field="legal_name", min_jaro_winkler=0.8
        ),
        EqualityRule(fields=("nuts_code",)),
    )
    assert config.blocking.normalised_fields == ("legal_name",)
    assert config.blocking.em_blocking_field == "country_code"


def test_normalised_fields_and_em_blocking_field_come_from_the_splink_section():
    config = _config(
        {
            "blocking_rules": [NAME_RULE],
            "normalised_fields": ["legal_name"],
            "em_blocking_field": "nuts_code",
        }
    )

    assert config.blocking.em_blocking_field == "nuts_code"


@pytest.mark.parametrize(
    "splink, named",
    [
        ({"blocking_rules": [{**NAME_RULE, "fuzzy": True}]}, "fuzzy"),
        ({"blocking_rules": ["post_code"]}, "post_code"),
        ({"blocking_rules": [NAME_RULE], "em_blocking_field": "city"}, "city"),
        ({"blocking_rules": []}, "blocking_rules"),
        ({}, "blocking_rules"),
    ],
)
def test_invalid_blocking_configuration_refuses_to_start(splink, named):
    with pytest.raises(ValueError, match=named):
        _config(splink)


def test_configuration_without_splink_section_has_no_blocking():
    assert ResolverConfig.from_dict(BASE).blocking is None


@pytest.mark.parametrize("config_file", SHIPPED_CONFIGS, ids=lambda p: p.name)
def test_shipped_configurations_have_valid_blocking(config_file):
    raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))

    assert ResolverConfig.from_dict(raw).blocking.rules
