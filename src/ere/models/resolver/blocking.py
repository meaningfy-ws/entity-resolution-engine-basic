"""Blocking settings: which stored mentions are scored against a new one (domain values, no I/O)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

DEFAULT_NORMALISED_FIELDS = ("legal_name",)
DEFAULT_EM_BLOCKING_FIELD = "country_code"
BLOCKING_RULES_KEY = "blocking_rules"


class BlockingRuleKey(StrEnum):
    """Keys of a name-similarity blocking rule in the resolver configuration."""

    SAME = "same"
    SIMILAR = "similar"
    FIELD = "field"
    MIN_JARO_WINKLER = "min_jaro_winkler"


RULE_KEYS = (BlockingRuleKey.SAME, BlockingRuleKey.SIMILAR)
SIMILAR_KEYS = (BlockingRuleKey.FIELD, BlockingRuleKey.MIN_JARO_WINKLER)


class EqualityRule(BaseModel):
    """Score a pair when all listed fields are equal (NULL never equals NULL)."""

    model_config = ConfigDict(frozen=True)

    fields: tuple[str, ...]


class NameSimilarityRule(BaseModel):
    """Score a pair when `same` is equal and the normalised `field` values are similar enough."""

    model_config = ConfigDict(frozen=True)

    same: str
    field: str
    min_jaro_winkler: float


BlockingRule = EqualityRule | NameSimilarityRule


class BlockingSettings(BaseModel):
    """Validated blocking rules plus the fields they rely on."""

    model_config = ConfigDict(frozen=True)

    rules: tuple[BlockingRule, ...]
    normalised_fields: tuple[str, ...] = DEFAULT_NORMALISED_FIELDS
    em_blocking_field: str = DEFAULT_EM_BLOCKING_FIELD

    @classmethod
    def from_config(
        cls,
        rules: Sequence,
        entity_fields: Sequence[str],
        normalised_fields: Sequence[str] = DEFAULT_NORMALISED_FIELDS,
        em_blocking_field: str = DEFAULT_EM_BLOCKING_FIELD,
    ) -> BlockingSettings:
        """Parse and validate configured rules; any problem raises `ValueError` naming the offending key or field."""
        if not rules:
            raise ValueError(f"At least one entry is required in {BLOCKING_RULES_KEY}")
        known = set(entity_fields)
        normalised = tuple(
            _entity_field(name, known, "normalised field") for name in normalised_fields
        )
        em_field = _entity_field(em_blocking_field, known, "EM blocking field")
        parsed = tuple(_parse_rule(rule, known, normalised) for rule in rules)
        return cls(
            rules=parsed, normalised_fields=normalised, em_blocking_field=em_field
        )


def _parse_rule(rule, known: set[str], normalised: tuple[str, ...]) -> BlockingRule:
    if isinstance(rule, str):
        return EqualityRule(fields=(_entity_field(rule, known, "blocking field"),))
    if isinstance(rule, list):
        return EqualityRule(
            fields=tuple(_entity_field(name, known, "blocking field") for name in rule)
        )
    if not isinstance(rule, dict):
        raise ValueError(
            f"Invalid blocking rule {rule!r}: expected a field name, a list of fields or a mapping"
        )
    _check_keys(rule, RULE_KEYS, "blocking rule")
    similar = rule[BlockingRuleKey.SIMILAR]
    if not isinstance(similar, dict):
        raise ValueError(
            f"'{BlockingRuleKey.SIMILAR.value}' of a blocking rule must be a mapping"
        )
    _check_keys(
        similar, SIMILAR_KEYS, f"'{BlockingRuleKey.SIMILAR.value}' of a blocking rule"
    )
    field = _entity_field(
        similar[BlockingRuleKey.FIELD], known, "name similarity field"
    )
    if field not in normalised:
        raise ValueError(
            f"Name similarity on {field!r} needs it among the normalised fields {list(normalised)}"
        )
    return NameSimilarityRule(
        same=_entity_field(rule[BlockingRuleKey.SAME], known, "blocking field"),
        field=field,
        min_jaro_winkler=_similarity_threshold(
            similar[BlockingRuleKey.MIN_JARO_WINKLER]
        ),
    )


def _check_keys(entry: dict, allowed: tuple[BlockingRuleKey, ...], where: str) -> None:
    allowed_names = ", ".join(key.value for key in allowed)
    unknown = sorted(set(entry) - {key.value for key in allowed})
    if unknown:
        raise ValueError(
            f"Unknown key(s) {', '.join(unknown)} in {where}; allowed keys: {allowed_names}"
        )
    missing = [key.value for key in allowed if key.value not in entry]
    if missing:
        raise ValueError(
            f"Missing key(s) {', '.join(missing)} in {where}; required keys: {allowed_names}"
        )


def _entity_field(name, known: set[str], role: str) -> str:
    if not isinstance(name, str) or not name.isidentifier():
        raise ValueError(f"The {role} {name!r} is not a valid identifier")
    if name not in known:
        raise ValueError(
            f"The {role} {name!r} is not one of the entity fields {sorted(known)}"
        )
    return name


def _similarity_threshold(value) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise ValueError(
            f"{BlockingRuleKey.MIN_JARO_WINKLER.value} must be a number between 0 and 1, got {value!r}"
        )
    return float(value)
