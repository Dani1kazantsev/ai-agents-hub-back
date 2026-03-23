"""Database MCP Server — read-only SQL access to external PostgreSQL."""

import json
import os
import re

import asyncpg
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("db")

DATABASE_URL = os.environ["EXTERNAL_DATABASE_URL"]  # postgresql://user:pass@host:port/dbname

FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|EXEC)\b",
    re.IGNORECASE,
)

_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    return _pool


def _validate_query(sql: str) -> None:
    if FORBIDDEN_KEYWORDS.search(sql):
        raise ValueError("Only SELECT queries are allowed. Write operations are forbidden.")


@mcp.tool()
async def read_query(sql: str) -> str:
    """Execute a read-only SQL SELECT query against the database. Only SELECT statements allowed. Auto-adds LIMIT 100 if not specified."""
    _validate_query(sql)
    if "LIMIT" not in sql.upper():
        sql = sql.rstrip(";") + " LIMIT 100"

    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
        if not rows:
            return json.dumps({"columns": [], "rows": [], "count": 0}, ensure_ascii=False)
        columns = list(rows[0].keys())
        result = [dict(row) for row in rows]
        return json.dumps({"columns": columns, "rows": result, "count": len(result)}, ensure_ascii=False, default=str)


@mcp.tool()
async def describe_table(table_name: str) -> str:
    """Get column schema for a database table."""
    safe_name = re.sub(r"[^a-zA-Z0-9_]", "", table_name)
    sql = """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name = $1 AND table_schema = 'public'
        ORDER BY ordinal_position
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, safe_name)
        columns = [{"column": r["column_name"], "type": r["data_type"], "nullable": r["is_nullable"], "default": r["column_default"]} for r in rows]
        return json.dumps({"table": safe_name, "columns": columns}, ensure_ascii=False)


@mcp.tool()
async def list_tables() -> str:
    """List all tables in the public schema with approximate row counts."""
    sql = """
        SELECT t.table_name, s.n_live_tup as row_count
        FROM information_schema.tables t
        LEFT JOIN pg_stat_user_tables s ON t.table_name = s.relname
        WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
        ORDER BY t.table_name
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
        tables = [{"name": r["table_name"], "rows": r["row_count"]} for r in rows]
        return json.dumps(tables, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
