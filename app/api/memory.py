"""Memory API — CRUD for agent memory entries."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.middleware.auth import get_or_create_user
from app.models.base import Agent, AgentMemory, User
from app.schemas.memory import MemoryEntry, MemoryStats, MemoryUpdate

router = APIRouter(prefix="/api/memory", tags=["memory"])


@router.get("/{agent_id}", response_model=list[MemoryEntry])
async def list_memories(
    agent_id: UUID,
    prefix: str = Query("", description="Filter by key prefix"),
    search: str = Query("", description="Search query"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    query = select(AgentMemory).where(AgentMemory.agent_id == agent_id)

    # For personal scope, filter by user
    query = query.where(
        (AgentMemory.user_id == user.id) | (AgentMemory.scope == "shared")
    )

    if prefix:
        query = query.where(AgentMemory.key.like(f"{prefix}%"))

    if search:
        query = query.where(
            AgentMemory.content.ilike(f"%{search}%") | AgentMemory.key.ilike(f"%{search}%")
        )

    query = query.order_by(AgentMemory.updated_at.desc())
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{agent_id}/stats", response_model=MemoryStats)
async def get_memory_stats(
    agent_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    base = select(AgentMemory).where(
        AgentMemory.agent_id == agent_id,
        (AgentMemory.user_id == user.id) | (AgentMemory.scope == "shared"),
    )

    count_q = select(func.count()).select_from(base.subquery())
    total = (await db.execute(count_q)).scalar_one()

    tokens_q = select(func.coalesce(func.sum(AgentMemory.token_count), 0)).where(
        AgentMemory.agent_id == agent_id,
        (AgentMemory.user_id == user.id) | (AgentMemory.scope == "shared"),
    )
    total_tokens = (await db.execute(tokens_q)).scalar_one()

    keys_q = select(AgentMemory.key).where(
        AgentMemory.agent_id == agent_id,
        (AgentMemory.user_id == user.id) | (AgentMemory.scope == "shared"),
    ).order_by(AgentMemory.key)
    keys = (await db.execute(keys_q)).scalars().all()

    return MemoryStats(total_entries=total, total_tokens=total_tokens, keys=list(keys))


@router.get("/{agent_id}/{key:path}", response_model=MemoryEntry)
async def get_memory(
    agent_id: UUID,
    key: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    result = await db.execute(
        select(AgentMemory).where(
            AgentMemory.agent_id == agent_id,
            AgentMemory.key == key,
            (AgentMemory.user_id == user.id) | (AgentMemory.scope == "shared"),
        )
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Memory entry not found")
    return entry


@router.put("/{agent_id}/{key:path}", response_model=MemoryEntry)
async def update_memory(
    agent_id: UUID,
    key: str,
    data: MemoryUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    result = await db.execute(
        select(AgentMemory).where(
            AgentMemory.agent_id == agent_id,
            AgentMemory.key == key,
            (AgentMemory.user_id == user.id) | (AgentMemory.scope == "shared"),
        )
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Memory entry not found")

    entry.content = data.content
    entry.tags = data.tags
    entry.token_count = len(data.content.split())
    entry.source = "manual"
    await db.commit()
    await db.refresh(entry)
    return entry


@router.delete("/{agent_id}/{key:path}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    agent_id: UUID,
    key: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    result = await db.execute(
        select(AgentMemory).where(
            AgentMemory.agent_id == agent_id,
            AgentMemory.key == key,
            (AgentMemory.user_id == user.id) | (AgentMemory.scope == "shared"),
        )
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Memory entry not found")

    await db.delete(entry)
    await db.commit()
