"""Pipelines API — manage and run agent chains.

Two modes:
- human_loop=false: auto-execute steps sequentially, stream results via WS
- human_loop=true: create a chat session with a Team Lead orchestrator agent
  that delegates to sub-agents and talks to the user between steps
"""

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import async_session_factory, get_db
from app.middleware.auth import get_or_create_user, validate_token_local
from app.models.base import Agent, ChatMessage, ChatSession, PipelineTemplate, User, UserPipeline
from app.services.llm_service import llm_service

router = APIRouter(prefix="/api/pipelines", tags=["pipelines"])


# --- Pipeline Templates ---
# Based on real AI projects: ai-team, ai-product-manager, ai-qa, ai-designer, ai_team_data

PIPELINE_TEMPLATES: list[dict] = [
    {
        "id": "task-development",
        "title": "Разработка задачи",
        "description": "Полный цикл: подготовка → анализ PM → дизайн → разработка → ревью → тестирование → деплой",
        "human_loop": True,
        "orchestrator_prompt": """Ты — Team Lead AI-команды. Ты управляешь пайплайном разработки задачи.

## Твоя роль
Ты — единственный оркестратор. Общаешься с пользователем, делегируешь задачи агентам через MCP-инструменты, передаёшь контекст между шагами.

## Пайплайн разработки задачи

### Шаг 1: Подготовка
Спроси у пользователя номер задачи (LX-XXX) или описание. Используй jira:get_issue чтобы получить данные задачи. Определи проект по labels. Получи архитектурный контекст через docs:get_context.

### Шаг 2: Анализ (для сложных задач)
Если задача сложная (> 5 SP или эпик) — проанализируй её как PM: декомпозируй на подзадачи, определи agent pipeline (какие агенты нужны), подготовь таск-лист. Покажи пользователю и дождись подтверждения.

### Шаг 3: Дизайн (для frontend/mobile задач)
Если задача frontend или mobile — подготовь UI-рекомендации: проверь Figma макеты через figma:get_file, сформулируй требования к UI. Покажи пользователю.

### Шаг 4: Разработка
Сформулируй задачу для разработчика с полным контекстом: описание, таск-лист от PM, UI-рекомендации от дизайнера, архитектурный контекст. Покажи план реализации пользователю.

### Шаг 5: Ревью
Покажи результат пользователю. Пользователь может попросить показать diff, исправить что-то, или одобрить.

### Шаг 6: Тестирование
После одобрения — проведи тестирование: lint, unit-тесты. Для новых задач (не Poppycock) — сгенерируй чеклист для QA.

### Шаг 7: Завершение
Переведи задачу в Jira: Developing → Developed. Создай MR если нужно.

## Правила
- Human-in-the-loop: показывай результат каждого шага, жди подтверждения
- Минимальные изменения — не рефактори то, что не просят
- Русский для общения, английский для кода/коммитов
- Приоритет: Poppycock → AI-ревью → Новые задачи
- Не мержь MR — только создавай. Мердж делает человек.""",
        "agents": ["pm-agent", "designer-agent", "frontend-dev", "backend-dev", "mobile-dev", "qa-agent", "devops-agent"],
        "steps_description": [
            {"agent": "automation", "label": "Подготовка задачи", "description": "Jira, ветка, контекст проекта"},
            {"agent": "pm-agent", "label": "Анализ PM", "description": "Декомпозиция, таск-лист, pipeline агентов"},
            {"agent": "designer-agent", "label": "UI-дизайн", "description": "Figma, дизайн-система, UI-рекомендации"},
            {"agent": "dev", "label": "Разработка", "description": "Реализация задачи (frontend/backend/mobile)"},
            {"agent": "user", "label": "Ревью", "description": "Проверка и одобрение результата"},
            {"agent": "qa-agent", "label": "Тестирование", "description": "Lint, тесты, чеклист для QA"},
            {"agent": "automation", "label": "Завершение", "description": "Коммит, push, MR, статус Jira"},
        ],
    },
    {
        "id": "mr-review",
        "title": "AI-ревью MR",
        "description": "Code review → тестирование → чеклист QA → перевод статуса",
        "human_loop": False,
        "orchestrator_prompt": None,
        "agents": ["frontend-dev", "backend-dev", "qa-agent"],
        "steps": [
            {"agent_slug": "backend-dev", "input_template": "РЕЖИМ РЕВЬЮ.\nЗадача: {input}\nПроанализируй MR: качество кода, безопасность, паттерны проекта. Если есть замечания — опиши конкретно."},
            {"agent_slug": "qa-agent", "input_template": "На основе code review сгенерируй чеклист для QA-тестирования:\n{prev_output}"},
        ],
        "steps_description": [
            {"agent": "dev", "label": "Code Review", "description": "Анализ качества кода, безопасность, паттерны"},
            {"agent": "qa-agent", "label": "QA чеклист", "description": "Генерация чеклиста для тестирования"},
        ],
    },
    {
        "id": "pm-task-creation",
        "title": "PM: Создание задачи",
        "description": "Анализ контекста → формулировка → создание в Jira",
        "human_loop": True,
        "orchestrator_prompt": """Ты — AI Product Manager. Помогаешь PM создавать задачи в Jira.

## Процесс

### Шаг 1: Понять задачу
Спроси пользователя что нужно сделать. Уточни проект, тип задачи, приоритет.

### Шаг 2: Архитектурный контекст
Определи проект по labels. Получи архитектурный контекст через docs:get_context — модули, роуты, API-зависимости.

### Шаг 3: Формулировка
На основе описания и контекста сформулируй:
- Summary (краткий заголовок)
- Type: Issue/Bug/Story/Epic/Task
- Labels (project label + type label)
- Story Points (Fibonacci: 1, 2, 3, 5, 8, 13; если > 8 — рекомендуй декомпозицию)
- Description: Контекст, Требования, Критерии приёмки, Edge Cases

Покажи preview пользователю. Дождись подтверждения.

### Шаг 4: Создание
После подтверждения — создай задачу через jira:create_issue.

## Правила
- Всегда показывай preview перед созданием
- Описания на русском
- ADF формат для Jira
- Story Points: если задача > 8 SP — предложи декомпозицию""",
        "agents": ["pm-agent"],
        "steps_description": [
            {"agent": "user", "label": "Описание задачи", "description": "Что нужно сделать"},
            {"agent": "pm-agent", "label": "Формулировка", "description": "Summary, тип, SP, описание"},
            {"agent": "user", "label": "Подтверждение", "description": "Preview перед созданием"},
            {"agent": "automation", "label": "Создание в Jira", "description": "Запись задачи"},
        ],
    },
    {
        "id": "pm-epic-decomposition",
        "title": "PM: Декомпозиция эпика",
        "description": "Получение эпика → анализ → разбивка на подзадачи → создание в Jira",
        "human_loop": True,
        "orchestrator_prompt": """Ты — AI Product Manager. Помогаешь декомпозировать эпики.

## Процесс

### Шаг 1: Получить эпик
Спроси номер эпика (LX-XXX). Получи данные через jira:get_issue. Определи проект и получи архитектурный контекст через docs:get_context.

### Шаг 2: Декомпозиция
Разбей эпик на подзадачи:
- Каждая подзадача: summary, тип, labels, SP, описание
- Зависимости между подзадачами
- Рекомендации по порядку выполнения
- Agent pipeline для каждой (какие агенты нужны)

Покажи список пользователю. Дождись подтверждения.

### Шаг 3: Создание
Создай подзадачи в Jira через jira:create_issue.

## Правила
- Fibonacci SP: 1, 2, 3, 5, 8, 13
- Если подзадача > 8 SP — разбей ещё
- Показывай preview всех подзадач перед созданием""",
        "agents": ["pm-agent"],
        "steps_description": [
            {"agent": "automation", "label": "Получение эпика", "description": "Данные из Jira + контекст"},
            {"agent": "pm-agent", "label": "Декомпозиция", "description": "Подзадачи, SP, зависимости"},
            {"agent": "user", "label": "Подтверждение", "description": "Ревью подзадач"},
            {"agent": "automation", "label": "Создание в Jira", "description": "Запись подзадач"},
        ],
    },
    {
        "id": "qa-checklist",
        "title": "QA: Чеклист и тестирование",
        "description": "Анализ задачи → чеклист → автотестирование → отчёт",
        "human_loop": True,
        "orchestrator_prompt": """Ты — AI QA Assistant. Помогаешь QA-инженерам с тестированием.

## Процесс

### Шаг 1: Получить задачу
Спроси номер задачи (LX-XXX). Получи данные через jira:get_issue. Определи проект по labels, получи архитектурный контекст через docs:get_context. Если есть ветка — получи diff изменений через gitlab:get_mr_diff.

### Шаг 2: Генерация чеклиста
На основе задачи, контекста и diff сгенерируй **короткий smoke-чеклист** (5-10 пунктов максимум, для мелких задач 3-5):
- Happy path ТОЛЬКО для изменённого функционала
- Негативные / граничные случаи только для изменённой логики (если применимо)
- **НЕ включай:** регрессию, тестирование незатронутого функционала, общие проверки (адаптивность, accessibility, SEO, производительность), проверки соседних модулей

Покажи чеклист пользователю. Дождись подтверждения или правок.

### Шаг 3: Публикация
После подтверждения — добавь чеклист в Jira комментарий через jira:add_comment.

### Шаг 4 (опционально): Автотестирование
Если QA просит — можно проверить чеклист на staging автоматически.

## Правила
- Чеклист на русском, в формате Jira taskList
- Всегда обогащай чеклист архитектурным контекстом
- Показывай preview перед записью в Jira""",
        "agents": ["qa-agent"],
        "steps_description": [
            {"agent": "automation", "label": "Получение задачи", "description": "Jira + контекст + diff"},
            {"agent": "qa-agent", "label": "Генерация чеклиста", "description": "Smoke-чеклист по затронутому функционалу (5-10 пунктов)"},
            {"agent": "user", "label": "Подтверждение", "description": "Ревью чеклиста"},
            {"agent": "automation", "label": "Публикация", "description": "Комментарий в Jira"},
        ],
    },
    {
        "id": "design-from-figma",
        "title": "Дизайн: Figma → Pencil",
        "description": "Чтение Figma макета → извлечение токенов → создание UI в Pencil",
        "human_loop": True,
        "orchestrator_prompt": """Ты — AI Designer Team Lead. Управляешь дизайн-пайплайном.

## Агенты
- figma-reader: читает Figma макеты, извлекает токены
- ui-designer: создаёт UI в Pencil.dev
- design-system: управляет дизайн-системой

## Процесс

### Шаг 1: Получить Figma макет
Спроси URL Figma файла. Прочитай структуру через figma:get_file, получи ноды через figma:get_file_nodes. Извлеки: цвета, типографику, отступы, border-radius, компоненты.

### Шаг 2: UI-рекомендации
На основе извлечённых данных сформулируй рекомендации по реализации. Сравни с текущей дизайн-системой.

### Шаг 3: Создание UI (если нужно)
Если пользователь хочет — создай UI в Pencil на основе макета. Покажи скриншот результата.

### Шаг 4: Обновление дизайн-системы (если нужно)
Если найдены новые токены — предложи обновить дизайн-систему.

## Правила
- Показывай результат каждого этапа
- Перед финализацией — спроси подтверждение
- Accessibility first: контрасты, размеры, фокус""",
        "agents": ["designer-agent"],
        "steps_description": [
            {"agent": "designer-agent", "label": "Чтение Figma", "description": "Структура, токены, компоненты"},
            {"agent": "designer-agent", "label": "UI-рекомендации", "description": "Анализ и рекомендации"},
            {"agent": "designer-agent", "label": "Создание UI", "description": "Реализация в Pencil"},
            {"agent": "designer-agent", "label": "Дизайн-система", "description": "Обновление токенов"},
        ],
    },
    {
        "id": "data-analysis",
        "title": "Data: Ad-hoc анализ",
        "description": "SQL-запрос → анализ данных → отчёт",
        "human_loop": False,
        "orchestrator_prompt": None,
        "agents": ["data-agent"],
        "steps": [
            {"agent_slug": "data-agent", "input_template": "{input}"},
        ],
        "steps_description": [
            {"agent": "data-agent", "label": "SQL-анализ", "description": "Запрос, анализ, отчёт"},
        ],
    },
    {
        "id": "sprint-report",
        "title": "PM: Спринт-отчёт",
        "description": "Сбор данных из Jira → анализ метрик → отчёт",
        "human_loop": False,
        "orchestrator_prompt": None,
        "agents": ["pm-agent", "data-agent"],
        "steps": [
            {"agent_slug": "pm-agent", "input_template": "Собери данные по текущему спринту: открытые задачи, выполненные, в работе, Poppycock. {input}"},
            {"agent_slug": "data-agent", "input_template": "Проанализируй метрики спринта и подготовь отчёт:\n{prev_output}"},
        ],
        "steps_description": [
            {"agent": "pm-agent", "label": "Сбор данных", "description": "Задачи спринта из Jira"},
            {"agent": "data-agent", "label": "Анализ метрик", "description": "Velocity, burndown, отчёт"},
        ],
    },
]


# --- Schemas ---

class PipelineRunRequest(BaseModel):
    input: str = ""


class StepDescSchema(BaseModel):
    agent: str = ""
    agent_slug: str = ""
    label: str = ""
    description: str = ""
    input_template: str = ""


class TemplateCreateUpdate(BaseModel):
    title: str
    description: str = ""
    human_loop: bool = True
    orchestrator_prompt: str | None = None
    agents: list[str] = []
    steps: list[StepDescSchema] = []


# --- In-memory state for auto-runs ---

_active_runs: dict[str, dict] = {}


# --- Helpers ---

def _template_to_dict(t: PipelineTemplate) -> dict:
    """Convert DB model to API response dict."""
    return {
        "id": str(t.id),
        "slug": t.slug,
        "title": t.title,
        "description": t.description or "",
        "human_loop": t.human_loop,
        "agents": t.agents or [],
        "steps": t.steps_description or [],
        "is_default": t.is_default,
    }


async def _seed_defaults(db: AsyncSession):
    """Seed default pipeline templates into DB if table is empty."""
    result = await db.execute(select(PipelineTemplate).limit(1))
    if result.scalar_one_or_none() is not None:
        return  # Already have data

    for t in PIPELINE_TEMPLATES:
        tpl = PipelineTemplate(
            slug=t["id"],
            title=t["title"],
            description=t.get("description", ""),
            human_loop=t.get("human_loop", True),
            orchestrator_prompt=t.get("orchestrator_prompt"),
            agents=t.get("agents", []),
            steps=t.get("steps", []),
            steps_description=t.get("steps_description", []),
            is_default=True,
            is_active=True,
        )
        db.add(tpl)
    await db.flush()


async def _get_template_by_id(db: AsyncSession, template_id: str) -> PipelineTemplate | None:
    """Try to find template by UUID or slug."""
    # Try UUID first
    try:
        uid = uuid.UUID(template_id)
        result = await db.execute(
            select(PipelineTemplate).where(PipelineTemplate.id == uid, PipelineTemplate.is_active == True)
        )
        tpl = result.scalar_one_or_none()
        if tpl:
            return tpl
    except (ValueError, AttributeError):
        pass

    # Fallback to slug
    result = await db.execute(
        select(PipelineTemplate).where(PipelineTemplate.slug == template_id, PipelineTemplate.is_active == True)
    )
    return result.scalar_one_or_none()


# --- Endpoints ---

@router.get("/templates")
async def list_templates(
    all: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """List pipeline templates. Default: user's pipelines. ?all=true: full catalog."""
    await _seed_defaults(db)

    if all:
        # Full catalog
        result = await db.execute(
            select(PipelineTemplate).where(PipelineTemplate.is_active == True).order_by(PipelineTemplate.created_at)
        )
        templates = result.scalars().all()

        # Mark which ones user has
        up_result = await db.execute(
            select(UserPipeline.template_id).where(UserPipeline.user_id == user.id)
        )
        user_template_ids = {row[0] for row in up_result.all()}

        return [
            {**_template_to_dict(t), "in_workspace": t.id in user_template_ids}
            for t in templates
        ]

    # User's pipelines only
    result = await db.execute(
        select(PipelineTemplate)
        .join(UserPipeline, UserPipeline.template_id == PipelineTemplate.id)
        .where(UserPipeline.user_id == user.id, PipelineTemplate.is_active == True)
        .order_by(PipelineTemplate.created_at)
    )
    templates = result.scalars().all()

    # Also include templates created by this user (even if not in user_pipelines)
    created_result = await db.execute(
        select(PipelineTemplate).where(
            PipelineTemplate.created_by == user.id,
            PipelineTemplate.is_active == True,
        )
    )
    created_templates = created_result.scalars().all()
    seen_ids = {t.id for t in templates}
    for t in created_templates:
        if t.id not in seen_ids:
            templates.append(t)

    return [_template_to_dict(t) for t in templates]


@router.post("/templates/{template_id}/add")
async def add_template_to_workspace(
    template_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """Add an existing template to user's workspace."""
    tpl = await _get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    # Check if already added
    existing = await db.execute(
        select(UserPipeline).where(
            UserPipeline.user_id == user.id,
            UserPipeline.template_id == tpl.id,
        )
    )
    if existing.scalar_one_or_none():
        return {"status": "already_added"}

    db.add(UserPipeline(user_id=user.id, template_id=tpl.id))
    await db.flush()
    await db.commit()
    return {"status": "added"}


@router.delete("/templates/{template_id}/remove")
async def remove_template_from_workspace(
    template_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """Remove a template from user's workspace (doesn't delete the template)."""
    tpl = await _get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    result = await db.execute(
        select(UserPipeline).where(
            UserPipeline.user_id == user.id,
            UserPipeline.template_id == tpl.id,
        )
    )
    up = result.scalar_one_or_none()
    if up:
        await db.delete(up)
        await db.flush()
        await db.commit()
    return {"status": "removed"}


@router.get("/templates/{template_id}")
async def get_template(template_id: str, db: AsyncSession = Depends(get_db)):
    tpl = await _get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    return _template_to_dict(tpl)


@router.post("/templates")
async def create_template(
    data: TemplateCreateUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """Create a new pipeline template."""
    slug = data.title.lower().replace(" ", "-").replace(":", "")
    # Ensure unique slug
    existing = await db.execute(select(PipelineTemplate).where(PipelineTemplate.slug == slug))
    if existing.scalar_one_or_none():
        slug = f"{slug}-{uuid.uuid4().hex[:6]}"

    steps_desc = [s.model_dump() for s in data.steps]
    # Build auto-mode steps from steps_desc
    auto_steps = [
        {"agent_slug": s.get("agent_slug") or s.get("agent", ""), "input_template": s.get("input_template") or s.get("description", "")}
        for s in steps_desc
    ]

    tpl = PipelineTemplate(
        slug=slug,
        title=data.title,
        description=data.description,
        human_loop=data.human_loop,
        orchestrator_prompt=data.orchestrator_prompt,
        agents=data.agents or [s.get("agent_slug") or s.get("agent", "") for s in steps_desc],
        steps=auto_steps,
        steps_description=steps_desc,
        is_default=False,
        is_active=True,
        created_by=user.id,
    )
    db.add(tpl)
    await db.flush()
    return _template_to_dict(tpl)


@router.put("/templates/{template_id}")
async def update_template(
    template_id: str,
    data: TemplateCreateUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """Update an existing pipeline template."""
    tpl = await _get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    steps_desc = [s.model_dump() for s in data.steps]
    auto_steps = [
        {"agent_slug": s.get("agent_slug") or s.get("agent", ""), "input_template": s.get("input_template") or s.get("description", "")}
        for s in steps_desc
    ]

    tpl.title = data.title
    tpl.description = data.description
    tpl.human_loop = data.human_loop
    tpl.orchestrator_prompt = data.orchestrator_prompt
    tpl.agents = data.agents or [s.get("agent_slug") or s.get("agent", "") for s in steps_desc]
    tpl.steps = auto_steps
    tpl.steps_description = steps_desc

    await db.flush()
    return _template_to_dict(tpl)


@router.delete("/templates/{template_id}")
async def delete_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """Soft-delete a pipeline template."""
    tpl = await _get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    tpl.is_active = False
    await db.flush()
    return {"status": "deleted"}


@router.post("/run/{template_id}")
async def start_pipeline_run(
    template_id: str,
    data: PipelineRunRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """Start a pipeline run.

    For human_loop=true: creates a chat session with TL orchestrator, returns session_id.
    For human_loop=false: creates an auto-run, returns run_id for WS connection.
    """
    tpl = await _get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    # Build a dict for _get_or_create_tl_agent compatibility
    template_dict = {
        "id": tpl.slug,
        "title": tpl.title,
        "description": tpl.description or "",
        "human_loop": tpl.human_loop,
        "orchestrator_prompt": tpl.orchestrator_prompt,
        "agents": tpl.agents or [],
        "steps": tpl.steps or [],
        "steps_description": tpl.steps_description or [],
    }

    if tpl.human_loop:
        tl_agent = await _get_or_create_tl_agent(db, template_dict)

        session = ChatSession(user_id=user.id, agent_id=tl_agent.id)
        db.add(session)
        await db.flush()

        if data.input:
            msg = ChatMessage(
                session_id=session.id,
                role="user",
                content=data.input,
            )
            db.add(msg)

        await db.commit()

        return {
            "mode": "human_loop",
            "session_id": str(session.id),
            "agent_name": tl_agent.name,
            "template_id": str(tpl.id),
        }

    else:
        run_id = str(uuid.uuid4())
        _active_runs[run_id] = {
            "template_id": str(tpl.id),
            "template_slug": tpl.slug,
            "status": "pending",
            "current_step": 0,
            "total_steps": len(tpl.steps or []),
            "started_at": datetime.utcnow().isoformat(),
            "results": [],
            "user_id": str(user.id),
            "initial_input": data.input,
        }
        return {
            "mode": "auto",
            "run_id": run_id,
            "template_id": str(tpl.id),
        }


async def _get_or_create_tl_agent(db: AsyncSession, template: dict) -> Agent:
    """Get or create a Team Lead agent for a human_loop pipeline."""
    agent_name = f"Team Lead: {template['title']}"

    result = await db.execute(
        select(Agent).where(Agent.name == agent_name, Agent.is_active == True)
    )
    agent = result.scalar_one_or_none()

    if agent:
        # Update system prompt in case template changed
        if agent.system_prompt != template["orchestrator_prompt"]:
            agent.system_prompt = template["orchestrator_prompt"]
        return agent

    # Collect tools from all participating agents
    all_tools = [
        "jira:search_issues", "jira:get_issue", "jira:create_issue",
        "jira:update_issue", "jira:add_comment", "jira:get_transitions",
        "jira:transition_issue",
        "gitlab:read_file", "gitlab:list_files", "gitlab:list_mrs",
        "gitlab:get_mr_diff", "gitlab:add_mr_comment",
        "gitlab:create_branch", "gitlab:commit_files", "gitlab:create_mr",
        "docs:get_context", "docs:get_project",
        "docs:get_team", "docs:search_docs",
        "db:read_query", "db:describe_table", "db:list_tables",
        "figma:get_file", "figma:get_file_nodes",
        "figma:get_file_styles", "figma:get_file_components",
    ]

    agent = Agent(
        name=agent_name,
        description=f"Оркестратор пайплайна: {template['description']}",
        model="claude-sonnet-4-6",
        system_prompt=template["orchestrator_prompt"],
        tools=all_tools,
        is_active=True,
        icon="users",
        color="#5988FF",
        tags=["pipeline", "orchestrator", template["id"]],
    )
    db.add(agent)
    await db.flush()
    return agent


@router.get("/runs")
async def list_runs():
    return [
        {
            "id": run_id,
            "template_id": state["template_id"],
            "status": state["status"],
            "current_step": state["current_step"],
            "total_steps": state["total_steps"],
            "started_at": state["started_at"],
            "results": state.get("results", []),
        }
        for run_id, state in _active_runs.items()
    ]


@router.get("/runs/{run_id}")
async def get_run(run_id: str, db: AsyncSession = Depends(get_db)):
    state = _active_runs.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found")

    tpl = await _get_template_by_id(db, state["template_id"])
    steps_desc = tpl.steps_description if tpl else []

    return {
        "id": run_id,
        "template_id": state["template_id"],
        "title": tpl.title if tpl else "Pipeline",
        "status": state["status"],
        "current_step": state["current_step"],
        "total_steps": state["total_steps"],
        "results": state.get("results", []),
        "steps": steps_desc,
    }


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    """Cancel an active pipeline run."""
    state = _active_runs.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found")
    if state["status"] not in ("pending", "running"):
        raise HTTPException(status_code=400, detail="Run is not active")
    state["status"] = "cancelled"
    return {"status": "cancelled"}


@router.delete("/runs/{run_id}")
async def delete_run(run_id: str):
    """Remove a completed/failed pipeline run from memory."""
    if run_id not in _active_runs:
        raise HTTPException(status_code=404, detail="Run not found")
    del _active_runs[run_id]
    return {"status": "deleted"}


@router.websocket("/ws/{run_id}")
async def pipeline_websocket(websocket: WebSocket, run_id: str):
    """WebSocket for auto-mode pipeline execution (human_loop=false)."""
    await websocket.accept()

    token = websocket.query_params.get("token")
    if not token:
        await websocket.send_json({"type": "error", "content": "Unauthorized"})
        await websocket.close(code=4001)
        return

    payload = await validate_token_local(token)
    if not payload:
        await websocket.send_json({"type": "error", "content": "Unauthorized"})
        await websocket.close(code=4001)
        return

    state = _active_runs.get(run_id)
    if not state:
        await websocket.send_json({"type": "error", "content": "Run not found"})
        await websocket.close()
        return

    client_disconnected = False

    async def _ws_send(data: dict):
        nonlocal client_disconnected
        if client_disconnected:
            return
        try:
            await websocket.send_json(data)
        except Exception:
            client_disconnected = True

    try:
        raw = await websocket.receive_text()
        data = json.loads(raw)
        if data.get("type") != "start":
            await _ws_send({"type": "error", "content": "Expected 'start' message"})
            await websocket.close()
            return

        async with async_session_factory() as tpl_db:
            tpl = await _get_template_by_id(tpl_db, state["template_id"])
        if not tpl or tpl.human_loop:
            await _ws_send({"type": "error", "content": "Template not found or is human_loop"})
            await websocket.close()
            return

        state["status"] = "running"
        prev_output = state["initial_input"]
        steps = tpl.steps or []

        slug_to_name = {
            "qa-agent": "QA Agent", "backend-dev": "Backend Dev",
            "frontend-dev": "Frontend Dev", "pm-agent": "PM Agent",
            "designer-agent": "Designer", "data-agent": "Data Agent",
            "devops-agent": "DevOps Agent", "mobile-dev": "Mobile Dev",
        }

        async with async_session_factory() as db:
            for step_idx, step_config in enumerate(steps):
                # Check for cancellation between steps
                if state["status"] == "cancelled":
                    await _ws_send({"type": "pipeline_cancelled"})
                    return

                state["current_step"] = step_idx + 1
                agent_slug = step_config["agent_slug"]

                agent_name = slug_to_name.get(agent_slug, agent_slug)
                result = await db.execute(
                    select(Agent).where(Agent.name == agent_name, Agent.is_active == True)
                )
                agent = result.scalar_one_or_none()

                if not agent:
                    state["results"].append({
                        "step": step_idx,
                        "agent": agent_slug,
                        "output": f"Agent '{agent_slug}' not found",
                        "status": "error",
                    })
                    await _ws_send({
                        "type": "step_error",
                        "step": step_idx,
                        "agent": agent_slug,
                        "content": f"Agent '{agent_slug}' not found",
                    })
                    continue

                input_template = step_config.get("input_template", "{input}")
                step_input = input_template.replace("{input}", state["initial_input"]).replace("{prev_output}", prev_output)

                await _ws_send({
                    "type": "step_start",
                    "step": step_idx,
                    "agent_name": agent.name,
                    "agent_slug": agent_slug,
                    "total_steps": len(steps),
                })

                ws_user_id = str(payload.get("user_id", ""))
                step_response = ""

                async for event in llm_service.stream_chat(
                    user_id=ws_user_id,
                    chat_session_id=f"pipeline-{run_id}-step-{step_idx}",
                    message=step_input,
                    system_prompt=agent.system_prompt or "You are a helpful assistant.",
                    model=agent.model,
                    allowed_tools=agent.tools or [],
                ):
                    if state["status"] == "cancelled":
                        break
                    if event.type == "text":
                        step_response += event.content
                        await _ws_send({
                            "type": "step_stream",
                            "step": step_idx,
                            "content": event.content,
                        })
                    elif event.type == "tool_use":
                        await _ws_send({
                            "type": "step_tool_use",
                            "step": step_idx,
                            "tool_name": event.tool_name,
                        })
                    elif event.type == "tool_result":
                        await _ws_send({
                            "type": "step_tool_result",
                            "step": step_idx,
                            "tool_name": event.tool_name,
                            "content": event.content[:500],
                        })

                if state["status"] == "cancelled":
                    await _ws_send({"type": "pipeline_cancelled"})
                    return

                state["results"].append({
                    "step": step_idx,
                    "agent": agent.name,
                    "output": step_response[:5000],
                    "status": "done",
                })

                await _ws_send({
                    "type": "step_done",
                    "step": step_idx,
                    "agent_name": agent.name,
                    "output_preview": step_response[:500],
                })

                prev_output = step_response

        state["status"] = "completed"
        await _ws_send({
            "type": "pipeline_done",
            "results": state["results"],
        })

    except WebSocketDisconnect:
        # Pipeline continues running in background — state is preserved in _active_runs
        if state["status"] == "running":
            pass  # Keep running status, frontend will poll via GET /runs/{run_id}
    except Exception as e:
        state["status"] = "error"
        await _ws_send({"type": "error", "content": str(e)})
