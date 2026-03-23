"""docs tools — architecture context from a GitLab documentation repository."""

import json
from urllib.parse import quote

import httpx

from app.config import settings
from app.tools.registry import tool_registry

GITLAB_URL = settings.GITLAB_URL
GITLAB_TOKEN = settings.GITLAB_TOKEN
HEADERS = {"PRIVATE-TOKEN": GITLAB_TOKEN}


async def _read_gl_file(path: str, ref: str = "main") -> str:
    encoded = quote(path, safe="")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{GITLAB_URL}/api/v4/projects/{settings.DOCS_PROJECT_ID}/repository/files/{encoded}?ref={ref}",
            headers=HEADERS,
        )
        if resp.status_code == 404:
            return ""
        resp.raise_for_status()
        import base64
        return base64.b64decode(resp.json().get("content", "")).decode("utf-8", errors="replace")


async def _list_gl_dir(path: str, ref: str = "main") -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{GITLAB_URL}/api/v4/projects/{settings.DOCS_PROJECT_ID}/repository/tree?path={quote(path)}&ref={ref}&per_page=100",
            headers=HEADERS,
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return resp.json()


async def get_context(project_name: str) -> str:
    """Get architecture context for a project."""
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


async def get_project(project_name: str) -> str:
    """Get project-specific architecture doc."""
    doc = await _read_gl_file(f"architecture/projects/{project_name}.md")
    if not doc:
        return json.dumps({"error": f"No documentation found for project '{project_name}'"}, ensure_ascii=False)
    return doc


async def get_team(project_name: str) -> str:
    """Get team info for a project."""
    doc = await _read_gl_file(f"teams/{project_name}.md")
    if not doc:
        doc = await _read_gl_file("teams/INDEX.md")
    return doc or json.dumps({"error": "No team info found"}, ensure_ascii=False)


async def search_docs(query: str) -> str:
    """Search across project documentation using GitLab search API."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{GITLAB_URL}/api/v4/projects/{settings.DOCS_PROJECT_ID}/search",
            params={"scope": "blobs", "search": query},
            headers=HEADERS,
        )
        resp.raise_for_status()
        results = resp.json()
        return json.dumps([{
            "filename": r.get("filename"),
            "data": r.get("data", "")[:500],
        } for r in results[:10]], ensure_ascii=False)


# --- Register ---

tool_registry.register("docs:get_context", {
    "name": "docs_get_context",
    "description": "Get architecture context for a project — documentation, index, and available flows.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_name": {"type": "string", "description": "Project name"},
        },
        "required": ["project_name"],
    },
}, get_context)

tool_registry.register("docs:get_project", {
    "name": "docs_get_project",
    "description": "Get project architecture documentation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_name": {"type": "string", "description": "Project name"},
        },
        "required": ["project_name"],
    },
}, get_project)

tool_registry.register("docs:get_team", {
    "name": "docs_get_team",
    "description": "Get team information for a project.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_name": {"type": "string", "description": "Project name"},
        },
        "required": ["project_name"],
    },
}, get_team)

tool_registry.register("docs:search_docs", {
    "name": "docs_search_docs",
    "description": "Search across all project documentation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
        },
        "required": ["query"],
    },
}, search_docs)
