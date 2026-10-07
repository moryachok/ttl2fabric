"""Minimal Fabric REST + OneLake DFS client (auth via azure-identity)."""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
from typing import Any, Iterator, Optional

import requests

log = logging.getLogger(__name__)

FABRIC_API = "https://api.fabric.microsoft.com/v1"
ONELAKE_DFS = "https://onelake.dfs.fabric.microsoft.com"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
STORAGE_SCOPE = "https://storage.azure.com/.default"
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class FabricError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, body: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.body = body


def is_guid(value: str) -> bool:
    return bool(_GUID.match(value or ""))


def make_credential(auth: str = "cli"):
    try:
        from azure.identity import AzureCliCredential, DefaultAzureCredential, InteractiveBrowserCredential
    except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
        raise FabricError("azure-identity is required: pip install -r requirements.txt") from exc
    if auth == "cli":
        return AzureCliCredential(process_timeout=60)
    if auth == "interactive":
        return InteractiveBrowserCredential()
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


class FabricClient:
    def __init__(self, credential=None, auth: str = "cli", max_retries: int = 6, timeout: int = 60):
        self._credential = credential
        self._auth = auth
        self._tokens: dict[str, tuple[str, float]] = {}
        self._session = requests.Session()
        self._max_retries = max_retries
        self._timeout = timeout

    # ------------------------------------------------------------------ auth
    def _token(self, scope: str) -> str:
        cached = self._tokens.get(scope)
        if cached and cached[1] - time.time() > 120:
            return cached[0]
        if self._credential is None:
            self._credential = make_credential(self._auth)
        try:
            tok = self._credential.get_token(scope)
        except Exception as exc:  # azure-identity raises several exception types
            raise FabricError(
                f"Failed to acquire a token for {scope}: {exc}. Run 'az login' (or use --auth default)."
            ) from exc
        self._tokens[scope] = (tok.token, float(tok.expires_on))
        return tok.token

    # --------------------------------------------------------------- request
    def request(
        self,
        method: str,
        url: str,
        scope: str = FABRIC_SCOPE,
        *,
        json_body: Any = None,
        headers: Optional[dict] = None,
        ok_statuses: tuple[int, ...] = (200, 201, 202),
        allow_404: bool = False,
    ) -> Optional[requests.Response]:
        hdrs = {"Authorization": f"Bearer {self._token(scope)}"}
        if scope == STORAGE_SCOPE:
            hdrs["x-ms-version"] = "2023-11-03"
        hdrs.update(headers or {})
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._session.request(method, url, headers=hdrs, json=json_body, timeout=self._timeout)
            except requests.RequestException as exc:
                if attempt <= self._max_retries:
                    time.sleep(min(2**attempt, 30))
                    continue
                raise FabricError(f"{method} {url} failed: {exc}") from exc
            if resp.status_code in ok_statuses:
                return resp
            if resp.status_code == 404 and allow_404:
                return None
            if resp.status_code in (429, 500, 502, 503, 504) and attempt <= self._max_retries:
                delay = _retry_after(resp) or min(2**attempt, 60)
                log.debug("HTTP %s from %s; retrying in %ss", resp.status_code, url, delay)
                time.sleep(delay)
                continue
            raise FabricError(
                f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:800]}",
                status=resp.status_code,
                body=resp.text,
            )

    def get_json(self, url: str, scope: str = FABRIC_SCOPE) -> dict:
        resp = self.request("GET", url, scope)
        return resp.json() if resp is not None and resp.content else {}

    def paginate(self, url: str, key: str = "value") -> Iterator[dict]:
        while url:
            payload = self.get_json(url)
            yield from payload.get(key) or []
            url = payload.get("continuationUri") or (
                _with_query(url, "continuationToken", payload["continuationToken"])
                if payload.get("continuationToken")
                else None
            )

    # ----------------------------------------------------------- resolution
    def resolve_workspace(self, name_or_id: str) -> dict:
        if is_guid(name_or_id):
            return self.get_json(f"{FABRIC_API}/workspaces/{name_or_id}")
        matches = [w for w in self.paginate(f"{FABRIC_API}/workspaces") if w.get("displayName") == name_or_id]
        if not matches:
            raise FabricError(f"Workspace '{name_or_id}' not found (or no access)")
        if len(matches) > 1:
            raise FabricError(f"Workspace name '{name_or_id}' is ambiguous; pass the workspace id")
        return matches[0]

    def resolve_lakehouse(self, workspace_id: str, name_or_id: str) -> dict:
        if is_guid(name_or_id):
            return self.get_json(f"{FABRIC_API}/workspaces/{workspace_id}/lakehouses/{name_or_id}")
        for lh in self.paginate(f"{FABRIC_API}/workspaces/{workspace_id}/lakehouses"):
            if lh.get("displayName") == name_or_id:
                return self.get_json(f"{FABRIC_API}/workspaces/{workspace_id}/lakehouses/{lh['id']}")
        raise FabricError(f"Lakehouse '{name_or_id}' not found in workspace {workspace_id}")

    def list_items(self, workspace_id: str, item_type: Optional[str] = None) -> list[dict]:
        url = f"{FABRIC_API}/workspaces/{workspace_id}/items"
        if item_type:
            url += f"?type={urllib.parse.quote(item_type)}"
        return list(self.paginate(url))

    # -------------------------------------------------------------- folders
    def list_folders(self, workspace_id: str) -> list[dict]:
        return list(self.paginate(f"{FABRIC_API}/workspaces/{workspace_id}/folders?recursive=true"))

    def create_folder(self, workspace_id: str, name: str, parent_id: Optional[str]) -> dict:
        body = {"displayName": name, **({"parentFolderId": parent_id} if parent_id else {})}
        resp = self.request("POST", f"{FABRIC_API}/workspaces/{workspace_id}/folders", json_body=body)
        return resp.json()

    def move_item(self, workspace_id: str, item_id: str, folder_id: Optional[str]) -> None:
        body = {"targetFolderId": folder_id} if folder_id else {}
        self.request("POST", f"{FABRIC_API}/workspaces/{workspace_id}/items/{item_id}/move", json_body=body)

    # -------------------------------------------------------------- LRO
    def wait_operation(self, operation_id: str, timeout_s: int = 1800, poll_s: float = 5.0) -> dict:
        deadline = time.time() + timeout_s
        while True:
            op = self.get_json(f"{FABRIC_API}/operations/{operation_id}")
            status = op.get("status")
            log.info("Operation %s: %s", operation_id, status)
            if status == "Succeeded":
                return op
            if status in ("Failed", "Cancelled"):
                err = op.get("error") or {}
                raise FabricError(
                    f"Operation {operation_id} {status}: {err.get('errorCode') or err.get('code')}: {err.get('message')}",
                    body=str(op),
                )
            if time.time() > deadline:
                raise FabricError(f"Operation {operation_id} timed out after {timeout_s}s (last status {status})")
            time.sleep(poll_s)

    def operation_result(self, operation_id: str) -> Optional[dict]:
        resp = self.request("GET", f"{FABRIC_API}/operations/{operation_id}/result", allow_404=True)
        return resp.json() if resp is not None and resp.content else None

    def get_definition(self, workspace_id: str, item_id: str) -> dict:
        """Item definition envelope ({"definition": {"parts": [...]}}), following the LRO when needed."""
        resp = self.request(
            "POST",
            f"{FABRIC_API}/workspaces/{workspace_id}/items/{item_id}/getDefinition",
            headers={"Content-Length": "0"},
        )
        if resp.status_code == 200 and resp.content:
            return resp.json()
        op_id = resp.headers.get("x-ms-operation-id")
        if not op_id:
            raise FabricError("getDefinition returned 202 without x-ms-operation-id")
        self.wait_operation(op_id, poll_s=3)
        result = self.operation_result(op_id)
        if not result:
            raise FabricError(f"getDefinition operation {op_id} returned no result")
        return result

    def run_lro(self, method: str, url: str, json_body: Any = None) -> Optional[dict]:
        """Send a request that may complete synchronously (200/201) or as a long-running operation (202)."""
        resp = self.request(method, url, json_body=json_body)
        if resp.status_code in (200, 201):
            return resp.json() if resp.content else None
        op_id = resp.headers.get("x-ms-operation-id")
        if not op_id:
            return None
        log.info("Accepted; polling operation %s", op_id)
        self.wait_operation(op_id)
        try:
            return self.operation_result(op_id)
        except FabricError:
            return None

    # ------------------------------------------------------------ OneLake
    def dfs_list(self, workspace_id: str, path: str, recursive: bool = False) -> list[tuple[str, bool, int]]:
        """List a OneLake directory. Returns (name relative to `path`, is_dir, size)."""
        base = (
            f"{ONELAKE_DFS}/{workspace_id}?resource=filesystem&recursive={'true' if recursive else 'false'}"
            f"&directory={urllib.parse.quote(path, safe='')}"
        )
        prefix = path.rstrip("/") + "/"
        out: list[tuple[str, bool, int]] = []
        continuation: Optional[str] = None
        while True:
            url = base if not continuation else f"{base}&continuation={urllib.parse.quote(continuation, safe='')}"
            resp = self.request("GET", url, STORAGE_SCOPE, allow_404=True)
            if resp is None:
                return out
            for entry in resp.json().get("paths") or []:
                name = entry.get("name", "")
                if name.startswith(prefix):
                    out.append(
                        (
                            name[len(prefix) :],
                            str(entry.get("isDirectory", "false")).lower() == "true",
                            int(entry.get("contentLength") or 0),
                        )
                    )
            continuation = resp.headers.get("x-ms-continuation")
            if not continuation:
                return out

    def dfs_read(self, workspace_id: str, path: str) -> Optional[bytes]:
        url = f"{ONELAKE_DFS}/{workspace_id}/{urllib.parse.quote(path)}"
        resp = self.request("GET", url, STORAGE_SCOPE, allow_404=True)
        return resp.content if resp is not None else None


def _retry_after(resp: requests.Response) -> Optional[float]:
    value = resp.headers.get("Retry-After")
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _with_query(url: str, key: str, value: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    query[key] = value
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
