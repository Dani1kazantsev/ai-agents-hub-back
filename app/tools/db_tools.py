"""Database tools — read-only SQL access to external PostgreSQL."""

import json
import re

from sqlalchemy import text

from app.db import async_session_factory
from app.tools.registry import tool_registry

FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|EXEC)\b",
    re.IGNORECASE,
)


def _validate_query(sql: str) -> None:
    if FORBIDDEN_KEYWORDS.search(sql):
        raise ValueError("Only SELECT queries are allowed. Write operations are forbidden.")


async def read_query(sql: str) -> str:
    _validate_query(sql)
    if "LIMIT" not in sql.upper():
        sql = sql.rstrip(";") + " LIMIT 100"

    async with async_session_factory() as session:
        result = await session.execute(text(sql))
        columns = list(result.keys())
        rows = [dict(zip(columns, row)) for row in result.fetchall()]
        return json.dumps({"columns": columns, "rows": rows, "count": len(rows)}, ensure_ascii=False, default=str)


async def describe_table(table_name: str) -> str:
    safe_name = re.sub(r"[^a-zA-Z0-9_]", "", table_name)
    sql = f"""
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name = '{safe_name}' AND table_schema = 'public'
        ORDER BY ordinal_position
    """
    async with async_session_factory() as session:
        result = await session.execute(text(sql))
        columns = [dict(zip(["column", "type", "nullable", "default"], row)) for row in result.fetchall()]
        return json.dumps({"table": safe_name, "columns": columns}, ensure_ascii=False)


async def list_tables() -> str:
    sql = """
        SELECT table_name, pg_stat_user_tables.n_live_tup as row_count
        FROM information_schema.tables
        LEFT JOIN pg_stat_user_tables ON table_name = relname
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """
    async with async_session_factory() as session:
        result = await session.execute(text(sql))
        tables = [{"name": row[0], "rows": row[1]} for row in result.fetchall()]
        return json.dumps(tables, ensure_ascii=False)


# --- Register ---

tool_registry.register("db:read_query", {
    "name": "db_read_query",
    "description": "Execute a read-only SQL SELECT query against the database. Only SELECT statements allowed. Auto-adds LIMIT 100 if not specified.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "SQL SELECT query"},
        },
        "required": ["sql"],
    },
}, read_query)

tool_registry.register("db:describe_table", {
    "name": "db_describe_table",
    "description": "Get column schema for a database table.",
    "input_schema": {
        "type": "object",
        "properties": {
            "table_name": {"type": "string", "description": "Table name"},
        },
        "required": ["table_name"],
    },
}, describe_table)

tool_registry.register("db:list_tables", {
    "name": "db_list_tables",
    "description": "List all tables in the public schema with approximate row counts.",
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}, list_tables)
