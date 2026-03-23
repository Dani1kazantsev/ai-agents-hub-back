"""Jira tools — search, get, create, update, comment, transitions."""

import json

import httpx

from app.config import settings
from app.tools.registry import tool_registry

JIRA_BASE = settings.JIRA_BASE_URL
JIRA_AUTH = (settings.JIRA_EMAIL, settings.JIRA_API_TOKEN)
HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}


async def _jira_request(method: str, path: str, body: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        url = f"{JIRA_BASE}{path}"
        resp = await client.request(method, url, auth=JIRA_AUTH, headers=HEADERS, json=body)
        resp.raise_for_status()
        return resp.json() if resp.content else {}


# --- Tool handlers ---

async def search_issues(jql: str, max_results: int = 20, fields: str = "summary,status,assignee,labels,priority") -> str:
    data = await _jira_request("POST", "/rest/api/3/search/jql", {
        "jql": jql,
        "maxResults": max_results,
        "fields": fields.split(","),
    })
    issues = data.get("issues", [])
    result = []
    for issue in issues:
        f = issue.get("fields", {})
        result.append({
            "key": issue["key"],
            "summary": f.get("summary"),
            "status": f.get("status", {}).get("name"),
            "assignee": (f.get("assignee") or {}).get("displayName"),
            "labels": f.get("labels", []),
            "priority": (f.get("priority") or {}).get("name"),
        })
    return json.dumps(result, ensure_ascii=False)


async def get_issue(issue_key: str) -> str:
    data = await _jira_request("GET", f"/rest/api/3/issue/{issue_key}")
    f = data.get("fields", {})
    return json.dumps({
        "key": data["key"],
        "summary": f.get("summary"),
        "description": f.get("description"),
        "status": f.get("status", {}).get("name"),
        "assignee": (f.get("assignee") or {}).get("displayName"),
        "labels": f.get("labels", []),
        "priority": (f.get("priority") or {}).get("name"),
        "story_points": f.get("customfield_10028"),
        "branch": f.get("customfield_10039"),
        "result": f.get("customfield_10069"),
        "created": f.get("created"),
        "updated": f.get("updated"),
    }, ensure_ascii=False)


async def create_issue(project_key: str, issue_type: str, summary: str, description: str = "", labels: str = "") -> str:
    body: dict = {
        "fields": {
            "project": {"key": project_key},
            "issuetype": {"name": issue_type},
            "summary": summary,
        }
    }
    if description:
        body["fields"]["description"] = {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}]
        }
    if labels:
        body["fields"]["labels"] = [l.strip() for l in labels.split(",")]
    data = await _jira_request("POST", "/rest/api/3/issue", body)
    return json.dumps({"key": data["key"], "url": f"{JIRA_BASE}/browse/{data['key']}"}, ensure_ascii=False)


async def update_issue(issue_key: str, fields_json: str) -> str:
    fields = json.loads(fields_json)
    await _jira_request("PUT", f"/rest/api/3/issue/{issue_key}", {"fields": fields})
    return json.dumps({"status": "updated", "key": issue_key}, ensure_ascii=False)


async def add_comment(issue_key: str, body_text: str) -> str:
    adf_body = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": body_text}]}]
        }
    }
    data = await _jira_request("POST", f"/rest/api/3/issue/{issue_key}/comment", adf_body)
    return json.dumps({"id": data.get("id"), "status": "comment_added"}, ensure_ascii=False)


async def get_transitions(issue_key: str) -> str:
    data = await _jira_request("GET", f"/rest/api/3/issue/{issue_key}/transitions")
    transitions = [{"id": t["id"], "name": t["name"]} for t in data.get("transitions", [])]
    return json.dumps(transitions, ensure_ascii=False)


async def transition_issue(issue_key: str, transition_id: str) -> str:
    await _jira_request("POST", f"/rest/api/3/issue/{issue_key}/transitions", {
        "transition": {"id": transition_id}
    })
    return json.dumps({"status": "transitioned", "key": issue_key}, ensure_ascii=False)


# --- Register tools ---

tool_registry.register("jira:search_issues", {
    "name": "jira_search_issues",
    "description": "Search Jira issues using JQL query. Returns list of issues with key, summary, status, assignee, labels.",
    "input_schema": {
        "type": "object",
        "properties": {
            "jql": {"type": "string", "description": "JQL query string"},
            "max_results": {"type": "integer", "description": "Max results (default 20)", "default": 20},
            "fields": {"type": "string", "description": "Comma-separated field names", "default": "summary,status,assignee,labels,priority"},
        },
        "required": ["jql"],
    },
}, search_issues)

tool_registry.register("jira:get_issue", {
    "name": "jira_get_issue",
    "description": "Get full details of a Jira issue by key (e.g. LX-1234).",
    "input_schema": {
        "type": "object",
        "properties": {
            "issue_key": {"type": "string", "description": "Issue key (e.g. LX-1234)"},
        },
        "required": ["issue_key"],
    },
}, get_issue)

tool_registry.register("jira:create_issue", {
    "name": "jira_create_issue",
    "description": "Create a new Jira issue. Returns the created issue key and URL.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_key": {"type": "string", "description": "Jira project key (e.g. LX)"},
            "issue_type": {"type": "string", "description": "Issue type: Issue, Bug, Story, Epic, Task"},
            "summary": {"type": "string", "description": "Issue title"},
            "description": {"type": "string", "description": "Issue description text"},
            "labels": {"type": "string", "description": "Comma-separated labels"},
        },
        "required": ["project_key", "issue_type", "summary"],
    },
}, create_issue)

tool_registry.register("jira:update_issue", {
    "name": "jira_update_issue",
    "description": "Update fields of a Jira issue. Pass fields as JSON string.",
    "input_schema": {
        "type": "object",
        "properties": {
            "issue_key": {"type": "string", "description": "Issue key"},
            "fields_json": {"type": "string", "description": "JSON string of fields to update"},
        },
        "required": ["issue_key", "fields_json"],
    },
}, update_issue)

tool_registry.register("jira:add_comment", {
    "name": "jira_add_comment",
    "description": "Add a text comment to a Jira issue.",
    "input_schema": {
        "type": "object",
        "properties": {
            "issue_key": {"type": "string", "description": "Issue key"},
            "body_text": {"type": "string", "description": "Comment text"},
        },
        "required": ["issue_key", "body_text"],
    },
}, add_comment)

tool_registry.register("jira:get_transitions", {
    "name": "jira_get_transitions",
    "description": "Get available status transitions for a Jira issue.",
    "input_schema": {
        "type": "object",
        "properties": {
            "issue_key": {"type": "string", "description": "Issue key"},
        },
        "required": ["issue_key"],
    },
}, get_transitions)

tool_registry.register("jira:transition_issue", {
    "name": "jira_transition_issue",
    "description": "Transition a Jira issue to a new status. Use get_transitions first to get the transition ID.",
    "input_schema": {
        "type": "object",
        "properties": {
            "issue_key": {"type": "string", "description": "Issue key"},
            "transition_id": {"type": "string", "description": "Transition ID from get_transitions"},
        },
        "required": ["issue_key", "transition_id"],
    },
}, transition_issue)
