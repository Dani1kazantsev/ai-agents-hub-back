from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from jose import jwt
from passlib.hash import bcrypt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.middleware.auth import get_or_create_user
from app.models.base import User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserInfo

router = APIRouter(prefix="/api/auth", tags=["auth"])

JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 7


def _create_token(user: User) -> str:
    payload = {
        "user_id": str(user.id),
        "email": user.email,
        "username": user.username,
        "role": user.role,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS),
    }
    return jwt.encode(payload, settings.AUTH_JWT_SECRET, algorithm=JWT_ALGORITHM)


@router.post("/register", response_model=TokenResponse)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    # Check email uniqueness
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    # Check username uniqueness
    existing = await db.execute(select(User).where(User.username == body.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken")

    # First user gets admin role
    count = await db.scalar(select(func.count()).select_from(User))
    role = "admin" if count == 0 else "user"

    user = User(
        email=body.email,
        username=body.username,
        password_hash=bcrypt.hash(body.password),
        role=role,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)

    return TokenResponse(access_token=_create_token(user))


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    # Find by username or email
    result = await db.execute(
        select(User).where(
            (User.username == body.username) | (User.email == body.username)
        )
    )
    user = result.scalar_one_or_none()

    if not user or not user.password_hash:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if not bcrypt.verify(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    return TokenResponse(access_token=_create_token(user))


@router.get("/me", response_model=UserInfo)
async def get_me(user: User = Depends(get_or_create_user)):
    return user
