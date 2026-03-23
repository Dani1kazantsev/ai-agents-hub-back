from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/ai_agent_hub"
    REDIS_URL: str = "redis://localhost:6379/0"
    AUTH_PROVIDER_URL: str = ""
    AUTH_JWT_SECRET: str = "access_secret"
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]
    DEBUG: bool = False

    # Claude CLI config directory base (per-user dirs created inside)
    CLAUDE_CONFIGS_DIR: str = ""

    # Jira
    JIRA_BASE_URL: str = ""
    JIRA_EMAIL: str = ""
    JIRA_API_TOKEN: str = ""

    # GitLab
    GITLAB_URL: str = ""
    GITLAB_TOKEN: str = ""

    # Docs project ID in GitLab (for docs MCP server)
    DOCS_PROJECT_ID: str = ""

    # External DB (for MCP db server, read-only)
    EXTERNAL_DATABASE_URL: str = ""

    # MCP servers directory (absolute path to mcp-servers/)
    MCP_SERVERS_DIR: str = ""

    # Figma
    FIGMA_ACCESS_TOKEN: str = ""

    # Pencil MCP server command (e.g. "npx @anthropic/pencil-mcp")
    PENCIL_MCP_COMMAND: str = ""

    # Path to projects.json (agents/config/projects.json from frontend repo)
    PROJECTS_CONFIG_PATH: str = ""

    # Hours of inactivity before chat session becomes inactive
    SESSION_INACTIVE_HOURS: int = 24

    # Internal service token for MCP orchestrator callbacks
    INTERNAL_SERVICE_TOKEN: str = ""

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
