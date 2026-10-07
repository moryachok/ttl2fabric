"""Create a Fabric Ontology item from a generated definition folder, or replace an existing one's definition."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .fabric_client import FABRIC_API, FabricClient
from .folders import FolderError, describe, resolve_folder
from .mcp_endpoint import endpoint_lines, item_url, mcp_endpoint

log = logging.getLogger(__name__)


class DeployError(RuntimeError):
    pass


def definition_dir(path: Path) -> Path:
    candidate = path / "definition"
    if (candidate / "model.tmdl").exists():
        return candidate
    if (path / "model.tmdl").exists():
        return path
    raise DeployError(f"No TMDL definition found in {path} (expected {candidate}/model.tmdl)")


def load_parts(def_dir: Path) -> list[dict]:
    parts = []
    for file in sorted(p for p in def_dir.rglob("*") if p.is_file()):
        rel = file.relative_to(def_dir).as_posix()
        parts.append(
            {
                "path": rel,
                "payload": base64.b64encode(file.read_bytes()).decode("ascii"),
                "payloadType": "InlineBase64",
            }
        )
    return parts


def definition_fingerprint(def_dir: Path) -> str:
    """Hash of every part (path + bytes); ties a definition folder to the report.json of the same run."""
    digest = hashlib.sha256()
    for file in sorted(p for p in def_dir.rglob("*") if p.is_file()):
        digest.update(file.relative_to(def_dir).as_posix().encode("utf-8") + b"\0")
        digest.update(file.read_bytes() + b"\0")
    return digest.hexdigest()


def build_envelope(def_dir: Path, display_name: str, description: Optional[str] = None) -> dict:
    parts = load_parts(def_dir)
    for part in parts:
        if part["path"] == ".platform":  # keep the stored .platform consistent with the item name
            platform = json.loads(base64.b64decode(part["payload"]))
            platform.setdefault("metadata", {})["displayName"] = display_name
            part["payload"] = base64.b64encode(json.dumps(platform, indent=2).encode("utf-8")).decode("ascii")
    body = {"displayName": display_name, "type": "Ontology", "definition": {"parts": parts}}
    if description:
        body["description"] = description
    return body


def _platform_name(def_dir: Path) -> Optional[str]:
    platform = def_dir / ".platform"
    if platform.exists():
        return (json.loads(platform.read_text(encoding="utf-8")).get("metadata") or {}).get("displayName")
    return None


_HEADER = re.compile(
    r"^(\t*)(table|column|entity|property|entityRelationship|expression|namespace|relationship|partition)\s+"
    r"('(?:[^']|'')*'|[^\s=]+)"
)
_LINEAGE = re.compile(r"^(\t*)lineageTag:\s*(\S+)\s*$")


def _tmdl_objects(text: str) -> dict[str, str]:
    """Map 'kind name/kind name' object paths to their lineageTag for one TMDL part."""
    tags: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for raw in text.replace("\r", "").split("\n"):
        header = _HEADER.match(raw)
        if header:
            depth = len(header.group(1))
            stack = [entry for entry in stack if entry[0] < depth]
            name = header.group(3)
            if name.startswith("'"):
                name = name[1:-1].replace("''", "'")
            stack.append((depth, f"{header.group(2)} {name}"))
            continue
        tag = _LINEAGE.match(raw)
        if tag and stack and len(tag.group(1)) == stack[-1][0] + 1:
            tags["/".join(path for _, path in stack)] = tag.group(2)
    return tags


def lineage_remap(new_parts: dict[str, str], old_parts: dict[str, str]) -> dict[str, str]:
    """generated lineageTag -> existing lineageTag for objects (same part + same object path) present in both.

    Keeps entity/property/relationship IDs stable when an item is updated. Property lineageTags equal their
    column lineageTags, so mapping the table column carries the property along.
    """
    old_index = {(path, obj): tag for path, text in old_parts.items() for obj, tag in _tmdl_objects(text).items()}
    remap: dict[str, str] = {}
    used: set[str] = set()
    for path, text in sorted(new_parts.items(), key=lambda kv: not kv[0].startswith("tables/")):
        for obj, tag in _tmdl_objects(text).items():
            old = old_index.get((path, obj))
            if not old or old == tag or tag in remap or old in used:
                continue
            remap[tag] = old
            used.add(old)
    return remap


def _decode(parts: list[dict]) -> dict[str, str]:
    return {p["path"]: base64.b64decode(p["payload"]).decode("utf-8") for p in parts}


def _encode(texts: dict[str, str]) -> list[dict]:
    return [
        {"path": path, "payload": base64.b64encode(text.encode("utf-8")).decode("ascii"), "payloadType": "InlineBase64"}
        for path, text in texts.items()
    ]


def _summary(parts: dict[str, str]) -> dict:
    entities = sorted(p[len("entities/") : -len(".tmdl")] for p in parts if p.startswith("entities/"))
    rel = parts.get("entityRelationships.tmdl", "")
    props = sum(len(re.findall(r"^\tproperty ", t.replace("\r", ""), re.M)) for p, t in parts.items() if p.startswith("entities/"))
    return {
        "entities": entities,
        "relationships": sorted(re.findall(r"^entityRelationship (\S+)", rel.replace("\r", ""), re.M)),
        "properties": props,
    }


def change_set(old_parts: dict[str, str], new_parts: dict[str, str]) -> list[str]:
    before, after = _summary(old_parts), _summary(new_parts)
    norm = lambda t: t.replace("\r", "")  # noqa: E731
    lines = [
        f"  Entities      : {len(before['entities'])} -> {len(after['entities'])}"
        f"  (+{len(set(after['entities']) - set(before['entities']))} / -{len(set(before['entities']) - set(after['entities']))})",
        f"  Properties    : {before['properties']} -> {after['properties']}",
        f"  Relationships : {len(before['relationships'])} -> {len(after['relationships'])}",
    ]
    removed = sorted(set(before["entities"]) - set(after["entities"]))
    if removed:
        lines.append(f"  REMOVED entities : {', '.join(removed[:15])}" + (" ..." if len(removed) > 15 else ""))
    changed = [p for p in new_parts if p in old_parts and norm(old_parts[p]) != norm(new_parts[p])]
    added = [p for p in new_parts if p not in old_parts]
    dropped = [p for p in old_parts if p not in new_parts]
    lines.append(f"  Parts         : {len(changed)} changed, {len(added)} added, {len(dropped)} removed")
    return lines


def preview(
    def_dir: Path,
    display_name: str,
    workspace: str,
    report: Optional[dict],
    changes: Optional[list[str]] = None,
    folder: Optional[str] = None,
) -> str:
    entities = sorted(p.stem for p in (def_dir / "entities").glob("*.tmdl"))
    rel_file = def_dir / "entityRelationships.tmdl"
    rels = rel_file.read_text(encoding="utf-8").count("entityRelationship ") if rel_file.exists() else 0
    title = " PREVIEW: create Ontology item " if changes is None else " PREVIEW: REPLACE definition of existing item "
    lines = [
        "",
        "+" + title.center(86, "-") + "+",
        f"  Workspace     : {workspace}",
        f"  Display name  : {display_name}",
    ]
    if folder:
        lines.append(f"  Folder        : {folder}")
    if changes is None:
        lines += [f"  Entities      : {len(entities)}", f"  Relationships : {rels}"]
        if report:
            lines.append(f"  Properties    : {report['output']['properties']}")
    else:
        lines += changes
    if report:
        lines.append(f"  Skipped       : {report['skipped']['total']} (see skipped.csv)")
        lines.append(f"  Validated     : {'yes' if report['verified'] else 'NO - bindings were not checked against the lakehouse'}")
    shown = ", ".join(entities[:15]) + (f", ... (+{len(entities) - 15})" if len(entities) > 15 else "")
    lines += [f"  Entity types  : {shown}", "+" + "-" * 86 + "+"]
    return "\n".join(lines)


def _confirm(yes: bool, prompt: str) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise DeployError("Confirmation required: re-run interactively or pass --yes")
    if input(prompt).strip() != "yes":
        raise DeployError("Aborted by user (nothing was changed)")


def deploy(
    client: FabricClient,
    out_path: Path,
    workspace: str,
    display_name: Optional[str] = None,
    description: Optional[str] = None,
    yes: bool = False,
    force: bool = False,
    dry_run: bool = False,
    update_existing: bool = False,
    folder: Optional[str] = None,
    create_folder: bool = False,
) -> dict:
    def_dir = definition_dir(out_path)
    out_root = def_dir.parent if def_dir.name == "definition" else def_dir
    report_file = out_root / "report.json"
    report = json.loads(report_file.read_text(encoding="utf-8")) if report_file.exists() else None
    name = display_name or _platform_name(def_dir)
    if not name:
        raise DeployError("Pass --name (no .platform displayName found)")
    if create_folder and folder is None:
        raise DeployError("--create-folder needs --folder")
    if report and report.get("unresolved") and not force:
        raise DeployError(
            f"{report['unresolved']} unresolved binding(s) in {report_file}. Re-run convert with "
            "--skip-missing-tables/--skip-missing-columns, or pass --force to deploy anyway."
        )
    if report and report.get("definitionSha256") != definition_fingerprint(def_dir) and not force:
        raise DeployError(
            f"{def_dir} does not match {report_file} (edited by hand or left over from another run). "
            "Re-run convert, or pass --force to deploy it anyway."
        )
    if report and not report.get("verified"):
        log.warning("This definition was generated WITHOUT physical validation; bindings may not exist.")

    if _platform_name(def_dir) and _platform_name(def_dir) != name:
        log.info(".platform displayName set to %s (from --name)", name)
    envelope = build_envelope(def_dir, name, description)

    if dry_run:
        target = out_root / "envelope.json"
        target.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
        print(preview(def_dir, name, workspace, report, folder=f"{folder} (resolved at deploy time)" if folder else None))
        print(f"Dry run: wrote {target} ({len(envelope['definition']['parts'])} parts); nothing was sent to Fabric.")
        return {"dryRun": True, "envelope": str(target)}

    ws = client.resolve_workspace(workspace)
    folders = client.list_folders(ws["id"]) if folder is not None or update_existing else []
    target_folder: Optional[str] = None
    if folder is not None:
        creator = (lambda seg, parent: client.create_folder(ws["id"], seg, parent)) if create_folder else None
        try:
            target_folder = resolve_folder(folder, folders, creator)
        except FolderError as exc:
            raise DeployError(str(exc)) from exc

    candidates = [i for i in client.list_items(ws["id"], "Ontology") if i.get("displayName") == name]
    if candidates and not update_existing:
        where = describe(candidates[0].get("folderId"), folders or client.list_folders(ws["id"]))
        raise DeployError(
            f"An Ontology named '{name}' already exists in workspace '{ws.get('displayName')}' "
            f"(folder {where}, id {candidates[0]['id']}). Choose another --name, or pass --update-existing to "
            "replace its definition (the current definition is backed up first)."
        )
    if candidates:
        if len(candidates) > 1:
            in_target = [i for i in candidates if (i.get("folderId") or None) == target_folder]
            if folder is None or len(in_target) != 1:
                raise DeployError(f"Several Ontologies are named '{name}'; pass --folder to pick one")
            candidates = in_target
        move_to = target_folder if folder is not None else candidates[0].get("folderId")
        return _update(client, ws, candidates[0], def_dir, out_root, envelope, report, yes, folders, move_to)

    folder_label = describe(target_folder, folders)
    print(preview(def_dir, name, f"{ws.get('displayName')} ({ws['id']})", report, folder=folder_label))
    _confirm(yes, "Type 'yes' to create this Ontology item: ")
    if target_folder:
        envelope = {**envelope, "folderId": target_folder}
    item = client.run_lro("POST", f"{FABRIC_API}/workspaces/{ws['id']}/items", envelope)
    if not item or "id" not in item:
        item = next(
            (
                i
                for i in client.list_items(ws["id"], "Ontology")
                if i.get("displayName") == name and (i.get("folderId") or None) == target_folder
            ),
            None,
        )
    if not item:
        raise DeployError("Create succeeded but the new item could not be found")
    print(f"Created Ontology '{name}' ({item['id']}) in folder {folder_label}\n{endpoint_lines(ws['id'], item['id'])}")
    result = {
        "id": item["id"],
        "workspaceId": ws["id"],
        "url": item_url(ws["id"], item["id"]),
        "mcpEndpoint": mcp_endpoint(ws["id"], item["id"]),
        "folderId": target_folder,
    }
    return result


def _update(
    client: FabricClient,
    ws: dict,
    item: dict,
    def_dir: Path,
    out_root: Path,
    envelope: dict,
    report,
    yes: bool,
    folders: list[dict],
    target_folder: Optional[str],
) -> dict:
    """Replace the definition of an existing item; keeps its id, lineageTags of matching objects, and a backup."""
    current = client.get_definition(ws["id"], item["id"])
    old_parts = _decode(current.get("definition", {}).get("parts", []))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = out_root / "backups" / f"{item['displayName']}_{item['id']}_{stamp}"
    for path, text in old_parts.items():
        target = backup / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    print(f"Backed up the current definition of '{item['displayName']}' to {backup}")

    new_parts = _decode(envelope["definition"]["parts"])
    remap = lineage_remap(new_parts, old_parts)
    if remap:
        pattern = re.compile("|".join(re.escape(k) for k in remap))
        new_parts = {path: pattern.sub(lambda m: remap[m.group(0)], text) for path, text in new_parts.items()}
        log.info("Reusing %d existing lineageTag(s) so entity/property IDs stay stable", len(remap))

    workspace_label = f"{ws.get('displayName')} ({ws['id']})"
    current_folder = item.get("folderId") or None
    move = (target_folder or None) != current_folder
    folder_label = describe(current_folder, folders)
    if move:
        folder_label = f"{folder_label} -> {describe(target_folder, folders)} (item and its graph/eventhouse are moved)"
    print(
        preview(def_dir, item["displayName"], workspace_label, report, change_set(old_parts, new_parts), folder_label)
    )
    print(f"  Item id       : {item['id']} (kept; the whole definition is replaced)")
    _confirm(yes, f"Type 'yes' to REPLACE the definition of '{item['displayName']}': ")
    client.run_lro(
        "POST",
        f"{FABRIC_API}/workspaces/{ws['id']}/items/{item['id']}/updateDefinition",
        {"definition": {"parts": _encode(new_parts)}},
    )
    if move:
        client.move_item(ws["id"], item["id"], target_folder)
        print(f"Moved '{item['displayName']}' to folder {describe(target_folder, folders)}")
    print(
        f"Updated Ontology '{item['displayName']}' ({item['id']})\n{endpoint_lines(ws['id'], item['id'])}\n"
        f"  Backup        : {backup}"
    )
    return {
        "id": item["id"],
        "workspaceId": ws["id"],
        "url": item_url(ws["id"], item["id"]),
        "mcpEndpoint": mcp_endpoint(ws["id"], item["id"]),
        "updated": True,
        "backup": str(backup),
        "folderId": target_folder,
    }
