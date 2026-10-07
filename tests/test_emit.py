import re

import pytest
from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from ttl2fabric.emit_tmdl import EmitError, render_definition, write_definition
from ttl2fabric.emit_ttl import QUALIFIED, build_graph


@pytest.fixture
def resolved(run):
    return run(skip_missing_tables=True, skip_missing_columns=True).ontology


def test_definition_layout(resolved):
    parts = render_definition(resolved, pinned_at="2026-01-01T00:00:00.000Z")
    assert set(parts) == {
        ".platform",
        "database.tmdl",
        "model.tmdl",
        "namespaces/default.tmdl",
        "expressions.tmdl",
        "tables/Customer.tmdl",
        "tables/Address.tmdl",
        "entities/Customer.tmdl",
        "entities/Address.tmdl",
        "relationships.tmdl",
        "entityRelationships.tmdl",
    }
    assert parts["database.tmdl"] == ["database", "\tcompatibilityLevel: 1000000", ""]
    assert parts["model.tmdl"][:2] == ["model Model", ""]
    expr = "\n".join(parts["expressions.tmdl"])
    assert "expression 'DirectLake - test_lakehouse' =" in expr
    assert "onelake.dfs.fabric.microsoft.com/11111111-1111-1111-1111-111111111111/22222222-2222" in expr


def test_table_and_entity_bindings(resolved):
    parts = render_definition(resolved, pinned_at="2026-01-01T00:00:00.000Z")
    table = "\n".join(parts["tables/Customer.tmdl"])
    entity = "\n".join(parts["entities/Customer.tmdl"])
    assert "\tcolumn 'case prop'\n\t\tdataType: string\n" in table
    assert "\t\tsourceColumn: case prop" in table
    assert "\tpartition Customer = entity\n\t\tmode: directLake\n\t\tsource\n\t\t\tentityName: customer__t\n" in table
    assert "\t\t\tschemaName: bronze" in table
    assert "\t\tannotation ONT_ItemKind = Lakehouse" in table
    assert entity.startswith("/// A party that buys services.\nentity Customer\n")
    assert "\tkeyProperty: 'Customer Key'" in entity
    assert "\tproperty 'Case Prop'\n\t\tdataType: string\n" in entity
    assert "\t\t\tvalueColumn: Customer.'case prop'" in entity
    assert "\tsynonym 'Account Holder'" in entity
    assert "\tannotation label = Customer" in entity
    assert "\tannotation classBusinessGrain = One row per customer." in entity
    # property lineageTag equals the backing column lineageTag (as in Fabric exports)
    col_tag = re.search(r"column 'case prop'\n\t\tdataType: string\n\t\tlineageTag: (\S+)", table).group(1)
    prop_tag = re.search(r"property 'Case Prop'\n\t\tdataType: string\n\t\tlineageTag: (\S+)", entity).group(1)
    assert col_tag == prop_tag
    # properties are ordinal-sorted
    props = re.findall(r"^\tproperty (.+)$", entity, flags=re.M)
    assert props == sorted(props, key=lambda p: p.strip("'"))


def test_relationship_parts(resolved):
    parts = render_definition(resolved, pinned_at="x")
    rel = "\n".join(parts["relationships.tmdl"])
    erel = "\n".join(parts["entityRelationships.tmdl"])
    assert "relationship Customer_HasMainAddress_Address\n\tfromColumn: Customer.'Main Address Key'\n" in rel
    assert "\ttoColumn: Address.'Address Key'" in rel
    assert "/// Identifies the main address of the customer.\nentityRelationship CustomerHasMainAddress\n" in erel
    assert "\t\trelationship: Customer_HasMainAddress_Address" in erel


def test_write_definition_crlf_and_deterministic(resolved, tmp_path):
    write_definition(resolved, tmp_path)
    first = {p.relative_to(tmp_path): p.read_bytes() for p in (tmp_path / "definition").rglob("*") if p.is_file()}
    assert b"\r\n" in first[(tmp_path / "definition/model.tmdl").relative_to(tmp_path)]
    write_definition(resolved, tmp_path)
    second = {p.relative_to(tmp_path): p.read_bytes() for p in (tmp_path / "definition").rglob("*") if p.is_file()}
    strip = lambda b: re.sub(rb"ONT_PinnedAtUtc = \S+", b"", b)  # noqa: E731
    assert {k: strip(v) for k, v in first.items()} == {k: strip(v) for k, v in second.items()}


def test_tmdl_requires_lakehouse_identity(run, source):
    from ttl2fabric.catalog import NullCatalog
    from ttl2fabric.resolver import Options, resolve

    res = resolve(source, NullCatalog(None), Options(), "T")
    with pytest.raises(EmitError):
        render_definition(res.ontology)


def test_ttl_matches_fabric_export_vocabulary(resolved):
    g = build_graph(resolved, "40cd8676-bd84-4581-9836-3c2752f0fe15")
    base = "https://fabric.microsoft.com/ontology/40cd8676-bd84-4581-9836-3c2752f0fe15"
    assert (URIRef(base), RDF.type, OWL.Ontology) in g
    customer = URIRef(base + "#Customer")
    assert (customer, RDF.type, OWL.Class) in g
    assert (customer, URIRef("urn:custom:classId"), None) in g
    shared = URIRef(base + "#Customer__Source%20System%20Id")
    assert (shared, QUALIFIED, None) in g and (shared, RDFS.domain, customer) in g
    unique = URIRef(base + "#Open%20Date")
    assert (unique, RDFS.range, XSD.dateTime) in g and (unique, QUALIFIED, None) not in g
    rel = URIRef(base + "#CustomerHasMainAddress")
    assert (rel, RDF.type, OWL.ObjectProperty) in g
    assert (rel, RDFS.range, URIRef(base + "#Address")) in g
    Graph().parse(data=g.serialize(format="turtle"), format="turtle")  # round-trips


def test_property_metadata_in_tmdl_and_ttl(resolved):
    entity = "\n".join(render_definition(resolved, pinned_at="x")["entities/Customer.tmdl"])
    block = (
        "\t/// Unique id of the customer.\n"
        "\tproperty 'Customer Key'\n"
        "\t\tdataType: string\n"
    )
    assert block in entity
    i = entity.index("\tproperty 'Customer Key'")
    j = entity.index("\tproperty ", i + 1)
    prop = entity[i:j]
    assert "\t\tbackingConfiguration\n\t\t\tvalueColumn: Customer.'Customer Key'\n\n\t\tannotation physicalDataPropertyName = customerKey\n" in prop
    assert "\t\tannotation sqlDataType = VARCHAR(256)\n" in prop

    g = build_graph(resolved, "40cd8676-bd84-4581-9836-3c2752f0fe15")
    base = "https://fabric.microsoft.com/ontology/40cd8676-bd84-4581-9836-3c2752f0fe15"
    key = URIRef(base + "#Customer%20Key")
    assert (key, URIRef("urn:custom:sqlDataType"), None) in g
    assert (key, RDFS.comment, None) in g
