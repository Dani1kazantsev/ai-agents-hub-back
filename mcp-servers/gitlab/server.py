"""GitLab MCP Server — repos, files, branches, MRs, diffs, comments."""

import base64
import json
import os
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("gitlab")

GITLAB_URL = os.environ["GITLAB_URL"]
GITLAB_TOKEN = os.environ["GITLAB_TOKEN"]
HEADERS = {"PRIVATE-TOKEN": GITLAB_TOKEN, "Content-Type": "application/json"}


async def _gl(method: str, path: str, body: dict | None = None) -> dict | list:
    async with httpx.AsyncClient(timeout=30) as client:
        url = f"{GITLAB_URL}/api/v4{path}"
        resp = await client.request(method, url, headers=HEADERS, json=body)
        resp.raise_for_status()
        return resp.json() if resp.content else {}


@mcp.tool()
async def read_file(project_id: int, file_path: str, ref: str = "main") -> str:
    """Read file content from a GitLab repository."""
    encoded = quote(file_path, safe="")
    data = await _gl("GET", f"/projects/{project_id}/repository/files/{encoded}?ref={ref}")
    content = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
    return content


@mcp.tool()
async def list_files(project_id: int, path: str = "", ref: str = "main") -> str:
    """List files and directories in a GitLab repository path."""
    items = await _gl("GET", f"/projects/{project_id}/repository/tree?path={quote(path)}&ref={ref}&per_page=100")
    result = [{"name": i["name"], "type": i["type"], "path": i["path"]} for i in items]
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def list_mrs(project_id: int, state: str = "opened") -> str:
    """List merge requests for a GitLab project."""
    mrs = await _gl("GET", f"/projects/{project_id}/merge_requests?state={state}&per_page=20")
    result = [{
        "iid": mr["iid"],
        "title": mr["title"],
        "author": mr["author"]["name"],
        "source_branch": mr["source_branch"],
        "target_branch": mr["target_branch"],
        "web_url": mr["web_url"],
        "created_at": mr["created_at"],
    } for mr in mrs]
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def get_mr_diff(project_id: int, mr_iid: int) -> str:
    """Get diff/changes of a merge request. Returns file changes with diffs."""
    data = await _gl("GET", f"/projects/{project_id}/merge_requests/{mr_iid}/changes")
    changes = data.get("changes", [])
    result = []
    for c in changes:
        result.append({
            "old_path": c.get("old_path"),
            "new_path": c.get("new_path"),
            "diff": c.get("diff", "")[:5000],
            "new_file": c.get("new_file"),
            "deleted_file": c.get("deleted_file"),
        })
    return json.dumps({
        "title": data.get("title"),
        "description": data.get("description"),
        "source_branch": data.get("source_branch"),
        "target_branch": data.get("target_branch"),
        "changes": result,
        "diff_refs": data.get("diff_refs"),
    }, ensure_ascii=False)


@mcp.tool()
async def add_mr_comment(project_id: int, mr_iid: int, body: str) -> str:
    """Add a comment/discussion to a merge request."""
    data = await _gl("POST", f"/projects/{project_id}/merge_requests/{mr_iid}/discussions", {"body": body})
    return json.dumps({"id": data.get("id"), "status": "comment_added"}, ensure_ascii=False)


@mcp.tool()
async def create_branch(project_id: int, branch_name: str, ref: str = "main") -> str:
    """Create a new branch in a GitLab project."""
    data = await _gl("POST", f"/projects/{project_id}/repository/branches", {
        "branch": branch_name, "ref": ref,
    })
    return json.dumps({"name": data.get("name"), "status": "branch_created"}, ensure_ascii=False)


@mcp.tool()
async def commit_files(project_id: int, branch: str, commit_message: str, actions_json: str) -> str:
    """Commit file changes to a branch. Actions: [{action: create|update|delete, file_path, content}]"""
    actions = json.loads(actions_json)
    data = await _gl("POST", f"/projects/{project_id}/repository/commits", {
        "branch": branch,
        "commit_message": commit_message,
        "actions": actions,
    })
    return json.dumps({"id": data.get("id"), "short_id": data.get("short_id"), "status": "committed"}, ensure_ascii=False)


@mcp.tool()
async def create_mr(
    project_id: int,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str = "",
) -> str:
    """Create a merge request in GitLab."""
    data = await _gl("POST", f"/projects/{project_id}/merge_requests", {
        "source_branch": source_branch,
        "target_branch": target_branch,
        "title": title,
        "description": description,
    })
    return json.dumps({"iid": data.get("iid"), "web_url": data.get("web_url"), "status": "mr_created"}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
