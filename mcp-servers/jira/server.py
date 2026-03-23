"""Jira MCP Server — search, get, create, update, comment, transitions."""

import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("jira")

JIRA_BASE = os.environ["JIRA_BASE_URL"]
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}


def _extract_adf_text(adf: dict | str | None) -> str:
    """Extract plain text from Atlassian Document Format (ADF)."""
    if not adf:
        return ""
    if isinstance(adf, str):
        return adf
    texts = []
    for block in adf.get("content", []):
        for inline in block.get("content", []):
            if inline.get("type") == "text":
                texts.append(inline.get("text", ""))
            elif inline.get("type") == "mention":
                texts.append(f"@{inline.get('attrs', {}).get('text', '')}")
        texts.append("\n")
    return "".join(texts).strip()


async def _jira(method: str, path: str, body: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        url = f"{JIRA_BASE}{path}"
        resp = await client.request(
            method, url,
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            headers=HEADERS,
            json=body,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}


@mcp.tool()
async def search_issues(
    jql: str,
    max_results: int = 20,
    fields: str = "summary,status,assignee,labels,priority",
) -> str:
    """Search Jira issues using JQL query. Returns list of issues with key, summary, status, assignee, labels."""
    data = await _jira("POST", "/rest/api/3/search/jql", {
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


@mcp.tool()
async def get_issue(issue_key: str, include_comments: bool = True) -> str:
    """Get full details of a Jira issue by key (e.g. LX-1234). Includes comments by default."""
    data = await _jira("GET", f"/rest/api/3/issue/{issue_key}")
    f = data.get("fields", {})
    result = {
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
    }
    if include_comments:
        comments_data = f.get("comment", {}).get("comments", [])
        if not comments_data:
            try:
                c_resp = await _jira("GET", f"/rest/api/3/issue/{issue_key}/comment")
                comments_data = c_resp.get("comments", [])
            except Exception:
                comments_data = []
        result["comments"] = [
            {
                "author": (c.get("author") or {}).get("displayName"),
                "body": _extract_adf_text(c.get("body", {})),
                "created": c.get("created"),
            }
            for c in comments_data
        ]
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def get_comments(issue_key: str) -> str:
    """Get all comments for a Jira issue."""
    data = await _jira("GET", f"/rest/api/3/issue/{issue_key}/comment")
    comments = [
        {
            "id": c.get("id"),
            "author": (c.get("author") or {}).get("displayName"),
            "body": _extract_adf_text(c.get("body", {})),
            "created": c.get("created"),
            "updated": c.get("updated"),
        }
        for c in data.get("comments", [])
    ]
    return json.dumps(comments, ensure_ascii=False)


@mcp.tool()
async def create_issue(
    project_key: str,
    issue_type: str,
    summary: str,
    description: str = "",
    labels: str = "",
) -> str:
    """Create a new Jira issue. Returns the created issue key and URL."""
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
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}],
        }
    if labels:
        body["fields"]["labels"] = [l.strip() for l in labels.split(",")]
    data = await _jira("POST", "/rest/api/3/issue", body)
    return json.dumps({"key": data["key"], "url": f"{JIRA_BASE}/browse/{data['key']}"}, ensure_ascii=False)


@mcp.tool()
async def update_issue(issue_key: str, fields_json: str) -> str:
    """Update fields of a Jira issue. Pass fields as JSON string."""
    fields = json.loads(fields_json)
    await _jira("PUT", f"/rest/api/3/issue/{issue_key}", {"fields": fields})
    return json.dumps({"status": "updated", "key": issue_key}, ensure_ascii=False)


@mcp.tool()
async def add_comment(issue_key: str, body_text: str) -> str:
    """Add a text comment to a Jira issue."""
    adf_body = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": body_text}]}],
        }
    }
    data = await _jira("POST", f"/rest/api/3/issue/{issue_key}/comment", adf_body)
    return json.dumps({"id": data.get("id"), "status": "comment_added"}, ensure_ascii=False)


@mcp.tool()
async def get_transitions(issue_key: str) -> str:
    """Get available status transitions for a Jira issue."""
    data = await _jira("GET", f"/rest/api/3/issue/{issue_key}/transitions")
    transitions = [{"id": t["id"], "name": t["name"]} for t in data.get("transitions", [])]
    return json.dumps(transitions, ensure_ascii=False)


@mcp.tool()
async def transition_issue(issue_key: str, transition_id: str) -> str:
    """Transition a Jira issue to a new status. Use get_transitions first to get the transition ID."""
    await _jira("POST", f"/rest/api/3/issue/{issue_key}/transitions", {
        "transition": {"id": transition_id},
    })
    return json.dumps({"status": "transitioned", "key": issue_key}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
