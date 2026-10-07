import io
import json

import pyarrow as pa
import pyarrow.parquet as pq

from ttl2fabric.catalog import (
    FileCatalog,
    columns_from_schema_string,
    delta_to_tmdl,
    read_delta_schema,
    xsd_to_tmdl,
)

SCHEMA_V1 = json.dumps(
    {
        "type": "struct",
        "fields": [
            {"name": "Customer Key", "type": "string", "metadata": {"delta.columnMapping.physicalName": "col-1"}},
            {"name": "Open Date", "type": "timestamp", "metadata": {}},
        ],
    }
)
SCHEMA_V2 = json.dumps(
    {
        "type": "struct",
        "fields": [
            {"name": "Customer Key", "type": "string", "metadata": {}},
            {"name": "Tags", "type": {"type": "array", "elementType": "string"}, "metadata": {}},
        ],
    }
)


def _commit(schema=None):
    lines = [json.dumps({"commitInfo": {}})]
    if schema:
        lines.append(json.dumps({"metaData": {"schemaString": schema}}))
    return ("\n".join(lines) + "\n").encode()


def test_logical_names_never_physical():
    cols = columns_from_schema_string(SCHEMA_V1)
    assert [c.name for c in cols] == ["Customer Key", "Open Date"]


def test_latest_metadata_in_json_commits():
    files = {"00000000000000000000.json": _commit(SCHEMA_V1), "00000000000000000001.json": _commit(SCHEMA_V2)}
    assert read_delta_schema(list(files), files.get) == SCHEMA_V2
    files["00000000000000000002.json"] = _commit()
    assert read_delta_schema(list(files), files.get) == SCHEMA_V2


def test_metadata_from_parquet_checkpoint_when_commits_cleaned():
    buf = io.BytesIO()
    table = pa.table({"metaData": [None, {"schemaString": SCHEMA_V1}]})
    pq.write_table(table, buf)
    files = {
        "00000000000000000010.checkpoint.parquet": buf.getvalue(),
        "_last_checkpoint": json.dumps({"version": 10}).encode(),
        "00000000000000000011.json": _commit(),
    }
    assert read_delta_schema(list(files), files.get) == SCHEMA_V1


def test_multipart_checkpoint_and_newer_commit_wins():
    buf = io.BytesIO()
    pq.write_table(pa.table({"metaData": [{"schemaString": SCHEMA_V1}]}), buf)
    files = {
        "00000000000000000010.checkpoint.0000000001.0000000002.parquet": buf.getvalue(),
        "00000000000000000010.checkpoint.0000000002.0000000002.parquet": buf.getvalue(),
        "00000000000000000012.json": _commit(SCHEMA_V2),
    }
    assert read_delta_schema(list(files), files.get) == SCHEMA_V2


def test_type_mapping():
    assert delta_to_tmdl("integer") == "int64"
    assert delta_to_tmdl("decimal(10,2)") == "double"
    assert delta_to_tmdl("timestamp_ntz") == "dateTime"
    assert delta_to_tmdl("boolean") == "boolean"
    assert delta_to_tmdl("binary") is None and delta_to_tmdl("array") is None
    assert xsd_to_tmdl("date") == "dateTime" and xsd_to_tmdl("decimal") == "double" and xsd_to_tmdl(None) == "string"


def test_find_table_rules(catalog):
    assert catalog.find_table("customer__t", ["bronze"]).schema == "bronze"
    assert catalog.find_table("customer__t", ["dbo", "bronze"]).schema == "dbo"
    assert catalog.find_table("customer__t", []).status == "ambiguous"
    assert catalog.find_table("CUSTOMER__T", ["bronze"]).status == "case"
    assert catalog.find_table("nope", []).status == "not_found"


def test_csv_catalog(tmp_path):
    csv_file = tmp_path / "cols.csv"
    csv_file.write_text(
        "TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,DATA_TYPE\nbronze,customer__t,Customer Key,varchar\n"
        "bronze,customer__t,Open Date,datetime2\n",
        encoding="utf-8",
    )
    cat = FileCatalog(str(csv_file))
    cols = cat.columns("bronze", "customer__t")
    assert [(c.name, delta_to_tmdl(c.type)) for c in cols] == [("Customer Key", "string"), ("Open Date", "dateTime")]
