"""Tool registry — maps tool names to implementations and Anthropic tool schemas."""

from __future__ import annotations

import importlib
from typing import Any, Callable, Coroutine

ToolHandler = Callable[..., Coroutine[Any, Any, str]]


class ToolRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandler] = {}
        self._schemas: dict[str, dict] = {}

    def register(self, name: str, schema: dict, handler: ToolHandler) -> None:
        self._handlers[name] = handler
        self._schemas[name] = schema

    async def execute(self, name: str, params: dict) -> str:
        handler = self._handlers.get(name)
        if not handler:
            return f"Error: unknown tool '{name}'"
        try:
            return await handler(**params)
        except Exception as e:
            return f"Error executing {name}: {e}"

    def get_schemas(self, tool_names: list[str]) -> list[dict]:
        """Return Anthropic-compatible tool schemas for given tool names."""
        result = []
        for name in tool_names:
            schema = self._schemas.get(name)
            if schema:
                result.append(schema)
        return result

    def has_tool(self, name: str) -> bool:
        return name in self._handlers

    @property
    def available_tools(self) -> list[str]:
        return list(self._handlers.keys())


tool_registry = ToolRegistry()


def load_all_tools() -> None:
    """Import all tool modules to trigger registration."""
    modules = [
        "app.tools.jira_tools",
        "app.tools.gitlab_tools",
        "app.tools.db_tools",
        "app.tools.docs_tools",
        "app.tools.figma_tools",
        "app.tools.pencil_tools",
    ]
    for mod in modules:
        importlib.import_module(mod)
