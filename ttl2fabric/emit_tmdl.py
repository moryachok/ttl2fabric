"""Emit the Fabric Ontology v2 item definition (TMDL parts) — layout mirrors a live getDefinition."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .model import Entity, ResolvedOntology
from .naming import lineage_tag, sanitize_identifier, single_line, tmdl_name, tmdl_ref

PLATFORM_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json"


class EmitError(RuntimeError):
    pass


def expression_name(onto: ResolvedOntology) -> str:
    return f"DirectLake - {onto.lakehouse.lakehouse_name}"


def _description(text: Optional[str], indent: str = "") -> list[str]:
    if not text:
        return []
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    return [f"{indent}/// {ln}" for ln in lines]


def _column_tag(onto: ResolvedOntology, entity: Entity, column: str) -> str:
    return lineage_tag(onto.name, "column", entity.name, column)


def render_table(onto: ResolvedOntology, e: Entity, pinned_at: str) -> list[str]:
    lh = onto.lakehouse
    out = [f"table {tmdl_name(e.name)}", f"\tlineageTag: {lineage_tag(onto.name, 'table', e.name)}", ""]
    columns = {p.column: p.data_type for p in e.properties}
    for col, dtype in e.hidden_columns:
        columns.setdefault(col, dtype)
    for col in sorted(columns):
        out += [
            f"\tcolumn {tmdl_name(col)}",
            f"\t\tdataType: {columns[col]}",
            f"\t\tlineageTag: {_column_tag(onto, e, col)}",
            f"\t\tsourceColumn: {col}",
            "",
        ]
    out += [
        f"\tpartition {tmdl_name(e.name)} = entity",
        "\t\tmode: directLake",
        "\t\tsource",
        f"\t\t\tentityName: {e.table_name}",
        f"\t\t\tschemaName: {e.schema or 'dbo'}",
        f"\t\t\texpressionSource: {tmdl_name(expression_name(onto))}",
        "",
    ]
    annotations = [
        ("ONT_WorkspaceId", lh.workspace_id),
        ("ONT_ItemId", lh.lakehouse_id),
        ("ONT_ItemKind", "Lakehouse"),
        ("ONT_ItemName", lh.lakehouse_name),
        ("ONT_WorkspaceName", lh.workspace_name),
        ("ONT_SqlEndpoint", lh.sql_endpoint),
        ("ONT_SqlDatabase", lh.lakehouse_name),
        ("ONT_PinnedAtUtc", pinned_at),
    ]
    for key, value in annotations:
        if value:
            out += [f"\t\tannotation {key} = {value}", ""]
    return out


def render_entity(onto: ResolvedOntology, e: Entity) -> list[str]:
    out = _description(e.description)
    out += [
        f"entity {tmdl_name(e.name)}",
        f"\tlineageTag: {lineage_tag(onto.name, 'entity', e.name)}",
        f"\tbackingTable: {tmdl_name(e.name)}",
        f"\tkeyProperty: {tmdl_name(e.key_property)}",
        "",
    ]
    for p in sorted(e.properties, key=lambda p: p.name):
        out += _description(p.description, "\t")
        out += [
            f"\tproperty {tmdl_name(p.name)}",
            f"\t\tdataType: {p.data_type}",
            f"\t\tlineageTag: {_column_tag(onto, e, p.column)}",
            "",
            "\t\tbackingConfiguration",
            f"\t\t\tvalueColumn: {tmdl_ref(e.name, p.column)}",
            "",
        ]
        for key, value in p.annotations.items():
            out += [f"\t\tannotation {sanitize_identifier(key, 'A')} = {single_line(value)}", ""]
    for syn in e.synonyms:
        out += [f"\tsynonym {tmdl_name(syn)}", ""]
    for key, value in e.annotations.items():
        out += [f"\tannotation {sanitize_identifier(key, 'A')} = {single_line(value)}", ""]
    return out


def render_relationships(onto: ResolvedOntology) -> list[str]:
    out: list[str] = []
    for r in onto.relationships:
        out += [
            f"relationship {tmdl_name(r.table_relationship_name)}",
            f"\tfromColumn: {tmdl_ref(r.from_entity, r.from_column)}",
            f"\ttoColumn: {tmdl_ref(r.to_entity, r.to_column)}",
            "",
        ]
    return out


def render_entity_relationships(onto: ResolvedOntology) -> list[str]:
    out: list[str] = []
    for r in onto.relationships:
        out += _description(r.description)
        out += [
            f"entityRelationship {tmdl_name(r.name)}",
            f"\tlabel: {r.name}",
            f"\tlineageTag: {lineage_tag(onto.name, 'relationship', r.name)}",
            f"\tfromEntity: {tmdl_name(r.from_entity)}",
            f"\ttoEntity: {tmdl_name(r.to_entity)}",
            "",
            "\tbackingConfiguration",
            f"\t\trelationship: {tmdl_name(r.table_relationship_name)}",
            "",
        ]
    return out


def render_model(onto: ResolvedOntology) -> list[str]:
    out = ["model Model", ""]
    out += [f"ref table {tmdl_name(e.name)}" for e in onto.entities] + [""]
    out += [f"ref entity {tmdl_name(e.name)}" for e in onto.entities] + [""]
    out += ["ref namespace default", ""]
    return out


def render_expressions(onto: ResolvedOntology) -> list[str]:
    lh = onto.lakehouse
    url = f"https://onelake.dfs.fabric.microsoft.com/{lh.workspace_id}/{lh.lakehouse_id}"
    return [
        f"expression {tmdl_name(expression_name(onto))} =",
        "\t\tlet",
        f'\t\t    Source = AzureStorage.DataLake("{url}", [HierarchicalNavigation=true])',
        "\t\tin",
        "\t\t    Source",
        f"\tlineageTag: {lineage_tag(onto.name, 'expression', lh.lakehouse_id)}",
        "",
    ]


def platform_json(display_name: str) -> str:
    return json.dumps(
        {
            "$schema": PLATFORM_SCHEMA,
            "metadata": {"type": "Ontology", "displayName": display_name},
            "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"},
        },
        indent=2,
    )


def render_definition(onto: ResolvedOntology, pinned_at: Optional[str] = None) -> dict[str, list[str] | str]:
    """Return {part path: lines (TMDL) or text (.platform)}."""
    if onto.lakehouse is None or not onto.lakehouse.lakehouse_id or not onto.lakehouse.workspace_id:
        raise EmitError(
            "TMDL output needs the lakehouse identity: pass --workspace/--lakehouse (live), a --catalog-file "
            "produced by 'ttl2fabric catalog', or --workspace-id/--lakehouse-id/--lakehouse-name"
        )
    if not onto.entities:
        raise EmitError("Nothing to emit: every entity was skipped (see skipped.csv)")
    pinned_at = pinned_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"
    parts: dict[str, list[str] | str] = {
        ".platform": platform_json(onto.name),
        "database.tmdl": ["database", "\tcompatibilityLevel: 1000000", ""],
        "model.tmdl": render_model(onto),
        "namespaces/default.tmdl": ["namespace default", "\tlineageTag: default", ""],
        "expressions.tmdl": render_expressions(onto),
    }
    for e in onto.entities:
        parts[f"tables/{e.name}.tmdl"] = render_table(onto, e, pinned_at)
        parts[f"entities/{e.name}.tmdl"] = render_entity(onto, e)
    if onto.relationships:
        parts["relationships.tmdl"] = render_relationships(onto)
        parts["entityRelationships.tmdl"] = render_entity_relationships(onto)
    lowered = [p.lower() for p in parts]
    if len(set(lowered)) != len(lowered):
        raise EmitError("Two parts map to the same file path (case-insensitive); check entity names")
    return parts


def write_definition(onto: ResolvedOntology, out_dir: Path, newline: str = "\r\n") -> list[Path]:
    parts = render_definition(onto)
    target = out_dir / "definition"
    if target.exists():
        shutil.rmtree(target)
    written: list[Path] = []
    for rel, content in parts.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        text = content if isinstance(content, str) else newline.join(content) + newline
        path.write_bytes(text.encode("utf-8"))
        written.append(path)
    return written
