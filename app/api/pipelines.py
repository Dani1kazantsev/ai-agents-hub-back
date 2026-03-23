"""Pipelines API — manage and run agent chains.

Two modes:
- human_loop=false: auto-execute steps sequentially, stream results via WS
- human_loop=true: create a chat session with a Team Lead orchestrator agent
  that delegates to sub-agents and talks to the user between steps
"""

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import async_session_factory, get_db
from app.middleware.auth import get_or_create_user, validate_token_local
from app.models.base import Agent, ChatMessage, ChatSession, PipelineTemplate, User, UserPipeline
from app.services.llm_service import llm_service

router = APIRouter(prefix="/api/pipelines", tags=["pipelines"])


# --- Pipeline Templates ---
# Based on real AI projects: ai-team, ai-product-manager, ai-qa, ai-designer, ai_team_data

PIPELINE_TEMPLATES: list[dict] = [
    {
        "id": "task-development",
        "title": "Task Development",
        "description": "Full cycle: preparation → PM analysis → design → development → review → testing → deploy",
        "human_loop": True,
        "orchestrator_prompt": """You are the Team Lead of an AI team. You manage the task development pipeline.

## Your Role
You are the sole orchestrator. You communicate with the user, delegate tasks to agents via MCP tools, and pass context between steps.

## Task Development Pipeline

### Step 1: Preparation
Ask the user for the task number (LX-XXX) or description. Use jira:get_issue to get task data. Determine the project by labels. Get architectural context via docs:get_context.

### Step 2: Analysis (for complex tasks)
If the task is complex (> 5 SP or epic) — analyze it as a PM: decompose into subtasks, determine the agent pipeline (which agents are needed), prepare a task list. Show the user and wait for confirmation.

### Step 3: Design (for frontend/mobile tasks)
If the task is frontend or mobile — prepare UI recommendations: check Figma mockups via figma:get_file, formulate UI requirements. Show the user.

### Step 4: Development
Formulate the task for the developer with full context: description, task list from PM, UI recommendations from designer, architectural context. Show the implementation plan to the user.

### Step 5: Review
Show the result to the user. The user may ask to see the diff, fix something, or approve.

### Step 6: Testing
After approval — run testing: lint, unit tests. For new tasks (not Poppycock) — generate a QA checklist.

### Step 7: Completion
Transition the task in Jira: Developing → Developed. Create an MR if needed.

## Rules
- Human-in-the-loop: show the result of each step, wait for confirmation
- Minimal changes — do not refactor what is not requested
- English for communication, English for code/commits
- Priority: Poppycock → AI review → New tasks
- Do not merge MRs — only create them. Merging is done by a human.""",
        "agents": ["pm-agent", "designer-agent", "frontend-dev", "backend-dev", "mobile-dev", "qa-agent", "devops-agent"],
        "steps_description": [
            {"agent": "automation", "label": "Task Preparation", "description": "Jira, branch, project context"},
            {"agent": "pm-agent", "label": "PM Analysis", "description": "Decomposition, task list, agent pipeline"},
            {"agent": "designer-agent", "label": "UI Design", "description": "Figma, design system, UI recommendations"},
            {"agent": "dev", "label": "Development", "description": "Task implementation (frontend/backend/mobile)"},
            {"agent": "user", "label": "Review", "description": "Verification and approval of the result"},
            {"agent": "qa-agent", "label": "Testing", "description": "Lint, tests, QA checklist"},
            {"agent": "automation", "label": "Completion", "description": "Commit, push, MR, Jira status"},
        ],
    },
    {
        "id": "mr-review",
        "title": "AI MR Review",
        "description": "Code review → testing → QA checklist → status transition",
        "human_loop": False,
        "orchestrator_prompt": None,
        "agents": ["frontend-dev", "backend-dev", "qa-agent"],
        "steps": [
            {"agent_slug": "backend-dev", "input_template": "REVIEW MODE.\nTask: {input}\nAnalyze the MR: code quality, security, project patterns. If there are issues — describe them specifically."},
            {"agent_slug": "qa-agent", "input_template": "Based on the code review, generate a QA testing checklist:\n{prev_output}"},
        ],
        "steps_description": [
            {"agent": "dev", "label": "Code Review", "description": "Code quality analysis, security, patterns"},
            {"agent": "qa-agent", "label": "QA Checklist", "description": "Test checklist generation"},
        ],
    },
    {
        "id": "pm-task-creation",
        "title": "PM: Task Creation",
        "description": "Context analysis → formulation → creation in Jira",
        "human_loop": True,
        "orchestrator_prompt": """You are an AI Product Manager. You help PMs create tasks in Jira.

## Process

### Step 1: Understand the task
Ask the user what needs to be done. Clarify the project, task type, and priority.

### Step 2: Architectural context
Determine the project by labels. Get architectural context via docs:get_context — modules, routes, API dependencies.

### Step 3: Formulation
Based on the description and context, formulate:
- Summary (short title)
- Type: Issue/Bug/Story/Epic/Task
- Labels (project label + type label)
- Story Points (Fibonacci: 1, 2, 3, 5, 8, 13; if > 8 — recommend decomposition)
- Description: Context, Requirements, Acceptance Criteria, Edge Cases

Show a preview to the user. Wait for confirmation.

### Step 4: Creation
After confirmation — create the task via jira:create_issue.

## Rules
- Always show a preview before creation
- ADF format for Jira
- Story Points: if task > 8 SP — suggest decomposition""",
        "agents": ["pm-agent"],
        "steps_description": [
            {"agent": "user", "label": "Task Description", "description": "What needs to be done"},
            {"agent": "pm-agent", "label": "Formulation", "description": "Summary, type, SP, description"},
            {"agent": "user", "label": "Confirmation", "description": "Preview before creation"},
            {"agent": "automation", "label": "Create in Jira", "description": "Record the task"},
        ],
    },
    {
        "id": "pm-epic-decomposition",
        "title": "PM: Epic Decomposition",
        "description": "Get epic → analysis → split into subtasks → create in Jira",
        "human_loop": True,
        "orchestrator_prompt": """You are an AI Product Manager. You help decompose epics.

## Process

### Step 1: Get the epic
Ask for the epic number (LX-XXX). Get data via jira:get_issue. Determine the project and get architectural context via docs:get_context.

### Step 2: Decomposition
Break the epic into subtasks:
- Each subtask: summary, type, labels, SP, description
- Dependencies between subtasks
- Recommendations for execution order
- Agent pipeline for each (which agents are needed)

Show the list to the user. Wait for confirmation.

### Step 3: Creation
Create subtasks in Jira via jira:create_issue.

## Rules
- Fibonacci SP: 1, 2, 3, 5, 8, 13
- If a subtask > 8 SP — break it down further
- Show a preview of all subtasks before creation""",
        "agents": ["pm-agent"],
        "steps_description": [
            {"agent": "automation", "label": "Get Epic", "description": "Jira data + context"},
            {"agent": "pm-agent", "label": "Decomposition", "description": "Subtasks, SP, dependencies"},
            {"agent": "user", "label": "Confirmation", "description": "Subtask review"},
            {"agent": "automation", "label": "Create in Jira", "description": "Record subtasks"},
        ],
    },
    {
        "id": "qa-checklist",
        "title": "QA: Checklist & Testing",
        "description": "Task analysis → checklist → automated testing → report",
        "human_loop": True,
        "orchestrator_prompt": """You are an AI QA Assistant. You help QA engineers with testing.

## Process

### Step 1: Get the task
Ask for the task number (LX-XXX). Get data via jira:get_issue. Determine the project by labels, get architectural context via docs:get_context. If there is a branch — get the diff of changes via gitlab:get_mr_diff.

### Step 2: Checklist generation
Based on the task, context, and diff, generate a **short smoke checklist** (5-10 items max, for small tasks 3-5):
- Happy path ONLY for the changed functionality
- Negative / edge cases only for changed logic (if applicable)
- **DO NOT include:** regression, testing of unaffected functionality, general checks (responsiveness, accessibility, SEO, performance), checks of adjacent modules

Show the checklist to the user. Wait for confirmation or edits.

### Step 3: Publication
After confirmation — add the checklist as a Jira comment via jira:add_comment.

### Step 4 (optional): Automated testing
If QA requests — the checklist can be verified on staging automatically.

## Rules
- Checklist in Jira taskList format
- Always enrich the checklist with architectural context
- Show a preview before writing to Jira""",
        "agents": ["qa-agent"],
        "steps_description": [
            {"agent": "automation", "label": "Get Task", "description": "Jira + context + diff"},
            {"agent": "qa-agent", "label": "Checklist Generation", "description": "Smoke checklist for affected functionality (5-10 items)"},
            {"agent": "user", "label": "Confirmation", "description": "Checklist review"},
            {"agent": "automation", "label": "Publication", "description": "Jira comment"},
        ],
    },
    {
        "id": "design-from-figma",
        "title": "Design: Figma → Pencil",
        "description": "Read Figma mockup → extract tokens → create UI in Pencil",
        "human_loop": True,
        "orchestrator_prompt": """You are an AI Designer Team Lead. You manage the design pipeline.

## Agents
- figma-reader: reads Figma mockups, extracts tokens
- ui-designer: creates UI in Pencil.dev
- design-system: manages the design system

## Process

### Step 1: Get Figma mockup
Ask for the Figma file URL. Read the structure via figma:get_file, get nodes via figma:get_file_nodes. Extract: colors, typography, spacing, border-radius, components.

### Step 2: UI recommendations
Based on extracted data, formulate implementation recommendations. Compare with the current design system.

### Step 3: Create UI (if needed)
If the user wants — create UI in Pencil based on the mockup. Show a screenshot of the result.

### Step 4: Update design system (if needed)
If new tokens are found — suggest updating the design system.

## Rules
- Show the result of each stage
- Before finalizing — ask for confirmation
- Accessibility first: contrast, sizes, focus""",
        "agents": ["designer-agent"],
        "steps_description": [
            {"agent": "designer-agent", "label": "Read Figma", "description": "Structure, tokens, components"},
            {"agent": "designer-agent", "label": "UI Recommendations", "description": "Analysis and recommendations"},
            {"agent": "designer-agent", "label": "Create UI", "description": "Implementation in Pencil"},
            {"agent": "designer-agent", "label": "Design System", "description": "Token updates"},
        ],
    },
    {
        "id": "data-analysis",
        "title": "Data: Ad-hoc Analysis",
        "description": "SQL query → data analysis → report",
        "human_loop": False,
        "orchestrator_prompt": None,
        "agents": ["data-agent"],
        "steps": [
            {"agent_slug": "data-agent", "input_template": "{input}"},
        ],
        "steps_description": [
            {"agent": "data-agent", "label": "SQL Analysis", "description": "Query, analysis, report"},
        ],
    },
    {
        "id": "sprint-report",
        "title": "PM: Sprint Report",
        "description": "Collect data from Jira → analyze metrics → report",
        "human_loop": False,
        "orchestrator_prompt": None,
        "agents": ["pm-agent", "data-agent"],
        "steps": [
            {"agent_slug": "pm-agent", "input_template": "Collect data for the current sprint: open tasks, completed, in progress, Poppycock. {input}"},
            {"agent_slug": "data-agent", "input_template": "Analyze sprint metrics and prepare a report:\n{prev_output}"},
        ],
        "steps_description": [
            {"agent": "pm-agent", "label": "Data Collection", "description": "Sprint tasks from Jira"},
            {"agent": "data-agent", "label": "Metrics Analysis", "description": "Velocity, burndown, report"},
        ],
    },
]


# --- Schemas ---

class PipelineRunRequest(BaseModel):
    input: str = ""


class StepDescSchema(BaseModel):
    agent: str = ""
    agent_slug: str = ""
    label: str = ""
    description: str = ""
    input_template: str = ""


class TemplateCreateUpdate(BaseModel):
    title: str
    description: str = ""
    human_loop: bool = True
    orchestrator_prompt: str | None = None
    agents: list[str] = []
    steps: list[StepDescSchema] = []


# --- In-memory state for auto-runs ---

_active_runs: dict[str, dict] = {}


# --- Helpers ---

def _template_to_dict(t: PipelineTemplate) -> dict:
    """Convert DB model to API response dict."""
    return {
        "id": str(t.id),
        "slug": t.slug,
        "title": t.title,
        "description": t.description or "",
        "human_loop": t.human_loop,
        "agents": t.agents or [],
        "steps": t.steps_description or [],
        "is_default": t.is_default,
    }


async def _seed_defaults(db: AsyncSession):
    """Seed default pipeline templates into DB if table is empty."""
    result = await db.execute(select(PipelineTemplate).limit(1))
    if result.scalar_one_or_none() is not None:
        return  # Already have data

    for t in PIPELINE_TEMPLATES:
        tpl = PipelineTemplate(
            slug=t["id"],
            title=t["title"],
            description=t.get("description", ""),
            human_loop=t.get("human_loop", True),
            orchestrator_prompt=t.get("orchestrator_prompt"),
            agents=t.get("agents", []),
            steps=t.get("steps", []),
            steps_description=t.get("steps_description", []),
            is_default=True,
            is_active=True,
        )
        db.add(tpl)
    await db.flush()


async def _get_template_by_id(db: AsyncSession, template_id: str) -> PipelineTemplate | None:
    """Try to find template by UUID or slug."""
    # Try UUID first
    try:
        uid = uuid.UUID(template_id)
        result = await db.execute(
            select(PipelineTemplate).where(PipelineTemplate.id == uid, PipelineTemplate.is_active == True)
        )
        tpl = result.scalar_one_or_none()
        if tpl:
            return tpl
    except (ValueError, AttributeError):
        pass

    # Fallback to slug
    result = await db.execute(
        select(PipelineTemplate).where(PipelineTemplate.slug == template_id, PipelineTemplate.is_active == True)
    )
    return result.scalar_one_or_none()


# --- Endpoints ---

@router.get("/templates")
async def list_templates(
    all: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """List pipeline templates. Default: user's pipelines. ?all=true: full catalog."""
    await _seed_defaults(db)

    if all:
        # Full catalog
        result = await db.execute(
            select(PipelineTemplate).where(PipelineTemplate.is_active == True).order_by(PipelineTemplate.created_at)
        )
        templates = result.scalars().all()

        # Mark which ones user has
        up_result = await db.execute(
            select(UserPipeline.template_id).where(UserPipeline.user_id == user.id)
        )
        user_template_ids = {row[0] for row in up_result.all()}

        return [
            {**_template_to_dict(t), "in_workspace": t.id in user_template_ids}
            for t in templates
        ]

    # User's pipelines only
    result = await db.execute(
        select(PipelineTemplate)
        .join(UserPipeline, UserPipeline.template_id == PipelineTemplate.id)
        .where(UserPipeline.user_id == user.id, PipelineTemplate.is_active == True)
        .order_by(PipelineTemplate.created_at)
    )
    templates = result.scalars().all()

    # Also include templates created by this user (even if not in user_pipelines)
    created_result = await db.execute(
        select(PipelineTemplate).where(
            PipelineTemplate.created_by == user.id,
            PipelineTemplate.is_active == True,
        )
    )
    created_templates = created_result.scalars().all()
    seen_ids = {t.id for t in templates}
    for t in created_templates:
        if t.id not in seen_ids:
            templates.append(t)

    return [_template_to_dict(t) for t in templates]


@router.post("/templates/{template_id}/add")
async def add_template_to_workspace(
    template_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """Add an existing template to user's workspace."""
    tpl = await _get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    # Check if already added
    existing = await db.execute(
        select(UserPipeline).where(
            UserPipeline.user_id == user.id,
            UserPipeline.template_id == tpl.id,
        )
    )
    if existing.scalar_one_or_none():
        return {"status": "already_added"}

    db.add(UserPipeline(user_id=user.id, template_id=tpl.id))
    await db.flush()
    await db.commit()
    return {"status": "added"}


@router.delete("/templates/{template_id}/remove")
async def remove_template_from_workspace(
    template_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """Remove a template from user's workspace (doesn't delete the template)."""
    tpl = await _get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    result = await db.execute(
        select(UserPipeline).where(
            UserPipeline.user_id == user.id,
            UserPipeline.template_id == tpl.id,
        )
    )
    up = result.scalar_one_or_none()
    if up:
        await db.delete(up)
        await db.flush()
        await db.commit()
    return {"status": "removed"}


@router.get("/templates/{template_id}")
async def get_template(template_id: str, db: AsyncSession = Depends(get_db)):
    tpl = await _get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    return _template_to_dict(tpl)


@router.post("/templates")
async def create_template(
    data: TemplateCreateUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """Create a new pipeline template."""
    slug = data.title.lower().replace(" ", "-").replace(":", "")
    # Ensure unique slug
    existing = await db.execute(select(PipelineTemplate).where(PipelineTemplate.slug == slug))
    if existing.scalar_one_or_none():
        slug = f"{slug}-{uuid.uuid4().hex[:6]}"

    steps_desc = [s.model_dump() for s in data.steps]
    # Build auto-mode steps from steps_desc
    auto_steps = [
        {"agent_slug": s.get("agent_slug") or s.get("agent", ""), "input_template": s.get("input_template") or s.get("description", "")}
        for s in steps_desc
    ]

    tpl = PipelineTemplate(
        slug=slug,
        title=data.title,
        description=data.description,
        human_loop=data.human_loop,
        orchestrator_prompt=data.orchestrator_prompt,
        agents=data.agents or [s.get("agent_slug") or s.get("agent", "") for s in steps_desc],
        steps=auto_steps,
        steps_description=steps_desc,
        is_default=False,
        is_active=True,
        created_by=user.id,
    )
    db.add(tpl)
    await db.flush()
    return _template_to_dict(tpl)


@router.put("/templates/{template_id}")
async def update_template(
    template_id: str,
    data: TemplateCreateUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """Update an existing pipeline template."""
    tpl = await _get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    steps_desc = [s.model_dump() for s in data.steps]
    auto_steps = [
        {"agent_slug": s.get("agent_slug") or s.get("agent", ""), "input_template": s.get("input_template") or s.get("description", "")}
        for s in steps_desc
    ]

    tpl.title = data.title
    tpl.description = data.description
    tpl.human_loop = data.human_loop
    tpl.orchestrator_prompt = data.orchestrator_prompt
    tpl.agents = data.agents or [s.get("agent_slug") or s.get("agent", "") for s in steps_desc]
    tpl.steps = auto_steps
    tpl.steps_description = steps_desc

    await db.flush()
    return _template_to_dict(tpl)


@router.delete("/templates/{template_id}")
async def delete_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """Soft-delete a pipeline template."""
    tpl = await _get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    tpl.is_active = False
    await db.flush()
    return {"status": "deleted"}


@router.post("/run/{template_id}")
async def start_pipeline_run(
    template_id: str,
    data: PipelineRunRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """Start a pipeline run.

    For human_loop=true: creates a chat session with TL orchestrator, returns session_id.
    For human_loop=false: creates an auto-run, returns run_id for WS connection.
    """
    tpl = await _get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    # Build a dict for _get_or_create_tl_agent compatibility
    template_dict = {
        "id": tpl.slug,
        "title": tpl.title,
        "description": tpl.description or "",
        "human_loop": tpl.human_loop,
        "orchestrator_prompt": tpl.orchestrator_prompt,
        "agents": tpl.agents or [],
        "steps": tpl.steps or [],
        "steps_description": tpl.steps_description or [],
    }

    if tpl.human_loop:
        tl_agent = await _get_or_create_tl_agent(db, template_dict)

        session = ChatSession(user_id=user.id, agent_id=tl_agent.id)
        db.add(session)
        await db.flush()

        if data.input:
            msg = ChatMessage(
                session_id=session.id,
                role="user",
                content=data.input,
            )
            db.add(msg)

        await db.commit()

        return {
            "mode": "human_loop",
            "session_id": str(session.id),
            "agent_name": tl_agent.name,
            "template_id": str(tpl.id),
        }

    else:
        run_id = str(uuid.uuid4())
        _active_runs[run_id] = {
            "template_id": str(tpl.id),
            "template_slug": tpl.slug,
            "status": "pending",
            "current_step": 0,
            "total_steps": len(tpl.steps or []),
            "started_at": datetime.utcnow().isoformat(),
            "results": [],
            "user_id": str(user.id),
            "initial_input": data.input,
        }
        return {
            "mode": "auto",
            "run_id": run_id,
            "template_id": str(tpl.id),
        }


async def _get_or_create_tl_agent(db: AsyncSession, template: dict) -> Agent:
    """Get or create a Team Lead agent for a human_loop pipeline."""
    agent_name = f"Team Lead: {template['title']}"

    result = await db.execute(
        select(Agent).where(Agent.name == agent_name, Agent.is_active == True)
    )
    agent = result.scalar_one_or_none()

    if agent:
        # Update system prompt in case template changed
        if agent.system_prompt != template["orchestrator_prompt"]:
            agent.system_prompt = template["orchestrator_prompt"]
        return agent

    # Collect tools from all participating agents
    all_tools = [
        "jira:search_issues", "jira:get_issue", "jira:create_issue",
        "jira:update_issue", "jira:add_comment", "jira:get_transitions",
        "jira:transition_issue",
        "gitlab:read_file", "gitlab:list_files", "gitlab:list_mrs",
        "gitlab:get_mr_diff", "gitlab:add_mr_comment",
        "gitlab:create_branch", "gitlab:commit_files", "gitlab:create_mr",
        "docs:get_context", "docs:get_project",
        "docs:get_team", "docs:search_docs",
        "db:read_query", "db:describe_table", "db:list_tables",
        "figma:get_file", "figma:get_file_nodes",
        "figma:get_file_styles", "figma:get_file_components",
    ]

    agent = Agent(
        name=agent_name,
        description=f"Pipeline orchestrator: {template['description']}",
        model="claude-sonnet-4-6",
        system_prompt=template["orchestrator_prompt"],
        tools=all_tools,
        is_active=True,
        icon="users",
        color="#5988FF",
        tags=["pipeline", "orchestrator", template["id"]],
    )
    db.add(agent)
    await db.flush()
    return agent


@router.get("/runs")
async def list_runs():
    return [
        {
            "id": run_id,
            "template_id": state["template_id"],
            "status": state["status"],
            "current_step": state["current_step"],
            "total_steps": state["total_steps"],
            "started_at": state["started_at"],
            "results": state.get("results", []),
        }
        for run_id, state in _active_runs.items()
    ]


@router.get("/runs/{run_id}")
async def get_run(run_id: str, db: AsyncSession = Depends(get_db)):
    state = _active_runs.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found")

    tpl = await _get_template_by_id(db, state["template_id"])
    steps_desc = tpl.steps_description if tpl else []

    return {
        "id": run_id,
        "template_id": state["template_id"],
        "title": tpl.title if tpl else "Pipeline",
        "status": state["status"],
        "current_step": state["current_step"],
        "total_steps": state["total_steps"],
        "results": state.get("results", []),
        "steps": steps_desc,
    }


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    """Cancel an active pipeline run."""
    state = _active_runs.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found")
    if state["status"] not in ("pending", "running"):
        raise HTTPException(status_code=400, detail="Run is not active")
    state["status"] = "cancelled"
    return {"status": "cancelled"}


@router.delete("/runs/{run_id}")
async def delete_run(run_id: str):
    """Remove a completed/failed pipeline run from memory."""
    if run_id not in _active_runs:
        raise HTTPException(status_code=404, detail="Run not found")
    del _active_runs[run_id]
    return {"status": "deleted"}


@router.websocket("/ws/{run_id}")
async def pipeline_websocket(websocket: WebSocket, run_id: str):
    """WebSocket for auto-mode pipeline execution (human_loop=false)."""
    await websocket.accept()

    token = websocket.query_params.get("token")
    if not token:
        await websocket.send_json({"type": "error", "content": "Unauthorized"})
        await websocket.close(code=4001)
        return

    payload = await validate_token_local(token)
    if not payload:
        await websocket.send_json({"type": "error", "content": "Unauthorized"})
        await websocket.close(code=4001)
        return

    state = _active_runs.get(run_id)
    if not state:
        await websocket.send_json({"type": "error", "content": "Run not found"})
        await websocket.close()
        return

    client_disconnected = False

    async def _ws_send(data: dict):
        nonlocal client_disconnected
        if client_disconnected:
            return
        try:
            await websocket.send_json(data)
        except Exception:
            client_disconnected = True

    try:
        raw = await websocket.receive_text()
        data = json.loads(raw)
        if data.get("type") != "start":
            await _ws_send({"type": "error", "content": "Expected 'start' message"})
            await websocket.close()
            return

        async with async_session_factory() as tpl_db:
            tpl = await _get_template_by_id(tpl_db, state["template_id"])
        if not tpl or tpl.human_loop:
            await _ws_send({"type": "error", "content": "Template not found or is human_loop"})
            await websocket.close()
            return

        state["status"] = "running"
        prev_output = state["initial_input"]
        steps = tpl.steps or []

        slug_to_name = {
            "qa-agent": "QA Agent", "backend-dev": "Backend Dev",
            "frontend-dev": "Frontend Dev", "pm-agent": "PM Agent",
            "designer-agent": "Designer", "data-agent": "Data Agent",
            "devops-agent": "DevOps Agent", "mobile-dev": "Mobile Dev",
        }

        async with async_session_factory() as db:
            for step_idx, step_config in enumerate(steps):
                # Check for cancellation between steps
                if state["status"] == "cancelled":
                    await _ws_send({"type": "pipeline_cancelled"})
                    return

                state["current_step"] = step_idx + 1
                agent_slug = step_config["agent_slug"]

                agent_name = slug_to_name.get(agent_slug, agent_slug)
                result = await db.execute(
                    select(Agent).where(Agent.name == agent_name, Agent.is_active == True)
                )
                agent = result.scalar_one_or_none()

                if not agent:
                    state["results"].append({
                        "step": step_idx,
                        "agent": agent_slug,
                        "output": f"Agent '{agent_slug}' not found",
                        "status": "error",
                    })
                    await _ws_send({
                        "type": "step_error",
                        "step": step_idx,
                        "agent": agent_slug,
                        "content": f"Agent '{agent_slug}' not found",
                    })
                    continue

                input_template = step_config.get("input_template", "{input}")
                step_input = input_template.replace("{input}", state["initial_input"]).replace("{prev_output}", prev_output)

                await _ws_send({
                    "type": "step_start",
                    "step": step_idx,
                    "agent_name": agent.name,
                    "agent_slug": agent_slug,
                    "total_steps": len(steps),
                })

                ws_user_id = str(payload.get("user_id", ""))
                step_response = ""

                async for event in llm_service.stream_chat(
                    user_id=ws_user_id,
                    chat_session_id=f"pipeline-{run_id}-step-{step_idx}",
                    message=step_input,
                    system_prompt=agent.system_prompt or "You are a helpful assistant.",
                    model=agent.model,
                    allowed_tools=agent.tools or [],
                ):
                    if state["status"] == "cancelled":
                        break
                    if event.type == "text":
                        step_response += event.content
                        await _ws_send({
                            "type": "step_stream",
                            "step": step_idx,
                            "content": event.content,
                        })
                    elif event.type == "tool_use":
                        await _ws_send({
                            "type": "step_tool_use",
                            "step": step_idx,
                            "tool_name": event.tool_name,
                        })
                    elif event.type == "tool_result":
                        await _ws_send({
                            "type": "step_tool_result",
                            "step": step_idx,
                            "tool_name": event.tool_name,
                            "content": event.content[:500],
                        })

                if state["status"] == "cancelled":
                    await _ws_send({"type": "pipeline_cancelled"})
                    return

                state["results"].append({
                    "step": step_idx,
                    "agent": agent.name,
                    "output": step_response[:5000],
                    "status": "done",
                })

                await _ws_send({
                    "type": "step_done",
                    "step": step_idx,
                    "agent_name": agent.name,
                    "output_preview": step_response[:500],
                })

                prev_output = step_response

        state["status"] = "completed"
        await _ws_send({
            "type": "pipeline_done",
            "results": state["results"],
        })

    except WebSocketDisconnect:
        # Pipeline continues running in background — state is preserved in _active_runs
        if state["status"] == "running":
            pass  # Keep running status, frontend will poll via GET /runs/{run_id}
    except Exception as e:
        state["status"] = "error"
        await _ws_send({"type": "error", "content": str(e)})
