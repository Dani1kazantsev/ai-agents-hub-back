"""Memory MCP Server — agent knowledge persistence across sessions."""

import json
import os

import asyncpg
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("memory")

DATABASE_URL = os.environ["MEMORY_DATABASE_URL"]
AGENT_ID = os.environ["MEMORY_AGENT_ID"]
USER_ID = os.environ.get("MEMORY_USER_ID", "")
SCOPE = os.environ.get("MEMORY_SCOPE", "personal")

_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    return _pool


def _user_filter() -> tuple[str, list]:
    """Build user_id filter based on scope."""
    if SCOPE == "shared":
        return "AND scope = 'shared'", []
    if USER_ID:
        return "AND (user_id = $3 OR scope = 'shared')", [USER_ID]
    return "", []


@mcp.tool()
async def search(query: str, limit: int = 10) -> str:
    """Search agent memory entries by text query. Returns matching entries sorted by relevance."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        # Full-text search using ILIKE for simplicity
        sql = """
            SELECT key, content, tags, updated_at
            FROM agent_memories
            WHERE agent_id = $1
              AND (content ILIKE '%' || $2 || '%' OR key ILIKE '%' || $2 || '%')
        """
        params: list = [AGENT_ID, query]

        if SCOPE == "shared":
            sql += " AND scope = 'shared'"
        elif USER_ID:
            sql += " AND (user_id = $3 OR scope = 'shared')"
            params.append(USER_ID)

        sql += f" ORDER BY updated_at DESC LIMIT {min(limit, 50)}"

        rows = await conn.fetch(sql, *params)
        results = [
            {"key": r["key"], "content": r["content"][:2000], "tags": r["tags"] or [], "updated_at": str(r["updated_at"])}
            for r in rows
        ]
        return json.dumps(results, ensure_ascii=False)


@mcp.tool()
async def read(key: str) -> str:
    """Read a specific memory entry by key. Returns the full content."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        sql = "SELECT key, content, tags, updated_at FROM agent_memories WHERE agent_id = $1 AND key = $2"
        params: list = [AGENT_ID, key]

        if SCOPE == "shared":
            sql += " AND scope = 'shared'"
        elif USER_ID:
            sql += " AND (user_id = $3 OR scope = 'shared')"
            params.append(USER_ID)

        row = await conn.fetchrow(sql, *params)
        if not row:
            return json.dumps({"error": f"Memory entry '{key}' not found"}, ensure_ascii=False)
        return json.dumps(
            {"key": row["key"], "content": row["content"], "tags": row["tags"] or [], "updated_at": str(row["updated_at"])},
            ensure_ascii=False,
        )


@mcp.tool()
async def write(key: str, content: str, tags: str = "") -> str:
    """Write or update a memory entry. Upserts by agent_id + user_id + key. Tags are comma-separated."""
    import uuid

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    token_count = len(content.split())  # rough estimate

    pool = await _get_pool()
    async with pool.acquire() as conn:
        user_id = USER_ID if USER_ID and SCOPE == "personal" else None

        # Check if exists
        check_sql = "SELECT id FROM agent_memories WHERE agent_id = $1 AND key = $2"
        check_params: list = [AGENT_ID, key]
        if user_id:
            check_sql += " AND user_id = $3"
            check_params.append(user_id)
        else:
            check_sql += " AND user_id IS NULL"

        existing = await conn.fetchrow(check_sql, *check_params)

        if existing:
            update_sql = """
                UPDATE agent_memories
                SET content = $1, tags = $2, token_count = $3, source = 'agent_write', updated_at = NOW()
                WHERE id = $4
            """
            await conn.execute(update_sql, content, json.dumps(tag_list), token_count, existing["id"])
            return json.dumps({"status": "updated", "key": key}, ensure_ascii=False)
        else:
            insert_sql = """
                INSERT INTO agent_memories (id, agent_id, user_id, scope, key, content, tags, source, token_count, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'agent_write', $8, NOW(), NOW())
            """
            new_id = uuid.uuid4()
            await conn.execute(
                insert_sql, new_id, AGENT_ID, user_id, SCOPE, key, content, json.dumps(tag_list), token_count
            )
            return json.dumps({"status": "created", "key": key}, ensure_ascii=False)


@mcp.tool()
async def list(prefix: str = "") -> str:
    """List all memory keys for this agent, optionally filtered by prefix."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        sql = "SELECT key, token_count, updated_at FROM agent_memories WHERE agent_id = $1"
        params: list = [AGENT_ID]

        if prefix:
            sql += " AND key LIKE $2 || '%'"
            params.append(prefix)

        if SCOPE == "shared":
            idx = len(params) + 1
            sql += f" AND scope = 'shared'"
        elif USER_ID:
            idx = len(params) + 1
            sql += f" AND (user_id = ${idx} OR scope = 'shared')"
            params.append(USER_ID)

        sql += " ORDER BY key"

        rows = await conn.fetch(sql, *params)
        results = [{"key": r["key"], "token_count": r["token_count"], "updated_at": str(r["updated_at"])} for r in rows]
        return json.dumps(results, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
