"""Unit tests for adapters.factories: RDFMapper construction."""

from pathlib import Path

from ere.adapters.factories import build_rdf_mapper
from ere.adapters.rdf_mapper_port import RDFMapper

TEST_RDF_MAPPING = Path(__file__).parent.parent.parent / "resources" / "rdf_mapping.yaml"


def test_build_rdf_mapper_with_explicit_path_returns_mapper():
    mapper = build_rdf_mapper(rdf_mapping_path=TEST_RDF_MAPPING)
    assert isinstance(mapper, RDFMapper)


def test_build_rdf_mapper_without_path_uses_default():
    mapper = build_rdf_mapper()
    assert isinstance(mapper, RDFMapper)
