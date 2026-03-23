import base64
import json
import os
import shutil
import uuid as uuid_mod
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import async_session_factory, get_db
from app.middleware.auth import get_or_create_user, validate_token_local, fetch_user_info
from app.models.base import Agent, ChatMessage, ChatSession, User
from app.schemas.chat import ChatSessionCreate, ChatSessionListResponse, ChatSessionResponse
from app.services.context_compaction import compact_context, should_compact
from app.services.llm_service import llm_service

UPLOADS_DIR = Path(os.environ.get("UPLOADS_DIR", Path(__file__).resolve().parent.parent.parent / "uploads"))

router = APIRouter(prefix="/api/chat", tags=["chat"])


async def get_ws_user(websocket: WebSocket) -> tuple[dict | None, str]:
    """Extract JWT, return (payload, sso_user_id)."""
    token = websocket.query_params.get("token")
    if not token:
        return None, ""
    payload = await validate_token_local(token)
    if not payload or not payload.get("user_id"):
        return None, ""
    sso_user_id = str(payload["user_id"])
    return payload, sso_user_id


async def get_db_user_id(db: AsyncSession, token: str) -> UUID | None:
    """Resolve SSO token to our DB user UUID."""
    user_info = await fetch_user_info(token)
    if not user_info:
        # Fallback: decode JWT to get SSO user_id
        payload = await validate_token_local(token)
        if not payload:
            return None
        # Can't reliably map without email, skip ownership check
        return None
    email = user_info.get("email", "")
    if not email:
        return None
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    return user.id if user else None


@router.post("/sessions", response_model=ChatSessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    data: ChatSessionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    result = await db.execute(select(Agent).where(Agent.id == data.agent_id, Agent.is_active == True))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    session = ChatSession(user_id=user.id, agent_id=data.agent_id)
    db.add(session)
    await db.flush()
    await db.refresh(session)
    result2 = await db.execute(
        select(ChatSession)
        .where(ChatSession.id == session.id)
        .options(selectinload(ChatSession.messages), selectinload(ChatSession.agent))
    )
    return result2.scalar_one()


@router.get("/sessions", response_model=ChatSessionListResponse)
async def list_sessions(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    # Mark stale active sessions as inactive
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.SESSION_INACTIVE_HOURS)
    await db.execute(
        update(ChatSession)
        .where(
            ChatSession.user_id == user.id,
            ChatSession.status == "active",
            ChatSession.updated_at < cutoff,
        )
        .values(status="inactive")
    )
    await db.flush()

    base_query = select(ChatSession).where(ChatSession.user_id == user.id)

    count_query = select(func.count()).select_from(base_query.subquery())
    total = (await db.execute(count_query)).scalar_one()

    query = (
        base_query
        .options(selectinload(ChatSession.messages), selectinload(ChatSession.agent))
        .offset(offset).limit(limit)
        .order_by(ChatSession.updated_at.desc())
    )
    result = await db.execute(query)
    sessions = result.scalars().all()

    return ChatSessionListResponse(items=sessions, total=total)


@router.get("/sessions/{session_id}", response_model=ChatSessionResponse)
async def get_session(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    result = await db.execute(
        select(ChatSession)
        .where(ChatSession.id == session_id, ChatSession.user_id == user.id)
        .options(selectinload(ChatSession.messages), selectinload(ChatSession.agent))
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    result = await db.execute(
        select(ChatSession)
        .where(ChatSession.id == session_id, ChatSession.user_id == user.id)
        .options(selectinload(ChatSession.messages))
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    for msg in session.messages:
        await db.delete(msg)
    await db.delete(session)
    await db.commit()

    # Clean up uploaded/generated files
    session_dir = UPLOADS_DIR / str(session_id)
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)


@router.post("/sessions/{session_id}/upload")
async def upload_file(
    session_id: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    result = await db.execute(
        select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == user.id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    session_dir = UPLOADS_DIR / str(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    safe_name = f"{uuid_mod.uuid4().hex[:8]}_{file.filename}"
    file_path = session_dir / safe_name
    content = await file.read()
    file_path.write_bytes(content)

    return {"path": str(file_path), "filename": file.filename, "size": len(content)}


@router.get("/files/{session_id}/{filename}")
async def download_file(
    session_id: UUID,
    filename: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    result = await db.execute(
        select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == user.id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Session not found")

    file_path = UPLOADS_DIR / str(session_id) / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(file_path, filename=filename)


def _save_image_to_session(session_id, image_data: str, image_mime: str) -> str:
    """Save base64 image to session uploads dir, return filename."""
    ext = "png"
    if "jpeg" in image_mime or "jpg" in image_mime:
        ext = "jpg"
    elif "webp" in image_mime:
        ext = "webp"

    filename = f"screenshot_{uuid_mod.uuid4().hex[:8]}.{ext}"
    session_dir = UPLOADS_DIR / str(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    file_path = session_dir / filename
    file_path.write_bytes(base64.b64decode(image_data))
    return filename


async def _process_and_stream(
    websocket: WebSocket,
    db: AsyncSession,
    session: ChatSession,
    agent: Agent,
    sso_user_id: str,
    user_content: str,
    allowed_tools: list[str],
    save_user_msg: bool = True,
    locale: str = "en",
):
    """Stream LLM response for a user message, save assistant message to DB."""
    import logging
    _logger = logging.getLogger(__name__)
    _logger.info(f">>> _process_and_stream called: session={session.id}, user={sso_user_id}, content={user_content[:50]}")
    session_id = session.id
    full_response = ""
    total_tokens = 0
    input_tokens = 0
    output_tokens = 0
    tool_calls_data = []
    tool_results_data = []
    tools_used = []
    new_claude_session_id = session.claude_session_id
    client_disconnected = False

    # Isolate working directory per session
    session_dir = UPLOADS_DIR / str(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    # Check for context compaction before sending
    if should_compact(session):
        summary = await compact_context(db, session, sso_user_id)
        if summary:
            user_content = f"[Контекст предыдущего диалога суммаризован]\n\n{summary}\n\n---\n\n{user_content}"
            async def _ws_send_compact(data: dict):
                try:
                    await websocket.send_json(data)
                except Exception:
                    pass
            await _ws_send_compact({"type": "context_compacted", "summary_length": len(summary)})

    async def _ws_send(data: dict):
        """Send JSON to WebSocket, silently marking disconnected on failure."""
        nonlocal client_disconnected
        if client_disconnected:
            return
        try:
            await websocket.send_json(data)
        except Exception:
            client_disconnected = True

    # Build agent config for MCP servers
    agent_config = {
        "memory_enabled": agent.memory_enabled,
        "memory_scope": agent.memory_scope,
        "tools": agent.tools or [],
        "db_user_id": str(session.user_id),  # DB UUID for memory MCP
    }

    async for event in llm_service.stream_chat(
        user_id=sso_user_id,
        chat_session_id=str(session_id),
        message=user_content,
        system_prompt=agent.system_prompt,
        model=agent.model,
        allowed_tools=allowed_tools,
        claude_session_id=session.claude_session_id,
        working_dir=str(session_dir),
        agent_id=str(agent.id),
        agent_config=agent_config,
        locale=locale,
    ):
        if event.type == "text":
            full_response += event.content
            await _ws_send({
                "type": "stream",
                "content": event.content,
            })
        elif event.type == "tool_use":
            tools_used.append(event.tool_name)
            tool_calls_data.append({
                "type": "tool_use",
                "id": event.tool_use_id,
                "name": event.tool_name,
                "input": event.tool_input,
            })
            await _ws_send({
                "type": "tool_use",
                "tool_name": event.tool_name,
                "tool_input": event.tool_input,
            })
            # Emit subagent_spawned for orchestrator spawn_agent
            if "orchestrator" in event.tool_name and "spawn" in event.tool_name:
                await _ws_send({
                    "type": "subagent_spawned",
                    "run_id": event.tool_use_id,
                    "agent_name": event.tool_input.get("agent_name", ""),
                    "task": event.tool_input.get("task", ""),
                })
        elif event.type == "tool_result":
            tool_results_data.append({
                "type": "tool_result",
                "tool_use_id": event.tool_use_id,
                "tool_name": event.tool_name,
                "content": event.content,
            })
            await _ws_send({
                "type": "tool_result",
                "tool_name": event.tool_name,
                "content": event.content[:2000],
            })
            # Emit subagent_completed for any orchestrator tool result
            if "orchestrator" in (event.tool_name or ""):
                import json as _json
                try:
                    result_data = _json.loads(event.content)
                    if "status" in result_data:
                        await _ws_send({
                            "type": "subagent_completed",
                            "run_id": event.tool_use_id,
                            "status": result_data.get("status", "completed"),
                            "result": result_data.get("result", "")[:500],
                        })
                except Exception:
                    pass
        elif event.type == "image":
            filename = _save_image_to_session(session_id, event.image_data, event.image_mime)
            image_url = f"/api/chat/files/{session_id}/{filename}"
            await _ws_send({
                "type": "image",
                "url": image_url,
                "tool_name": event.tool_name,
            })
            # Append image reference to response text
            full_response += f"\n![screenshot]({image_url})\n"
        elif event.type == "done":
            total_tokens = event.tokens_used
            if event.content:
                new_claude_session_id = event.content
        elif event.type == "error":
            await _ws_send({
                "type": "error",
                "content": event.content,
            })

    # Always save assistant message to DB, even if client disconnected
    assistant_message = ChatMessage(
        session_id=session_id,
        role="assistant",
        content=full_response,
        tool_calls=tool_calls_data if tool_calls_data else None,
        tool_results=tool_results_data if tool_results_data else None,
        tokens_used=total_tokens,
    )
    db.add(assistant_message)

    session.total_tokens += total_tokens
    if new_claude_session_id and new_claude_session_id != session.claude_session_id:
        session.claude_session_id = new_claude_session_id

    # Update context token tracking (rough estimate from total — Claude CLI reports combined)
    # Use 70/30 split as approximation when detailed usage unavailable
    if total_tokens > 0:
        session.context_input_tokens += int(total_tokens * 0.7)
        session.context_output_tokens += int(total_tokens * 0.3)

    await db.commit()
    await db.refresh(session, ["messages"])

    # Send context stats
    if session.context_limit > 0:
        usage_pct = round(session.context_input_tokens / session.context_limit * 100, 1)
        await _ws_send({
            "type": "context_stats",
            "input_tokens": session.context_input_tokens,
            "output_tokens": session.context_output_tokens,
            "context_limit": session.context_limit,
            "usage_percent": usage_pct,
        })

    if client_disconnected:
        return

    # Scan for created files (images, .pen, etc.) and send download links
    downloadable_exts = {".pen", ".png", ".jpg", ".jpeg", ".webp", ".pdf", ".svg", ".zip", ".html"}
    files_created = []
    for f in session_dir.iterdir():
        if f.is_file() and f.suffix.lower() in downloadable_exts:
            url = f"/api/chat/files/{session_id}/{f.name}"
            files_created.append({"filename": f.name, "url": url, "size": f.stat().st_size})
            # Send images inline
            if f.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                await _ws_send({"type": "image", "url": url, "tool_name": ""})

    await _ws_send({
        "type": "done",
        "tokens_used": total_tokens,
        "tools_used": tools_used,
        "actions": _get_available_actions(agent, tools_used),
        "files": files_created,
    })


@router.websocket("/ws/{session_id}")
async def chat_websocket(websocket: WebSocket, session_id: UUID):
    await websocket.accept()

    user_payload, sso_user_id = await get_ws_user(websocket)
    if not user_payload:
        await websocket.send_json({"type": "error", "content": "Unauthorized"})
        await websocket.close(code=4001)
        return

    try:
        async with async_session_factory() as db:
            result = await db.execute(
                select(ChatSession)
                .where(ChatSession.id == session_id)
                .options(selectinload(ChatSession.messages), selectinload(ChatSession.agent))
            )
            session = result.scalar_one_or_none()
            if not session:
                await websocket.send_json({"type": "error", "content": "Session not found"})
                await websocket.close()
                return

            agent = session.agent
            allowed_tools = ["Read", "Glob", "Grep", "Bash", "Edit", "Write"]

            # Memory tools — always enabled by default
            if agent.memory_enabled:
                allowed_tools.extend([
                    "memory:search", "memory:read", "memory:write", "memory:list",
                ])

            # Orchestrator tools — always available for all agents
            allowed_tools.extend([
                "orchestrator:spawn_agent", "orchestrator:list_running",
                "orchestrator:get_result", "orchestrator:kill_agent",
            ])

            # Default locale
            locale = "en"

            # Check for unprocessed user message (e.g. from pipeline creation)
            if session.messages:
                last_msg = sorted(session.messages, key=lambda m: m.created_at)[-1]
                if last_msg.role == "user":
                    await _process_and_stream(
                        websocket, db, session, agent, sso_user_id,
                        last_msg.content, allowed_tools, save_user_msg=False,
                        locale=locale,
                    )

            while True:
                raw = await websocket.receive_text()
                data = json.loads(raw)
                msg_type = data.get("type", "message")

                if msg_type == "tool_confirm_response":
                    continue

                user_content = data.get("content", "")
                locale = data.get("locale", "en")

                # Reactivate inactive session on new message
                if session.status == "inactive":
                    session.status = "active"

                user_message = ChatMessage(
                    session_id=session_id,
                    role="user",
                    content=user_content,
                )
                db.add(user_message)
                await db.commit()

                await _process_and_stream(
                    websocket, db, session, agent, sso_user_id,
                    user_content, allowed_tools,
                    locale=locale,
                )

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "content": str(e)})
            await websocket.close()
        except Exception:
            pass


@router.get("/sessions/{session_id}/context")
async def get_context_stats(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    result = await db.execute(
        select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == user.id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    usage_pct = 0.0
    if session.context_limit > 0:
        usage_pct = round(session.context_input_tokens / session.context_limit * 100, 1)

    return {
        "input_tokens": session.context_input_tokens,
        "output_tokens": session.context_output_tokens,
        "context_limit": session.context_limit,
        "usage_percent": usage_pct,
    }


def _get_available_actions(agent: Agent, tools_used: list[str]) -> list[dict]:
    actions = []
    used_set = set(tools_used)

    if any("git" in t.lower() for t in used_set):
        actions.append({"label": "Создать коммит", "action": "git_commit", "icon": "git-commit"})
        actions.append({"label": "Создать MR", "action": "git_mr", "icon": "git-pull-request"})

    if any("edit" in t.lower() or "write" in t.lower() for t in used_set):
        actions.append({"label": "Показать diff", "action": "show_diff", "icon": "file-diff"})

    return actions
