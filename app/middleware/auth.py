import uuid

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.models.base import User

security = HTTPBearer()


async def validate_token_local(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, settings.AUTH_JWT_SECRET, algorithms=["HS256"])
        return payload
    except JWTError:
        return None


async def fetch_user_info(token: str) -> dict | None:
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{settings.AUTH_PROVIDER_URL}/info",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
            if response.status_code == 200:
                return response.json()
        except httpx.RequestError:
            pass
    return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    token = credentials.credentials

    local_payload = await validate_token_local(token)
    if local_payload is None:
        user_info = await fetch_user_info(token)
        if not user_info:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
            )
        return user_info

    user_info = await fetch_user_info(token)
    if user_info:
        return user_info

    # Auth provider unavailable — use all fields from JWT payload directly
    return local_payload


async def get_or_create_user(
    payload: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    sso_user_id = payload.get("user_id")
    email = payload.get("email", "")
    username = payload.get("username", email.split("@")[0] if email else f"user_{sso_user_id}")
    fullname = payload.get("fullname", "")
    first_name = payload.get("first_name") or fullname.split(" ", 1)[-1] if fullname else None
    last_name = payload.get("last_name") or fullname.split(" ", 1)[0] if fullname else None

    stmt = select(User)
    if email:
        stmt = stmt.where(User.email == email)
    else:
        stmt = stmt.where(User.username == username)

    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            email=email or f"{username}@example.com",
            username=username,
            first_name=first_name,
            last_name=last_name,
            role=payload.get("role", "user"),
            groups=payload.get("groups", []),
        )
        db.add(user)
        await db.flush()
        await db.refresh(user)
    else:
        if first_name:
            user.first_name = first_name
        if last_name:
            user.last_name = last_name
        if payload.get("groups"):
            user.groups = payload["groups"]
        await db.flush()

    return user
