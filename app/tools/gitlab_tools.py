"""GitLab tools — repos, files, branches, MRs, diffs, comments."""

import json
from urllib.parse import quote

import httpx

from app.config import settings
from app.tools.registry import tool_registry

GITLAB_URL = settings.GITLAB_URL
GITLAB_TOKEN = settings.GITLAB_TOKEN
HEADERS = {"PRIVATE-TOKEN": GITLAB_TOKEN, "Content-Type": "application/json"}


async def _gl(method: str, path: str, body: dict | None = None) -> dict | list:
    async with httpx.AsyncClient(timeout=30) as client:
        url = f"{GITLAB_URL}/api/v4{path}"
        resp = await client.request(method, url, headers=HEADERS, json=body)
        resp.raise_for_status()
        return resp.json() if resp.content else {}


# --- Handlers ---

async def read_file(project_id: int, file_path: str, ref: str = "main") -> str:
    encoded = quote(file_path, safe="")
    data = await _gl("GET", f"/projects/{project_id}/repository/files/{encoded}?ref={ref}")
    import base64
    content = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
    return content


async def list_files(project_id: int, path: str = "", ref: str = "main") -> str:
    items = await _gl("GET", f"/projects/{project_id}/repository/tree?path={quote(path)}&ref={ref}&per_page=100")
    result = [{"name": i["name"], "type": i["type"], "path": i["path"]} for i in items]
    return json.dumps(result, ensure_ascii=False)


async def list_mrs(project_id: int, state: str = "opened") -> str:
    mrs = await _gl("GET", f"/projects/{project_id}/merge_requests?state={state}&per_page=20")
    result = [{"iid": mr["iid"], "title": mr["title"], "author": mr["author"]["name"],
               "source_branch": mr["source_branch"], "target_branch": mr["target_branch"],
               "web_url": mr["web_url"], "created_at": mr["created_at"]} for mr in mrs]
    return json.dumps(result, ensure_ascii=False)


async def get_mr_diff(project_id: int, mr_iid: int) -> str:
    data = await _gl("GET", f"/projects/{project_id}/merge_requests/{mr_iid}/changes")
    changes = data.get("changes", [])
    result = []
    for c in changes:
        result.append({
            "old_path": c.get("old_path"),
            "new_path": c.get("new_path"),
            "diff": c.get("diff", "")[:5000],  # trim large diffs
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


async def add_mr_comment(project_id: int, mr_iid: int, body: str) -> str:
    data = await _gl("POST", f"/projects/{project_id}/merge_requests/{mr_iid}/discussions", {"body": body})
    return json.dumps({"id": data.get("id"), "status": "comment_added"}, ensure_ascii=False)


async def create_branch(project_id: int, branch_name: str, ref: str = "main") -> str:
    data = await _gl("POST", f"/projects/{project_id}/repository/branches", {
        "branch": branch_name, "ref": ref,
    })
    return json.dumps({"name": data.get("name"), "status": "branch_created"}, ensure_ascii=False)


async def commit_files(project_id: int, branch: str, commit_message: str, actions_json: str) -> str:
    actions = json.loads(actions_json)
    data = await _gl("POST", f"/projects/{project_id}/repository/commits", {
        "branch": branch,
        "commit_message": commit_message,
        "actions": actions,
    })
    return json.dumps({"id": data.get("id"), "short_id": data.get("short_id"), "status": "committed"}, ensure_ascii=False)


async def create_mr(project_id: int, source_branch: str, target_branch: str, title: str, description: str = "") -> str:
    data = await _gl("POST", f"/projects/{project_id}/merge_requests", {
        "source_branch": source_branch,
        "target_branch": target_branch,
        "title": title,
        "description": description,
    })
    return json.dumps({"iid": data.get("iid"), "web_url": data.get("web_url"), "status": "mr_created"}, ensure_ascii=False)


# --- Register ---

tool_registry.register("gitlab:read_file", {
    "name": "gitlab_read_file",
    "description": "Read file content from a GitLab repository.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "GitLab project ID"},
            "file_path": {"type": "string", "description": "Path to file in repo"},
            "ref": {"type": "string", "description": "Branch or commit ref (default: main)", "default": "main"},
        },
        "required": ["project_id", "file_path"],
    },
}, read_file)

tool_registry.register("gitlab:list_files", {
    "name": "gitlab_list_files",
    "description": "List files and directories in a GitLab repository path.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "GitLab project ID"},
            "path": {"type": "string", "description": "Directory path (empty for root)", "default": ""},
            "ref": {"type": "string", "description": "Branch ref", "default": "main"},
        },
        "required": ["project_id"],
    },
}, list_files)

tool_registry.register("gitlab:list_mrs", {
    "name": "gitlab_list_mrs",
    "description": "List merge requests for a GitLab project.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "GitLab project ID"},
            "state": {"type": "string", "description": "MR state: opened, closed, merged, all", "default": "opened"},
        },
        "required": ["project_id"],
    },
}, list_mrs)

tool_registry.register("gitlab:get_mr_diff", {
    "name": "gitlab_get_mr_diff",
    "description": "Get diff/changes of a merge request. Returns file changes with diffs.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "GitLab project ID"},
            "mr_iid": {"type": "integer", "description": "Merge request IID"},
        },
        "required": ["project_id", "mr_iid"],
    },
}, get_mr_diff)

tool_registry.register("gitlab:add_mr_comment", {
    "name": "gitlab_add_mr_comment",
    "description": "Add a comment/discussion to a merge request.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "GitLab project ID"},
            "mr_iid": {"type": "integer", "description": "Merge request IID"},
            "body": {"type": "string", "description": "Comment text in markdown"},
        },
        "required": ["project_id", "mr_iid", "body"],
    },
}, add_mr_comment)

tool_registry.register("gitlab:create_branch", {
    "name": "gitlab_create_branch",
    "description": "Create a new branch in a GitLab project.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "GitLab project ID"},
            "branch_name": {"type": "string", "description": "New branch name"},
            "ref": {"type": "string", "description": "Source branch/commit", "default": "main"},
        },
        "required": ["project_id", "branch_name"],
    },
}, create_branch)

tool_registry.register("gitlab:commit_files", {
    "name": "gitlab_commit_files",
    "description": "Commit file changes to a branch. Actions: [{action: create|update|delete, file_path, content}]",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "GitLab project ID"},
            "branch": {"type": "string", "description": "Target branch"},
            "commit_message": {"type": "string", "description": "Commit message"},
            "actions_json": {"type": "string", "description": "JSON array of actions: [{action, file_path, content}]"},
        },
        "required": ["project_id", "branch", "commit_message", "actions_json"],
    },
}, commit_files)

tool_registry.register("gitlab:create_mr", {
    "name": "gitlab_create_mr",
    "description": "Create a merge request in GitLab.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "GitLab project ID"},
            "source_branch": {"type": "string", "description": "Source branch"},
            "target_branch": {"type": "string", "description": "Target branch"},
            "title": {"type": "string", "description": "MR title"},
            "description": {"type": "string", "description": "MR description"},
        },
        "required": ["project_id", "source_branch", "target_branch", "title"],
    },
}, create_mr)
