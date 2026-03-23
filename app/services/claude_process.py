"""
Claude Code CLI Process Manager.

Manages per-user Claude Code CLI processes communicating via stream-json protocol.
Each user gets an isolated CLAUDE_CONFIG_DIR for their auth tokens.
MCP servers (Jira, GitLab, DB, docs) are configured via generated .mcp.json.
"""

import asyncio
import json
import logging
import os
import platform
import signal
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator

from app.config import settings

logger = logging.getLogger(__name__)

def _detect_pencil_mcp() -> tuple[str, list[str]] | None:
    """Auto-detect Pencil MCP server binary based on platform.

    Returns (command, args) or None if not found.
    Checks env var PENCIL_MCP_COMMAND first, then known install paths.
    """
    # 1. Explicit env var / config — highest priority
    if settings.PENCIL_MCP_COMMAND:
        parts = settings.PENCIL_MCP_COMMAND.split()
        return parts[0], parts[1:]

    # 2. Auto-detect by platform
    system = platform.system()
    candidates: list[tuple[str, list[str]]] = []

    if system == "Darwin":
        # macOS — bundled with Pencil.app
        candidates.append((
            "/Applications/Pencil.app/Contents/Resources/app.asar.unpacked/out/mcp-server-darwin-arm64",
            ["--app", "desktop"],
        ))
        candidates.append((
            "/Applications/Pencil.app/Contents/Resources/app.asar.unpacked/out/mcp-server-darwin-x64",
            ["--app", "desktop"],
        ))
    elif system == "Windows":
        # Windows — typical install paths
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            candidates.append((
                str(Path(local_app_data) / "Programs" / "Pencil" / "resources" / "app.asar.unpacked" / "out" / "mcp-server-win32-x64.exe"),
                ["--app", "desktop"],
            ))
    elif system == "Linux":
        # Linux — common paths
        candidates.append((
            "/opt/Pencil/resources/app.asar.unpacked/out/mcp-server-linux-x64",
            ["--app", "desktop"],
        ))
        home = Path.home()
        candidates.append((
            str(home / ".local" / "share" / "Pencil" / "resources" / "app.asar.unpacked" / "out" / "mcp-server-linux-x64"),
            ["--app", "desktop"],
        ))

    for cmd, args in candidates:
        if Path(cmd).is_file():
            logger.info(f"Auto-detected Pencil MCP: {cmd}")
            return cmd, args

    return None


# Base directory for per-user Claude configs
CLAUDE_CONFIGS_BASE = Path(os.environ.get(
    "CLAUDE_CONFIGS_DIR",
    Path.home() / ".claude-hub-configs",
))

# Backend root and MCP servers base directory
BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
MCP_SERVERS_DIR = Path(settings.MCP_SERVERS_DIR) if settings.MCP_SERVERS_DIR else BACKEND_ROOT / "mcp-servers"
MCP_PYTHON = str(BACKEND_ROOT / ".venv" / "bin" / "python")


@dataclass
class StreamEvent:
    """Event emitted from Claude CLI process."""
    type: str  # "text", "tool_use", "tool_result", "image", "done", "error"
    content: str = ""
    tokens_used: int = 0
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    tool_use_id: str = ""
    image_data: str = ""  # base64 image data
    image_mime: str = ""  # e.g. "image/png"


@dataclass
class ClaudeSession:
    """Tracks a running Claude CLI process for a chat session."""
    process: asyncio.subprocess.Process
    session_id: str  # our chat session ID
    claude_session_id: str | None = None  # Claude CLI session ID for --resume
    user_id: str = ""


class ClaudeProcessManager:
    """Manages Claude CLI processes per user/session."""

    def __init__(self):
        # {chat_session_id: ClaudeSession}
        self._sessions: dict[str, ClaudeSession] = {}

    def get_user_config_dir(self, user_id: str) -> Path:
        """Get isolated config directory for a user."""
        config_dir = CLAUDE_CONFIGS_BASE / user_id
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir

    def _get_mcp_config_path(
        self,
        user_id: str,
        agent_id: str | None = None,
        agent_config: dict | None = None,
        chat_session_id: str | None = None,
    ) -> Path | None:
        """Generate MCP config with real credentials in user's config dir.

        Args:
            user_id: Platform user ID
            agent_id: Agent UUID (for memory MCP)
            agent_config: Dict with agent settings (memory_enabled, memory_scope, tools)
            chat_session_id: Chat session UUID (for orchestrator MCP)
        """
        if not MCP_SERVERS_DIR or not MCP_SERVERS_DIR.exists():
            logger.warning(f"MCP_SERVERS_DIR not set or missing: {MCP_SERVERS_DIR}")
            return None

        config = {"mcpServers": {}}

        # Determine which MCP services the agent needs from its tools list
        agent_tools = agent_config.get("tools", []) if agent_config else []
        needed_services = set()
        for tool in agent_tools:
            if ":" in tool:
                needed_services.add(tool.split(":")[0])

        if ("jira" in needed_services) and settings.JIRA_BASE_URL and settings.JIRA_API_TOKEN:
            config["mcpServers"]["jira"] = {
                "command": MCP_PYTHON,
                "args": [str(MCP_SERVERS_DIR / "jira" / "server.py")],
                "env": {
                    "JIRA_BASE_URL": settings.JIRA_BASE_URL,
                    "JIRA_EMAIL": settings.JIRA_EMAIL,
                    "JIRA_API_TOKEN": settings.JIRA_API_TOKEN,
                },
            }

        if settings.GITLAB_URL and settings.GITLAB_TOKEN:
            if "gitlab" in needed_services:
                config["mcpServers"]["gitlab"] = {
                    "command": MCP_PYTHON,
                    "args": [str(MCP_SERVERS_DIR / "gitlab" / "server.py")],
                    "env": {
                        "GITLAB_URL": settings.GITLAB_URL,
                        "GITLAB_TOKEN": settings.GITLAB_TOKEN,
                    },
                }
            if "docs" in needed_services:
                docs_env = {
                    "GITLAB_URL": settings.GITLAB_URL,
                    "GITLAB_TOKEN": settings.GITLAB_TOKEN,
                    "DOCS_PROJECT_ID": settings.DOCS_PROJECT_ID,
                }
                if settings.PROJECTS_CONFIG_PATH:
                    docs_env["PROJECTS_CONFIG_PATH"] = settings.PROJECTS_CONFIG_PATH
                config["mcpServers"]["docs"] = {
                    "command": MCP_PYTHON,
                    "args": [str(MCP_SERVERS_DIR / "docs" / "server.py")],
                    "env": docs_env,
                }

        if ("db" in needed_services) and settings.EXTERNAL_DATABASE_URL:
            config["mcpServers"]["db"] = {
                "command": MCP_PYTHON,
                "args": [str(MCP_SERVERS_DIR / "db" / "server.py")],
                "env": {
                    "EXTERNAL_DATABASE_URL": settings.EXTERNAL_DATABASE_URL,
                },
            }

        if ("figma" in needed_services) and settings.FIGMA_ACCESS_TOKEN:
            config["mcpServers"]["figma"] = {
                "command": MCP_PYTHON,
                "args": [str(MCP_SERVERS_DIR / "figma" / "server.py")],
                "env": {
                    "FIGMA_ACCESS_TOKEN": settings.FIGMA_ACCESS_TOKEN,
                },
            }

        # Pencil MCP server — only if agent has pencil:* tools
        if "pencil" in needed_services:
            pencil = _detect_pencil_mcp()
            if pencil:
                cmd, args = pencil
                config["mcpServers"]["pencil"] = {
                    "command": cmd,
                    "args": args,
                }

        # Memory MCP server — always enabled (memory_enabled defaults to True)
        memory_enabled = True
        memory_scope = "personal"
        if agent_config:
            memory_enabled = agent_config.get("memory_enabled", True)
            memory_scope = agent_config.get("memory_scope", "personal")

        if memory_enabled and agent_id:
            db_url = settings.DATABASE_URL.replace("+asyncpg", "")
            # Use DB user UUID (not SSO ID) for memory MCP
            db_user_id = agent_config.get("db_user_id", "") if agent_config else ""
            memory_env = {
                "MEMORY_DATABASE_URL": db_url,
                "MEMORY_AGENT_ID": agent_id,
                "MEMORY_SCOPE": memory_scope,
            }
            if memory_scope == "personal" and db_user_id:
                memory_env["MEMORY_USER_ID"] = db_user_id
            config["mcpServers"]["memory"] = {
                "command": MCP_PYTHON,
                "args": [str(MCP_SERVERS_DIR / "memory" / "server.py")],
                "env": memory_env,
            }

        # Orchestrator MCP server — always enabled for agent orchestration
        if chat_session_id and agent_id:
            backend_url = "http://127.0.0.1:8000"
            config["mcpServers"]["orchestrator"] = {
                "command": MCP_PYTHON,
                "args": [str(MCP_SERVERS_DIR / "orchestrator" / "server.py")],
                "env": {
                    "ORCHESTRATOR_BACKEND_URL": backend_url,
                    "ORCHESTRATOR_AUTH_TOKEN": settings.INTERNAL_SERVICE_TOKEN or "internal",
                    "ORCHESTRATOR_PARENT_SESSION_ID": chat_session_id,
                    "ORCHESTRATOR_USER_ID": user_id,
                    "ORCHESTRATOR_DEPTH": str(agent_config.get("_depth", 1)) if agent_config else "1",
                },
            }

        if not config["mcpServers"]:
            return None

        config_dir = self.get_user_config_dir(user_id)
        mcp_config_path = config_dir / "mcp-config.json"
        mcp_config_path.write_text(json.dumps(config, indent=2))
        return mcp_config_path

    # Built-in Claude CLI tools — passed as-is
    BUILTIN_TOOLS = {"Read", "Write", "Edit", "Glob", "Grep", "Bash", "Agent", "NotebookEdit"}

    @staticmethod
    def _resolve_tool_names(tool_names: list[str]) -> list[str]:
        """Map agent YAML tool names to Claude CLI tool names.

        Agent configs use 'service:action' format (e.g. jira:search_issues).
        Claude CLI expects 'mcp__server__tool' for MCP tools.
        Built-in tools (Read, Glob, etc.) pass through unchanged.
        """
        resolved = []
        for name in tool_names:
            if name in ClaudeProcessManager.BUILTIN_TOOLS:
                resolved.append(name)
            elif ":" in name:
                # jira:search_issues -> mcp__jira__search_issues
                server, action = name.split(":", 1)
                resolved.append(f"mcp__{server}__{action}")
            else:
                resolved.append(name)
        return resolved

    async def check_user_auth(self, user_id: str) -> dict:
        """Check if user has authenticated with Claude CLI."""
        config_dir = self.get_user_config_dir(user_id)
        env = self._get_env(user_id)

        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "auth", "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=10
            )

            if proc.returncode == 0:
                try:
                    status = json.loads(stdout.decode())
                    return {"authenticated": True, "details": status}
                except json.JSONDecodeError:
                    return {"authenticated": True, "details": stdout.decode().strip()}

            return {"authenticated": False, "error": stderr.decode().strip()}

        except FileNotFoundError:
            return {"authenticated": False, "error": "Claude CLI not installed"}
        except asyncio.TimeoutError:
            return {"authenticated": False, "error": "Auth check timed out"}

    def _get_env(self, user_id: str) -> dict:
        """Build environment for Claude CLI process."""
        config_dir = self.get_user_config_dir(user_id)
        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
        # Remove vars that interfere with subprocess Claude CLI
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("CLAUDECODE", None)
        return env

    async def send_message(
        self,
        user_id: str,
        chat_session_id: str,
        message: str,
        system_prompt: str | None = None,
        model: str | None = None,
        allowed_tools: list[str] | None = None,
        working_dir: str | None = None,
        claude_session_id: str | None = None,
        agent_id: str | None = None,
        agent_config: dict | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """
        Send a message to Claude CLI and stream back events.

        Uses `claude -p` with stream-json output. Each call is a new process,
        but we use --resume with claude_session_id for conversation continuity.
        """
        env = self._get_env(user_id)

        cmd = ["claude", "-p", message]

        # Output format
        cmd.extend(["--output-format", "stream-json"])
        cmd.extend(["--verbose"])

        # Bypass permissions — agents run non-interactively
        cmd.append("--dangerously-skip-permissions")

        # Model
        if model:
            cmd.extend(["--model", model])

        # System prompt — always enforce Russian + append agent prompt
        base_system = "Всегда отвечай на русском языке."
        if system_prompt:
            base_system = f"{base_system}\n\n{system_prompt}"

        # Memory instructions — always enabled by default
        memory_enabled = agent_config.get("memory_enabled", True) if agent_config else True
        if memory_enabled:
            base_system += (
                "\n\n## Память агента (КРИТИЧЕСКИ ВАЖНО!)\n"
                "ЗАПРЕЩЕНО использовать файловую систему, Write, Read, auto-memory или CLAUDE.md для сохранения памяти!\n"
                "Для памяти используй ИСКЛЮЧИТЕЛЬНО MCP-инструменты memory-сервера:\n"
                "- mcp__memory__read(key) — прочитать запись. Пример: key='MEMORY.md'\n"
                "- mcp__memory__write(key, content, tags) — создать/обновить запись в БД\n"
                "- mcp__memory__search(query) — полнотекстовый поиск по памяти\n"
                "- mcp__memory__list(prefix) — список всех ключей в памяти\n\n"
                "Когда пользователь просит 'запомни' или 'сохрани' — ОБЯЗАТЕЛЬНО вызови mcp__memory__write.\n"
                "НЕ ПИШИ файлы на диск для сохранения памяти. НЕ используй Write/Edit для этого.\n"
                "Память хранится в PostgreSQL через MCP-сервер, а НЕ в файловой системе.\n"
            )

        # Orchestrator instructions — always available
        base_system += (
            "\n\n## Оркестрирование\n"
            "Ты можешь делегировать задачи другим агентам через MCP-инструменты:\n"
            "- mcp__orchestrator__spawn_agent(agent_name, task) — запустить подагента\n"
            "- mcp__orchestrator__list_running() — список запущенных подагентов\n"
            "- mcp__orchestrator__get_result(run_id) — результат подагента\n"
            "- mcp__orchestrator__kill_agent(run_id) — отменить подагента\n"
        )

        cmd.extend(["--append-system-prompt", base_system])

        # Resume previous session for context continuity
        if claude_session_id:
            cmd.extend(["--resume", claude_session_id])

        # MCP config — pass agent details for memory + orchestrator servers
        mcp_config_path = self._get_mcp_config_path(
            user_id,
            agent_id=agent_id,
            agent_config=agent_config,
            chat_session_id=chat_session_id,
        )
        if mcp_config_path:
            cmd.extend(["--mcp-config", str(mcp_config_path)])

        # Map agent tool names (jira:search_issues) to MCP tool names (mcp__jira__search_issues)
        # and keep built-in Claude tools as-is
        cli_tools = self._resolve_tool_names(allowed_tools or [])

        if cli_tools:
            cmd.extend(["--allowedTools", ",".join(cli_tools)])
        else:
            # Allow basic read tools by default
            cmd.extend(["--allowedTools", "Read,Glob,Grep"])

        # Working directory
        cwd = working_dir or os.getcwd()

        logger.info(f"Spawning Claude CLI for user={user_id}, session={chat_session_id}")
        logger.debug(f"Command: {' '.join(cmd)}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
            )

            captured_session_id = claude_session_id
            total_tokens = 0

            # Parse NDJSON stream from stdout
            async for event in self._parse_stream(proc):
                if event.type == "_session_id":
                    captured_session_id = event.content
                    continue
                if event.type == "done":
                    event.content = captured_session_id or ""
                    total_tokens = event.tokens_used
                yield event

            # Wait for process to finish
            await proc.wait()

            # Check stderr for errors
            if proc.returncode != 0:
                stderr_data = await proc.stderr.read()
                error_msg = stderr_data.decode().strip() if stderr_data else "Unknown error"
                if "not authenticated" in error_msg.lower() or "login" in error_msg.lower():
                    yield StreamEvent(type="error", content="Claude CLI not authenticated. Please connect your Claude account.")
                else:
                    yield StreamEvent(type="error", content=f"Claude CLI error: {error_msg}")

        except FileNotFoundError:
            yield StreamEvent(type="error", content="Claude CLI not installed on server")
        except Exception as e:
            logger.exception(f"Error in Claude process for user={user_id}")
            yield StreamEvent(type="error", content=str(e))

    async def _parse_stream(
        self, proc: asyncio.subprocess.Process
    ) -> AsyncGenerator[StreamEvent, None]:
        """Parse NDJSON stream from Claude CLI stdout."""
        assert proc.stdout is not None

        buffer = b""
        while True:
            chunk = await proc.stdout.read(8192)
            if not chunk:
                break

            buffer += chunk
            lines = buffer.split(b"\n")
            # Keep incomplete last line in buffer
            buffer = lines[-1]

            for line in lines[:-1]:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    async for event in self._process_event(data):
                        yield event
                except json.JSONDecodeError:
                    logger.debug(f"Non-JSON line from CLI: {line[:200]}")

        # Process remaining buffer
        if buffer.strip():
            try:
                data = json.loads(buffer)
                async for event in self._process_event(data):
                    yield event
            except json.JSONDecodeError:
                pass

    @staticmethod
    def _extract_content_blocks(content) -> tuple[str, list[dict]]:
        """Extract text and image blocks from MCP tool result content.

        Returns (text_content, image_blocks) where image_blocks are
        dicts with 'data' (base64) and 'mime' keys.
        """
        if isinstance(content, str):
            return content, []
        if not isinstance(content, list):
            return str(content), []

        texts = []
        images = []
        for block in content:
            if not isinstance(block, dict):
                texts.append(str(block))
                continue
            block_type = block.get("type", "")
            if block_type == "image":
                source = block.get("source", {})
                if source.get("type") == "base64":
                    images.append({
                        "data": source.get("data", ""),
                        "mime": source.get("media_type", "image/png"),
                    })
            elif block_type == "text":
                texts.append(block.get("text", ""))
            else:
                texts.append(block.get("text", str(block)))
        return "\n".join(texts), images

    async def _process_event(self, data: dict) -> AsyncGenerator[StreamEvent, None]:
        """Convert a Claude CLI stream-json event to our StreamEvent."""
        event_type = data.get("type", "")

        # Capture session ID from init or result messages
        if "session_id" in data:
            yield StreamEvent(type="_session_id", content=data["session_id"])

        if event_type == "stream_event":
            inner = data.get("event", {})
            delta = inner.get("delta", {})
            delta_type = delta.get("type", "")

            if delta_type == "text_delta":
                yield StreamEvent(type="text", content=delta.get("text", ""))

            elif delta_type == "input_json_delta":
                # Tool input streaming — we accumulate on frontend
                pass

        elif event_type == "assistant":
            # Complete assistant message (may contain tool_use blocks)
            message = data.get("message", data)
            content_blocks = message.get("content", [])
            if isinstance(content_blocks, list):
                for block in content_blocks:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_use":
                            yield StreamEvent(
                                type="tool_use",
                                tool_name=block.get("name", ""),
                                tool_input=block.get("input", {}),
                                tool_use_id=block.get("id", ""),
                            )
                        elif block.get("type") == "tool_result":
                            text_content, images = self._extract_content_blocks(block.get("content", ""))
                            yield StreamEvent(
                                type="tool_result",
                                tool_name=block.get("tool_name", ""),
                                tool_use_id=block.get("tool_use_id", ""),
                                content=text_content[:10000],
                            )
                            for img in images:
                                yield StreamEvent(
                                    type="image",
                                    image_data=img["data"],
                                    image_mime=img["mime"],
                                    tool_name=block.get("tool_name", ""),
                                )

        elif event_type == "tool_use":
            yield StreamEvent(
                type="tool_use",
                tool_name=data.get("name", data.get("tool_name", "")),
                tool_input=data.get("input", {}),
                tool_use_id=data.get("id", ""),
            )

        elif event_type == "tool_result":
            text_content, images = self._extract_content_blocks(data.get("content", ""))
            yield StreamEvent(
                type="tool_result",
                tool_name=data.get("tool_name", ""),
                tool_use_id=data.get("tool_use_id", ""),
                content=text_content[:10000],
            )
            for img in images:
                yield StreamEvent(
                    type="image",
                    image_data=img["data"],
                    image_mime=img["mime"],
                    tool_name=data.get("tool_name", ""),
                )

        elif event_type == "result":
            # Final result message
            tokens = 0
            usage = data.get("usage", {})
            if usage:
                tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

            result_text = data.get("result", "")
            if result_text:
                yield StreamEvent(type="text", content=result_text)

            yield StreamEvent(
                type="done",
                tokens_used=tokens,
                content=data.get("session_id", ""),
            )

        elif event_type == "error":
            yield StreamEvent(
                type="error",
                content=data.get("error", {}).get("message", str(data)),
            )

    async def kill_session(self, chat_session_id: str):
        """Kill a running Claude process for a session."""
        session = self._sessions.pop(chat_session_id, None)
        if session and session.process.returncode is None:
            try:
                session.process.send_signal(signal.SIGTERM)
                await asyncio.wait_for(session.process.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                session.process.kill()

    async def cleanup(self):
        """Kill all running processes."""
        for session_id in list(self._sessions.keys()):
            await self.kill_session(session_id)


# Singleton
claude_manager = ClaudeProcessManager()
