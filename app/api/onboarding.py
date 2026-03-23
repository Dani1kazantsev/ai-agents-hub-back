"""Onboarding API — suggest pipelines based on user description."""

import json
import logging
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.middleware.auth import get_current_user, get_or_create_user
from app.models.base import PipelineTemplate, User, UserPipeline
from app.services.llm_service import llm_service


def _sso_user_id(payload: dict) -> str:
    return str(payload.get("user_id", ""))

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])
logger = logging.getLogger(__name__)


class SuggestRequest(BaseModel):
    description: str


class SuggestedPipeline(BaseModel):
    title: str
    description: str
    human_loop: bool = True
    orchestrator_prompt: str | None = None
    agents: list[str] = []
    steps_description: list[dict] = []


class SuggestResponse(BaseModel):
    existing: list[dict] = []
    suggested: list[SuggestedPipeline] = []
    other: list[dict] = []


class CompleteRequest(BaseModel):
    selected_existing: list[str] = []
    selected_suggested: list[SuggestedPipeline] = []


# Relevance threshold (0-10). Only templates scoring >= this go to "existing"
RELEVANCE_THRESHOLD = 7
# Maximum existing templates to recommend (even if LLM scores them all high)
MAX_EXISTING_RECOMMENDATIONS = 4

SUGGEST_SYSTEM_PROMPT = """Ты — AI-аналитик платформы AI Agent Hub.

Пользователь описал свои задачи. Тебе нужно:
1. Оценить релевантность КАЖДОГО существующего пайплайна (0-10 баллов)
2. Создать 1-3 НОВЫХ пайплайна, заточенных под КОНКРЕТНЫЕ потребности пользователя

## Существующие пайплайны (оцени КАЖДЫЙ):
{existing_templates}

## Доступные агенты (для новых пайплайнов):
- qa-agent: QA — тестирование, баги, чеклисты
- frontend-dev: Frontend — Vue/Nuxt разработка
- backend-dev: Backend — Python/Go разработка
- mobile-dev: Mobile — Flutter разработка
- pm-agent: PM — задачи, эпики, планирование
- designer-agent: Дизайнер — UI/UX, Figma, Pencil
- data-agent: Data — SQL, аналитика, отчёты
- devops-agent: DevOps — CI/CD, деплой, инфра

## Оценка релевантности:
- 9-10: пайплайн ТОЧНО нужен пользователю (прямо упомянул эту задачу)
- 7-8: скорее всего пригодится (косвенно связано с задачами)
- 4-6: может пригодиться, но не приоритет
- 0-3: совсем не про задачи пользователя

## ОБЯЗАТЕЛЬНО создай новые пайплайны!
Даже если некоторые существующие подходят — у пользователя ВСЕГДА есть уникальные потребности, которые не покрыты стандартными шаблонами. Придумай 1-3 пайплайна КОНКРЕТНО под его описание.

## Формат ответа (СТРОГО JSON, без markdown, без текста вокруг):
{{
  "scores": {{
    "slug1": 9,
    "slug2": 3,
    "slug3": 7
  }},
  "suggested": [
    {{
      "title": "Название на русском",
      "description": "Что делает пайплайн (1 строка)",
      "human_loop": true,
      "agents": ["agent-slug1"],
      "steps_description": [
        {{"agent": "agent-slug", "label": "Название шага", "description": "Что делает"}}
      ]
    }}
  ]
}}"""


def _template_to_dict(t: PipelineTemplate) -> dict:
    return {
        "id": str(t.id),
        "slug": t.slug,
        "title": t.title,
        "description": t.description or "",
        "human_loop": t.human_loop,
        "agents": t.agents or [],
        "steps": t.steps_description or [],
    }


@router.post("/suggest", response_model=SuggestResponse)
async def suggest_pipelines(
    data: SuggestRequest,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user),
    user: User = Depends(get_or_create_user),
):
    """Analyze user description and suggest matching/new pipelines."""
    result = await db.execute(
        select(PipelineTemplate).where(PipelineTemplate.is_active == True)
    )
    templates = result.scalars().all()
    template_map = {t.slug: t for t in templates}

    existing_info = "\n".join(
        f"- slug: \"{t.slug}\", title: \"{t.title}\", description: \"{t.description or ''}\", "
        f"agents: {t.agents or []}"
        for t in templates
    )

    system_prompt = SUGGEST_SYSTEM_PROMPT.format(existing_templates=existing_info)
    user_message = f"Мои задачи и потребности:\n{data.description}"

    # Collect full LLM response
    sso_id = _sso_user_id(payload)
    full_response = ""
    async for event in llm_service.stream_chat(
        user_id=sso_id,
        chat_session_id=f"onboarding-suggest-{user.id}",
        message=user_message,
        system_prompt=system_prompt,
        model="claude-sonnet-4-6",
    ):
        if event.type == "text":
            full_response += event.content

    logger.info(f"Onboarding LLM response: {full_response[:1000]}")

    # Parse JSON
    try:
        json_str = full_response.strip()
        # Extract JSON if wrapped in markdown
        if "```" in json_str:
            start = json_str.find("{")
            end = json_str.rfind("}") + 1
            json_str = json_str[start:end]
        elif json_str.startswith("{"):
            pass
        else:
            start = json_str.find("{")
            end = json_str.rfind("}") + 1
            if start >= 0 and end > start:
                json_str = json_str[start:end]
        parsed = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        logger.error(f"Failed to parse LLM response: {full_response[:500]}")
        return SuggestResponse(
            existing=[],
            suggested=[],
            other=[_template_to_dict(t) for t in templates],
        )

    # Process scores — sort by score, apply threshold and cap
    scores: dict = parsed.get("scores", {})
    scored_templates = []
    for slug, score in scores.items():
        t = template_map.get(slug)
        if t:
            scored_templates.append((t, int(score) if isinstance(score, (int, float)) else 0))

    # Sort by score descending
    scored_templates.sort(key=lambda x: x[1], reverse=True)

    # Split into matched (high relevance) and other
    matched_existing = []
    matched_slugs = set()
    for t, score in scored_templates:
        if score >= RELEVANCE_THRESHOLD and len(matched_existing) < MAX_EXISTING_RECOMMENDATIONS:
            matched_existing.append(_template_to_dict(t))
            matched_slugs.add(t.slug)

    # All remaining go to "other"
    other_existing = []
    for t in templates:
        if t.slug not in matched_slugs:
            other_existing.append(_template_to_dict(t))

    # Process new suggested pipelines
    suggested_new = []
    for s in parsed.get("suggested", []):
        if not s.get("title"):
            continue
        suggested_new.append(SuggestedPipeline(
            title=s["title"],
            description=s.get("description", ""),
            human_loop=s.get("human_loop", True),
            agents=s.get("agents", []),
            steps_description=s.get("steps_description", []),
        ))

    return SuggestResponse(
        existing=matched_existing,
        suggested=suggested_new,
        other=other_existing,
    )


@router.post("/complete")
async def complete_onboarding(
    data: CompleteRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """Save selected pipelines and mark onboarding as completed."""
    created_ids = []

    for pipeline in data.selected_suggested:
        slug = pipeline.title.lower().replace(" ", "-").replace(":", "").replace(".", "")
        existing = await db.execute(
            select(PipelineTemplate).where(PipelineTemplate.slug == slug)
        )
        if existing.scalar_one_or_none():
            slug = f"{slug}-{uuid.uuid4().hex[:6]}"

        steps_desc = pipeline.steps_description
        auto_steps = [
            {
                "agent_slug": s.get("agent_slug") or s.get("agent", ""),
                "input_template": s.get("input_template") or s.get("description", ""),
            }
            for s in steps_desc
        ]

        orchestrator_prompt = None
        if pipeline.human_loop and pipeline.agents:
            orchestrator_prompt = (
                f"Ты — Team Lead AI-команды. Ты управляешь пайплайном «{pipeline.title}».\n\n"
                f"## Описание\n{pipeline.description}\n\n"
                f"## Шаги\n" +
                "\n".join(
                    f"### Шаг {i+1}: {s.get('label', '')}\n{s.get('description', '')}"
                    for i, s in enumerate(steps_desc)
                ) +
                "\n\n## Правила\n"
                "- Human-in-the-loop: показывай результат каждого шага, жди подтверждения\n"
                "- Русский для общения, английский для кода/коммитов"
            )

        tpl = PipelineTemplate(
            slug=slug,
            title=pipeline.title,
            description=pipeline.description,
            human_loop=pipeline.human_loop,
            orchestrator_prompt=orchestrator_prompt,
            agents=pipeline.agents,
            steps=auto_steps,
            steps_description=steps_desc,
            is_default=False,
            is_active=True,
            created_by=user.id,
        )
        db.add(tpl)
        await db.flush()
        created_ids.append(str(tpl.id))
        # Add new pipeline to user's workspace
        db.add(UserPipeline(user_id=user.id, template_id=tpl.id))

    # Add selected existing templates to user's workspace
    for template_id in data.selected_existing:
        try:
            tid = uuid.UUID(template_id)
        except ValueError:
            continue
        # Check not already added
        exists = await db.execute(
            select(UserPipeline).where(
                UserPipeline.user_id == user.id,
                UserPipeline.template_id == tid,
            )
        )
        if not exists.scalar_one_or_none():
            db.add(UserPipeline(user_id=user.id, template_id=tid))

    user.onboarding_completed = True
    await db.flush()
    await db.commit()

    return {
        "status": "completed",
        "created_pipeline_ids": created_ids,
        "selected_existing": data.selected_existing,
    }


@router.post("/skip")
async def skip_onboarding(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """Skip onboarding and mark as completed."""
    user.onboarding_completed = True
    await db.flush()
    await db.commit()
    return {"status": "skipped"}
