"""Bind the source ontology to physical tables, applying validation and skip policies."""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .catalog import Catalog, Column, delta_to_tmdl, xsd_to_tmdl
from .model import (
    Entity,
    Finding,
    Kind,
    Property,
    PropertyOrigin,
    Reason,
    Relationship,
    ResolvedOntology,
    SkipRecord,
    SourceClass,
    SourceDataProperty,
    SourceObjectProperty,
    SourceOntology,
)
from .naming import (
    acronyms_from_labels,
    humanize,
    relationship_name,
    sanitize_identifier,
    single_line,
    unique_name,
)

log = logging.getLogger(__name__)

KEY_TYPES = {"string", "int64"}
_JOIN = re.compile(r"^\s*([\w$#]+(?:\.[\w$#]+)*)\.([\w$#]+)\s*=\s*([\w$#]+(?:\.[\w$#]+)*)\.([\w$#]+)\s*$")
_LABEL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]*$")
_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")


@dataclass
class Options:
    include_entities: list[str] = field(default_factory=list)
    exclude_entities: list[str] = field(default_factory=list)
    subject_areas: list[str] = field(default_factory=list)
    schemas: list[str] = field(default_factory=list)
    skip_missing_tables: bool = False
    skip_missing_columns: bool = False
    case_sensitive: bool = False
    fuzzy_columns: bool = False
    fk_properties: bool = True
    include_unmapped_columns: bool = False
    allow_self_relationships: bool = False
    one_relationship_per_pair: bool = False
    allow_any_key_type: bool = False
    entity_naming: str = "local"  # local | label
    column_naming: str = "label"  # label | physical  (candidate priority + guess when unverified)
    property_descriptions: bool = True
    property_annotations: bool = True
    annotation_exclude: list[str] = field(default_factory=list)  # annotation keys never emitted


@dataclass
class Resolution:
    ontology: ResolvedOntology
    skips: list[SkipRecord]
    findings: list[Finding]
    verified: bool

    @property
    def unresolved(self) -> list[Finding]:
        return [f for f in self.findings if f.code.startswith("UNRESOLVED")]


@dataclass
class Match:
    status: str  # exact | case | fuzzy | case_mismatch | ambiguous | not_found
    column: Optional[Column] = None
    candidate: Optional[str] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("exact", "case", "fuzzy")


def _norm(text: str, keep_case: bool) -> str:
    out = _NON_ALNUM.sub("", text)
    return out if keep_case else out.lower()


def match_column(candidates: list[str], columns: list[Column], case_sensitive: bool, fuzzy: bool) -> Match:
    """Tiered matching: exact -> case-insensitive -> (opt-in) ignoring spaces/punctuation."""
    cands = [c for c in dict.fromkeys(candidates) if c]
    shown = " / ".join(repr(c) for c in cands)
    for cand in cands:
        for col in columns:
            if col.name == cand:
                return Match("exact", col, cand)
    for cand in cands:
        hits = [col for col in columns if col.name.casefold() == cand.casefold()]
        if len(hits) > 1:
            return Match("ambiguous", None, cand, f"{cand!r} matches several columns ignoring case: {[h.name for h in hits]}")
        if hits:
            if case_sensitive:
                return Match(
                    "case_mismatch", hits[0], cand, f"{cand!r} differs only by letter case from column {hits[0].name!r}"
                )
            return Match("case", hits[0], cand, f"{cand!r} matched column {hits[0].name!r} ignoring case")
    for cand in cands:
        hits = [col for col in columns if _norm(col.name, case_sensitive) == _norm(cand, case_sensitive)]
        loose = [col for col in columns if _norm(col.name, False) == _norm(cand, False)]
        if fuzzy and len(hits) > 1:
            return Match("ambiguous", None, cand, f"{cand!r} fuzzily matches several columns: {[h.name for h in hits]}")
        if fuzzy and hits:
            return Match("fuzzy", hits[0], cand, f"{cand!r} matched column {hits[0].name!r} ignoring spaces/punctuation")
        if fuzzy and loose and case_sensitive:
            return Match(
                "case_mismatch", loose[0], cand, f"{cand!r} matches column {loose[0].name!r} only when ignoring case"
            )
        if loose:
            return Match(
                "not_found",
                None,
                cand,
                f"no column named {shown}; closest by spelling: {loose[0].name!r} (use --fuzzy-columns to accept)",
            )
    return Match("not_found", None, None, f"no column named {shown}")


class _Resolver:
    def __init__(self, src: SourceOntology, catalog: Optional[Catalog], opts: Options, name: str):
        self.src = src
        self.catalog = catalog
        self.o = opts
        self.name = name
        self.verified = bool(catalog is not None and catalog.verified)
        self.skips: list[SkipRecord] = []
        self.findings: list[Finding] = []
        labels = [dp.label for c in src.classes.values() for dp in c.data_properties]
        self.acronyms = acronyms_from_labels(labels + [c.label for c in src.classes.values()])
        counter: dict[str, Counter] = defaultdict(Counter)
        for c in src.classes.values():
            for dp in c.data_properties:
                if dp.physical_name:
                    counter[dp.physical_name.casefold()][dp.label] += 1
        self.phys_label = {k: v.most_common(1)[0][0] for k, v in counter.items()}
        self.entities: list[Entity] = []
        self.by_iri: dict[str, Entity] = {}
        self.columns: dict[str, Optional[list[Column]]] = {}
        self.dp_of: dict[tuple[str, str], SourceDataProperty] = {}
        self.entity_skip_reason: dict[str, str] = {}
        self._item_findings_start: Optional[int] = None

    # ----------------------------------------------------------- recording
    def skip(self, kind: Kind, name: str, entity: Optional[str], reason: Reason, detail: str, iri: Optional[str] = None):
        self.skips.append(SkipRecord(kind, name, entity, reason, detail, iri))
        level = logging.INFO if kind == Kind.ENTITY else logging.DEBUG
        where = f"{entity}.{name}" if entity and kind == Kind.PROPERTY else name
        log.log(level, "SKIP %-12s %-40s %s: %s", kind.value, where, reason.value, detail)

    def find(self, code: str, kind: Kind, entity: Optional[str], name: str, detail: str):
        self.findings.append(Finding(code, kind, entity, name, detail))
        log.debug("NOTE %s %s %s: %s", code, entity or "", name, detail)

    def skip_entity(self, cls: SourceClass, ename: str, reason: Reason, detail: str, cascade: bool = True):
        if self._item_findings_start is not None:
            del self.findings[self._item_findings_start :]  # notes about an item that is not emitted are void
        self.skip(Kind.ENTITY, ename, ename, reason, detail, cls.iri)
        self.entity_skip_reason[cls.iri] = f"{reason.value}: {detail}"
        if not cascade:
            return
        already = {s.name.casefold() for s in self.skips if s.kind == Kind.PROPERTY and s.entity == ename}
        for dp in cls.data_properties:
            if dp.label.casefold() not in already:
                self.skip(
                    Kind.PROPERTY, dp.label, ename, Reason.ENTITY_SKIPPED, f"entity {ename} skipped ({reason.value})", dp.iri
                )

    # ------------------------------------------------------------ entities
    def entity_name(self, cls: SourceClass) -> str:
        if self.o.entity_naming == "label" and _LABEL_NAME.match(cls.label.strip()):
            return cls.label.strip()
        return sanitize_identifier(cls.local_name, "Entity")

    def selected(self, cls: SourceClass, ename: str) -> Optional[str]:
        keys = {cls.local_name.casefold(), cls.label.casefold(), ename.casefold(), (cls.physical_table or "").casefold()}
        include = {x.casefold() for x in self.o.include_entities}
        exclude = {x.casefold() for x in self.o.exclude_entities}
        if include and not keys & include:
            return "not listed in --entities"
        if exclude and keys & exclude:
            return "listed in --exclude-entities"
        if self.o.subject_areas:
            area = cls.annotations.get("subjectArea", "")
            if area.casefold() not in {x.casefold() for x in self.o.subject_areas}:
                return f"subject area {area!r} not in --subject-areas"
        return None

    def resolve_entities(self) -> None:
        classes = sorted(self.src.classes.values(), key=lambda c: c.local_name)
        names = {c.iri: self.entity_name(c) for c in classes}
        lookups = {}
        if self.verified:
            for c in classes:
                if c.physical_table and not self.selected(c, names[c.iri]):
                    lookups[c.iri] = self.catalog.find_table(c.physical_table, self.o.schemas)
            wanted = [(lk.schema, lk.name) for lk in lookups.values() if lk.status in ("found", "case")]
            log.info("Reading column metadata for %d table(s) ...", len(set(wanted)))
            self.table_errors = self.catalog.prefetch(wanted)
        taken: set[str] = set()
        for cls in classes:
            ename = names[cls.iri]
            self._item_findings_start = len(self.findings)
            why = self.selected(cls, ename)
            if why:
                self.skip_entity(cls, ename, Reason.FILTERED_OUT, why, cascade=False)
                continue
            if not cls.physical_table:
                self.skip_entity(cls, ename, Reason.NO_PHYSICAL_TABLE, "class has no physicalClassName annotation")
                continue
            if not cls.primary_key:
                self.skip_entity(cls, ename, Reason.KEY_NOT_DEFINED, "class has no primaryKey annotation")
                continue
            if len(cls.primary_key) > 1:
                self.skip_entity(
                    cls,
                    ename,
                    Reason.COMPOSITE_KEY_UNSUPPORTED,
                    f"composite key ({', '.join(cls.primary_key)}); add a single surrogate key column",
                )
                continue
            if ename.casefold() in taken:
                self.skip_entity(cls, ename, Reason.DUPLICATE_ENTITY_NAME, f"another class already maps to {ename!r}")
                continue
            located = self.locate_table(cls, ename, lookups.get(cls.iri))
            if located is None:
                continue
            schema, table, columns = located
            entity = self.build_entity(cls, ename, schema, table, columns)
            if entity is None:
                continue
            taken.add(ename.casefold())
            self.entities.append(entity)
            self.by_iri[cls.iri] = entity
            self.columns[ename] = columns
        self._item_findings_start = None

    def locate_table(self, cls: SourceClass, ename: str, lk) -> Optional[tuple[str, str, Optional[list[Column]]]]:
        default_schema = self.o.schemas[0] if self.o.schemas else "dbo"
        if not self.verified:
            return default_schema, cls.physical_table, None
        where = ", ".join(self.o.schemas) if self.o.schemas else "any schema"
        if lk.status == "ambiguous":
            self.skip_entity(
                cls, ename, Reason.TABLE_AMBIGUOUS, f"table {cls.physical_table!r} exists in {lk.candidates}; pass --schema"
            )
            return None
        if lk.status == "not_found" or (lk.status == "case" and self.o.case_sensitive):
            if lk.status == "case":
                reason = Reason.TABLE_CASE_MISMATCH
                detail = f"table {cls.physical_table!r} differs only by letter case from {lk.schema}.{lk.name}"
            else:
                reason = Reason.TABLE_NOT_FOUND
                detail = f"table {cls.physical_table!r} not found in {where}"
            if self.o.skip_missing_tables:
                self.skip_entity(cls, ename, reason, detail)
                return None
            self.find("UNRESOLVED_TABLE", Kind.ENTITY, ename, ename, detail)
            return default_schema, cls.physical_table, None
        if lk.status == "case":
            self.find(
                "TABLE_CASE_RESOLVED", Kind.ENTITY, ename, ename, f"{cls.physical_table!r} bound to {lk.schema}.{lk.name}"
            )
        err = self.table_errors.get((lk.schema, lk.name))
        if err:
            if self.o.skip_missing_tables:
                self.skip_entity(cls, ename, Reason.TABLE_SCHEMA_UNREADABLE, err)
                return None
            self.find("UNRESOLVED_TABLE_SCHEMA", Kind.ENTITY, ename, ename, err)
            return lk.schema or "dbo", lk.name, None
        return lk.schema or "dbo", lk.name, self.catalog.columns(lk.schema, lk.name)

    def candidates(self, label: str, physical: Optional[str]) -> list[str]:
        pair = [label, physical] if self.o.column_naming == "label" else [physical, label]
        return [c for c in dict.fromkeys(pair) if c]

    def build_entity(
        self, cls: SourceClass, ename: str, schema: str, table: str, columns: Optional[list[Column]]
    ) -> Optional[Entity]:
        props: list[Property] = []
        for dp in cls.data_properties:
            fallback = xsd_to_tmdl(dp.xsd_range)
            cands = self.candidates(dp.label, dp.physical_name)
            if any(p.name.casefold() == dp.label.casefold() for p in props):
                self.skip(Kind.PROPERTY, dp.label, ename, Reason.DUPLICATE_PROPERTY_NAME, "label used twice", dp.iri)
                continue
            if columns is None:
                column, dtype = cands[0], fallback
            else:
                m = match_column(cands, columns, self.o.case_sensitive, self.o.fuzzy_columns)
                if not m.ok:
                    reason = {
                        "case_mismatch": Reason.COLUMN_CASE_MISMATCH,
                        "ambiguous": Reason.COLUMN_AMBIGUOUS,
                    }.get(m.status, Reason.COLUMN_NOT_FOUND)
                    detail = f"{m.detail} in {schema}.{table}"
                    if self.o.skip_missing_columns or m.status == "ambiguous":
                        self.skip(Kind.PROPERTY, dp.label, ename, reason, detail, dp.iri)
                        continue
                    self.find("UNRESOLVED_COLUMN", Kind.PROPERTY, ename, dp.label, detail)
                    column, dtype = cands[0], fallback
                else:
                    if m.status != "exact":
                        self.find(f"COLUMN_{m.status.upper()}_RESOLVED", Kind.PROPERTY, ename, dp.label, m.detail)
                    dtype = delta_to_tmdl(m.column.type)
                    if dtype is None:
                        self.skip(
                            Kind.PROPERTY,
                            dp.label,
                            ename,
                            Reason.UNSUPPORTED_COLUMN_TYPE,
                            f"column {m.column.name!r} has unsupported type {m.column.type}",
                            dp.iri,
                        )
                        continue
                    column = m.column.name
                    if dtype != fallback:
                        self.find(
                            "TYPE_FROM_TABLE",
                            Kind.PROPERTY,
                            ename,
                            dp.label,
                            f"TTL range xsd:{dp.xsd_range} -> table type {m.column.type} ({dtype})",
                        )
            owner = next((p for p in props if p.column == column), None)
            if owner is not None:
                self.skip(
                    Kind.PROPERTY,
                    dp.label,
                    ename,
                    Reason.DUPLICATE_COLUMN,
                    f"column {column!r} is already bound to property {owner.name!r}",
                    dp.iri,
                )
                continue
            props.append(
                Property(
                    name=dp.label,
                    column=column,
                    data_type=dtype,
                    origin=PropertyOrigin.DATA,
                    description=single_line(dp.comment) or None if self.o.property_descriptions else None,
                    source_iri=dp.iri,
                    annotations=self.property_annotations(dp),
                )
            )
            self.dp_of[(ename, dp.label)] = dp

        key_label = cls.primary_key[0]
        key = next((p for p in props if p.name.casefold() == key_label.casefold()), None)
        if key is None:
            was_skipped = any(
                s.kind == Kind.PROPERTY and s.entity == ename and s.name.casefold() == key_label.casefold()
                for s in self.skips
            )
            if was_skipped:
                self.skip_entity(cls, ename, Reason.KEY_COLUMN_UNRESOLVED, f"key property {key_label!r} could not be bound")
            else:
                self.skip_entity(
                    cls, ename, Reason.KEY_PROPERTY_NOT_FOUND, f"primaryKey {key_label!r} is not a data property of the class"
                )
            return None
        if key.data_type not in KEY_TYPES and not self.o.allow_any_key_type:
            self.skip_entity(
                cls,
                ename,
                Reason.KEY_TYPE_UNSUPPORTED,
                f"key {key.name!r} has type {key.data_type}; must be string or int64 (or use --allow-any-key-type)",
            )
            return None
        annotations = self.clean_annotations({"label": cls.label, **cls.annotations})
        return Entity(
            name=ename,
            label=cls.label,
            source_iri=cls.iri,
            table_name=table,
            schema=schema,
            key_property=key.name,
            description=cls.comment,
            synonyms=cls.synonyms,
            annotations=annotations,
            properties=props,
        )

    def clean_annotations(self, annotations: dict[str, str]) -> dict[str, str]:
        excluded = {k.casefold() for k in self.o.annotation_exclude}
        return {
            k: single_line(v) for k, v in annotations.items() if single_line(v) and k.casefold() not in excluded
        }

    def property_annotations(self, dp: SourceDataProperty) -> dict[str, str]:
        if not self.o.property_annotations:
            return {}
        return self.clean_annotations(dp.annotations)

    def fk_metadata(self, op: SourceObjectProperty, fk: str, target: str, target_column: str):
        """Description + annotations for an FK property, taken from the object property that defines it."""
        description = None
        if self.o.property_descriptions:
            description = f"Foreign key to {target} ({target_column})." + (f" {op.comment}" if op.comment else "")
        annotations: dict[str, str] = {}
        if self.o.property_annotations:
            annotations = self.clean_annotations(
                {
                    "physicalDataPropertyName": fk,
                    "classification": "Foreign Key",
                    "references": f"{target}.{target_column}",
                    "objectProperty": op.label,
                    "joinCondition": op.join_condition or "",
                    "cardinality": op.cardinality or "",
                    **{k: v for k, v in op.annotations.items() if k not in ("joinCondition", "cardinality")},
                }
            )
        return description, annotations

    # ------------------------------------------------------- relationships
    def resolve_relationships(self) -> list[Relationship]:
        rels: list[Relationship] = []
        rel_names: set[str] = set()
        table_rel_names: set[str] = set()
        pairs: set[tuple[str, str]] = set()
        classes = self.src.classes
        for op in sorted(self.src.object_properties, key=lambda p: p.local_name):
            rng_local = classes[op.range_iri].local_name if op.range_iri in classes else None
            rname = relationship_name(op.local_name, rng_local)
            start = len(self.findings)

            def rskip(reason: Reason, detail: str):
                del self.findings[start:]  # e.g. an UNRESOLVED_FK_COLUMN for a relationship that is dropped
                self.skip(Kind.RELATIONSHIP, rname, None, reason, detail, op.iri)

            if not op.domain_iri or not op.range_iri or op.domain_iri not in classes or op.range_iri not in classes:
                rskip(Reason.MISSING_DOMAIN_OR_RANGE, "domain/range missing or not a class of this ontology")
                continue
            src_cls, tgt_cls = classes[op.domain_iri], classes[op.range_iri]
            src_e, tgt_e = self.by_iri.get(op.domain_iri), self.by_iri.get(op.range_iri)
            if src_e is None:
                rskip(
                    Reason.SOURCE_ENTITY_SKIPPED,
                    f"source {src_cls.local_name} not created ({self.entity_skip_reason.get(op.domain_iri, 'skipped')})",
                )
                continue
            if tgt_e is None:
                rskip(
                    Reason.TARGET_ENTITY_SKIPPED,
                    f"target {tgt_cls.local_name} not created ({self.entity_skip_reason.get(op.range_iri, 'skipped')})",
                )
                continue
            if src_e is tgt_e and not self.o.allow_self_relationships:
                rskip(Reason.SELF_RELATIONSHIP, "source and target are the same entity (use --allow-self-relationships)")
                continue
            if not op.join_condition:
                rskip(Reason.JOIN_CONDITION_MISSING, "object property has no joinCondition")
                continue
            m = _JOIN.match(op.join_condition)
            if not m:
                rskip(Reason.JOIN_CONDITION_UNPARSEABLE, f"expected 'a.col = b.col', got {op.join_condition!r}")
                continue
            lt, lc, rt, rc = m.groups()
            oriented = self.orient(lt, lc, rt, rc, src_cls, tgt_cls, tgt_e)
            if oriented is None:
                rskip(
                    Reason.JOIN_TABLE_MISMATCH,
                    f"{op.join_condition!r} does not join {src_cls.physical_table} to {tgt_cls.physical_table}",
                )
                continue
            fk, pk = oriented
            key_prop = tgt_e.property_by_name(tgt_e.key_property)
            if not self.is_key(pk, tgt_e, key_prop):
                rskip(
                    Reason.TARGET_COLUMN_NOT_KEY,
                    f"join column {pk!r} is not the key {key_prop.name!r} of {tgt_e.name}",
                )
                continue
            fk_binding = self.bind_fk(src_e, fk, key_prop, rname, op.iri)
            if fk_binding is None:
                continue
            from_column, fk_type, fk_name = fk_binding
            if fk_type != key_prop.data_type:
                rskip(
                    Reason.FK_TYPE_MISMATCH,
                    f"{src_e.name}.{from_column} is {fk_type} but key {tgt_e.name}.{key_prop.column} is {key_prop.data_type}",
                )
                continue
            if self.o.one_relationship_per_pair and (src_e.name, tgt_e.name) in pairs:
                rskip(
                    Reason.DUPLICATE_RELATIONSHIP_PAIR,
                    f"{src_e.name} -> {tgt_e.name} already has a relationship (--one-relationship-per-pair)",
                )
                continue
            self.attach_fk(src_e, from_column, fk_type, fk_name, self.fk_metadata(op, fk, tgt_e.name, key_prop.name))
            pairs.add((src_e.name, tgt_e.name))
            name = unique_name(rname, rel_names)
            rel_names.add(name)
            tname = unique_name(sanitize_identifier(op.local_name, "Rel"), table_rel_names)
            table_rel_names.add(tname)
            if name != rname:
                self.find("RELATIONSHIP_RENAMED", Kind.RELATIONSHIP, None, rname, f"renamed to {name} (name collision)")
            rels.append(
                Relationship(
                    name=name,
                    table_relationship_name=tname,
                    source_iri=op.iri,
                    from_entity=src_e.name,
                    to_entity=tgt_e.name,
                    from_column=from_column,
                    to_column=key_prop.column,
                    description=op.comment,
                    label=op.label,
                )
            )
        return rels

    def orient(
        self, lt, lc, rt, rc, src_cls: SourceClass, tgt_cls: SourceClass, tgt_e: Entity
    ) -> Optional[tuple[str, str]]:
        def same(qualified: str, table: Optional[str]) -> bool:
            return bool(table) and qualified.split(".")[-1].casefold() == table.casefold()

        options = []
        if same(lt, src_cls.physical_table) and same(rt, tgt_cls.physical_table):
            options.append((lc, rc))
        if same(lt, tgt_cls.physical_table) and same(rt, src_cls.physical_table):
            options.append((rc, lc))
        if not options:
            return None
        if len(options) > 1:  # self relationship: the PK side is the one naming the key
            key_prop = tgt_e.property_by_name(tgt_e.key_property)
            options.sort(key=lambda o: not self.is_key(o[1], tgt_e, key_prop))
        return options[0]

    def is_key(self, pk: str, tgt_e: Entity, key_prop: Property) -> bool:
        dp = self.dp_of.get((tgt_e.name, key_prop.name))
        known = {_norm(key_prop.column, False), _norm(key_prop.name, False)}
        if dp is not None and dp.physical_name:
            known.add(_norm(dp.physical_name, False))
        return _norm(pk, False) in known

    def fk_candidates(self, fk: str) -> list[str]:
        label = self.phys_label.get(fk.casefold())
        human = humanize(fk, self.acronyms)
        ordered = [label, human, fk] if self.o.column_naming == "label" else [fk, label, human]
        return [c for c in dict.fromkeys(ordered) if c]

    def bind_fk(
        self, src_e: Entity, fk: str, key_prop: Property, rname: str, iri: str
    ) -> Optional[tuple[str, str, str]]:
        """Returns (column, data type, property name) for the FK column on the source entity."""
        for p in src_e.properties:
            dp = self.dp_of.get((src_e.name, p.name))
            if dp is not None and dp.physical_name and dp.physical_name.casefold() == fk.casefold():
                return p.column, p.data_type, p.name
        cands = self.fk_candidates(fk)
        for p in src_e.properties:
            if p.column in cands:
                return p.column, p.data_type, p.name
        name = self.phys_label.get(fk.casefold()) or humanize(fk, self.acronyms)
        columns = self.columns.get(src_e.name)
        if columns is None:
            return cands[0], key_prop.data_type, name
        m = match_column(cands, columns, self.o.case_sensitive, self.o.fuzzy_columns)
        if not m.ok:
            detail = f"FK {fk!r}: {m.detail} in {src_e.schema}.{src_e.table_name}"
            if self.o.skip_missing_columns or m.status == "ambiguous":
                self.skip(Kind.RELATIONSHIP, rname, None, Reason.FK_COLUMN_UNRESOLVED, detail, iri)
                return None
            self.find("UNRESOLVED_FK_COLUMN", Kind.RELATIONSHIP, src_e.name, rname, detail)
            return cands[0], key_prop.data_type, name
        if m.status != "exact":
            self.find(f"COLUMN_{m.status.upper()}_RESOLVED", Kind.RELATIONSHIP, src_e.name, rname, m.detail)
        dtype = delta_to_tmdl(m.column.type)
        if dtype is None:
            self.skip(
                Kind.RELATIONSHIP,
                rname,
                None,
                Reason.UNSUPPORTED_COLUMN_TYPE,
                f"FK column {m.column.name!r} has unsupported type {m.column.type}",
                iri,
            )
            return None
        return m.column.name, dtype, name

    def attach_fk(self, src_e: Entity, column: str, dtype: str, name: str, metadata=(None, None)) -> None:
        if src_e.property_by_column(column) is not None or any(c == column for c, _ in src_e.hidden_columns):
            return
        if not self.o.fk_properties:
            src_e.hidden_columns.append((column, dtype))
            return
        final = unique_name(name, {p.name for p in src_e.properties}, " ")
        if final != name:
            self.find("FK_PROPERTY_RENAMED", Kind.PROPERTY, src_e.name, name, f"renamed to {final!r} (name taken)")
        description, annotations = metadata
        src_e.properties.append(
            Property(final, column, dtype, PropertyOrigin.FOREIGN_KEY, description, annotations=annotations or {})
        )
        self.find("FK_PROPERTY_ADDED", Kind.PROPERTY, src_e.name, final, f"foreign-key column {column!r}")

    # ----------------------------------------------------- FK columns of skipped relationships
    def add_orphan_fk_columns(self) -> None:
        """Expose FK columns implied by joinConditions even when their relationship was skipped.

        Only in verified mode (the column must physically exist) so no binding is ever invented.
        """
        classes = self.src.classes
        for op in sorted(self.src.object_properties, key=lambda p: p.local_name):
            src_e = self.by_iri.get(op.domain_iri or "")
            columns = self.columns.get(src_e.name) if src_e else None
            if not src_e or not columns or not op.join_condition:
                continue
            m = _JOIN.match(op.join_condition)
            src_cls = classes[op.domain_iri]
            if not m:
                continue
            lt, lc, rt, rc = m.groups()
            table = (src_cls.physical_table or "").casefold()
            left, right = lt.split(".")[-1].casefold() == table, rt.split(".")[-1].casefold() == table
            if left and right:  # self relationship: the FK is the side that is not the key
                key = src_e.property_by_name(src_e.key_property)
                fk = lc if self.is_key(rc, src_e, key) else rc
            elif left:
                fk = lc
            elif right:
                fk = rc
            else:
                continue
            cands = self.fk_candidates(fk)
            if any(p.column in cands for p in src_e.properties):
                continue
            mc = match_column(cands, columns, self.o.case_sensitive, self.o.fuzzy_columns)
            dtype = delta_to_tmdl(mc.column.type) if mc.ok else None
            if dtype:
                tgt_cls = classes.get(op.range_iri or "")
                tgt_e = self.by_iri.get(op.range_iri or "")
                target = tgt_e.name if tgt_e else (tgt_cls.local_name if tgt_cls else "?")
                target_col = tgt_e.key_property if tgt_e else (tgt_cls.primary_key[0] if tgt_cls and tgt_cls.primary_key else "?")
                name = self.phys_label.get(fk.casefold()) or humanize(fk, self.acronyms)
                self.attach_fk(src_e, mc.column.name, dtype, name, self.fk_metadata(op, fk, target, target_col))

    # ----------------------------------------------------- unmapped columns
    def add_unmapped_columns(self) -> None:
        for e in self.entities:
            columns = self.columns.get(e.name)
            if not columns:
                continue
            bound = {p.column for p in e.properties} | {c for c, _ in e.hidden_columns}
            for col in columns:
                if col.name in bound:
                    continue
                dtype = delta_to_tmdl(col.type)
                if dtype is None:
                    self.find("UNMAPPED_COLUMN_UNSUPPORTED", Kind.PROPERTY, e.name, col.name, f"type {col.type}")
                    continue
                name = col.name if _LABEL_NAME.match(col.name) else humanize(col.name, self.acronyms)
                name = unique_name(name, {p.name for p in e.properties}, " ")
                e.properties.append(Property(name, col.name, dtype, PropertyOrigin.UNMAPPED))
                self.find("UNMAPPED_COLUMN_ADDED", Kind.PROPERTY, e.name, name, f"physical column {col.name!r}")

    def final_checks(self) -> None:
        for e in self.entities:
            key = e.property_by_name(e.key_property)
            assert key is not None, f"missing key on {e.name}"
            assert self.o.allow_any_key_type or key.data_type in KEY_TYPES, f"invalid key type on {e.name}"
            names = Counter(p.name.casefold() for p in e.properties)
            dup = [n for n, c in names.items() if c > 1]
            assert not dup, f"duplicate property names on {e.name}: {dup}"

    def run(self) -> Resolution:
        self.table_errors = {}
        self.resolve_entities()
        relationships = self.resolve_relationships()
        if self.o.fk_properties and self.verified:
            self.add_orphan_fk_columns()
        if self.o.include_unmapped_columns:
            self.add_unmapped_columns()
        self.final_checks()
        lakehouse = self.catalog.lakehouse if self.catalog is not None else None
        onto = ResolvedOntology(self.name, lakehouse, self.entities, relationships)
        return Resolution(onto, self.skips, self.findings, self.verified)


def resolve(src: SourceOntology, catalog: Optional[Catalog], opts: Options, name: str) -> Resolution:
    return _Resolver(src, catalog, opts, name).run()
