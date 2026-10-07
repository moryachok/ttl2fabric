"""Naming helpers: normalisation, humanising, TMDL quoting, deterministic IDs."""

from __future__ import annotations

import re
import uuid
from typing import Iterable, Optional

_NON_ALNUM = re.compile(r"[^0-9a-zA-Z]+")
_TOKEN = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_TMDL_BARE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LINEAGE_NS = uuid.UUID("6f1d7c62-6a3e-4e8e-9a43-0b1c5b7e2f10")


def normalize(name: str) -> str:
    """Case/space/punctuation-insensitive form used for fuzzy column matching."""
    return _NON_ALNUM.sub("", name).lower()


def single_line(text: Optional[str]) -> str:
    return " ".join((text or "").split())


def acronyms_from_labels(labels: Iterable[str]) -> set[str]:
    """Collect upper-case tokens (MDU, CRM, BSS, ...) used in business labels."""
    found: set[str] = set()
    for label in labels:
        for token in re.split(r"[\s/_-]+", label or ""):
            if len(token) >= 2 and token.isalpha() and token.isupper():
                found.add(token)
    return found


def humanize(identifier: str, acronyms: Optional[set[str]] = None) -> str:
    """'mduOwnerCustomerKey' -> 'MDU Owner Customer Key' (given MDU is a known acronym)."""
    acronyms = acronyms or set()
    words: list[str] = []
    for chunk in _NON_ALNUM.split(identifier):
        if not chunk:
            continue
        chunk = chunk[0].upper() + chunk[1:]
        for token in _TOKEN.findall(chunk):
            if token.upper() in acronyms:
                words.append(token.upper())
            elif token.isupper() and len(token) > 1:
                words.append(token)
            else:
                words.append(token[0].upper() + token[1:])
    return " ".join(words)


def sanitize_identifier(name: str, fallback: str = "X") -> str:
    """Make a name safe for Fabric entity / relationship identifiers: ^[A-Za-z][A-Za-z0-9_]*$."""
    cleaned = _NON_ALNUM.sub("_", name).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    if not cleaned:
        cleaned = fallback
    if not cleaned[0].isalpha():
        cleaned = f"{fallback}_{cleaned}"
    return cleaned


def relationship_name(local_name: str, range_local_name: Optional[str]) -> str:
    """'Customer_HasMainAddress_Address' -> 'CustomerHasMainAddress' (as Fabric v2 names it)."""
    base = local_name
    if range_local_name and base.endswith(f"_{range_local_name}") and base != f"_{range_local_name}":
        base = base[: -(len(range_local_name) + 1)]
    return sanitize_identifier(base.replace("_", ""), fallback="Rel")


def tmdl_name(name: str) -> str:
    """Quote a TMDL object name when needed ('Main Address Key'); embedded quotes are doubled."""
    if _TMDL_BARE.match(name):
        return name
    return "'" + name.replace("'", "''") + "'"


def tmdl_ref(table: str, column: str) -> str:
    return f"{tmdl_name(table)}.{tmdl_name(column)}"


def lineage_tag(*parts: str) -> str:
    """Deterministic GUID so re-running the converter yields an identical definition."""
    return str(uuid.uuid5(_LINEAGE_NS, "\x1f".join(parts)))


def unique_name(candidate: str, taken: set[str], separator: str = "_") -> str:
    """Return candidate or candidate_2, candidate_3, ... (case-insensitive uniqueness)."""
    lowered = {t.lower() for t in taken}
    if candidate.lower() not in lowered:
        return candidate
    i = 2
    while f"{candidate}{separator}{i}".lower() in lowered:
        i += 1
    return f"{candidate}{separator}{i}"
