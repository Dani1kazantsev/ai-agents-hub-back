from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import admin, agents, auth, chat, claude_auth, integrations, memory, onboarding, pipelines, subagents
from app.config import settings
from app.db import engine
from app.models.base import Base
from app.services.claude_process import claude_manager


async def _auto_seed():
    """Seed agents if DB is empty (first run)."""
    from sqlalchemy import func, select
    from app.db import async_session_factory
    from app.models.base import Agent

    async with async_session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(Agent))
        if count == 0:
            from app.seed import seed
            await seed()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await _auto_seed()

    from app.services.claude_process import refresh_integration_cache
    await refresh_integration_cache()

    app.state.redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)

    yield

    # Cleanup Claude CLI processes
    await claude_manager.cleanup()
    await app.state.redis.close()
    await engine.dispose()


app = FastAPI(
    title="AI Agent Hub",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(agents.router)
app.include_router(chat.router)
app.include_router(claude_auth.router)
app.include_router(admin.router)
app.include_router(pipelines.router)
app.include_router(onboarding.router)
app.include_router(memory.router)
app.include_router(subagents.router)
app.include_router(integrations.router)


@app.get("/health")
async def healthcheck():
    return {"status": "ok"}
