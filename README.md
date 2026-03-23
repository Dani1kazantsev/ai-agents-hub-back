# AI Agent Hub — Backend

FastAPI backend for [AI Agent Hub](https://github.com/Dani1kazantsev/ai-agents-hub). Manages agents, chat sessions, Claude CLI processes, and MCP integrations.

> **Main repo with full docs and quickstart:** [ai-agents-hub](https://github.com/Dani1kazantsev/ai-agents-hub)

## Tech Stack

- **API**: FastAPI + async SQLAlchemy 2.0 + asyncpg
- **LLM**: Claude Code CLI (per-user subprocess, stream-json protocol)
- **Auth**: Built-in email/password (bcrypt + JWT) or external OAuth2
- **DB**: PostgreSQL 16, Redis 7
- **MCP**: Jira, GitLab, DB, Docs, Figma, Pencil, Memory, Orchestrator (Python FastMCP, stdio)

## Structure

```
app/
├── api/               # Route handlers (auth, agents, chat, admin, pipelines, onboarding)
├── middleware/         # JWT auth middleware
├── models/            # SQLAlchemy models (User, Agent, ChatSession, ChatMessage, etc.)
├── schemas/           # Pydantic schemas
├── services/          # Claude process manager, LLM service, sub-agent registry
└── tools/             # Tool wrappers (legacy, reference only)
mcp-servers/
├── jira/              # 7 tools (search, get, create, update, comment, transitions)
├── gitlab/            # 8 tools (files, MRs, branches, commits)
├── db/                # 3 tools (read_query, describe_table, list_tables) — read-only
├── docs/              # 4 tools (context, project, team, search_docs)
├── figma/             # 5 tools (file, nodes, styles, components, images)
├── memory/            # 4 tools (read, write, search, list) — PostgreSQL-backed
└── orchestrator/      # 4 tools (spawn_agent, list_running, get_result, kill_agent)
migrations/            # Alembic migrations
```

## Quick Start

```bash
# With Docker (recommended — used by frontend's docker-compose)
docker pull dani1kazantsev/ai-agents-hub-back:latest

# Local development
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # edit with your values
uvicorn app.main:app --reload  # http://localhost:8000
```

On first startup, the backend automatically creates tables and seeds default agents.

## Environment

Copy `.env.example` and fill in values:

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `AUTH_JWT_SECRET` | Yes (prod) | JWT signing secret (change from default!) |
| `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | No | Jira integration |
| `GITLAB_URL`, `GITLAB_TOKEN` | No | GitLab integration |
| `EXTERNAL_DATABASE_URL` | No | Read-only DB for data agent |
| `FIGMA_ACCESS_TOKEN` | No | Figma integration |
| `DOCS_PROJECT_ID` | No | GitLab project ID for docs MCP |

## Docker Hub

```
docker pull dani1kazantsev/ai-agents-hub-back:latest
```

## License

MIT
