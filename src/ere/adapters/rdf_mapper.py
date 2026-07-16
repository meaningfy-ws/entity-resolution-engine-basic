"""Config-driven RDF Turtle parser and entity attribute mapper."""

from pathlib import Path
from typing import Any

import yaml
from rdflib import Graph, Namespace, RDF, URIRef


def load_entity_mappings(yaml_path: str | Path) -> dict[str, dict[str, Any]]:
    """
    Load entity type mappings from rdf_mapping.yaml.

    Returns:
        Dict keyed by entity_type string (e.g. "ORGANISATION") ->
          {"rdf_type": URIRef, "fields": {field_name: [URIRef, ...]}}
        where each value in "fields" is a list of resolved URIRefs (property path steps).
    """
    yaml_path = Path(yaml_path)
    with open(yaml_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Build namespace prefix registry
    namespaces_config = config.get("namespaces", {})
    namespace_registry = {
        prefix: Namespace(uri) for prefix, uri in namespaces_config.items()
    }

    # Resolve entity type mappings
    entity_types_config = config.get("entity_types", {})
    resolved_mappings = {}

    for entity_type, mapping in entity_types_config.items():
        rdf_type_str = mapping["rdf_type"]
        rdf_type_uri = _resolve_prefixed_uri(rdf_type_str, namespace_registry)

        # Resolve field property paths
        field_mappings = {}
        for field_name, path_str in mapping["fields"].items():
            # Handle null fields (e.g. country_code for PROCEDURE)
            if path_str is None:
                field_mappings[field_name] = None
            else:
                # Split on "/" and resolve each segment
                steps = path_str.split("/")
                resolved_steps = [
                    _resolve_prefixed_uri(step, namespace_registry) for step in steps
                ]
                field_mappings[field_name] = resolved_steps

        resolved_mappings[entity_type] = {
            "rdf_type": rdf_type_uri,
            "fields": field_mappings,
        }

    return resolved_mappings


def extract_mention_attributes(
    content: str, entity_type_config: dict[str, Any]
) -> dict[str, str | None]:
    """
    Parse Turtle RDF content and extract attribute dict per config-specified property paths.

    Args:
        content: Turtle RDF string (must be non-empty).
        entity_type_config: Dict with "rdf_type" (URIRef) and "fields" (dict of field -> [URIRef, ...]).

    Returns:
        Dict mapping field names to their extracted values (strings or None if not found).

    Raises:
        ValueError: If content is empty, malformed, or no entity of rdf_type is found.
    """
    if not content or not content.strip():
        raise ValueError("RDF content is empty or whitespace-only")

    # Parse Turtle
    graph = Graph()
    try:
        graph.parse(data=content, format="turtle")
    except Exception as exc:
        raise ValueError(f"Failed to parse RDF Turtle: {exc}") from exc

    # Find the subject with the target RDF type
    rdf_type = entity_type_config["rdf_type"]
    entity_subject = graph.value(predicate=RDF.type, object=rdf_type)

    if entity_subject is None:
        raise ValueError(f"No entity of type {rdf_type} found in RDF content")

    # Extract attributes per config
    attributes = {}
    for field_name, path_steps in entity_type_config["fields"].items():
        # Skip null field mappings (e.g. country_code for PROCEDURE)
        if path_steps is None:
            attributes[field_name] = None
            continue

        current = entity_subject
        for predicate in path_steps:
            if current is None:
                break
            current = graph.value(current, predicate)

        # Convert to string if found
        if current is not None:
            attributes[field_name] = str(current)
        else:
            attributes[field_name] = None

    return attributes


def _resolve_prefixed_uri(prefixed_str: str, namespace_registry: dict) -> URIRef:
    """
    Resolve a prefixed URI string like "org:Organization" to a URIRef.

    Args:
        prefixed_str: String like "prefix:localName" or just "localName".
        namespace_registry: Dict of prefix -> Namespace objects.

    Returns:
        Resolved URIRef.

    Raises:
        ValueError: If prefix is not in the registry.
    """
    if ":" in prefixed_str:
        prefix, local_name = prefixed_str.split(":", 1)
        if prefix not in namespace_registry:
            raise ValueError(f"Unknown namespace prefix: {prefix}")
        namespace = namespace_registry[prefix]
        return URIRef(namespace[local_name])
    else:
        # Bare local name - return as-is (should not happen in practice)
        return URIRef(prefixed_str)
