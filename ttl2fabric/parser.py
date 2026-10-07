"""Parse an OWL/Turtle ontology (custom class/property annotations) into the source IR."""

from __future__ import annotations

import logging
import re
from typing import Optional

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from .model import SourceClass, SourceDataProperty, SourceObjectProperty, SourceOntology

log = logging.getLogger(__name__)

# Annotation local names (in the vocabulary namespace) with structural meaning.
A_PHYSICAL_CLASS = "physicalClassName"
A_PRIMARY_KEY = "primaryKey"
A_SYNONYMS = "synonyms"
A_PHYSICAL_PROPERTY = "physicalDataPropertyName"
A_SQL_TYPE = "sqlDataType"
A_JOIN = "joinCondition"
A_CARDINALITY = "cardinality"
A_ENUM_VALUE = "hasEnumerationValue"

# Order used for entity annotations (others follow alphabetically).
CLASS_ANNOTATION_ORDER = [
    "classId",
    "subjectArea",
    "classRootDomain",
    "classParentDomain",
    "sidClass",
    "classBusinessGrain",
    "classNaturalGrain",
    "primaryKey",
    "classHierarchyName",
    "classHierarchyLevel",
    "classType",
    "physicalClassName",
]

# Order used for property annotations (others follow alphabetically).
PROPERTY_ANNOTATION_ORDER = [
    "dataPropertyId",
    "physicalDataPropertyName",
    "subjectArea",
    "sqlDataType",
    "classification",
    "aggregationAllowedFlag",
    "measureClassification",
    "timeRole",
    "mandatoryOptionalInd",
    "synonyms",
    "glossaryTerm",
    "businessRule",
    "enumerationValues",
]
A_ENUM_LITERAL = "enumerationValue"


def order_annotations(annotations: dict[str, str], order: list[str]) -> dict[str, str]:
    ordered = {k: annotations[k] for k in order if k in annotations}
    ordered.update({k: annotations[k] for k in sorted(annotations) if k not in ordered})
    return ordered


def local_name(iri: str) -> str:
    return re.split(r"[#/]", str(iri))[-1]


def _text(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _split_list(value: Optional[str], separators: str) -> list[str]:
    if not value:
        return []
    seen: dict[str, None] = {}
    for item in re.split(f"[{re.escape(separators)}]", value):
        item = item.strip()
        if item and item.lower() not in {k.lower() for k in seen}:
            seen[item] = None
    return list(seen)


def detect_vocab_namespace(graph: Graph, ontology_iri: Optional[str]) -> str:
    for prefix, ns in graph.namespaces():
        if prefix == "":
            return str(ns)
    for candidate in graph.subjects(RDF.type, OWL.AnnotationProperty):
        name = str(candidate)
        if local_name(name) == A_PHYSICAL_CLASS:
            return name[: -len(A_PHYSICAL_CLASS)]
    if ontology_iri:
        return ontology_iri.rstrip("#/") + "#"
    raise ValueError("Cannot detect the vocabulary namespace; pass --vocab-ns explicitly")


def parse_ttl(path: str, vocab_ns: Optional[str] = None, rdf_format: Optional[str] = None) -> SourceOntology:
    graph = Graph()
    log.info("Parsing %s ...", path)
    graph.parse(path, format=rdf_format or _guess_format(path))
    log.info("Parsed %d triples", len(graph))

    ontology_iri = next((str(s) for s in graph.subjects(RDF.type, OWL.Ontology) if isinstance(s, URIRef)), None)
    ontology_label = _text(graph.value(URIRef(ontology_iri), RDFS.label)) if ontology_iri else None
    ns = vocab_ns or detect_vocab_namespace(graph, ontology_iri)
    log.info("Vocabulary namespace: %s", ns)

    def ann(subject, name: str) -> Optional[str]:
        return _text(graph.value(subject, URIRef(ns + name)))

    # Meta classes (EnumerationValue, BusinessTerm, ...) have instances; domain classes in a TBox do not.
    meta_classes = {
        c
        for c in graph.subjects(RDF.type, OWL.Class)
        if isinstance(c, URIRef) and any(True for _ in graph.subjects(RDF.type, c))
    }

    classes: dict[str, SourceClass] = {}
    for c in sorted((c for c in graph.subjects(RDF.type, OWL.Class) if isinstance(c, URIRef)), key=str):
        if c in meta_classes:
            continue
        annotations: dict[str, str] = {}
        for pred, obj in graph.predicate_objects(c):
            p = str(pred)
            if p.startswith(ns) and isinstance(obj, Literal):
                name = p[len(ns) :]
                if name != A_SYNONYMS:
                    annotations[name] = str(obj).strip()
        ordered = order_annotations(annotations, CLASS_ANNOTATION_ORDER)
        label = _text(graph.value(c, RDFS.label)) or local_name(c)
        classes[str(c)] = SourceClass(
            iri=str(c),
            local_name=local_name(c),
            label=label,
            comment=_text(graph.value(c, RDFS.comment)),
            physical_table=ann(c, A_PHYSICAL_CLASS),
            primary_key=_split_list(ann(c, A_PRIMARY_KEY), ",+"),
            synonyms=_split_list(ann(c, A_SYNONYMS), ";"),
            annotations=ordered,
        )

    unconverted: dict[str, int] = {}
    if meta_classes:
        unconverted["meta classes (" + ", ".join(sorted(local_name(m) for m in meta_classes)) + ")"] = len(meta_classes)
        instances = sum(1 for m in meta_classes for s in graph.subjects(RDF.type, m) if isinstance(s, URIRef))
        if instances:
            unconverted["named individuals of meta classes (e.g. business terms)"] = instances

    enum_props = 0
    for p in sorted(graph.subjects(RDF.type, OWL.DatatypeProperty), key=str):
        if not isinstance(p, URIRef):
            continue
        domains = [d for d in graph.objects(p, RDFS.domain) if str(d) in classes]
        if not domains:
            unconverted["datatype properties without a known domain class"] = (
                unconverted.get("datatype properties without a known domain class", 0) + 1
            )
            continue
        if any(True for _ in graph.objects(p, URIRef(ns + A_ENUM_VALUE))):
            enum_props += 1
        rng = graph.value(p, RDFS.range)
        annotations = {
            str(pred)[len(ns) :]: str(obj).strip()
            for pred, obj in graph.predicate_objects(p)
            if str(pred).startswith(ns) and isinstance(obj, Literal) and str(obj).strip()
        }
        enum_values = _enumeration_values(graph, p, ns)
        if enum_values:
            annotations["enumerationValues"] = "; ".join(enum_values)
        annotations = order_annotations(annotations, PROPERTY_ANNOTATION_ORDER)
        for d in domains:
            classes[str(d)].data_properties.append(
                SourceDataProperty(
                    iri=str(p),
                    local_name=local_name(p),
                    label=_text(graph.value(p, RDFS.label)) or local_name(p),
                    domain_iri=str(d),
                    xsd_range=local_name(rng) if rng is not None else None,
                    physical_name=ann(p, A_PHYSICAL_PROPERTY),
                    sql_type=ann(p, A_SQL_TYPE),
                    comment=_text(graph.value(p, RDFS.comment)),
                    annotations=annotations,
                )
            )
    if enum_props:
        unconverted["enumeration value definitions/synonyms (values kept as 'enumerationValues' annotation)"] = enum_props

    object_properties: list[SourceObjectProperty] = []
    for p in sorted(graph.subjects(RDF.type, OWL.ObjectProperty), key=str):
        if not isinstance(p, URIRef):
            continue
        dom = graph.value(p, RDFS.domain)
        rng = graph.value(p, RDFS.range)
        object_properties.append(
            SourceObjectProperty(
                iri=str(p),
                local_name=local_name(p),
                label=_text(graph.value(p, RDFS.label)) or local_name(p),
                domain_iri=str(dom) if isinstance(dom, URIRef) else None,
                range_iri=str(rng) if isinstance(rng, URIRef) else None,
                join_condition=ann(p, A_JOIN),
                comment=_text(graph.value(p, RDFS.comment)),
                cardinality=ann(p, A_CARDINALITY),
                annotations={
                    str(pred)[len(ns) :]: str(obj).strip()
                    for pred, obj in graph.predicate_objects(p)
                    if str(pred).startswith(ns) and isinstance(obj, Literal)
                },
            )
        )

    for props in classes.values():
        props.data_properties.sort(key=lambda dp: (dp.label, dp.iri))

    log.info(
        "Source: %d classes (%d with a physical table), %d datatype properties, %d object properties",
        len(classes),
        sum(1 for c in classes.values() if c.physical_table),
        sum(len(c.data_properties) for c in classes.values()),
        len(object_properties),
    )
    return SourceOntology(
        iri=ontology_iri,
        label=ontology_label,
        vocab_ns=ns,
        classes=classes,
        object_properties=object_properties,
        unconverted=unconverted,
    )


def _enumeration_values(graph: Graph, prop, ns: str) -> list[str]:
    """Permitted values from `:hasEnumerationValue [ :enumerationValue "x" ]`, in document-independent order."""
    values: list[str] = []
    for node in graph.objects(prop, URIRef(ns + A_ENUM_VALUE)):
        value = _text(graph.value(node, URIRef(ns + A_ENUM_LITERAL))) or _text(graph.value(node, RDFS.label))
        if value is None and isinstance(node, Literal):
            value = _text(node)
        if value and value not in values:
            values.append(value)
    return sorted(values, key=str.casefold)


def _guess_format(path: str) -> str:
    lowered = path.lower()
    if lowered.endswith((".rdf", ".owl", ".xml")):
        return "xml"
    if lowered.endswith((".nt",)):
        return "nt"
    if lowered.endswith((".jsonld", ".json")):
        return "json-ld"
    return "turtle"


__all__ = ["parse_ttl", "local_name", "BNode"]
