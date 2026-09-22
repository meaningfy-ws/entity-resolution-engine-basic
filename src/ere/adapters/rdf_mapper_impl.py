"""Concrete RDF mapper implementation for Turtle RDF extraction.

This adapter implements the RDFMapper port using the rdf_mapper utilities
for Turtle RDF parsing and attribute extraction per YAML configuration.
"""

import hashlib
import logging
from pathlib import Path

from erspec.models.core import EntityMention

from ere.adapters.rdf_mapper import load_entity_mappings, extract_mention_attributes
from ere.models.ports.rdf_mapper import RDFMapper
from ere.models.resolver import Mention, MentionId

log = logging.getLogger(__name__)


class TurtleRDFMapper(RDFMapper):
    """Concrete RDF mapper for Turtle RDF format."""

    def __init__(self, rdf_mapping_path: str | Path = None):
        """
        Initialize the RDF mapper with configuration.

        Args:
            rdf_mapping_path: Path to rdf_mapping.yaml config file.
                             If None, uses default relative path.
        """
        self._mappings = self._load_mappings(rdf_mapping_path)

    @staticmethod
    def _load_mappings(rdf_mapping_path: str | Path = None) -> dict:
        """
        Load entity mappings from rdf_mapping.yaml.

        Args:
            rdf_mapping_path: Path to rdf_mapping.yaml. If None, uses default.

        Returns:
            dict: Entity type mappings from config.
        """
        if rdf_mapping_path is None:
            rdf_mapping_path = (
                Path(__file__).parent.parent.parent / "config" / "rdf_mapping.yaml"
            )
        else:
            rdf_mapping_path = Path(rdf_mapping_path)
        return load_entity_mappings(rdf_mapping_path)

    def map_entity_mention_to_domain(self, entity_mention: EntityMention) -> Mention:
        """
        Map EntityMention (erspec) to Mention (domain).

        Performs RDF parsing and attribute extraction per config.

        Args:
            entity_mention: EntityMention from erspec.

        Returns:
            Mention: Domain object with id and attributes.

        Raises:
            ValueError: If RDF parsing fails or entity type is unknown.
        """
        eid = entity_mention.identifiedBy
        entity_type_config = self._mappings.get(eid.entity_type)
        if entity_type_config is None:
            raise ValueError(
                f"No rdf_mapping configured for entity_type '{eid.entity_type}'"
            )

        mention_id = MentionId(
            value=self._derive_mention_id(
                eid.source_id, eid.request_id, eid.entity_type
            )
        )
        attributes = extract_mention_attributes(
            entity_mention.content, entity_type_config
        )
        return Mention(id=mention_id, attributes=attributes)

    @staticmethod
    def _derive_mention_id(source_id: str, request_id: str, entity_type: str) -> str:
        """
        Derive a stable MentionId from source_id, request_id, and entity_type.

        Per ERE spec section 4, the mention ID is deterministic and reproducible.
        """
        raw = source_id + request_id + entity_type
        mention_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        log.trace(
            "Deterministic ID assigned: %s for triad (%s, %s, %s)",
            mention_id,
            source_id,
            request_id,
            entity_type,
        )
        return mention_id
