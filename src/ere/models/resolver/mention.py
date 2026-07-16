"""Mention domain model: an entity record being resolved."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from .ids import MentionId


class Mention(BaseModel):
    """
    A mention: the entity being resolved.

    Has an ID and a flat dict of attributes (legal_name, country_code, city, ...).
    Accepts both structured form and legacy flat-dict form for backward compatibility.
    """

    model_config = ConfigDict(frozen=True)

    id: MentionId
    attributes: dict[str, str | None]

    @model_validator(mode="before")
    @classmethod
    def _from_flat_dict(cls, raw_input: object) -> object:
        """
        Accept the legacy flat-dict format used throughout the codebase:
            {"mention_id": "m1", "legal_name": "Acme", "country_code": "US"}
        and convert to the structured form expected by the model.
        """
        if (
            isinstance(raw_input, dict)
            and "mention_id" in raw_input
            and "id" not in raw_input
        ):
            return {
                "id": MentionId(value=raw_input["mention_id"]),
                "attributes": {k: v for k, v in raw_input.items() if k != "mention_id"},
            }
        return raw_input

    def get(self, key: str) -> str | None:
        """Get an attribute value by key, returning None if absent."""
        return self.attributes.get(key)

    def to_flat_dict(self) -> dict:
        """
        Return a flat dict representation of the mention.

        Reconstructs the legacy format: {"mention_id": "m1", ...attributes}.
        Used by adapters and external systems that need a flat representation.
        """
        return {"mention_id": self.id.value, **self.attributes}
