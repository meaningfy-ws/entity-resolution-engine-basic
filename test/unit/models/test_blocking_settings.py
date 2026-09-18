"""Unit tests: blocking settings as typed domain values (models).

Spec: candidate-scoring — "Only similarly named organisations of the same country are scored",
"Invalid blocking configuration", "Training blocking is unchanged".
"""

import pytest

from ere.models.resolver import (
    BlockingSettings,
    EqualityRule,
    NameSimilarityRule,
)

ENTITY_FIELDS = ["legal_name", "country_code", "post_code"]
NAME_RULE = {
    "same": "country_code",
    "similar": {"field": "legal_name", "min_jaro_winkler": 0.8},
}


def _parse(rules, **kwargs) -> BlockingSettings:
    return BlockingSettings.from_config(rules, entity_fields=ENTITY_FIELDS, **kwargs)


def test_rule_forms_are_parsed_into_typed_rules():
    settings = _parse(["country_code", ["country_code", "post_code"], NAME_RULE])

    assert settings.rules == (
        EqualityRule(fields=("country_code",)),
        EqualityRule(fields=("country_code", "post_code")),
        NameSimilarityRule(
            same="country_code", field="legal_name", min_jaro_winkler=0.8
        ),
    )


def test_defaults_normalise_legal_name_and_train_on_country():
    settings = _parse([NAME_RULE])

    assert settings.normalised_fields == ("legal_name",)
    assert settings.em_blocking_field == "country_code"


@pytest.mark.parametrize("key", ["similar_to", "fuzzy"])
def test_unknown_key_is_refused_naming_it_and_the_allowed_keys(key):
    with pytest.raises(ValueError, match=f"{key}.*same.*similar"):
        _parse([{**NAME_RULE, key: 1}])


@pytest.mark.parametrize(
    "rule, named",
    [
        ({"similar": NAME_RULE["similar"]}, "same"),
        ({"same": "country_code"}, "similar"),
        ({"same": "country_code", "similar": {"min_jaro_winkler": 0.8}}, "field"),
        (
            {"same": "country_code", "similar": {"field": "legal_name"}},
            "min_jaro_winkler",
        ),
    ],
)
def test_missing_key_is_refused_naming_it(rule, named):
    with pytest.raises(ValueError, match=named):
        _parse([rule])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.5, "high", True])
def test_similarity_threshold_must_be_a_number_between_0_and_1(value):
    rule = {
        "same": "country_code",
        "similar": {"field": "legal_name", "min_jaro_winkler": value},
    }
    with pytest.raises(ValueError, match="min_jaro_winkler"):
        _parse([rule])


@pytest.mark.parametrize(
    "rules",
    [
        [{"same": "country_code) OR (1=1", "similar": NAME_RULE["similar"]}],
        ["country_code; DROP TABLE mentions"],
        [["country_code", "post code"]],
    ],
)
def test_field_names_must_be_identifiers(rules):
    with pytest.raises(ValueError, match="identifier"):
        _parse(rules)


@pytest.mark.parametrize(
    "rules, kwargs, named",
    [
        (["nuts_code"], {}, "nuts_code"),
        ([{"same": "region", "similar": NAME_RULE["similar"]}], {}, "region"),
        ([NAME_RULE], {"em_blocking_field": "nuts_code"}, "nuts_code"),
        ([NAME_RULE], {"normalised_fields": ["post_name"]}, "post_name"),
    ],
)
def test_fields_must_be_entity_fields(rules, kwargs, named):
    with pytest.raises(ValueError, match=named):
        _parse(rules, **kwargs)


def test_name_similarity_needs_a_normalised_field():
    rule = {
        "same": "country_code",
        "similar": {"field": "post_code", "min_jaro_winkler": 0.9},
    }

    with pytest.raises(ValueError, match="post_code.*normalised"):
        _parse([rule])

    assert (
        _parse([rule], normalised_fields=["legal_name", "post_code"]).rules[0].field
        == "post_code"
    )


def test_at_least_one_rule_is_required():
    with pytest.raises(ValueError, match="blocking_rules"):
        _parse([])
