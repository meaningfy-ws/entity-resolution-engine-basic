"""Unit tests: rendering typed blocking rules and the trained-parameters hand-over (adapter).

Spec: candidate-scoring — "Invalid blocking configuration"; resolution-model-lifecycle — trained model kept.
"""

import pytest
import yaml

from ere.adapters.splink_linker_impl import (
    ModelSource,
    SpLinkSimilarityLinker,
    render_blocking_rules,
)
from ere.models.resolver import BlockingSettings
from test.conftest import TEST_RESOURCES_DIR

ENTITY_FIELDS = ["legal_name", "country_code", "post_code"]


def test_typed_rules_render_as_sql_over_both_sides():
    settings = BlockingSettings.from_config(
        [
            "country_code",
            ["country_code", "post_code"],
            {
                "same": "country_code",
                "similar": {"field": "legal_name", "min_jaro_winkler": 0.8},
            },
        ],
        entity_fields=ENTITY_FIELDS,
    )

    assert render_blocking_rules(settings.rules) == [
        "l.country_code = r.country_code",
        "l.country_code = r.country_code AND l.post_code = r.post_code",
        "l.country_code = r.country_code AND jaro_winkler_similarity(l.legal_name_norm, r.legal_name_norm) >= 0.8",
    ]


def test_em_training_blocks_on_the_configured_field():
    raw = yaml.safe_load(
        (TEST_RESOURCES_DIR / "resolver.yaml").read_text(encoding="utf-8")
    )
    blocking = BlockingSettings.from_config(
        raw["splink"]["blocking_rules"],
        entity_fields=raw["entity_fields"],
        em_blocking_field="nuts_code",
    )
    linker = SpLinkSimilarityLinker(raw["entity_fields"], raw, blocking=blocking)

    rule_sql = (
        linker._get_em_training_rule().get_blocking_rule("duckdb").blocking_rule_sql
    )  # pylint: disable=protected-access

    assert rule_sql == 'l."nuts_code" = r."nuts_code"'


def test_trained_parameters_survive_a_failed_linker_rebuild(monkeypatch):
    raw = yaml.safe_load(
        (TEST_RESOURCES_DIR / "resolver.yaml").read_text(encoding="utf-8")
    )
    linker = SpLinkSimilarityLinker(raw["entity_fields"], raw)
    trained_settings = linker._linker.misc.save_model_to_json()  # pylint: disable=protected-access
    linker._pending_trained_settings = trained_settings  # pylint: disable=protected-access
    linker.model_source = ModelSource.TRAINED
    original_new_linker = linker._new_linker  # pylint: disable=protected-access
    calls = []

    def failing_once(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("rebuild failed")
        return original_new_linker(*args, **kwargs)

    monkeypatch.setattr(linker, "_new_linker", failing_once)

    with pytest.raises(RuntimeError):
        linker._apply_trained_settings()  # pylint: disable=protected-access
    linker._apply_trained_settings()  # pylint: disable=protected-access

    assert len(calls) == 2
    assert linker._pending_trained_settings is None  # pylint: disable=protected-access
