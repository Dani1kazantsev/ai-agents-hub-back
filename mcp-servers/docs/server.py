"""Docs MCP Server — architecture context from a GitLab documentation repository."""

import base64
import json
import os
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("docs")

GITLAB_URL = os.environ["GITLAB_URL"]
GITLAB_TOKEN = os.environ["GITLAB_TOKEN"]
DOCS_PROJECT_ID = int(os.environ.get("DOCS_PROJECT_ID", "0"))
HEADERS = {"PRIVATE-TOKEN": GITLAB_TOKEN}

# Projects config — loaded from agents/config/projects.json
_default_projects_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "ai-agents-hub", "agents", "config", "projects.json")
PROJECTS_CONFIG_PATH = os.environ.get("PROJECTS_CONFIG_PATH", _default_projects_path)


async def _read_gl_file(path: str, ref: str = "main") -> str:
    encoded = quote(path, safe="")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{GITLAB_URL}/api/v4/projects/{DOCS_PROJECT_ID}/repository/files/{encoded}?ref={ref}",
            headers=HEADERS,
        )
        if resp.status_code == 404:
            return ""
        resp.raise_for_status()
        return base64.b64decode(resp.json().get("content", "")).decode("utf-8", errors="replace")


async def _list_gl_dir(path: str, ref: str = "main") -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{GITLAB_URL}/api/v4/projects/{DOCS_PROJECT_ID}/repository/tree?path={quote(path)}&ref={ref}&per_page=100",
            headers=HEADERS,
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def list_projects() -> str:
    """List all projects with their gitlab_id, jira_label, stack, and type. Use this to find project IDs."""
    try:
        with open(PROJECTS_CONFIG_PATH) as f:
            data = json.load(f)
        return json.dumps(data["projects"], ensure_ascii=False)
    except FileNotFoundError:
        return json.dumps({"error": f"projects.json not found at {PROJECTS_CONFIG_PATH}"}, ensure_ascii=False)


@mcp.tool()
async def get_context(project_name: str) -> str:
    """Get architecture context for a project — documentation, index, and available flows."""
    project_doc = await _read_gl_file(f"architecture/projects/{project_name}.md")
    index = await _read_gl_file("architecture/INDEX.md")
    flows_items = await _list_gl_dir("architecture/flows")
    flow_names = [f["name"] for f in flows_items if f["type"] == "blob"]

    return json.dumps({
        "project": project_name,
        "project_doc": project_doc or "No project documentation found.",
        "index": index or "No index found.",
        "available_flows": flow_names,
    }, ensure_ascii=False)


@mcp.tool()
async def get_project(project_name: str) -> str:
    """Get project architecture documentation."""
    doc = await _read_gl_file(f"architecture/projects/{project_name}.md")
    if not doc:
        return json.dumps({"error": f"No documentation found for project '{project_name}'"}, ensure_ascii=False)
    return doc


@mcp.tool()
async def get_team(project_name: str) -> str:
    """Get team information for a project."""
    doc = await _read_gl_file(f"teams/{project_name}.md")
    if not doc:
        doc = await _read_gl_file("teams/INDEX.md")
    return doc or json.dumps({"error": "No team info found"}, ensure_ascii=False)


@mcp.tool()
async def search_docs(query: str) -> str:
    """Search across all project documentation."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{GITLAB_URL}/api/v4/projects/{DOCS_PROJECT_ID}/search",
            params={"scope": "blobs", "search": query},
            headers=HEADERS,
        )
        resp.raise_for_status()
        results = resp.json()
        return json.dumps([{
            "filename": r.get("filename"),
            "data": r.get("data", "")[:500],
        } for r in results[:10]], ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
