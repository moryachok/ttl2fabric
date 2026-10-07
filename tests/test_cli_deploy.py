import base64
import csv
import json
from pathlib import Path

import pytest

from ttl2fabric.cli import main
from ttl2fabric.deploy import DeployError, deploy


def convert(tmp_path, fixtures_dir, *extra):
    out = tmp_path / "out"
    code = main(
        [
            "convert",
            str(fixtures_dir / "mini.ttl"),
            "--name",
            "TestOnto",
            "-o",
            str(out),
            "--catalog-file",
            str(fixtures_dir / "catalog.json"),
            "--schema",
            "bronze",
            "--log-level",
            "WARNING",
            *extra,
        ]
    )
    return code, out


def test_cli_strict_run_writes_all_outputs(tmp_path, fixtures_dir):
    code, out = convert(tmp_path, fixtures_dir, "--strict")
    assert code == 0
    for name in ["report.json", "skipped.csv", "skipped.jsonl", "findings.csv", "TestOnto.ttl", "ttl2fabric.log"]:
        assert (out / name).exists(), name
    assert (out / "definition" / "model.tmdl").exists()
    rows = list(csv.DictReader(open(out / "skipped.csv", encoding="utf-8")))
    assert {"kind", "entity", "name", "reason", "detail"} <= set(rows[0])
    assert any(r["reason"] == "COLUMN_CASE_MISMATCH" and r["name"] == "Case Prop" for r in rows)
    report = json.loads((out / "report.json").read_text())
    assert report["verified"] and report["unresolved"] == 0
    assert report["output"]["entities"] == 2
    log_text = (out / "ttl2fabric.log").read_text()
    assert "SKIP" in log_text and "Missing Prop" in log_text


def test_cli_unresolved_exit_code(tmp_path, fixtures_dir):
    code, out = convert(tmp_path, fixtures_dir)
    assert code == 2
    assert json.loads((out / "report.json").read_text())["unresolved"] > 0


def test_cli_rejects_skip_flags_without_catalog(tmp_path, fixtures_dir):
    with pytest.raises(SystemExit):
        main(["convert", str(fixtures_dir / "mini.ttl"), "-o", str(tmp_path / "x"), "--skip-missing-tables"])


def test_cli_offline_ids_without_catalog(tmp_path, fixtures_dir):
    out = tmp_path / "offline"
    code = main(
        [
            "convert",
            str(fixtures_dir / "mini.ttl"),
            "--name",
            "Offline",
            "-o",
            str(out),
            "--workspace-id",
            "11111111-1111-1111-1111-111111111111",
            "--lakehouse-id",
            "22222222-2222-2222-2222-222222222222",
            "--lakehouse-name",
            "lh",
            "--schema",
            "bronze",
            "--log-level",
            "ERROR",
        ]
    )
    assert code == 0
    table = (out / "definition" / "tables" / "Customer.tmdl").read_text()
    assert "schemaName: bronze" in table and "sourceColumn: Missing Prop" in table
    assert not json.loads((out / "report.json").read_text())["verified"]


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.content = b"{}" if payload is not None else b""

    def json(self):
        return self._payload


FOLDERS = [
    {"id": "f-org", "displayName": "org"},
    {"id": "f-ds", "displayName": "data-services", "parentFolderId": "f-org"},
    {"id": "f-onto", "displayName": "ontology", "parentFolderId": "f-ds"},
]


class FakeClient:
    def __init__(self, existing=(), definition=None, folder_of=None):
        self.existing = list(existing)
        self.definition = definition
        self.folder_of = folder_of or {}
        self.folders = [dict(f) for f in FOLDERS]
        self.posted = None
        self.moved = None
        self.created_folders = []

    def resolve_workspace(self, name):
        return {"id": "ws-id", "displayName": name}

    def list_items(self, ws, item_type=None):
        return [{"id": "x", "displayName": n, "folderId": self.folder_of.get(n)} for n in self.existing]

    def list_folders(self, ws):
        return [dict(f) for f in self.folders]

    def create_folder(self, ws, name, parent):
        folder = {"id": f"new-{name}", "displayName": name, **({"parentFolderId": parent} if parent else {})}
        self.folders.append(folder)
        self.created_folders.append((name, parent))
        return folder

    def move_item(self, ws, item_id, folder_id):
        self.moved = (item_id, folder_id)

    def run_lro(self, method, url, json_body=None):
        self.posted = (method, url, json_body)
        return {"id": "new-item-id"}

    def get_definition(self, ws, item_id):
        return {"definition": {"parts": self.definition or []}}


def test_deploy_dry_run_and_create(tmp_path, fixtures_dir):
    code, out = convert(tmp_path, fixtures_dir, "--strict")
    assert code == 0
    result = deploy(FakeClient(), out, "ws", dry_run=True)
    envelope = json.loads((out / "envelope.json").read_text())
    paths = {p["path"] for p in envelope["definition"]["parts"]}
    assert "entities/Customer.tmdl" in paths and ".platform" in paths
    assert all("\\" not in p for p in paths)
    model = next(p for p in envelope["definition"]["parts"] if p["path"] == "model.tmdl")
    assert base64.b64decode(model["payload"]).startswith(b"model Model\r\n")
    assert envelope["type"] == "Ontology" and envelope["displayName"] == "TestOnto"
    assert result["dryRun"]

    client = FakeClient()
    created = deploy(client, out, "ws", yes=True)
    assert created["id"] == "new-item-id"
    assert client.posted[1].endswith("/workspaces/ws-id/items")

    with pytest.raises(DeployError, match="already exists"):
        deploy(FakeClient(existing=["TestOnto"]), out, "ws", yes=True)


def test_deploy_refuses_unresolved(tmp_path, fixtures_dir):
    code, out = convert(tmp_path, fixtures_dir)
    assert code == 2
    with pytest.raises(DeployError, match="unresolved"):
        deploy(FakeClient(), out, "ws", yes=True)
    assert deploy(FakeClient(), out, "ws", yes=True, force=True)["id"] == "new-item-id"


def test_stale_definition_never_deployed(tmp_path, fixtures_dir):
    code, out = convert(tmp_path, fixtures_dir)  # unresolved definition
    assert code == 2
    code, out = convert(tmp_path, fixtures_dir, "--strict", "--format", "ttl")
    assert code == 0 and not (out / "definition").exists()
    with pytest.raises(DeployError, match="No TMDL definition"):
        deploy(FakeClient(), out, "ws", yes=True)


def test_deploy_detects_tampered_definition(tmp_path, fixtures_dir):
    code, out = convert(tmp_path, fixtures_dir, "--strict")
    model = out / "definition" / "model.tmdl"
    model.write_bytes(model.read_bytes() + b"ref table Extra\r\n")
    with pytest.raises(DeployError, match="does not match"):
        deploy(FakeClient(), out, "ws", yes=True)


def test_csv_catalog_without_lakehouse_identity(tmp_path, fixtures_dir):
    csv_file = tmp_path / "cols.csv"
    csv_file.write_text(
        "TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,DATA_TYPE\n"
        "bronze,customer__t,Customer Key,varchar\nbronze,address__t,Address Key,varchar\n",
        encoding="utf-8",
    )
    code = main(
        ["convert", str(fixtures_dir / "mini.ttl"), "-o", str(tmp_path / "csv"), "--catalog-file", str(csv_file),
         "--schema", "bronze", "--strict", "--log-level", "ERROR"]
    )
    assert code == 0
    assert (tmp_path / "csv" / "report.json").exists() and not (tmp_path / "csv" / "definition").exists()


def _parts(envelope):
    return {p["path"]: base64.b64decode(p["payload"]).decode() for p in envelope["definition"]["parts"]}


def test_update_existing_replaces_definition_and_keeps_ids(tmp_path, fixtures_dir):
    code, out = convert(tmp_path, fixtures_dir, "--strict")
    deploy(FakeClient(), out, "ws", dry_run=True)
    generated = json.loads((out / "envelope.json").read_text())
    parts = _parts(generated)

    # simulate the live item: different (older) lineageTags and a damaged entity without properties
    import re

    table = parts["tables/Customer.tmdl"]
    old_table_tag = re.search(r"^table Customer\r\n\tlineageTag: (\S+)", table, re.M).group(1)
    old_col_tag = re.search(r"column 'Customer Key'\r\n\t\tdataType: string\r\n\t\tlineageTag: (\S+)", table).group(1)
    live = dict(parts)
    live["tables/Customer.tmdl"] = table.replace(old_table_tag, "aaaaaaaa-0000-0000-0000-000000000001").replace(
        old_col_tag, "aaaaaaaa-0000-0000-0000-000000000002"
    )
    entity = parts["entities/Customer.tmdl"]
    entity_tag = re.search(r"^entity Customer\r\n\tlineageTag: (\S+)", entity, re.M).group(1)
    live["entities/Customer.tmdl"] = "entity Customer\r\n\tlineageTag: aaaaaaaa-0000-0000-0000-000000000003\r\n"
    del live["relationships.tmdl"]
    old = [{"path": k, "payload": base64.b64encode(v.encode()).decode(), "payloadType": "InlineBase64"} for k, v in live.items()]

    with pytest.raises(DeployError, match="--update-existing"):
        deploy(FakeClient(existing=["TestOnto"], definition=old), out, "ws", yes=True)

    client = FakeClient(existing=["TestOnto"], definition=old)
    result = deploy(client, out, "ws", yes=True, update_existing=True)
    method, url, body = client.posted
    assert url.endswith("/workspaces/ws-id/items/x/updateDefinition") and result["updated"]
    sent = _parts(body)
    assert set(sent) == set(parts)
    assert "\tlineageTag: aaaaaaaa-0000-0000-0000-000000000001" in sent["tables/Customer.tmdl"]
    assert "\tlineageTag: aaaaaaaa-0000-0000-0000-000000000003" in sent["entities/Customer.tmdl"]
    assert entity_tag not in sent["entities/Customer.tmdl"]
    # the key property keeps the id of its backing column (property tag == column tag)
    assert sent["entities/Customer.tmdl"].count("aaaaaaaa-0000-0000-0000-000000000002") == 1
    assert sent["tables/Customer.tmdl"].count("aaaaaaaa-0000-0000-0000-000000000002") == 1
    backup = Path(result["backup"])
    assert (backup / "entities" / "Customer.tmdl").read_text().startswith("entity Customer")
    assert not (backup / "relationships.tmdl").exists()


def test_lineage_scanner_handles_quoted_names():
    from ttl2fabric.deploy import _tmdl_objects

    text = "table T\r\n\tlineageTag: t1\r\n\r\n\tcolumn 'O''Brien Key'\r\n\t\tdataType: string\r\n\t\tlineageTag: c1\r\n"
    assert _tmdl_objects(text) == {"table T": "t1", "table T/column O'Brien Key": "c1"}


def test_envelope_platform_uses_target_name(tmp_path, fixtures_dir):
    code, out = convert(tmp_path, fixtures_dir, "--strict")
    deploy(FakeClient(), out, "ws", display_name="Other", dry_run=True)
    platform = json.loads(_parts(json.loads((out / "envelope.json").read_text()))[".platform"])
    assert platform["metadata"] == {"type": "Ontology", "displayName": "Other"}


def test_resolve_folder_rules():
    from ttl2fabric.folders import FolderError, describe, resolve_folder

    folders = [dict(f) for f in FOLDERS]
    assert resolve_folder("org/data-services/ontology", folders) == "f-onto"
    assert resolve_folder("/ORG/Data-Services/", folders) == "f-ds"
    assert resolve_folder("/", folders) is None
    with pytest.raises(FolderError, match="available: ontology"):
        resolve_folder("org/data-services/nope", folders)
    created = []
    fid = resolve_folder(
        "org/new/deeper", folders, lambda n, p: created.append((n, p)) or {"id": f"id-{n}", "displayName": n}
    )
    assert fid == "id-deeper" and created == [("new", "f-org"), ("deeper", "id-new")]
    assert describe("f-onto", folders) == "org/data-services/ontology"
    assert describe(None, folders) == "/ (workspace root)"
    guid = "12345678-1234-1234-1234-123456789012"
    with pytest.raises(FolderError, match="not found"):
        resolve_folder(guid, folders)


def test_create_into_folder(tmp_path, fixtures_dir):
    code, out = convert(tmp_path, fixtures_dir, "--strict")
    client = FakeClient()
    result = deploy(client, out, "ws", yes=True, folder="org/data-services/ontology")
    assert client.posted[2]["folderId"] == "f-onto" and result["folderId"] == "f-onto"

    client = FakeClient()
    deploy(client, out, "ws", yes=True, folder="org/ttl2fabric", create_folder=True)
    assert client.created_folders == [("ttl2fabric", "f-org")]
    assert client.posted[2]["folderId"] == "new-ttl2fabric"

    with pytest.raises(DeployError, match="--create-folder"):
        deploy(FakeClient(), out, "ws", yes=True, folder="org/missing")
    with pytest.raises(DeployError, match="needs --folder"):
        deploy(FakeClient(), out, "ws", yes=True, create_folder=True)

    client = FakeClient()
    deploy(client, out, "ws", yes=True)
    assert "folderId" not in client.posted[2]


def test_update_moves_item_only_when_folder_differs(tmp_path, fixtures_dir):
    code, out = convert(tmp_path, fixtures_dir, "--strict")
    deploy(FakeClient(), out, "ws", dry_run=True)
    current = json.loads((out / "envelope.json").read_text())["definition"]["parts"]

    client = FakeClient(existing=["TestOnto"], definition=current, folder_of={"TestOnto": "f-ds"})
    deploy(client, out, "ws", yes=True, update_existing=True, folder="org/data-services/ontology")
    assert client.moved == ("x", "f-onto")

    client = FakeClient(existing=["TestOnto"], definition=current, folder_of={"TestOnto": "f-onto"})
    deploy(client, out, "ws", yes=True, update_existing=True, folder="org/data-services/ontology")
    assert client.moved is None

    client = FakeClient(existing=["TestOnto"], definition=current, folder_of={"TestOnto": "f-onto"})
    deploy(client, out, "ws", yes=True, update_existing=True)  # no --folder: stays where it is
    assert client.moved is None

    client = FakeClient(existing=["TestOnto"], definition=current, folder_of={"TestOnto": "f-onto"})
    deploy(client, out, "ws", yes=True, update_existing=True, folder="/")
    assert client.moved == ("x", None)

    with pytest.raises(DeployError, match="folder org/data-services/ontology"):
        deploy(FakeClient(existing=["TestOnto"], folder_of={"TestOnto": "f-onto"}), out, "ws", yes=True)


def test_deploy_prints_item_url_and_mcp_endpoint(tmp_path, fixtures_dir, capsys):
    code, out = convert(tmp_path, fixtures_dir, "--strict")
    result = deploy(FakeClient(), out, "ws", yes=True)
    printed = capsys.readouterr().out
    assert "Ontology item : https://app.fabric.microsoft.com/groups/ws-id/ontologies/new-item-id" in printed
    endpoint = "https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/ws-id/items/new-item-id/ontologyEndpoint"
    assert f"MCP endpoint  : {endpoint}" in printed and result["mcpEndpoint"] == endpoint

    deploy(FakeClient(), out, "ws", dry_run=True)
    current = json.loads((out / "envelope.json").read_text())["definition"]["parts"]
    capsys.readouterr()
    result = deploy(FakeClient(existing=["TestOnto"], definition=current), out, "ws", yes=True, update_existing=True)
    printed = capsys.readouterr().out
    assert "MCP endpoint  : https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/ws-id/items/x/ontologyEndpoint" in printed
    assert result["url"].endswith("/ontologies/x")
