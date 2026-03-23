from fastapi import APIRouter, Depends

from app.middleware.auth import get_or_create_user
from app.models.base import User
from app.schemas.auth import UserInfo

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/me", response_model=UserInfo)
async def get_me(user: User = Depends(get_or_create_user)):
    return user
