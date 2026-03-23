"""Figma tools — read files, components, styles, images."""

import json

import httpx

from app.config import settings
from app.tools.registry import tool_registry

FIGMA_BASE = "https://api.figma.com/v1"
HEADERS = {"X-Figma-Token": settings.FIGMA_ACCESS_TOKEN}


async def _figma(path: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{FIGMA_BASE}{path}", headers=HEADERS)
        resp.raise_for_status()
        return resp.json()


async def get_file(file_key: str) -> str:
    data = await _figma(f"/files/{file_key}?depth=2")
    doc = data.get("document", {})
    pages = []
    for page in doc.get("children", []):
        frames = [{"id": f["id"], "name": f["name"], "type": f["type"]}
                  for f in page.get("children", [])[:50]]
        pages.append({"name": page["name"], "frames": frames})
    return json.dumps({"name": data.get("name"), "pages": pages}, ensure_ascii=False)


async def get_file_nodes(file_key: str, node_ids: str) -> str:
    data = await _figma(f"/files/{file_key}/nodes?ids={node_ids}&depth=3")
    return json.dumps(data.get("nodes", {}), ensure_ascii=False, default=str)


async def get_file_styles(file_key: str) -> str:
    data = await _figma(f"/files/{file_key}/styles")
    styles = data.get("meta", {}).get("styles", [])
    return json.dumps(styles, ensure_ascii=False)


async def get_file_components(file_key: str) -> str:
    data = await _figma(f"/files/{file_key}/components")
    components = data.get("meta", {}).get("components", [])
    return json.dumps(components, ensure_ascii=False)


async def get_file_images(file_key: str, node_ids: str, format: str = "png", scale: str = "2") -> str:
    data = await _figma(f"/images/{file_key}?ids={node_ids}&format={format}&scale={scale}")
    return json.dumps(data.get("images", {}), ensure_ascii=False)


# --- Register ---

tool_registry.register("figma:get_file", {
    "name": "figma_get_file",
    "description": "Get Figma file structure — pages and frames overview.",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_key": {"type": "string", "description": "Figma file key from URL"},
        },
        "required": ["file_key"],
    },
}, get_file)

tool_registry.register("figma:get_file_nodes", {
    "name": "figma_get_file_nodes",
    "description": "Get detailed node data from a Figma file.",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_key": {"type": "string", "description": "Figma file key"},
            "node_ids": {"type": "string", "description": "Comma-separated node IDs"},
        },
        "required": ["file_key", "node_ids"],
    },
}, get_file_nodes)

tool_registry.register("figma:get_file_styles", {
    "name": "figma_get_file_styles",
    "description": "Get all styles from a Figma file (colors, text, effects).",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_key": {"type": "string", "description": "Figma file key"},
        },
        "required": ["file_key"],
    },
}, get_file_styles)

tool_registry.register("figma:get_file_components", {
    "name": "figma_get_file_components",
    "description": "Get all components from a Figma file.",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_key": {"type": "string", "description": "Figma file key"},
        },
        "required": ["file_key"],
    },
}, get_file_components)

tool_registry.register("figma:get_file_images", {
    "name": "figma_get_file_images",
    "description": "Export nodes from Figma as images (PNG/SVG/JPG).",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_key": {"type": "string", "description": "Figma file key"},
            "node_ids": {"type": "string", "description": "Comma-separated node IDs to export"},
            "format": {"type": "string", "description": "Image format: png, svg, jpg", "default": "png"},
            "scale": {"type": "string", "description": "Scale factor (1-4)", "default": "2"},
        },
        "required": ["file_key", "node_ids"],
    },
}, get_file_images)
