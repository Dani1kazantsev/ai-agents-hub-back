"""Context compaction service — summarize old messages when context window fills up."""

import asyncio
import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import ChatMessage, ChatSession
from app.services.claude_process import claude_manager

logger = logging.getLogger(__name__)

COMPACTION_THRESHOLD = 0.7  # Trigger at 70% usage


def should_compact(session: ChatSession) -> bool:
    """Check if context compaction is needed."""
    if session.context_input_tokens <= 0 or session.context_limit <= 0:
        return False
    usage = session.context_input_tokens / session.context_limit
    return usage > COMPACTION_THRESHOLD


async def compact_context(
    db: AsyncSession,
    session: ChatSession,
    user_id: str,
) -> str | None:
    """Summarize old messages and reset session for continued conversation.

    Returns the summary text if compaction happened, None otherwise.
    """
    if not should_compact(session):
        return None

    # Gather messages for summarization
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.created_at)
    )
    messages = result.scalars().all()

    if len(messages) < 4:
        return None

    # Build conversation text for summarization (first 80% of messages)
    cutoff = int(len(messages) * 0.8)
    old_messages = messages[:cutoff]

    conversation_text = ""
    for msg in old_messages:
        role = "Пользователь" if msg.role == "user" else "Ассистент"
        conversation_text += f"{role}: {msg.content[:500]}\n"

    # Use a one-shot Claude CLI call to summarize
    summary_prompt = (
        f"Кратко суммаризуй следующий диалог (максимум 500 слов). "
        f"Сохрани ключевые решения, факты и контекст:\n\n{conversation_text[:8000]}"
    )

    summary_parts = []
    async for event in claude_manager.send_message(
        user_id=user_id,
        chat_session_id=f"compaction-{session.id}",
        message=summary_prompt,
        model="claude-haiku-4-5",
        allowed_tools=[],
    ):
        if event.type == "text":
            summary_parts.append(event.content)

    summary = "".join(summary_parts)
    if not summary:
        return None

    # Reset claude_session_id to start fresh, but keep the summary as context
    session.claude_session_id = None
    session.context_input_tokens = 0
    session.context_output_tokens = 0

    await db.commit()

    logger.info(f"Context compacted for session {session.id}: {len(old_messages)} messages summarized")
    return summary
