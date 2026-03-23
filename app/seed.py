import asyncio
import json
import os
from pathlib import Path

import yaml
from sqlalchemy import select

from app.db import async_session_factory, engine
from app.models.base import Agent, Base

AGENTS_DIR = Path(__file__).resolve().parent.parent.parent / "ai-agents-hub" / "agents"


def load_agents_from_yaml() -> list[dict]:
    """Load agent configs from YAML files in agents/ directory."""
    agents = []
    agents_dir = AGENTS_DIR
    if not agents_dir.exists():
        # Fallback: try relative to this file
        agents_dir = Path(__file__).resolve().parent.parent / "agents"

    for yaml_file in sorted(agents_dir.glob("*.yaml")):
        with open(yaml_file) as f:
            data = yaml.safe_load(f)

        agent = {
            "name": data["name"],
            "description": data.get("description", ""),
            "model": data.get("model", "claude-sonnet-4-6"),
            "system_prompt": data.get("system_prompt", ""),
            "tools": data.get("tools", []),
            "allowed_roles": data.get("allowed_roles", []),
            "max_tokens_per_session": data.get("max_tokens_per_session", 50000),
            "icon": data.get("icon", ""),
            "color": data.get("color", ""),
            "tags": data.get("tags", []),
        }
        agents.append(agent)
        print(f"  Loaded: {yaml_file.name} → {agent['name']}")

    return agents


# Fallback hardcoded agents if YAML files not found
AGENTS_FALLBACK = [
    {
        "name": "QA Agent",
        "description": "Test cases, bug reports, MR review from QA perspective, automated testing",
        "model": "claude-sonnet-4-6",
        "system_prompt": "You are a Senior QA Engineer.",
        "tools": [
            "jira:search_issues", "jira:get_issue", "jira:create_issue", "jira:add_comment",
            "gitlab:read_file", "gitlab:list_files", "gitlab:list_mrs", "gitlab:get_mr_diff",
            "gitlab:add_mr_comment", "docs:get_context", "docs:get_project", "db:read_query",
        ],
        "allowed_roles": ["qa", "dev", "pm", "lead"],
        "max_tokens_per_session": 100000,
        "icon": "bug",
        "color": "#10B981",
        "tags": ["qa", "testing", "review", "bugs"],
    },
    {
        "name": "Frontend Dev",
        "description": "Development and review of frontend tasks in Vue/Nuxt/TypeScript",
        "model": "claude-sonnet-4-6",
        "system_prompt": "You are a Senior Frontend Developer.",
        "tools": [
            "gitlab:read_file", "gitlab:list_files", "gitlab:list_mrs", "gitlab:get_mr_diff",
            "gitlab:add_mr_comment", "gitlab:create_branch", "gitlab:commit_files", "gitlab:create_mr",
            "jira:search_issues", "jira:get_issue", "jira:add_comment",
            "docs:get_context", "docs:get_project", "db:read_query",
        ],
        "allowed_roles": ["dev", "lead", "qa"],
        "max_tokens_per_session": 150000,
        "icon": "code",
        "color": "#6366F1",
        "tags": ["dev", "frontend", "vue", "code-review"],
    },
    {
        "name": "Backend Dev",
        "description": "Development and review of backend tasks in Python/Django/Go",
        "model": "claude-sonnet-4-6",
        "system_prompt": "You are a Senior Backend Developer.",
        "tools": [
            "gitlab:read_file", "gitlab:list_files", "gitlab:list_mrs", "gitlab:get_mr_diff",
            "gitlab:add_mr_comment", "gitlab:create_branch", "gitlab:commit_files", "gitlab:create_mr",
            "jira:search_issues", "jira:get_issue", "jira:add_comment",
            "docs:get_context", "docs:get_project", "db:read_query", "db:describe_table", "db:list_tables",
        ],
        "allowed_roles": ["dev", "lead", "qa"],
        "max_tokens_per_session": 150000,
        "icon": "terminal",
        "color": "#6366F1",
        "tags": ["dev", "backend", "python", "go", "code-review"],
    },
    {
        "name": "Mobile Dev",
        "description": "Development and review of mobile applications in Flutter/Dart",
        "model": "claude-sonnet-4-6",
        "system_prompt": "You are a Senior Mobile Developer.",
        "tools": [
            "gitlab:read_file", "gitlab:list_files", "gitlab:list_mrs", "gitlab:get_mr_diff",
            "gitlab:add_mr_comment", "gitlab:create_branch", "gitlab:commit_files", "gitlab:create_mr",
            "jira:search_issues", "jira:get_issue", "jira:add_comment",
            "docs:get_context", "docs:get_project",
        ],
        "allowed_roles": ["dev", "lead"],
        "max_tokens_per_session": 150000,
        "icon": "smartphone",
        "color": "#0EA5E9",
        "tags": ["dev", "mobile", "flutter", "dart"],
    },
    {
        "name": "PM Agent",
        "description": "Task formulation, epic decomposition, backlog analysis, prioritization",
        "model": "claude-sonnet-4-6",
        "system_prompt": "You are a Product Manager.",
        "tools": [
            "jira:search_issues", "jira:get_issue", "jira:create_issue", "jira:update_issue",
            "jira:add_comment", "jira:get_transitions", "jira:transition_issue",
            "docs:get_context", "docs:get_project", "docs:get_team", "db:read_query",
        ],
        "allowed_roles": ["pm", "lead", "dev"],
        "max_tokens_per_session": 80000,
        "icon": "clipboard",
        "color": "#F59E0B",
        "tags": ["pm", "planning", "analytics", "backlog"],
    },
    {
        "name": "Designer",
        "description": "UI/UX design, Figma and Pencil workflows, design system, review",
        "model": "claude-sonnet-4-6",
        "system_prompt": "You are a Senior UI/UX Designer.",
        "tools": [
            "figma:get_file", "figma:get_file_nodes", "figma:get_file_styles",
            "figma:get_file_components", "figma:get_file_images",
            "gitlab:read_file", "gitlab:list_mrs", "gitlab:get_mr_diff", "gitlab:add_mr_comment",
            "jira:search_issues", "jira:get_issue", "docs:get_project",
        ],
        "allowed_roles": ["designer", "dev", "pm", "lead"],
        "max_tokens_per_session": 100000,
        "icon": "palette",
        "color": "#EC4899",
        "tags": ["design", "ux", "figma", "pencil", "accessibility"],
    },
    {
        "name": "Data Agent",
        "description": "SQL queries, data analysis, report generation, metrics",
        "model": "claude-sonnet-4-6",
        "system_prompt": "You are a Data Analyst.",
        "tools": [
            "db:read_query", "db:describe_table", "db:list_tables",
            "docs:get_context", "docs:get_project", "docs:search_docs",
            "jira:search_issues",
        ],
        "allowed_roles": ["dev", "pm", "lead", "analyst"],
        "max_tokens_per_session": 100000,
        "icon": "database",
        "color": "#8B5CF6",
        "tags": ["data", "sql", "analytics", "metrics"],
    },
    {
        "name": "DevOps Agent",
        "description": "CI/CD pipelines, deployment, monitoring, infrastructure",
        "model": "claude-sonnet-4-6",
        "system_prompt": "You are a Senior DevOps Engineer.",
        "tools": [
            "gitlab:read_file", "gitlab:list_files", "gitlab:list_mrs", "gitlab:get_mr_diff",
            "gitlab:add_mr_comment", "jira:search_issues", "jira:get_issue",
            "docs:get_context", "docs:get_project",
        ],
        "allowed_roles": ["dev", "lead", "devops"],
        "max_tokens_per_session": 100000,
        "icon": "server",
        "color": "#EF4444",
        "tags": ["devops", "ci-cd", "infrastructure", "deploy"],
    },
]


async def seed():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Try loading from YAML first
    print("Loading agent configs...")
    agents_data = load_agents_from_yaml()
    if not agents_data:
        print("No YAML configs found, using fallback definitions")
        agents_data = AGENTS_FALLBACK

    async with async_session_factory() as session:
        for agent_data in agents_data:
            result = await session.execute(
                select(Agent).where(Agent.name == agent_data["name"])
            )
            existing = result.scalar_one_or_none()

            if existing:
                for key, value in agent_data.items():
                    setattr(existing, key, value)
                print(f"  Updated: {agent_data['name']}")
            else:
                agent = Agent(**agent_data)
                session.add(agent)
                print(f"  Created: {agent_data['name']}")

        await session.commit()

    await engine.dispose()
    print(f"Seed complete! {len(agents_data)} agents.")


if __name__ == "__main__":
    asyncio.run(seed())
