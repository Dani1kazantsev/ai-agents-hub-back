from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class MemoryEntry(BaseModel):
    id: UUID
    agent_id: UUID
    user_id: UUID | None = None
    scope: str
    key: str
    content: str
    tags: list[str] = []
    source: str | None = None
    token_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MemoryUpdate(BaseModel):
    content: str
    tags: list[str] = []


class MemoryStats(BaseModel):
    total_entries: int
    total_tokens: int
    keys: list[str]
