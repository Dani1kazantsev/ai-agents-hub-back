from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ChatMessageCreate(BaseModel):
    content: str


class ChatMessageResponse(BaseModel):
    id: UUID
    session_id: UUID
    role: str
    content: str
    tool_calls: list | dict | None = None
    tool_results: list | dict | None = None
    tokens_used: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatSessionCreate(BaseModel):
    agent_id: UUID


class ChatSessionResponse(BaseModel):
    id: UUID
    user_id: UUID
    agent_id: UUID
    agent_name: str | None = None
    status: str
    total_tokens: int
    claude_session_id: str | None = None
    created_at: datetime
    updated_at: datetime
    messages: list[ChatMessageResponse] = []

    model_config = {"from_attributes": True}


class ChatSessionListResponse(BaseModel):
    items: list[ChatSessionResponse]
    total: int


class ContextStats(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    context_limit: int = 200000
    usage_percent: float = 0.0
