"""Unit tests: a persisted Splink model reloads without changing scores (adapter).

Spec: resolution-model-lifecycle — "Trained model is persisted and reloaded".
"""

import json

import pytest
import yaml

from ere.adapters.splink_linker_impl import (
    ModelSource,
    SpLinkSimilarityLinker,
    build_tf_df,
)
from ere.models.resolver import Mention
from test.conftest import TEST_RESOURCES_DIR

ENTITY_FIELDS = ["legal_name", "country_code"]
SEED_MENTIONS = [
    Mention(mention_id="s1", legal_name="Acme Industries GmbH", country_code="DEU"),
    Mention(mention_id="s2", legal_name="Acme Industrie GmbH", country_code="DEU"),
    Mention(mention_id="s3", legal_name="Bestco Logistics", country_code="DEU"),
]
QUERY = Mention(mention_id="q1", legal_name="ACME Industries GmbH.", country_code="DEU")


def _config() -> dict:
    raw = yaml.safe_load(
        (TEST_RESOURCES_DIR / "resolver.yaml").read_text(encoding="utf-8")
    )
    raw["entity_fields"] = ENTITY_FIELDS
    raw["splink"]["comparisons"] = [
        c for c in raw["splink"]["comparisons"] if c["field"] in ENTITY_FIELDS
    ]
    raw["splink"]["blocking_rules"] = ["country_code"]
    return raw


def _scores(linker: SpLinkSimilarityLinker) -> dict[str, float]:
    return {
        link.other(QUERY.id).value: link.score for link in linker.find_matches(QUERY)
    }


def _altered_model(linker: SpLinkSimilarityLinker) -> dict:
    """The linker's model with the legal-name match probabilities lowered: scores must visibly change."""
    model = linker._linker.misc.save_model_to_json()  # pylint: disable=protected-access  # test fixture: current parameters
    for comparison in model["comparisons"]:
        if comparison["output_column_name"] == "legal_name":
            for level in comparison["comparison_levels"]:
                if "m_probability" in level:
                    level["m_probability"] = 0.01
    return model


def test_reloaded_model_is_used_for_scoring(tmp_path):
    model_path = tmp_path / "model.json"
    seeds = build_tf_df(SEED_MENTIONS, ENTITY_FIELDS)
    cold_start = SpLinkSimilarityLinker(ENTITY_FIELDS, _config(), initial_df=seeds)
    model_path.write_text(json.dumps(_altered_model(cold_start)), encoding="utf-8")

    reloaded = SpLinkSimilarityLinker(
        ENTITY_FIELDS, _config(), initial_df=seeds, model_path=str(model_path)
    )

    assert reloaded.model_source == ModelSource.PERSISTED
    assert _scores(cold_start)
    assert _scores(reloaded) != _scores(cold_start)


def test_trained_parameters_are_used_by_the_next_scoring_call():
    seeds = build_tf_df(SEED_MENTIONS, ENTITY_FIELDS)
    linker = SpLinkSimilarityLinker(ENTITY_FIELDS, _config(), initial_df=seeds)
    before = _scores(linker)
    linker._pending_trained_settings = _altered_model(linker)  # pylint: disable=protected-access  # what training hands over

    after = _scores(linker)

    assert before
    assert after != before


@pytest.mark.parametrize("content", ["", "{ not json", '{"link_type": "dedupe_only"}'])
def test_unusable_model_file_falls_back_to_cold_start_and_is_left_untouched(
    tmp_path, content
):
    model_path = tmp_path / "model.json"
    model_path.write_text(content, encoding="utf-8")
    seeds = build_tf_df(SEED_MENTIONS, ENTITY_FIELDS)

    linker = SpLinkSimilarityLinker(
        ENTITY_FIELDS, _config(), initial_df=seeds, model_path=str(model_path)
    )
    linker.train()

    assert linker.model_source == ModelSource.COLD_START
    assert not linker.needs_training()
    assert model_path.read_text(encoding="utf-8") == content


def test_persisted_model_is_frozen(tmp_path):
    model_path = tmp_path / "model.json"
    seeds = build_tf_df(SEED_MENTIONS, ENTITY_FIELDS)
    SpLinkSimilarityLinker(
        ENTITY_FIELDS, _config(), initial_df=seeds
    )._linker.misc.save_model_to_json(str(model_path), overwrite=True)  # pylint: disable=protected-access
    content = model_path.read_text(encoding="utf-8")
    linker = SpLinkSimilarityLinker(
        ENTITY_FIELDS, _config(), initial_df=seeds, model_path=str(model_path)
    )

    linker.train()

    assert linker.model_source == ModelSource.PERSISTED
    assert model_path.read_text(encoding="utf-8") == content
