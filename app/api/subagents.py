"""Subagents API — internal endpoints for orchestrator MCP + user-facing list."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Header, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import async_session_factory, get_db
from app.middleware.auth import get_or_create_user
from app.models.base import ChatSession, SubagentRun, User
from app.services import subagent_registry

router = APIRouter(tags=["subagents"])


def _verify_internal_token(authorization: str = Header(...)):
    """Verify internal service token for MCP orchestrator callbacks."""
    expected = settings.INTERNAL_SERVICE_TOKEN or "internal"
    token = authorization.replace("Bearer ", "")
    if token != expected:
        raise HTTPException(status_code=403, detail="Invalid service token")


@router.post("/api/internal/subagents/spawn")
async def spawn_subagent(
    data: dict,
    _: str = Depends(_verify_internal_token),
):
    """Spawn a sub-agent. Called by orchestrator MCP server."""
    async with async_session_factory() as db:
        # Resolve user_id: orchestrator passes SSO ID, we need DB UUID
        # Get it from the parent session
        parent_sid = data["parent_session_id"]
        parent_result = await db.execute(
            select(ChatSession).where(ChatSession.id == UUID(parent_sid))
        )
        parent_session = parent_result.scalar_one_or_none()
        db_user_id = str(parent_session.user_id) if parent_session else data["user_id"]

        result = await subagent_registry.spawn(
            db=db,
            parent_session_id=parent_sid,
            agent_name=data["agent_name"],
            task=data["task"],
            user_id=db_user_id,
            depth=data.get("depth", 1),
        )
        return result


@router.get("/api/internal/subagents/{parent_session_id}")
async def list_subagents_internal(
    parent_session_id: str,
    _: str = Depends(_verify_internal_token),
):
    """List subagent runs for a parent session. Called by orchestrator MCP."""
    async with async_session_factory() as db:
        return await subagent_registry.get_runs(db, parent_session_id)


@router.get("/api/internal/subagents/result/{run_id}")
async def get_subagent_result(
    run_id: str,
    _: str = Depends(_verify_internal_token),
):
    """Get subagent run result. Called by orchestrator MCP."""
    async with async_session_factory() as db:
        result = await db.execute(
            select(SubagentRun).where(SubagentRun.id == UUID(run_id))
        )
        run = result.scalar_one_or_none()
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return {
            "id": str(run.id),
            "status": run.status,
            "result": run.result,
            "agent_id": str(run.agent_id),
        }


@router.delete("/api/internal/subagents/{run_id}")
async def kill_subagent_internal(
    run_id: str,
    _: str = Depends(_verify_internal_token),
):
    """Kill a subagent. Called by orchestrator MCP."""
    async with async_session_factory() as db:
        ok = await subagent_registry.kill(db, run_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Run not found")
        return {"status": "killed"}


# User-facing endpoint
@router.get("/api/chat/sessions/{session_id}/subagents")
async def list_session_subagents(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """List subagent runs for a chat session (user-facing)."""
    return await subagent_registry.get_runs(db, str(session_id))
