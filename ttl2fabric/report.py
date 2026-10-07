"""Skip log, findings and summary report."""

from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .model import Kind, PropertyOrigin, SourceOntology
from .resolver import Resolution

log = logging.getLogger(__name__)

SKIP_FIELDS = ["kind", "entity", "name", "reason", "detail", "source_iri"]
FINDING_FIELDS = ["code", "kind", "entity", "name", "detail"]


def write_reports(
    out_dir: Path,
    src: SourceOntology,
    res: Resolution,
    input_path: str,
    options: dict[str, Any],
    outputs: list[str],
    definition_sha256: Optional[str] = None,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "skipped.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SKIP_FIELDS)
        writer.writeheader()
        for s in res.skips:
            writer.writerow(s.as_dict())
    with open(out_dir / "skipped.jsonl", "w", encoding="utf-8") as fh:
        for s in res.skips:
            fh.write(json.dumps(s.as_dict(), ensure_ascii=False) + "\n")
    with open(out_dir / "findings.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FINDING_FIELDS)
        writer.writeheader()
        for f in res.findings:
            writer.writerow(f.as_dict())

    onto = res.ontology
    lh = onto.lakehouse
    report = {
        "tool": f"ttl2fabric {__version__}",
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input": input_path,
        "ontologyName": onto.name,
        "verified": res.verified,
        "unresolved": len(res.unresolved),
        "definitionSha256": definition_sha256,
        "lakehouse": None
        if lh is None
        else {
            "workspaceId": lh.workspace_id,
            "workspaceName": lh.workspace_name,
            "lakehouseId": lh.lakehouse_id,
            "lakehouseName": lh.lakehouse_name,
        },
        "options": options,
        "source": {
            "classes": len(src.classes),
            "classesWithPhysicalTable": sum(1 for c in src.classes.values() if c.physical_table),
            "dataProperties": sum(len(c.data_properties) for c in src.classes.values()),
            "objectProperties": len(src.object_properties),
            "notConverted": src.unconverted,
        },
        "output": {
            "files": outputs,
            "entities": len(onto.entities),
            "properties": sum(len(e.properties) for e in onto.entities),
            "foreignKeyProperties": sum(
                1 for e in onto.entities for p in e.properties if p.origin == PropertyOrigin.FOREIGN_KEY
            ),
            "unmappedColumnProperties": sum(
                1 for e in onto.entities for p in e.properties if p.origin == PropertyOrigin.UNMAPPED
            ),
            "relationships": len(onto.relationships),
        },
        "skipped": {
            "total": len(res.skips),
            "byKind": dict(Counter(s.kind.value for s in res.skips)),
            "byKindAndReason": {
                f"{k}:{r}": n for (k, r), n in sorted(Counter((s.kind.value, s.reason.value) for s in res.skips).items())
            },
        },
        "findings": {"total": len(res.findings), "byCode": dict(sorted(Counter(f.code for f in res.findings).items()))},
        "entities": [
            {
                "name": e.name,
                "table": f"{e.schema}.{e.table_name}",
                "key": e.key_property,
                "properties": len(e.properties),
            }
            for e in onto.entities
        ],
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def print_summary(report: dict, out_dir: Path) -> None:
    o, s = report["output"], report["skipped"]
    source_name = (report.get("lakehouse") or {}).get("lakehouseName") or "catalog file"
    lines = [
        "",
        "=" * 72,
        f" ttl2fabric — {report['ontologyName']}",
        "=" * 72,
        f" Physical validation : {'yes (' + source_name + ')' if report['verified'] else 'NO (bindings not verified)'}",
        f" Entities            : {o['entities']}",
        f" Properties          : {o['properties']}  (FK: {o['foreignKeyProperties']}, unmapped: {o['unmappedColumnProperties']})",
        f" Relationships       : {o['relationships']}",
        f" Skipped             : {s['total']}  " + ", ".join(f"{k}={v}" for k, v in sorted(s["byKind"].items())),
    ]
    for key, n in sorted(s["byKindAndReason"].items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"     {n:>6}  {key}")
    if report["findings"]["total"]:
        lines.append(f" Findings            : {report['findings']['total']}")
        for code, n in report["findings"]["byCode"].items():
            lines.append(f"     {n:>6}  {code}")
    if report["unresolved"]:
        lines.append(
            f" UNRESOLVED bindings : {report['unresolved']}  -> fix them or rerun with "
            "--skip-missing-tables/--skip-missing-columns (deploy refuses unresolved output)"
        )
    lines += [f" Output              : {out_dir}", "   " + "\n   ".join(o["files"]), "=" * 72]
    print("\n".join(lines))


__all__ = ["write_reports", "print_summary", "Kind"]
