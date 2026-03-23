"""
LLM Service — delegates to Claude Code CLI via ClaudeProcessManager.

Each user authenticates with their own Claude Team account.
No API key needed — billing through user's subscription.
"""

from collections.abc import AsyncGenerator

from app.services.claude_process import StreamEvent, claude_manager


class LLMService:
    """Thin wrapper around ClaudeProcessManager for backward compatibility."""

    async def stream_chat(
        self,
        user_id: str,
        chat_session_id: str,
        message: str,
        system_prompt: str | None = None,
        model: str | None = None,
        allowed_tools: list[str] | None = None,
        claude_session_id: str | None = None,
        working_dir: str | None = None,
        agent_id: str | None = None,
        agent_config: dict | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """
        Send a message and stream responses from Claude CLI.

        Args:
            user_id: Platform user ID (for config isolation)
            chat_session_id: Our chat session ID
            message: User message text
            system_prompt: Agent system prompt
            model: Model name (sonnet/opus/haiku)
            allowed_tools: Tools to auto-approve
            claude_session_id: Claude CLI session ID for --resume
            working_dir: Working directory for Claude CLI process
            agent_id: Agent UUID (for memory/orchestrator MCP)
            agent_config: Agent configuration dict
        """
        async for event in claude_manager.send_message(
            user_id=user_id,
            chat_session_id=chat_session_id,
            message=message,
            system_prompt=system_prompt,
            model=model,
            allowed_tools=allowed_tools,
            claude_session_id=claude_session_id,
            working_dir=working_dir,
            agent_id=agent_id,
            agent_config=agent_config,
        ):
            yield event

    async def check_auth(self, user_id: str) -> dict:
        """Check if user has authenticated with Claude."""
        return await claude_manager.check_user_auth(user_id)


llm_service = LLMService()
