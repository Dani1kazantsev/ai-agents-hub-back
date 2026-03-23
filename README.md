# AI Agent Hub — Backend

FastAPI backend for AI Agent Hub. Manages agents, chat sessions, Claude CLI processes, and MCP integrations.

## Tech Stack

- **API**: FastAPI + async SQLAlchemy 2.0 + asyncpg
- **LLM**: Claude Code CLI (subprocess per user, stream-json protocol)
- **Auth**: OAuth2 / JWT HS256, per-user Claude CLI auth isolation
- **DB**: PostgreSQL 16
- **MCP**: Jira, GitLab, DB, Docs, Figma, Pencil servers (Python FastMCP, stdio)

## Structure

```
app/
├── api/               # Route handlers (auth, agents, chat, admin, pipelines, claude_auth)
├── middleware/         # JWT auth middleware
├── models/            # SQLAlchemy models (User, Agent, ChatSession, ChatMessage)
├── schemas/           # Pydantic schemas
├── services/          # Claude process manager, LLM service
└── tools/             # Legacy tool wrappers (unused, reference only)
mcp-servers/
├── jira/              # 7 tools (search, get, create, update, comment, transitions)
├── gitlab/            # 8 tools (files, MRs, branches, commits)
├── db/                # 3 tools (read_query, describe_table, list_tables) — read-only
├── docs/              # 4 tools (context, project, team, search_docs)
└── figma/             # 5 tools (file, nodes, styles, components, images)
migrations/            # Alembic migrations
```

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload  # port 8000
```

## Environment

Copy `.env.example` and fill in values. Key vars:
- `DATABASE_URL` — PostgreSQL connection
- `AUTH_PROVIDER_URL`, `AUTH_JWT_SECRET` — OAuth2 auth provider
- `JIRA_*`, `GITLAB_*`, `EXTERNAL_DATABASE_URL`, `FIGMA_ACCESS_TOKEN` — MCP server credentials

## License

MIT
