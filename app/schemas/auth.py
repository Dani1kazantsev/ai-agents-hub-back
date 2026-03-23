from uuid import UUID

from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    email: str
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserInfo(BaseModel):
    id: UUID
    email: str
    username: str
    first_name: str | None = None
    last_name: str | None = None
    role: str = "user"
    groups: list[str] = []
    token_budget: int = 100000
    tokens_used: int = 0
    is_active: bool = True
    onboarding_completed: bool = False

    model_config = {"from_attributes": True}
