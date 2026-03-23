from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class AgentCreate(BaseModel):
    name: str
    description: str | None = None
    model: str = "claude-sonnet-4-6"
    system_prompt: str | None = None
    tools: list[str] = []
    allowed_roles: list[str] = []
    max_tokens_per_session: int = 50000
    icon: str | None = None
    color: str | None = None
    tags: list[str] = []
    memory_enabled: bool = True
    memory_scope: str = "personal"


class AgentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    model: str | None = None
    system_prompt: str | None = None
    tools: list[str] | None = None
    allowed_roles: list[str] | None = None
    max_tokens_per_session: int | None = None
    icon: str | None = None
    color: str | None = None
    tags: list[str] | None = None
    is_active: bool | None = None
    memory_enabled: bool | None = None
    memory_scope: str | None = None


class AgentResponse(BaseModel):
    id: UUID
    name: str
    description: str | None = None
    model: str
    system_prompt: str | None = None
    tools: list[str] = []
    allowed_roles: list[str] = []
    max_tokens_per_session: int
    icon: str | None = None
    color: str | None = None
    tags: list[str] = []
    is_active: bool
    memory_enabled: bool = True
    memory_scope: str = "personal"
    created_by: UUID | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AgentListResponse(BaseModel):
    items: list[AgentResponse]
    total: int
