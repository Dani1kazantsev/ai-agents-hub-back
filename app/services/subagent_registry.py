"""Subagent Registry — spawn, track, and manage sub-agent processes."""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Agent, ChatMessage, ChatSession, SubagentRun
from app.services.claude_process import claude_manager

logger = logging.getLogger(__name__)

MAX_DEPTH = 3


async def spawn(
    db: AsyncSession,
    parent_session_id: str,
    agent_name: str,
    task: str,
    user_id: str,
    depth: int = 1,
) -> dict:
    """Spawn a sub-agent: create session, run Claude CLI, collect result."""
    if depth > MAX_DEPTH:
        return {"error": f"Maximum nesting depth ({MAX_DEPTH}) exceeded", "status": "failed"}

    # Find agent by name — case-insensitive partial match
    result = await db.execute(
        select(Agent).where(
            Agent.name.ilike(f"%{agent_name}%"),
            Agent.is_active == True,
        )
    )
    agent = result.scalars().first()
    if not agent:
        # Try exact match as fallback
        result = await db.execute(
            select(Agent).where(Agent.name == agent_name, Agent.is_active == True)
        )
        agent = result.scalar_one_or_none()
    if not agent:
        return {"error": f"Agent '{agent_name}' not found", "status": "failed"}

    # Create child session
    child_session = ChatSession(
        user_id=uuid.UUID(user_id),
        agent_id=agent.id,
        status="active",
    )
    db.add(child_session)
    await db.flush()

    # Create subagent run record
    run = SubagentRun(
        parent_session_id=uuid.UUID(parent_session_id),
        child_session_id=child_session.id,
        agent_id=agent.id,
        task=task,
        status="running",
        depth=depth,
    )
    db.add(run)
    await db.commit()

    # Save user message in child session
    user_msg = ChatMessage(
        session_id=child_session.id,
        role="user",
        content=task,
    )
    db.add(user_msg)
    await db.commit()

    # Run Claude CLI for the sub-agent
    allowed_tools = ["Read", "Glob", "Grep", "Bash", "Edit", "Write"]
    if agent.tools:
        from app.services.claude_process import ClaudeProcessManager
        allowed_tools.extend(ClaudeProcessManager._resolve_tool_names(agent.tools))

    full_response = ""
    total_tokens = 0

    try:
        async for event in claude_manager.send_message(
            user_id=user_id,
            chat_session_id=str(child_session.id),
            message=task,
            system_prompt=agent.system_prompt,
            model=agent.model,
            allowed_tools=allowed_tools,
        ):
            if event.type == "text":
                full_response += event.content
            elif event.type == "done":
                total_tokens = event.tokens_used
                if event.content:
                    child_session.claude_session_id = event.content
            elif event.type == "error":
                run.status = "failed"
                run.result = event.content
                run.completed_at = datetime.now(timezone.utc)
                await db.commit()
                return {
                    "run_id": str(run.id),
                    "status": "failed",
                    "error": event.content,
                    "agent_name": agent_name,
                }

        # Save assistant message
        assistant_msg = ChatMessage(
            session_id=child_session.id,
            role="assistant",
            content=full_response,
            tokens_used=total_tokens,
        )
        db.add(assistant_msg)

        # Update run
        run.status = "completed"
        run.result = full_response
        run.completed_at = datetime.now(timezone.utc)
        child_session.total_tokens = total_tokens
        child_session.status = "inactive"
        await db.commit()

        return {
            "run_id": str(run.id),
            "status": "completed",
            "result": full_response[:5000],
            "agent_name": agent_name,
            "tokens_used": total_tokens,
        }

    except Exception as e:
        logger.exception(f"Subagent spawn failed: {e}")
        run.status = "failed"
        run.result = str(e)
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
        return {
            "run_id": str(run.id),
            "status": "failed",
            "error": str(e),
            "agent_name": agent_name,
        }


async def get_runs(db: AsyncSession, parent_session_id: str) -> list[dict]:
    """Get all subagent runs for a parent session."""
    result = await db.execute(
        select(SubagentRun)
        .where(SubagentRun.parent_session_id == uuid.UUID(parent_session_id))
        .order_by(SubagentRun.created_at.desc())
    )
    runs = result.scalars().all()
    return [
        {
            "id": str(r.id),
            "agent_id": str(r.agent_id),
            "task": r.task[:200],
            "status": r.status,
            "result": r.result[:2000] if r.result else None,
            "depth": r.depth,
            "created_at": str(r.created_at),
            "completed_at": str(r.completed_at) if r.completed_at else None,
        }
        for r in runs
    ]


async def kill(db: AsyncSession, run_id: str) -> bool:
    """Kill a running subagent."""
    result = await db.execute(
        select(SubagentRun).where(SubagentRun.id == uuid.UUID(run_id))
    )
    run = result.scalar_one_or_none()
    if not run:
        return False

    if run.child_session_id:
        await claude_manager.kill_session(str(run.child_session_id))

    run.status = "killed"
    run.completed_at = datetime.now(timezone.utc)
    await db.commit()
    return True


async def kill_tree(db: AsyncSession, parent_session_id: str) -> int:
    """Kill all running subagents for a parent session."""
    result = await db.execute(
        select(SubagentRun).where(
            SubagentRun.parent_session_id == uuid.UUID(parent_session_id),
            SubagentRun.status.in_(["pending", "running"]),
        )
    )
    runs = result.scalars().all()
    count = 0
    for run in runs:
        if run.child_session_id:
            await claude_manager.kill_session(str(run.child_session_id))
        run.status = "killed"
        run.completed_at = datetime.now(timezone.utc)
        count += 1
    await db.commit()
    return count
