"""RDF mapper port interface (abstract base class).

This ABC defines the contract for RDF extraction and mention mapping.
The resolution service layer depends only on this port, not on concrete
implementations. This enables testing with different RDF formats and
swapping extraction strategies without changing service logic.
"""

from abc import ABC, abstractmethod

from erspec.models.core import EntityMention

from ere.models.resolver import Mention


class RDFMapper(ABC):
    """
    Port: abstract interface for RDF extraction and entity mention mapping.

    Responsibilities:
    - Parse RDF content (Turtle, RDF/XML, etc.)
    - Extract entity attributes
    - Map erspec EntityMention to domain Mention
    """

    @abstractmethod
    def map_entity_mention_to_domain(self, entity_mention: EntityMention) -> Mention:
        """
        Map EntityMention (erspec) to Mention (domain).

        Performs RDF parsing and attribute extraction per configuration.

        Args:
            entity_mention: EntityMention with identifiedBy and content (RDF).

        Returns:
            Mention: Domain object with id and attributes.

        Raises:
            ValueError: If RDF parsing fails or entity type is unknown.
        """
