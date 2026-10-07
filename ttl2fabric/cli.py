"""Command line interface: convert | catalog | deploy."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .catalog import Catalog, CatalogError, FabricCatalog, FileCatalog, NullCatalog
from .deploy import DeployError, definition_fingerprint, deploy
from .emit_tmdl import EmitError, write_definition
from .emit_ttl import default_ontology_id, write_ttl
from .fabric_client import FabricClient, FabricError
from .model import LakehouseRef
from .naming import sanitize_identifier
from .parser import parse_ttl
from .report import print_summary, write_reports
from .resolver import Options, resolve

log = logging.getLogger("ttl2fabric")

EXIT_OK, EXIT_ERROR, EXIT_UNRESOLVED = 0, 1, 2

EPILOG = """examples:
  # pure conversion (no Fabric access, bindings not verified)
  python -m ttl2fabric convert inputs/model.ttl --name TelcoMain --workspace-id <ws-guid> \\
      --lakehouse-id <lh-guid> --lakehouse-name ontology_lakehouse --schema bronze

  # validate against the lakehouse and skip anything that does not physically exist
  python -m ttl2fabric convert inputs/model.ttl --name TelcoMain --workspace customers \\
      --lakehouse ontology_lakehouse --schema bronze --skip-missing-tables --skip-missing-columns

  # create the ontology item from the generated folder
  python -m ttl2fabric deploy build/TelcoMain --workspace customers
"""


def _csv_list(values: Optional[list[str]]) -> list[str]:
    out: list[str] = []
    for v in values or []:
        out += [x.strip() for x in v.split(",") if x.strip()]
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ttl2fabric",
        description="Convert an OWL/Turtle ontology into a Fabric IQ Ontology (v2) item definition.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    p.add_argument("--version", action="version", version=f"ttl2fabric {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    common.add_argument("--log-file", help="Log file (default: <output>/ttl2fabric.log for convert)")
    common.add_argument(
        "--auth", default="cli", choices=["cli", "default", "interactive"], help="Azure credential (default: az CLI)"
    )
    sub = p.add_subparsers(dest="command", required=True)

    # ---------------------------------------------------------------- convert
    c = sub.add_parser(
        "convert",
        parents=[common],
        help="Convert a TTL file to a Fabric Ontology definition (TMDL) and Fabric-style TTL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    c.add_argument("input", help="Input ontology (.ttl; .rdf/.owl/.nt/.jsonld also accepted)")
    c.add_argument("-o", "--output", help="Output folder (default: build/<name>)")
    c.add_argument("--name", help="Ontology item display name (default: derived from the ontology label)")
    c.add_argument("--format", choices=["both", "tmdl", "ttl"], default="both", help="Outputs to write (default: both)")
    c.add_argument("--ontology-id", help="GUID used in the TTL base IRI (default: deterministic from --name)")
    c.add_argument("--vocab-ns", help="Namespace of the annotation vocabulary (default: auto-detect)")

    g = c.add_argument_group("physical source (lakehouse)")
    g.add_argument("--workspace", help="Workspace name or id — enables live validation via Fabric APIs")
    g.add_argument("--lakehouse", help="Lakehouse name or id (with --workspace)")
    g.add_argument("--catalog-file", help="Offline catalog: JSON from 'ttl2fabric catalog' or INFORMATION_SCHEMA CSV")
    g.add_argument("--schema", action="append", help="Schema(s) to search, in priority order (repeat or comma-separate)")
    g.add_argument("--workspace-id", help="Override/offline: workspace GUID for bindings")
    g.add_argument("--workspace-name", help="Override/offline: workspace name (annotation only)")
    g.add_argument("--lakehouse-id", help="Override/offline: lakehouse GUID for bindings")
    g.add_argument("--lakehouse-name", help="Override/offline: lakehouse name (used in the DirectLake expression)")
    g.add_argument("--sql-endpoint", help="Override/offline: lakehouse SQL endpoint host (annotation only)")

    v = c.add_argument_group("validation & skipping (need --workspace/--lakehouse or --catalog-file)")
    v.add_argument(
        "--skip-missing-tables",
        action="store_true",
        help="Skip entities whose table is missing (and their properties + relationships)",
    )
    v.add_argument(
        "--skip-missing-columns",
        action="store_true",
        help="Skip properties (and relationships) whose column is missing or does not match",
    )
    v.add_argument(
        "--case-sensitive",
        action="store_true",
        help="Treat names that differ only by letter case as NOT matching (skipped with --skip-missing-*)",
    )
    v.add_argument(
        "--fuzzy-columns", action="store_true", help="Also match names ignoring spaces/underscores/punctuation"
    )
    v.add_argument(
        "--strict", action="store_true", help="Shortcut for --skip-missing-tables --skip-missing-columns --case-sensitive"
    )

    m = c.add_argument_group("modelling")
    m.add_argument("--entities", action="append", help="Only these classes (local name, label or table; comma list)")
    m.add_argument("--exclude-entities", action="append", help="Exclude these classes (comma list)")
    m.add_argument("--subject-areas", action="append", help="Only classes in these subjectArea values (comma list)")
    m.add_argument(
        "--entity-naming",
        choices=["local", "label"],
        default="local",
        help="Entity names from the class local name (FinancialAccount, default) or label (Financial Account)",
    )
    m.add_argument(
        "--column-naming",
        choices=["label", "physical"],
        default="label",
        help="Which TTL name is tried first as the column name: rdfs:label (default) or physicalDataPropertyName",
    )
    m.add_argument("--no-fk-properties", action="store_true", help="Do not expose FK columns as entity properties")
    m.add_argument(
        "--include-unmapped-columns",
        action="store_true",
        help="Also add physical columns that the TTL does not describe as properties",
    )
    m.add_argument("--allow-self-relationships", action="store_true", help="Keep relationships from an entity to itself")
    m.add_argument(
        "--one-relationship-per-pair",
        action="store_true",
        help="Keep only the first relationship between the same two entities",
    )
    m.add_argument(
        "--allow-any-key-type",
        action="store_true",
        help="Allow entity keys that are not string/int64 (e.g. a date key); skipped by default",
    )
    m.add_argument(
        "--no-property-descriptions",
        action="store_true",
        help="Do not emit property descriptions (rdfs:comment); emitted by default",
    )
    m.add_argument(
        "--no-property-annotations",
        action="store_true",
        help="Do not emit property 'Additional metadata' (sqlDataType, synonyms, ...); emitted by default",
    )
    m.add_argument(
        "--annotation-exclude",
        action="append",
        help="Metadata keys to leave out of entities and properties, e.g. dataPropertyId,classId (comma list)",
    )

    # ---------------------------------------------------------------- catalog
    k = sub.add_parser("catalog", parents=[common], help="Dump lakehouse tables + columns to JSON (for offline use)")
    k.add_argument("--workspace", required=True, help="Workspace name or id")
    k.add_argument("--lakehouse", required=True, help="Lakehouse name or id")
    k.add_argument("--schema", action="append", help="Only these schemas (repeat or comma-separate)")
    k.add_argument("--tables", action="append", help="Only these tables (comma list)")
    k.add_argument("-o", "--output", default="catalog.json", help="Output JSON file (default: catalog.json)")

    # ----------------------------------------------------------------- deploy
    d = sub.add_parser(
        "deploy", parents=[common], help="Create an Ontology item from a generated folder (or replace an existing one)"
    )
    d.add_argument("path", help="Output folder of 'convert' (or its definition/ subfolder)")
    d.add_argument("--workspace", required=True, help="Target workspace name or id")
    d.add_argument("--name", help="Display name (default: from definition/.platform)")
    d.add_argument("--description", help="Item description")
    d.add_argument("--yes", action="store_true", help="Skip the interactive confirmation")
    d.add_argument("--force", action="store_true", help="Deploy even if the report lists unresolved bindings")
    d.add_argument("--dry-run", action="store_true", help="Only write envelope.json; do not call Fabric")
    d.add_argument(
        "--folder",
        help="Workspace folder for the item: path like 'data-services/ontology', a folder id, or '/' for "
        "the workspace root. With --update-existing the item is moved there if needed",
    )
    d.add_argument("--create-folder", action="store_true", help="Create missing folders of --folder")
    d.add_argument(
        "--update-existing",
        action="store_true",
        help="If an Ontology with this name exists, replace its definition (backup + preview + confirmation; "
        "keeps the item id and the lineageTags of matching entities/properties)",
    )
    return p


def setup_logging(level: str, log_file: Optional[Path]) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(getattr(logging, level))
    console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root.addHandler(console)
    if log_file:
        add_file_log(log_file)
    for noisy in ("azure", "urllib3", "rdflib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def add_file_log(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    logging.getLogger().addHandler(fh)


def _lakehouse_overrides(args, base: Optional[LakehouseRef]) -> Optional[LakehouseRef]:
    if not any([args.workspace_id, args.lakehouse_id, args.lakehouse_name, args.workspace_name, args.sql_endpoint]):
        return base
    ref = base or LakehouseRef("", "", "", "")
    return dataclasses.replace(
        ref,
        workspace_id=args.workspace_id or ref.workspace_id,
        workspace_name=args.workspace_name or ref.workspace_name,
        lakehouse_id=args.lakehouse_id or ref.lakehouse_id,
        lakehouse_name=args.lakehouse_name or ref.lakehouse_name or "lakehouse",
        sql_endpoint=args.sql_endpoint or ref.sql_endpoint,
    )


def cmd_convert(args) -> int:
    if args.strict:
        args.skip_missing_tables = args.skip_missing_columns = args.case_sensitive = True
    setup_logging(args.log_level, None)
    src = parse_ttl(args.input, vocab_ns=args.vocab_ns)
    name = args.name or sanitize_identifier(src.label or Path(args.input).stem, "Ontology")
    out_dir = Path(args.output or Path("build") / name)
    add_file_log(Path(args.log_file) if args.log_file else out_dir / "ttl2fabric.log")
    log.info("ttl2fabric %s — converting %s -> %s", __version__, args.input, out_dir)

    schemas = _csv_list(args.schema)
    catalog: Catalog
    if args.catalog_file:
        catalog = FileCatalog(args.catalog_file)
    elif args.workspace or args.lakehouse:
        if not (args.workspace and args.lakehouse):
            raise SystemExit("error: --workspace and --lakehouse must be used together")
        catalog = FabricCatalog(FabricClient(auth=args.auth), args.workspace, args.lakehouse, schemas)
    else:
        catalog = NullCatalog(None)
        if args.skip_missing_tables or args.skip_missing_columns or args.case_sensitive or args.fuzzy_columns:
            raise SystemExit(
                "error: validation/skip flags need a physical catalog: pass --workspace/--lakehouse or --catalog-file"
            )
        log.warning("No physical catalog: bindings are generated from the TTL and NOT verified")
    catalog.lakehouse = _lakehouse_overrides(args, catalog.lakehouse)

    opts = Options(
        include_entities=_csv_list(args.entities),
        exclude_entities=_csv_list(args.exclude_entities),
        subject_areas=_csv_list(args.subject_areas),
        schemas=schemas,
        skip_missing_tables=args.skip_missing_tables,
        skip_missing_columns=args.skip_missing_columns,
        case_sensitive=args.case_sensitive,
        fuzzy_columns=args.fuzzy_columns,
        fk_properties=not args.no_fk_properties,
        include_unmapped_columns=args.include_unmapped_columns,
        allow_self_relationships=args.allow_self_relationships,
        one_relationship_per_pair=args.one_relationship_per_pair,
        allow_any_key_type=args.allow_any_key_type,
        entity_naming=args.entity_naming,
        column_naming=args.column_naming,
        property_descriptions=not args.no_property_descriptions,
        property_annotations=not args.no_property_annotations,
        annotation_exclude=_csv_list(args.annotation_exclude),
    )
    res = resolve(src, catalog, opts, name)

    outputs: list[str] = []
    definition = out_dir / "definition"
    definition_sha: Optional[str] = None
    if definition.exists():
        shutil.rmtree(definition)  # never leave a previous run's definition around to be deployed by mistake
    if args.format in ("both", "tmdl"):
        try:
            write_definition(res.ontology, out_dir)
            definition_sha = definition_fingerprint(definition)
            outputs.append("definition/ (TMDL item definition)")
        except EmitError as exc:
            if args.format == "tmdl":
                raise
            log.warning("TMDL definition not written: %s", exc)
    if args.format in ("both", "ttl") and res.ontology.entities:
        write_ttl(res.ontology, out_dir / f"{name}.ttl", args.ontology_id or default_ontology_id(name))
        outputs.append(f"{name}.ttl")
    if catalog.verified:
        (out_dir / "catalog.json").write_text(json.dumps(catalog.to_json(), indent=2), encoding="utf-8")
        outputs.append("catalog.json")
    outputs += ["report.json", "skipped.csv", "skipped.jsonl", "findings.csv", "ttl2fabric.log"]
    options = {k: v for k, v in dataclasses.asdict(opts).items()}
    report = write_reports(out_dir, src, res, args.input, options, outputs, definition_sha)
    print_summary(report, out_dir)
    return EXIT_UNRESOLVED if res.unresolved else EXIT_OK


def cmd_catalog(args) -> int:
    setup_logging(args.log_level, Path(args.log_file) if args.log_file else None)
    cat = FabricCatalog(FabricClient(auth=args.auth), args.workspace, args.lakehouse, _csv_list(args.schema))
    only = {t.casefold() for t in _csv_list(args.tables)}
    keys = [(s, t) for s in cat.schemas() for t in cat._tables[s] if not only or t.casefold() in only]
    errors = cat.prefetch(keys)
    for (s, t), err in errors.items():
        log.warning("Columns unavailable for %s: %s", f"{s}.{t}" if s else t, err)
    Path(args.output).write_text(json.dumps(cat.to_json(), indent=2), encoding="utf-8")
    print(f"Wrote {args.output}: {len(keys)} tables ({len(errors)} without column metadata)")
    return EXIT_OK


def cmd_deploy(args) -> int:
    setup_logging(args.log_level, Path(args.log_file) if args.log_file else None)
    deploy(
        FabricClient(auth=args.auth),
        Path(args.path),
        args.workspace,
        display_name=args.name,
        description=args.description,
        yes=args.yes,
        force=args.force,
        dry_run=args.dry_run,
        update_existing=args.update_existing,
        folder=args.folder,
        create_folder=args.create_folder,
    )
    return EXIT_OK


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        commands = {"convert": cmd_convert, "catalog": cmd_catalog, "deploy": cmd_deploy}
        return commands[args.command](args)
    except (FabricError, CatalogError, EmitError, DeployError, FileNotFoundError, ValueError) as exc:
        logging.getLogger("ttl2fabric").error("%s", exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130
