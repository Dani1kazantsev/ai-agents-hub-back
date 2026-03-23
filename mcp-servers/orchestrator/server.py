"""Orchestrator MCP Server — spawn and manage sub-agents via backend HTTP callbacks."""

import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("orchestrator")

BACKEND_URL = os.environ["ORCHESTRATOR_BACKEND_URL"]
AUTH_TOKEN = os.environ["ORCHESTRATOR_AUTH_TOKEN"]
PARENT_SESSION_ID = os.environ["ORCHESTRATOR_PARENT_SESSION_ID"]
USER_ID = os.environ["ORCHESTRATOR_USER_ID"]
DEPTH = int(os.environ.get("ORCHESTRATOR_DEPTH", "1"))

HEADERS = {
    "Authorization": f"Bearer {AUTH_TOKEN}",
    "Content-Type": "application/json",
}


@mcp.tool()
async def spawn_agent(agent_name: str, task: str) -> str:
    """Spawn a sub-agent to handle a specific task. Blocks until the sub-agent completes and returns its result."""
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{BACKEND_URL}/api/internal/subagents/spawn",
            headers=HEADERS,
            json={
                "parent_session_id": PARENT_SESSION_ID,
                "agent_name": agent_name,
                "task": task,
                "user_id": USER_ID,
                "depth": DEPTH,
            },
        )
        if resp.status_code != 200:
            return json.dumps({"error": f"Failed to spawn agent: {resp.text}"}, ensure_ascii=False)
        data = resp.json()
        return json.dumps(data, ensure_ascii=False)


@mcp.tool()
async def list_running() -> str:
    """List all running sub-agents for the current parent session."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BACKEND_URL}/api/internal/subagents/{PARENT_SESSION_ID}",
            headers=HEADERS,
        )
        if resp.status_code != 200:
            return json.dumps({"error": resp.text}, ensure_ascii=False)
        return json.dumps(resp.json(), ensure_ascii=False)


@mcp.tool()
async def get_result(run_id: str) -> str:
    """Get the result of a completed sub-agent run by its ID."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BACKEND_URL}/api/internal/subagents/result/{run_id}",
            headers=HEADERS,
        )
        if resp.status_code != 200:
            return json.dumps({"error": resp.text}, ensure_ascii=False)
        return json.dumps(resp.json(), ensure_ascii=False)


@mcp.tool()
async def kill_agent(run_id: str) -> str:
    """Kill a running sub-agent by its run ID."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.delete(
            f"{BACKEND_URL}/api/internal/subagents/{run_id}",
            headers=HEADERS,
        )
        if resp.status_code != 200:
            return json.dumps({"error": resp.text}, ensure_ascii=False)
        return json.dumps({"status": "killed", "run_id": run_id}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
