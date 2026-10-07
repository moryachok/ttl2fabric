"""Emit the ontology as Turtle in the vocabulary Fabric Ontology v2 exports (see a v2 TTL export)."""

from __future__ import annotations

import uuid
from collections import Counter
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from .model import ResolvedOntology

QUALIFIED = URIRef("urn:fabric-internal:qualifiedPropertyName")
_XSD = {
    "string": XSD.string,
    "int64": XSD.integer,
    "double": XSD.double,
    "dateTime": XSD.dateTime,
    "boolean": XSD.boolean,
}


def default_ontology_id(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ttl2fabric:{name}"))


def build_graph(onto: ResolvedOntology, ontology_id: Optional[str] = None) -> Graph:
    base = f"https://fabric.microsoft.com/ontology/{ontology_id or default_ontology_id(onto.name)}"
    fab = Namespace(base + "#")
    g = Graph()
    for prefix, ns in (("rdf", RDF), ("rdfs", RDFS), ("xsd", XSD), ("owl", OWL), ("skos", SKOS), ("fabric", fab)):
        g.bind(prefix, ns, override=True)

    def iri(local: str) -> URIRef:
        return URIRef(base + "#" + quote(local, safe=""))

    g.add((URIRef(base), RDF.type, OWL.Ontology))
    g.add((URIRef(base), RDFS.label, Literal(onto.name)))

    usage = Counter(p.name for e in onto.entities for p in e.properties)
    for e in onto.entities:
        cls = iri(e.name)
        g.add((cls, RDF.type, OWL.Class))
        g.add((cls, RDFS.label, Literal(e.name)))
        if e.description:
            g.add((cls, RDFS.comment, Literal(e.description)))
        for syn in e.synonyms:
            g.add((cls, SKOS.altLabel, Literal(syn)))
        for key, value in e.annotations.items():
            g.add((cls, URIRef(f"urn:custom:{key}"), Literal(value)))
        for p in e.properties:
            qualified = usage[p.name] > 1
            prop = URIRef(base + "#" + quote(f"{e.name}__{p.name}" if qualified else p.name, safe=""))
            g.add((prop, RDF.type, OWL.DatatypeProperty))
            g.add((prop, RDFS.domain, cls))
            g.add((prop, RDFS.label, Literal(p.name)))
            g.add((prop, RDFS.range, _XSD.get(p.data_type, XSD.string)))
            if p.description:
                g.add((prop, RDFS.comment, Literal(p.description)))
            for key, value in p.annotations.items():
                g.add((prop, URIRef(f"urn:custom:{key}"), Literal(value)))
            if qualified:
                g.add((prop, QUALIFIED, Literal("true")))

    for r in onto.relationships:
        rel = iri(r.name)
        g.add((rel, RDF.type, OWL.ObjectProperty))
        g.add((rel, RDFS.label, Literal(r.name)))
        g.add((rel, RDFS.domain, iri(r.from_entity)))
        g.add((rel, RDFS.range, iri(r.to_entity)))
        if r.description:
            g.add((rel, RDFS.comment, Literal(r.description)))
    return g


def write_ttl(onto: ResolvedOntology, path: Path, ontology_id: Optional[str] = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    build_graph(onto, ontology_id).serialize(destination=str(path), format="turtle", encoding="utf-8")
    return path
