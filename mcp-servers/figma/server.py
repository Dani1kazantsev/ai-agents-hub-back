"""Figma MCP Server — read files, components, styles, images."""

import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("figma")

FIGMA_BASE = "https://api.figma.com/v1"
FIGMA_TOKEN = os.environ["FIGMA_ACCESS_TOKEN"]
HEADERS = {"X-Figma-Token": FIGMA_TOKEN}


async def _figma(path: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{FIGMA_BASE}{path}", headers=HEADERS)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def get_file(file_key: str) -> str:
    """Get Figma file structure — pages and frames overview."""
    data = await _figma(f"/files/{file_key}?depth=2")
    doc = data.get("document", {})
    pages = []
    for page in doc.get("children", []):
        frames = [{"id": f["id"], "name": f["name"], "type": f["type"]}
                  for f in page.get("children", [])[:50]]
        pages.append({"name": page["name"], "frames": frames})
    return json.dumps({"name": data.get("name"), "pages": pages}, ensure_ascii=False)


@mcp.tool()
async def get_file_nodes(file_key: str, node_ids: str) -> str:
    """Get detailed node data from a Figma file."""
    data = await _figma(f"/files/{file_key}/nodes?ids={node_ids}&depth=3")
    return json.dumps(data.get("nodes", {}), ensure_ascii=False, default=str)


@mcp.tool()
async def get_file_styles(file_key: str) -> str:
    """Get all styles from a Figma file (colors, text, effects)."""
    data = await _figma(f"/files/{file_key}/styles")
    styles = data.get("meta", {}).get("styles", [])
    return json.dumps(styles, ensure_ascii=False)


@mcp.tool()
async def get_file_components(file_key: str) -> str:
    """Get all components from a Figma file."""
    data = await _figma(f"/files/{file_key}/components")
    components = data.get("meta", {}).get("components", [])
    return json.dumps(components, ensure_ascii=False)


@mcp.tool()
async def get_file_images(file_key: str, node_ids: str, format: str = "png", scale: str = "2") -> str:
    """Export nodes from Figma as images (PNG/SVG/JPG)."""
    data = await _figma(f"/images/{file_key}?ids={node_ids}&format={format}&scale={scale}")
    return json.dumps(data.get("images", {}), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
