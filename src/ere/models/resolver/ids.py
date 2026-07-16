"""Value-object identifiers for mentions and clusters."""

from pydantic import BaseModel, ConfigDict


class MentionId(BaseModel):
    """Unique identifier for a mention (entity record)."""

    model_config = ConfigDict(frozen=True)

    value: str

    def __str__(self) -> str:
        return self.value


class ClusterId(BaseModel):
    """Identifier for a cluster. Always derived from the MentionId of the founding mention."""

    model_config = ConfigDict(frozen=True)

    value: str

    def __str__(self) -> str:
        return self.value
