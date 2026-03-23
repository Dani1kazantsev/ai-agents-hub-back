"""
Claude Auth API — endpoints for managing Claude CLI authentication per user.

Users authenticate with their own Claude Team account via `claude auth login`.
Uses SSO user_id (not DB UUID) as the stable key for CLAUDE_CONFIG_DIR.
"""

import asyncio
import json
import os

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.middleware.auth import get_current_user, get_or_create_user, validate_token_local
from app.models.base import User
from app.services.claude_process import claude_manager

router = APIRouter(prefix="/api/claude-auth", tags=["claude-auth"])
security = HTTPBearer()


def _sso_user_id(payload: dict) -> str:
    """Extract SSO user_id from auth payload. Used as key for Claude config dir."""
    return str(payload.get("user_id", ""))


@router.get("/status")
async def claude_auth_status(
    payload: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """Check if the current user has authenticated with Claude CLI."""
    sso_id = _sso_user_id(payload)
    result = await claude_manager.check_user_auth(sso_id)
    is_auth = result.get("authenticated", False)

    # Sync DB field
    if user.claude_authenticated != is_auth:
        user.claude_authenticated = is_auth
        await db.commit()

    return {
        "authenticated": is_auth,
        "details": result.get("details"),
        "error": result.get("error"),
    }


@router.websocket("/terminal")
async def claude_auth_terminal(websocket: WebSocket):
    """
    WebSocket terminal for `claude auth login`.

    Protocol:
    - Server sends: {"type": "output", "data": "..."} — terminal output
    - Server sends: {"type": "auth_url", "url": "..."} — extracted auth URL
    - Server sends: {"type": "done", "success": true/false}
    - Client sends: {"type": "input", "data": "..."} — terminal input
    """
    await websocket.accept()

    token = websocket.query_params.get("token")
    if not token:
        await websocket.send_json({"type": "error", "content": "Unauthorized"})
        await websocket.close(code=4001)
        return

    payload = await validate_token_local(token)
    if not payload or not payload.get("user_id"):
        await websocket.send_json({"type": "error", "content": "Unauthorized"})
        await websocket.close(code=4001)
        return

    sso_id = _sso_user_id(payload)
    config_dir = claude_manager.get_user_config_dir(sso_id)

    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDECODE", None)
    # Prevent CLI from opening browser automatically — we show the URL in the UI
    env["BROWSER"] = "echo"
    env["DISPLAY"] = ""

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "auth", "login",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        async def read_stream(stream, stream_name):
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")

                await websocket.send_json({
                    "type": "output",
                    "data": text,
                })

                if "http" in text:
                    import re
                    urls = re.findall(r'https?://[^\s<>"\']+', text)
                    for url in urls:
                        if "anthropic" in url or "claude" in url:
                            await websocket.send_json({
                                "type": "auth_url",
                                "url": url,
                            })

        stdout_task = asyncio.create_task(read_stream(proc.stdout, "stdout"))
        stderr_task = asyncio.create_task(read_stream(proc.stderr, "stderr"))

        async def handle_input():
            try:
                while True:
                    raw = await websocket.receive_text()
                    data = json.loads(raw)
                    if data.get("type") == "input" and proc.stdin:
                        proc.stdin.write(data.get("data", "").encode())
                        await proc.stdin.drain()
            except (WebSocketDisconnect, Exception):
                pass

        input_task = asyncio.create_task(handle_input())

        await proc.wait()

        for task in [stdout_task, stderr_task, input_task]:
            if not task.done():
                task.cancel()

        success = proc.returncode == 0

        if success:
            check = await claude_manager.check_user_auth(sso_id)
            success = check.get("authenticated", False)

        await websocket.send_json({
            "type": "done",
            "success": success,
        })

    except WebSocketDisconnect:
        pass
    except FileNotFoundError:
        await websocket.send_json({
            "type": "error",
            "content": "Claude CLI not installed on server. Install with: npm install -g @anthropic-ai/claude-code",
        })
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "content": str(e)})
        except Exception:
            pass
    finally:
        if proc and proc.returncode is None:
            proc.kill()
        try:
            await websocket.close()
        except Exception:
            pass


@router.post("/logout")
async def claude_auth_logout(payload: dict = Depends(get_current_user)):
    """Logout user from Claude CLI."""
    sso_id = _sso_user_id(payload)
    env = os.environ.copy()
    config_dir = claude_manager.get_user_config_dir(sso_id)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDECODE", None)

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "auth", "logout",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10)
        return {"success": proc.returncode == 0}
    except Exception as e:
        return {"success": False, "error": str(e)}
