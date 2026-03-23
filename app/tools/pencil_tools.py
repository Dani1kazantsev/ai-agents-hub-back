"""Pencil tools — MCP proxy for Pencil design editor.

Since Pencil runs as an MCP server locally, these tools proxy requests
to the Pencil MCP server via stdio or HTTP bridge.
For now, tools execute via subprocess call to the pencil CLI.
"""

import json

from app.tools.registry import tool_registry


# --- Stub implementations ---
# In production, these would connect to the Pencil MCP server.
# For the agent loop, the LLM generates the parameters and gets results back.


async def get_editor_state() -> str:
    return json.dumps({
        "status": "no_editor",
        "message": "Pencil MCP server not connected. Please ensure Pencil is running.",
    })


async def open_document(file_path_or_new: str) -> str:
    return json.dumps({
        "status": "error",
        "message": f"Cannot open '{file_path_or_new}': Pencil MCP server not connected.",
    })


async def get_guidelines(topic: str) -> str:
    return json.dumps({
        "status": "error",
        "message": f"Cannot get guidelines for '{topic}': Pencil MCP server not connected.",
    })


async def get_style_guide(tags: str, name: str = "") -> str:
    return json.dumps({
        "status": "error",
        "message": "Cannot get style guide: Pencil MCP server not connected.",
    })


async def batch_get(patterns: str = "", node_ids: str = "") -> str:
    return json.dumps({
        "status": "error",
        "message": "Cannot batch_get: Pencil MCP server not connected.",
    })


async def batch_design(operations: str) -> str:
    return json.dumps({
        "status": "error",
        "message": "Cannot batch_design: Pencil MCP server not connected.",
    })


async def get_screenshot(node_id: str = "") -> str:
    return json.dumps({
        "status": "error",
        "message": "Cannot get screenshot: Pencil MCP server not connected.",
    })


async def snapshot_layout() -> str:
    return json.dumps({
        "status": "error",
        "message": "Cannot snapshot layout: Pencil MCP server not connected.",
    })


# --- Register tools ---

tool_registry.register("pencil:get_editor_state", {
    "name": "pencil_get_editor_state",
    "description": "Get current Pencil editor state — active file, selection, context.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}, get_editor_state)

tool_registry.register("pencil:open_document", {
    "name": "pencil_open_document",
    "description": "Open a .pen file or create a new one. Pass 'new' for empty file or a file path.",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path_or_new": {
                "type": "string",
                "description": "File path to open or 'new' for empty document",
            },
        },
        "required": ["file_path_or_new"],
    },
}, open_document)

tool_registry.register("pencil:get_guidelines", {
    "name": "pencil_get_guidelines",
    "description": "Get design guidelines for working with .pen files. Topics: code, table, tailwind, landing-page, slides, design-system, mobile-app, web-app.",
    "input_schema": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "Guideline topic: code|table|tailwind|landing-page|slides|design-system|mobile-app|web-app",
                "enum": ["code", "table", "tailwind", "landing-page", "slides", "design-system", "mobile-app", "web-app"],
            },
        },
        "required": ["topic"],
    },
}, get_guidelines)

tool_registry.register("pencil:get_style_guide", {
    "name": "pencil_get_style_guide",
    "description": "Get a style guide for design inspiration based on tags or name.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tags": {"type": "string", "description": "Comma-separated style tags"},
            "name": {"type": "string", "description": "Specific style guide name", "default": ""},
        },
        "required": ["tags"],
    },
}, get_style_guide)

tool_registry.register("pencil:batch_get", {
    "name": "pencil_batch_get",
    "description": "Retrieve nodes from .pen file by patterns or node IDs.",
    "input_schema": {
        "type": "object",
        "properties": {
            "patterns": {"type": "string", "description": "Search patterns to match nodes", "default": ""},
            "node_ids": {"type": "string", "description": "Comma-separated node IDs to retrieve", "default": ""},
        },
    },
}, batch_get)

tool_registry.register("pencil:batch_design", {
    "name": "pencil_batch_design",
    "description": "Execute design operations (insert, copy, update, replace, move, delete, image) on .pen file nodes. Max 25 operations per call.",
    "input_schema": {
        "type": "object",
        "properties": {
            "operations": {
                "type": "string",
                "description": "Operations script — one operation per line using I(), C(), R(), U(), D(), M(), G() syntax",
            },
        },
        "required": ["operations"],
    },
}, batch_design)

tool_registry.register("pencil:get_screenshot", {
    "name": "pencil_get_screenshot",
    "description": "Get a screenshot of a node in a .pen file for visual validation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "Node ID to screenshot", "default": ""},
        },
    },
}, get_screenshot)

tool_registry.register("pencil:snapshot_layout", {
    "name": "pencil_snapshot_layout",
    "description": "Get computed layout rectangles of each node in the .pen file.",
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}, snapshot_layout)
