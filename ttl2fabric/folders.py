"""Workspace folder resolution: 'a/b/c' paths or folder GUIDs -> folder id (optionally creating folders)."""

from __future__ import annotations

import logging
from typing import Callable, Optional

from .fabric_client import is_guid

log = logging.getLogger(__name__)

ROOT_ALIASES = {"", "/", "root", "(root)"}


class FolderError(RuntimeError):
    pass


def folder_paths(folders: list[dict]) -> dict[str, str]:
    """folder id -> 'parent/child' display path."""
    by_id = {f["id"]: f for f in folders}

    def path(folder_id: str) -> str:
        parts, seen = [], set()
        while folder_id and folder_id in by_id and folder_id not in seen:
            seen.add(folder_id)
            parts.append(by_id[folder_id]["displayName"])
            folder_id = by_id[folder_id].get("parentFolderId")
        return "/".join(reversed(parts))

    return {f["id"]: path(f["id"]) for f in folders}


def describe(folder_id: Optional[str], folders: list[dict]) -> str:
    if not folder_id:
        return "/ (workspace root)"
    return folder_paths(folders).get(folder_id) or folder_id


def resolve_folder(
    spec: str,
    folders: list[dict],
    create: Optional[Callable[[str, Optional[str]], dict]] = None,
) -> Optional[str]:
    """Return the folder id for `spec` (path or GUID); None means the workspace root.

    Path segments match exactly first, then case-insensitively. Missing segments are created with `create`
    (when given) or reported with the folders that do exist at that level.
    """
    spec = (spec or "").strip()
    if spec.lower() in ROOT_ALIASES:
        return None
    if is_guid(spec):
        if not any(f["id"].lower() == spec.lower() for f in folders):
            raise FolderError(f"Folder id {spec} not found in the workspace")
        return spec
    parent: Optional[str] = None
    walked: list[str] = []
    for segment in [s.strip() for s in spec.strip("/").split("/") if s.strip()]:
        children = [f for f in folders if (f.get("parentFolderId") or None) == parent]
        match = [f for f in children if f["displayName"] == segment] or [
            f for f in children if f["displayName"].casefold() == segment.casefold()
        ]
        if len(match) > 1:
            raise FolderError(f"Folder '{'/'.join(walked + [segment])}' is ambiguous; pass the folder id")
        if match:
            parent = match[0]["id"]
        elif create is not None:
            created = create(segment, parent)
            log.info("Created folder '%s' (%s)", "/".join(walked + [segment]), created["id"])
            folders.append({**created, "parentFolderId": parent})
            parent = created["id"]
        else:
            where = "/".join(walked) or "/ (workspace root)"
            options = ", ".join(sorted(f["displayName"] for f in children)) or "no subfolders"
            raise FolderError(
                f"Folder '{segment}' not found under {where} (available: {options}). "
                "Check the path or pass --create-folder"
            )
        walked.append(segment)
    return parent
