from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.middleware.auth import get_or_create_user
from app.models.base import IntegrationConfig, User
from app.services.claude_process import (
    _detect_pencil_mcp,
    _get_integration_env,
    _load_mcp_registry,
    refresh_integration_cache,
)

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


def require_admin(user: User):
    groups = user.groups or []
    is_admin = (
        user.role == "admin"
        or "is_superuser" in groups
        or "is_staff" in groups
        or "admins" in groups
    )
    if not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )


class IntegrationUpdate(BaseModel):
    credentials: dict = {}
    is_enabled: bool = True


class IntegrationTest(BaseModel):
    credentials: dict = {}


def _mask_value(value: str, field_type: str) -> str:
    """Mask sensitive values — show only last 4 chars for password fields."""
    if field_type == "password" and value and len(value) > 4:
        return "*" * (len(value) - 4) + value[-4:]
    return value


@router.get("")
async def list_integrations(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    require_admin(user)

    registry = _load_mcp_registry()
    result_db = await db.execute(select(IntegrationConfig))
    configs = {c.service_name: c for c in result_db.scalars().all()}

    integrations = []
    for name, server_def in registry.items():
        db_config = configs.get(name)

        fields = []
        is_connected = True
        current_values = {}

        for env_key, env_spec in server_def.get("env", {}).items():
            setting_name = env_spec["setting"]
            field_label = env_spec.get("label", setting_name)
            field_type = env_spec.get("type", "text")
            required = env_spec.get("required", False)

            # Get value: DB first, then settings fallback
            value = _get_integration_env(name, setting_name)

            if required and not value:
                is_connected = False

            current_values[setting_name] = _mask_value(value, field_type) if value else ""

            fields.append({
                "key": setting_name,
                "label": field_label,
                "type": field_type,
                "required": required,
            })

        integrations.append({
            "name": name,
            "description": server_def.get("description", ""),
            "fields": fields,
            "is_connected": is_connected,
            "is_enabled": db_config.is_enabled if db_config else True,
            "values": current_values,
        })

    # Pencil — special case (external binary)
    pencil_detected = _detect_pencil_mcp() is not None
    integrations.append({
        "name": "pencil",
        "description": "Pencil design tool (auto-detected)",
        "fields": [],
        "is_connected": pencil_detected,
        "is_enabled": pencil_detected,
        "values": {},
    })

    return integrations


@router.put("/{name}")
async def update_integration(
    name: str,
    body: IntegrationUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    require_admin(user)

    result = await db.execute(
        select(IntegrationConfig).where(IntegrationConfig.service_name == name)
    )
    config = result.scalar_one_or_none()

    if config:
        config.credentials = body.credentials
        config.is_enabled = body.is_enabled
        config.updated_by = user.id
    else:
        config = IntegrationConfig(
            service_name=name,
            credentials=body.credentials,
            is_enabled=body.is_enabled,
            updated_by=user.id,
        )
        db.add(config)

    await db.flush()
    await db.refresh(config)

    await refresh_integration_cache()

    return {"ok": True, "service_name": name, "is_enabled": config.is_enabled}


@router.post("/{name}/test")
async def test_integration(
    name: str,
    body: IntegrationTest,
    user: User = Depends(get_or_create_user),
):
    require_admin(user)

    creds = body.credentials

    try:
        if name == "jira":
            import httpx

            base_url = creds.get("JIRA_BASE_URL", "").rstrip("/")
            email = creds.get("JIRA_EMAIL", "")
            token = creds.get("JIRA_API_TOKEN", "")
            if not all([base_url, email, token]):
                return {"success": False, "message": "Missing required fields: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN"}
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{base_url}/rest/api/3/myself",
                    auth=(email, token),
                )
            if resp.status_code == 200:
                data = resp.json()
                return {"success": True, "message": f"Connected as {data.get('displayName', data.get('emailAddress', 'OK'))}"}
            return {"success": False, "message": f"Jira returned HTTP {resp.status_code}: {resp.text[:200]}"}

        elif name == "gitlab":
            import httpx

            url = creds.get("GITLAB_URL", "").rstrip("/")
            token = creds.get("GITLAB_TOKEN", "")
            if not all([url, token]):
                return {"success": False, "message": "Missing required fields: GITLAB_URL, GITLAB_TOKEN"}
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{url}/api/v4/user",
                    headers={"PRIVATE-TOKEN": token},
                )
            if resp.status_code == 200:
                data = resp.json()
                return {"success": True, "message": f"Connected as {data.get('name', data.get('username', 'OK'))}"}
            return {"success": False, "message": f"GitLab returned HTTP {resp.status_code}: {resp.text[:200]}"}

        elif name == "db":
            import asyncpg

            db_url = creds.get("EXTERNAL_DATABASE_URL", "")
            if not db_url:
                return {"success": False, "message": "Missing required field: EXTERNAL_DATABASE_URL"}
            conn = await asyncpg.connect(db_url, timeout=10)
            try:
                result = await conn.fetchval("SELECT 1")
                return {"success": True, "message": f"Database connected, SELECT 1 = {result}"}
            finally:
                await conn.close()

        elif name == "figma":
            import httpx

            token = creds.get("FIGMA_ACCESS_TOKEN", "")
            if not token:
                return {"success": False, "message": "Missing required field: FIGMA_ACCESS_TOKEN"}
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.figma.com/v1/me",
                    headers={"X-Figma-Token": token},
                )
            if resp.status_code == 200:
                data = resp.json()
                return {"success": True, "message": f"Connected as {data.get('handle', data.get('email', 'OK'))}"}
            return {"success": False, "message": f"Figma returned HTTP {resp.status_code}: {resp.text[:200]}"}

        elif name == "docs":
            import httpx

            url = creds.get("GITLAB_URL", "").rstrip("/")
            token = creds.get("GITLAB_TOKEN", "")
            if not all([url, token]):
                return {"success": False, "message": "Missing required fields: GITLAB_URL, GITLAB_TOKEN"}
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{url}/api/v4/user",
                    headers={"PRIVATE-TOKEN": token},
                )
            if resp.status_code == 200:
                data = resp.json()
                return {"success": True, "message": f"Connected as {data.get('name', data.get('username', 'OK'))}"}
            return {"success": False, "message": f"GitLab returned HTTP {resp.status_code}: {resp.text[:200]}"}

        else:
            return {"success": True, "message": "No test available for this integration"}

    except Exception as e:
        return {"success": False, "message": str(e)}
