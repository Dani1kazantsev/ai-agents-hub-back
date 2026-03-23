from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.middleware.auth import get_or_create_user
from app.models.base import Agent, User
from app.schemas.agents import AgentCreate, AgentListResponse, AgentResponse, AgentUpdate
from app.services.claude_process import _detect_pencil_mcp, _load_mcp_registry

router = APIRouter(prefix="/api/agents", tags=["agents"])


def require_admin(user: User):
    groups = user.groups or []
    is_admin = (
        user.role == "admin"
        or "is_superuser" in groups
        or "is_staff" in groups
        or "admins" in groups
    )
    if not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )


@router.get("/integrations/status")
async def integrations_status(user: User = Depends(get_or_create_user)):
    """Check which external integrations are available (from MCP registry)."""
    registry = _load_mcp_registry()
    result = {}
    for name, server_def in registry.items():
        # Server is available if all required env vars are set
        available = True
        for env_spec in server_def.get("env", {}).values():
            if env_spec.get("required") and not getattr(settings, env_spec["setting"], ""):
                available = False
                break
        result[name] = available
    # Pencil is special — external binary
    result["pencil"] = _detect_pencil_mcp() is not None
    return result


@router.get("", response_model=AgentListResponse)
async def list_agents(
    search: str | None = Query(None),
    tags: list[str] | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    query = select(Agent).where(Agent.is_active == True)

    if search:
        query = query.where(Agent.name.ilike("%" + search.replace("%", "\\%").replace("_", "\\_") + "%"))

    if tags:
        for tag in tags:
            query = query.where(Agent.tags.contains([tag]))

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar_one()

    query = query.offset(offset).limit(limit).order_by(Agent.created_at.desc())
    result = await db.execute(query)
    agents = result.scalars().all()

    return AgentListResponse(items=agents, total=total)


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.post("", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(
    data: AgentCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    require_admin(user)
    agent = Agent(**data.model_dump(), created_by=user.id)
    db.add(agent)
    await db.flush()
    await db.refresh(agent)
    return agent


@router.put("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: UUID,
    data: AgentUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    require_admin(user)
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(agent, field, value)

    await db.flush()
    await db.refresh(agent)
    return agent


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    require_admin(user)
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    await db.delete(agent)
    await db.flush()
