"""Intermediate representation shared by the parser, resolver and emitters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Source model (what the input TTL says)
# ---------------------------------------------------------------------------


@dataclass
class SourceDataProperty:
    iri: str
    local_name: str
    label: str
    domain_iri: str
    xsd_range: Optional[str]  # local name of the xsd datatype, e.g. "string", "date"
    physical_name: Optional[str]
    sql_type: Optional[str] = None
    comment: Optional[str] = None
    annotations: dict[str, str] = field(default_factory=dict)


@dataclass
class SourceObjectProperty:
    iri: str
    local_name: str
    label: str
    domain_iri: Optional[str]
    range_iri: Optional[str]
    join_condition: Optional[str]
    comment: Optional[str] = None
    cardinality: Optional[str] = None
    annotations: dict[str, str] = field(default_factory=dict)


@dataclass
class SourceClass:
    iri: str
    local_name: str
    label: str
    comment: Optional[str]
    physical_table: Optional[str]
    primary_key: list[str]  # labels of the key properties (composite keys have >1)
    synonyms: list[str]
    annotations: dict[str, str]  # ordered: annotation local name -> value
    data_properties: list[SourceDataProperty] = field(default_factory=list)


@dataclass
class SourceOntology:
    iri: Optional[str]
    label: Optional[str]
    vocab_ns: str
    classes: dict[str, SourceClass]  # keyed by IRI, insertion ordered as in the file
    object_properties: list[SourceObjectProperty]
    unconverted: dict[str, int] = field(default_factory=dict)  # e.g. enumeration sets, business terms


# ---------------------------------------------------------------------------
# Resolved model (what will be emitted)
# ---------------------------------------------------------------------------


class PropertyOrigin(str, Enum):
    DATA = "data"  # owl:DatatypeProperty in the input
    FOREIGN_KEY = "fk"  # FK column taken from an object property joinCondition
    UNMAPPED = "unmapped"  # physical column not described by the input TTL


@dataclass
class Property:
    name: str
    column: str  # physical column name in the source table
    data_type: str  # TMDL data type: string | int64 | double | dateTime | boolean
    origin: PropertyOrigin
    description: Optional[str] = None
    source_iri: Optional[str] = None
    annotations: dict[str, str] = field(default_factory=dict)


@dataclass
class Entity:
    name: str
    label: str
    source_iri: str
    table_name: str  # physical table name, e.g. customer__t
    schema: Optional[str]  # physical schema, e.g. bronze (None for schema-less lakehouses)
    key_property: str  # property name of the entity key
    description: Optional[str]
    synonyms: list[str]
    annotations: dict[str, str]
    properties: list[Property] = field(default_factory=list)
    # Physical columns needed by relationships but not exposed as entity properties: (column, data_type)
    hidden_columns: list[tuple[str, str]] = field(default_factory=list)

    def property_by_name(self, name: str) -> Optional[Property]:
        return next((p for p in self.properties if p.name == name), None)

    def property_by_column(self, column: str) -> Optional[Property]:
        return next((p for p in self.properties if p.column == column), None)


@dataclass
class Relationship:
    name: str  # entity relationship name, e.g. CustomerHasMainAddress
    table_relationship_name: str  # backing table relationship name
    source_iri: str
    from_entity: str
    to_entity: str
    from_column: str
    to_column: str
    description: Optional[str] = None
    label: Optional[str] = None


@dataclass
class LakehouseRef:
    workspace_id: str
    workspace_name: str
    lakehouse_id: str
    lakehouse_name: str
    sql_endpoint: Optional[str] = None
    schema_enabled: bool = False


@dataclass
class ResolvedOntology:
    name: str
    lakehouse: Optional[LakehouseRef]
    entities: list[Entity]
    relationships: list[Relationship]

    def entity(self, name: str) -> Optional[Entity]:
        return next((e for e in self.entities if e.name == name), None)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


class Kind(str, Enum):
    ENTITY = "entity"
    PROPERTY = "property"
    RELATIONSHIP = "relationship"


class Reason(str, Enum):
    # entity
    FILTERED_OUT = "FILTERED_OUT"
    NO_PHYSICAL_TABLE = "NO_PHYSICAL_TABLE"
    TABLE_NOT_FOUND = "TABLE_NOT_FOUND"
    TABLE_CASE_MISMATCH = "TABLE_CASE_MISMATCH"
    TABLE_AMBIGUOUS = "TABLE_AMBIGUOUS"
    TABLE_SCHEMA_UNREADABLE = "TABLE_SCHEMA_UNREADABLE"
    KEY_NOT_DEFINED = "KEY_NOT_DEFINED"
    COMPOSITE_KEY_UNSUPPORTED = "COMPOSITE_KEY_UNSUPPORTED"
    KEY_PROPERTY_NOT_FOUND = "KEY_PROPERTY_NOT_FOUND"
    KEY_COLUMN_UNRESOLVED = "KEY_COLUMN_UNRESOLVED"
    KEY_TYPE_UNSUPPORTED = "KEY_TYPE_UNSUPPORTED"
    DUPLICATE_ENTITY_NAME = "DUPLICATE_ENTITY_NAME"
    INVALID_NAME = "INVALID_NAME"
    # property
    COLUMN_NOT_FOUND = "COLUMN_NOT_FOUND"
    COLUMN_CASE_MISMATCH = "COLUMN_CASE_MISMATCH"
    COLUMN_AMBIGUOUS = "COLUMN_AMBIGUOUS"
    UNSUPPORTED_COLUMN_TYPE = "UNSUPPORTED_COLUMN_TYPE"
    DUPLICATE_PROPERTY_NAME = "DUPLICATE_PROPERTY_NAME"
    DUPLICATE_COLUMN = "DUPLICATE_COLUMN"
    ENTITY_SKIPPED = "ENTITY_SKIPPED"
    # relationship
    SOURCE_ENTITY_SKIPPED = "SOURCE_ENTITY_SKIPPED"
    TARGET_ENTITY_SKIPPED = "TARGET_ENTITY_SKIPPED"
    MISSING_DOMAIN_OR_RANGE = "MISSING_DOMAIN_OR_RANGE"
    JOIN_CONDITION_MISSING = "JOIN_CONDITION_MISSING"
    JOIN_CONDITION_UNPARSEABLE = "JOIN_CONDITION_UNPARSEABLE"
    JOIN_TABLE_MISMATCH = "JOIN_TABLE_MISMATCH"
    SELF_RELATIONSHIP = "SELF_RELATIONSHIP"
    FK_COLUMN_UNRESOLVED = "FK_COLUMN_UNRESOLVED"
    TARGET_COLUMN_UNRESOLVED = "TARGET_COLUMN_UNRESOLVED"
    TARGET_COLUMN_NOT_KEY = "TARGET_COLUMN_NOT_KEY"
    FK_TYPE_MISMATCH = "FK_TYPE_MISMATCH"
    DUPLICATE_RELATIONSHIP_PAIR = "DUPLICATE_RELATIONSHIP_PAIR"


@dataclass
class SkipRecord:
    kind: Kind
    name: str
    entity: Optional[str]
    reason: Reason
    detail: str
    source_iri: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "entity": self.entity or "",
            "name": self.name,
            "reason": self.reason.value,
            "detail": self.detail,
            "source_iri": self.source_iri or "",
        }


@dataclass
class Finding:
    """A non-fatal observation (the item is still emitted)."""

    code: str
    kind: Kind
    entity: Optional[str]
    name: str
    detail: str

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "kind": self.kind.value,
            "entity": self.entity or "",
            "name": self.name,
            "detail": self.detail,
        }
