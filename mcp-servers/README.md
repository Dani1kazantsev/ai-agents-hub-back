# MCP Servers

Each MCP server is a standalone Python process using [FastMCP](https://github.com/jlowin/fastmcp) with stdio transport.

## Adding a new MCP server

### 1. Create the server

```bash
mkdir mcp-servers/my-service
```

Create `mcp-servers/my-service/server.py`:

```python
import os
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("my-service")

# Read config from environment variables
API_KEY = os.environ["MY_SERVICE_API_KEY"]

@mcp.tool()
async def my_tool(query: str) -> str:
    """Description shown to the AI agent."""
    # Your integration logic here
    return "result"

if __name__ == "__main__":
    mcp.run()
```

### 2. Register in registry.json

Add your server to `mcp-servers/registry.json`:

```json
{
  "servers": {
    "my-service": {
      "description": "My custom integration",
      "server_path": "my-service/server.py",
      "env": {
        "MY_SERVICE_API_KEY": {"setting": "MY_SERVICE_API_KEY", "required": true},
        "MY_SERVICE_URL": {"setting": "MY_SERVICE_URL", "required": false}
      }
    }
  }
}
```

Each env var maps to a setting name in `app/config.py`. The `required` flag determines whether the server is available — if any required env var is missing, the server won't be offered to agents.

### 3. Add settings

In `app/config.py`, add:

```python
MY_SERVICE_API_KEY: str = ""
MY_SERVICE_URL: str = ""
```

And in `.env`:

```
MY_SERVICE_API_KEY=your-key-here
```

### 4. Use in agent configs

Add tools to agent YAML files in `agents/`:

```yaml
tools:
  - my-service:my_tool
```

The tool name format is `service:tool_name`. The backend maps this to `mcp__my-service__my_tool` for Claude CLI.

## Built-in servers

| Server | Tools | Env vars needed |
|--------|-------|-----------------|
| jira | 7 | `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` |
| gitlab | 8 | `GITLAB_URL`, `GITLAB_TOKEN` |
| db | 3 | `EXTERNAL_DATABASE_URL` |
| docs | 4 | `GITLAB_URL`, `GITLAB_TOKEN` (+ optional `DOCS_PROJECT_ID`) |
| figma | 5 | `FIGMA_ACCESS_TOKEN` |
| pencil | 15 | Auto-detected from Pencil app |
| memory | 4 | Auto-configured (uses main DATABASE_URL) |
| orchestrator | 4 | Auto-configured (internal) |

## How it works

1. Agent config lists tools like `jira:search_issues`
2. Backend extracts needed services (`jira`)
3. Checks `registry.json` for server definition
4. Resolves env vars from `app/config.py` settings
5. Generates per-user `.mcp.json` config for Claude CLI
6. Claude CLI spawns MCP servers as stdio subprocesses
