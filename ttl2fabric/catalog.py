"""Physical catalog of lakehouse tables/columns: live (Fabric/OneLake), file-based, or none."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from .fabric_client import FabricClient, FabricError
from .model import LakehouseRef

log = logging.getLogger(__name__)

_COMMIT = re.compile(r"^(\d{20})\.json$")
_CHECKPOINT = re.compile(r"^(\d{20})\.checkpoint(?:\.[^/]+)?\.(parquet|json)$")


class CatalogError(RuntimeError):
    pass


@dataclass
class Column:
    name: str
    type: str  # Delta/Spark type name, e.g. string, integer, timestamp, decimal(10,2)


@dataclass
class TableLookup:
    status: str  # found | case | not_found | ambiguous
    schema: Optional[str] = None
    name: Optional[str] = None
    candidates: list[str] = field(default_factory=list)


class Catalog:
    """Base class. `verified` is False for the NullCatalog (no physical validation possible)."""

    verified = True

    def __init__(self, lakehouse: Optional[LakehouseRef]):
        self.lakehouse = lakehouse
        self._tables: dict[Optional[str], list[str]] = {}  # schema -> table names
        self._columns: dict[tuple[Optional[str], str], list[Column]] = {}

    # -- tables
    def schemas(self) -> list[Optional[str]]:
        return list(self._tables)

    def find_table(self, table: str, schemas: list[str]) -> TableLookup:
        """Exact name first, then case-insensitive. With --schema the first listed schema that has a hit wins."""
        wanted = [s.lower() for s in schemas]
        search = [s for s in self._tables if s is None or not wanted or s.lower() in wanted]
        if wanted:
            search.sort(key=lambda s: wanted.index(s.lower()) if s is not None else -1)
        for status, match in (("found", lambda t: t == table), ("case", lambda t: t.casefold() == table.casefold())):
            hits = [(s, t) for s in search for t in self._tables[s] if match(t)]
            if not hits:
                continue
            if wanted:
                hits = [h for h in hits if h[0] == hits[0][0]]
            if len(hits) > 1:
                return TableLookup("ambiguous", candidates=[_fq(s, t) for s, t in hits])
            return TableLookup(status, hits[0][0], hits[0][1])
        return TableLookup("not_found")

    # -- columns
    def columns(self, schema: Optional[str], table: str) -> list[Column]:
        key = (schema, table)
        if key not in self._columns:
            self._columns[key] = self._load_columns(schema, table)
        return self._columns[key]

    def prefetch(self, tables: Iterable[tuple[Optional[str], str]]) -> dict[tuple[Optional[str], str], str]:
        """Load columns for many tables; returns {table: error} for failures."""
        errors: dict[tuple[Optional[str], str], str] = {}
        for key in dict.fromkeys(tables):
            try:
                self.columns(*key)
            except CatalogError as exc:
                errors[key] = str(exc)
        return errors

    def _load_columns(self, schema: Optional[str], table: str) -> list[Column]:
        raise CatalogError(f"No column metadata for {_fq(schema, table)}")

    # -- persistence
    def to_json(self) -> dict:
        lh = self.lakehouse
        return {
            "generatedAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "lakehouse": None
            if lh is None
            else {
                "workspaceId": lh.workspace_id,
                "workspaceName": lh.workspace_name,
                "lakehouseId": lh.lakehouse_id,
                "lakehouseName": lh.lakehouse_name,
                "sqlEndpoint": lh.sql_endpoint,
                "schemaEnabled": lh.schema_enabled,
            },
            "tables": [
                {
                    "schema": s,
                    "name": t,
                    **(
                        {"columns": [{"name": c.name, "type": c.type} for c in self._columns[(s, t)]]}
                        if (s, t) in self._columns
                        else {}
                    ),
                }
                for s in self._tables
                for t in sorted(self._tables[s])
            ],
        }


class NullCatalog(Catalog):
    verified = False

    def find_table(self, table: str, schemas: list[str]) -> TableLookup:  # pragma: no cover - not used
        return TableLookup("found", schemas[0] if schemas else None, table)


class FileCatalog(Catalog):
    """Catalog loaded from our JSON dump or an INFORMATION_SCHEMA.COLUMNS CSV export."""

    def __init__(self, path: str, lakehouse: Optional[LakehouseRef] = None):
        super().__init__(lakehouse)
        with open(path, "r", encoding="utf-8-sig") as fh:
            text = fh.read()
        if path.lower().endswith(".csv"):
            self._load_csv(text)
        else:
            self._load_json(json.loads(text))
        log.info("Loaded catalog file %s: %d tables", path, sum(len(v) for v in self._tables.values()))

    def _load_json(self, data: dict) -> None:
        lh = data.get("lakehouse")
        if lh and self.lakehouse is None:
            self.lakehouse = LakehouseRef(
                workspace_id=lh.get("workspaceId", ""),
                workspace_name=lh.get("workspaceName", ""),
                lakehouse_id=lh.get("lakehouseId", ""),
                lakehouse_name=lh.get("lakehouseName", ""),
                sql_endpoint=lh.get("sqlEndpoint"),
                schema_enabled=bool(lh.get("schemaEnabled")),
            )
        for t in data.get("tables") or []:
            schema = t.get("schema")
            self._tables.setdefault(schema, []).append(t["name"])
            if "columns" in t:
                self._columns[(schema, t["name"])] = [Column(c["name"], str(c.get("type", "string"))) for c in t["columns"]]

    def _load_csv(self, text: str) -> None:
        reader = csv.DictReader(io.StringIO(text))
        fields = {f.lower(): f for f in reader.fieldnames or []}
        need = ["table_name", "column_name"]
        if not all(n in fields for n in need):
            raise CatalogError("CSV catalog needs TABLE_NAME and COLUMN_NAME columns (plus TABLE_SCHEMA, DATA_TYPE)")
        for row in reader:
            schema = row.get(fields.get("table_schema", ""), None) or None
            table = row[fields["table_name"]]
            col = row[fields["column_name"]]
            dtype = sql_to_delta_type(row.get(fields.get("data_type", ""), "") or "string")
            tables = self._tables.setdefault(schema, [])
            if table not in tables:
                tables.append(table)
            self._columns.setdefault((schema, table), []).append(Column(col, dtype))

    def _load_columns(self, schema: Optional[str], table: str) -> list[Column]:
        raise CatalogError(f"Catalog file has no column list for {_fq(schema, table)}")


class FabricCatalog(Catalog):
    """Live catalog: tables via OneLake DFS listing, columns from the Delta transaction log."""

    def __init__(self, client: FabricClient, workspace: str, lakehouse: str, schemas: Optional[list[str]] = None):
        ws = client.resolve_workspace(workspace)
        lh = client.resolve_lakehouse(ws["id"], lakehouse)
        props = lh.get("properties") or {}
        sql = props.get("sqlEndpointProperties") or {}
        ref = LakehouseRef(
            workspace_id=ws["id"],
            workspace_name=ws.get("displayName", ""),
            lakehouse_id=lh["id"],
            lakehouse_name=lh.get("displayName", ""),
            sql_endpoint=sql.get("connectionString"),
            schema_enabled=bool(props.get("defaultSchema")),
        )
        super().__init__(ref)
        self._client = client
        log.info(
            "Lakehouse %s (%s) in workspace %s (%s); schema-enabled=%s",
            ref.lakehouse_name,
            ref.lakehouse_id,
            ref.workspace_name,
            ref.workspace_id,
            ref.schema_enabled,
        )
        self._list_tables(schemas or [])

    def _root(self) -> str:
        return f"{self.lakehouse.lakehouse_id}/Tables"

    def _list_tables(self, schemas: list[str]) -> None:
        entries = self._client.dfs_list(self.lakehouse.workspace_id, self._root())
        dirs = [name for name, is_dir, _ in entries if is_dir and "/" not in name]
        if self.lakehouse.schema_enabled:
            wanted = {s.lower() for s in schemas}
            for schema in dirs:
                if wanted and schema.lower() not in wanted:
                    continue
                children = self._client.dfs_list(self.lakehouse.workspace_id, f"{self._root()}/{schema}")
                self._tables[schema] = sorted(n for n, d, _ in children if d and "/" not in n)
        else:
            self._tables[None] = sorted(dirs)
        total = sum(len(v) for v in self._tables.values())
        log.info("Found %d tables in %d schema(s): %s", total, len(self._tables), ", ".join(str(s) for s in self._tables))
        if total == 0:
            raise CatalogError("The lakehouse returned zero tables; refusing to validate against an empty catalog")

    def prefetch(self, tables: Iterable[tuple[Optional[str], str]]) -> dict[tuple[Optional[str], str], str]:
        todo = [t for t in dict.fromkeys(tables) if t not in self._columns]
        errors: dict[tuple[Optional[str], str], str] = {}

        def load(key):
            try:
                return key, self._load_columns(*key), None
            except (CatalogError, FabricError) as exc:
                return key, None, str(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            for key, cols, err in pool.map(load, todo):
                if err:
                    errors[key] = err
                else:
                    self._columns[key] = cols
        return errors

    def _table_path(self, schema: Optional[str], table: str) -> str:
        return f"{self._root()}/{schema}/{table}" if schema else f"{self._root()}/{table}"

    def _load_columns(self, schema: Optional[str], table: str) -> list[Column]:
        ws = self.lakehouse.workspace_id
        log_dir = f"{self._table_path(schema, table)}/_delta_log"
        files = [n for n, is_dir, _ in self._client.dfs_list(ws, log_dir) if not is_dir and "/" not in n]
        if not files:
            raise CatalogError(f"{_fq(schema, table)}: no _delta_log found (not a Delta table or a broken shortcut)")
        schema_json = read_delta_schema(files, lambda name: self._client.dfs_read(ws, f"{log_dir}/{name}"))
        if schema_json is None:
            raise CatalogError(f"{_fq(schema, table)}: no metaData action found in the Delta log")
        return columns_from_schema_string(schema_json)


# ---------------------------------------------------------------------------
# Delta log helpers
# ---------------------------------------------------------------------------


def read_delta_schema(files: list[str], read) -> Optional[str]:
    """Return the latest metaData.schemaString given the _delta_log file names and a reader callable."""
    commits = sorted(((int(m.group(1)), n) for n in files if (m := _COMMIT.match(n))), reverse=True)
    checkpoints: dict[int, list[str]] = {}
    for n in files:
        m = _CHECKPOINT.match(n)
        if m:
            checkpoints.setdefault(int(m.group(1)), []).append(n)
    cp_version = max(checkpoints) if checkpoints else -1
    if "_last_checkpoint" in files:
        raw = read("_last_checkpoint")
        if raw:
            try:
                v = int(json.loads(raw).get("version", -1))
                if v in checkpoints:
                    cp_version = v
            except (ValueError, AttributeError):
                pass

    for version, name in commits:
        if version <= cp_version:
            break
        found = _metadata_from_json_lines(read(name))
        if found:
            return found
    if cp_version >= 0:
        for name in sorted(checkpoints[cp_version]):
            raw = read(name)
            if raw is None:
                continue
            found = _metadata_from_json_lines(raw) if name.endswith(".json") else _metadata_from_parquet(raw)
            if found:
                return found
    # Checkpoint unreadable/absent: fall back to whatever older commits remain.
    for version, name in commits:
        if version <= cp_version:
            found = _metadata_from_json_lines(read(name))
            if found:
                return found
    return None


def _metadata_from_json_lines(raw: Optional[bytes]) -> Optional[str]:
    if not raw:
        return None
    found = None
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            action = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "metaData" in action:
            found = action["metaData"].get("schemaString")
    return found


def _metadata_from_parquet(raw: bytes) -> Optional[str]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover
        raise CatalogError("pyarrow is required to read Delta checkpoints: pip install pyarrow") from exc
    table = pq.read_table(io.BytesIO(raw), columns=["metaData"])
    for row in table.column("metaData").to_pylist():
        if row and row.get("schemaString"):
            return row["schemaString"]
    return None


def columns_from_schema_string(schema_string: str) -> list[Column]:
    """Logical column names (never delta.columnMapping.physicalName) with Spark type names."""
    schema = json.loads(schema_string)
    out = []
    for f in schema.get("fields", []):
        t = f.get("type")
        out.append(Column(f["name"], t if isinstance(t, str) else str(t.get("type", "struct"))))
    return out


# ---------------------------------------------------------------------------
# Type mapping
# ---------------------------------------------------------------------------

_INT = {"integer", "int", "long", "bigint", "short", "smallint", "byte", "tinyint"}
_FLOAT = {"double", "float", "real"}
_DATE = {"date", "timestamp", "timestamp_ntz", "datetime", "datetime2", "smalldatetime", "datetimeoffset"}
_STR = {"string", "char", "varchar", "nchar", "nvarchar", "text", "ntext", "uniqueidentifier"}


def delta_to_tmdl(delta_type: str) -> Optional[str]:
    """Map a Delta/Spark (or SQL) column type to a TMDL data type; None when unsupported."""
    t = delta_type.strip().lower()
    base = t.split("(")[0]
    if base in _STR:
        return "string"
    if base in _INT:
        return "int64"
    if base in _FLOAT or base in {"decimal", "numeric", "number", "money", "smallmoney"}:
        return "double"
    if base in _DATE:
        return "dateTime"
    if base in {"boolean", "bool", "bit"}:
        return "boolean"
    return None


def sql_to_delta_type(sql_type: str) -> str:
    t = sql_type.strip().lower()
    base = t.split("(")[0]
    return {
        "varchar": "string",
        "nvarchar": "string",
        "char": "string",
        "nchar": "string",
        "uniqueidentifier": "string",
        "int": "integer",
        "bigint": "long",
        "smallint": "short",
        "tinyint": "byte",
        "float": "double",
        "real": "float",
        "bit": "boolean",
        "datetime2": "timestamp",
        "datetime": "timestamp",
        "date": "date",
        "varbinary": "binary",
    }.get(base, t)


_XSD = {
    "string": "string",
    "normalizedString": "string",
    "token": "string",
    "anyURI": "string",
    "integer": "int64",
    "int": "int64",
    "long": "int64",
    "short": "int64",
    "byte": "int64",
    "nonNegativeInteger": "int64",
    "positiveInteger": "int64",
    "negativeInteger": "int64",
    "nonPositiveInteger": "int64",
    "unsignedInt": "int64",
    "unsignedLong": "int64",
    "decimal": "double",
    "double": "double",
    "float": "double",
    "date": "dateTime",
    "dateTime": "dateTime",
    "dateTimeStamp": "dateTime",
    "boolean": "boolean",
}


def xsd_to_tmdl(xsd_local: Optional[str]) -> str:
    return _XSD.get(xsd_local or "string", "string")


def _fq(schema: Optional[str], table: str) -> str:
    return f"{schema}.{table}" if schema else table
