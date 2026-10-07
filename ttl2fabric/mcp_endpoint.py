"""Ontology item and MCP endpoint URLs (see Fabric docs: 'Consume ontology as an MCP server')."""

from __future__ import annotations


def item_url(workspace_id: str, item_id: str) -> str:
    return f"https://app.fabric.microsoft.com/groups/{workspace_id}/ontologies/{item_id}"


def mcp_endpoint(workspace_id: str, item_id: str) -> str:
    return f"https://api.fabric.microsoft.com/v1/mcp/dataPlane/workspaces/{workspace_id}/items/{item_id}/ontologyEndpoint"


def endpoint_lines(workspace_id: str, item_id: str) -> str:
    return f"  Ontology item : {item_url(workspace_id, item_id)}\n  MCP endpoint  : {mcp_endpoint(workspace_id, item_id)}"
