import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.middleware.auth import get_or_create_user
from app.models.base import Agent, ChatMessage, ChatSession, User
from app.services.claude_process import CLAUDE_CONFIGS_BASE

router = APIRouter(prefix="/api/admin", tags=["admin"])


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


@router.get("/stats")
async def get_stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    require_admin(user)

    total_sessions = (await db.execute(select(func.count(ChatSession.id)))).scalar_one()
    total_tokens = (await db.execute(select(func.coalesce(func.sum(ChatSession.total_tokens), 0)))).scalar_one()
    active_agents = (await db.execute(select(func.count(Agent.id)).where(Agent.is_active == True))).scalar_one()
    total_agents = (await db.execute(select(func.count(Agent.id)))).scalar_one()
    active_users = (await db.execute(select(func.count(User.id)).where(User.is_active == True))).scalar_one()

    return {
        "total_sessions": total_sessions,
        "total_tokens": total_tokens,
        "active_agents": active_agents,
        "total_agents": total_agents,
        "active_users": active_users,
        "token_budget": 5_000_000,
    }


@router.get("/agents")
async def get_agents_with_stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    require_admin(user)

    query = (
        select(
            Agent,
            func.count(ChatSession.id).label("session_count"),
            func.coalesce(func.sum(ChatSession.total_tokens), 0).label("total_tokens"),
        )
        .outerjoin(ChatSession, ChatSession.agent_id == Agent.id)
        .group_by(Agent.id)
        .order_by(Agent.created_at.desc())
    )

    result = await db.execute(query)
    rows = result.all()

    return [
        {
            "id": str(agent.id),
            "name": agent.name,
            "model": agent.model,
            "icon": agent.icon,
            "color": agent.color,
            "is_active": agent.is_active,
            "session_count": session_count,
            "total_tokens": total_tokens,
            "created_at": agent.created_at.isoformat(),
        }
        for agent, session_count, total_tokens in rows
    ]


@router.get("/users")
async def get_users_with_stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    require_admin(user)

    query = (
        select(
            User,
            func.count(ChatSession.id).label("session_count"),
            func.coalesce(func.sum(ChatSession.total_tokens), 0).label("total_tokens_used_sessions"),
        )
        .outerjoin(ChatSession, ChatSession.user_id == User.id)
        .group_by(User.id)
        .order_by(User.created_at.desc())
    )

    result = await db.execute(query)
    rows = result.all()

    return [
        {
            "id": str(u.id),
            "email": u.email,
            "username": u.username,
            "role": u.role,
            "token_budget": u.token_budget,
            "tokens_used": u.tokens_used,
            "session_count": session_count,
            "total_tokens_used_sessions": total_tokens_used_sessions,
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat(),
        }
        for u, session_count, total_tokens_used_sessions in rows
    ]


@router.get("/usage")
async def get_usage_stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """Per-user usage stats with daily/weekly/monthly breakdowns."""
    require_admin(user)

    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    # Base: join messages → sessions → users to get per-user token sums
    def _usage_query(since: datetime):
        return (
            select(
                User.id,
                func.coalesce(func.sum(ChatMessage.tokens_used), 0).label("tokens"),
                func.count(ChatMessage.id.distinct()).label("messages"),
            )
            .select_from(User)
            .outerjoin(ChatSession, ChatSession.user_id == User.id)
            .outerjoin(
                ChatMessage,
                and_(
                    ChatMessage.session_id == ChatSession.id,
                    ChatMessage.created_at >= since,
                ),
            )
            .group_by(User.id)
        )

    daily_result = await db.execute(_usage_query(day_ago))
    daily_map = {row[0]: {"tokens": row[1], "messages": row[2]} for row in daily_result.all()}

    weekly_result = await db.execute(_usage_query(week_ago))
    weekly_map = {row[0]: {"tokens": row[1], "messages": row[2]} for row in weekly_result.all()}

    monthly_result = await db.execute(_usage_query(month_ago))
    monthly_map = {row[0]: {"tokens": row[1], "messages": row[2]} for row in monthly_result.all()}

    # Get all users with basic info
    users_result = await db.execute(
        select(User).where(User.is_active == True).order_by(User.created_at)
    )
    users = users_result.scalars().all()

    return [
        {
            "id": str(u.id),
            "email": u.email,
            "username": u.username,
            "token_budget": u.token_budget,
            "tokens_used_total": u.tokens_used,
            "daily": daily_map.get(u.id, {"tokens": 0, "messages": 0}),
            "weekly": weekly_map.get(u.id, {"tokens": 0, "messages": 0}),
            "monthly": monthly_map.get(u.id, {"tokens": 0, "messages": 0}),
        }
        for u in users
    ]


def _get_claude_config_dir(user_id: str) -> Path | None:
    """Find the Claude config directory for a user."""
    per_user = CLAUDE_CONFIGS_BASE / user_id
    if per_user.exists():
        return per_user
    main_dir = Path.home() / ".claude"
    if main_dir.exists():
        return main_dir
    return None


def _read_claude_stats(user_id: str) -> dict | None:
    """Read stats-cache.json from user's CLAUDE_CONFIG_DIR."""
    config_dir = _get_claude_config_dir(user_id)
    if not config_dir:
        return None
    stats_path = config_dir / "stats-cache.json"
    if stats_path.exists():
        try:
            return json.loads(stats_path.read_text())
        except Exception:
            pass
    return None


def _compute_live_stats(config_dir: Path, date_str: str) -> dict:
    """Parse session .jsonl files to compute live stats for a given date.

    Returns dict with keys: messages, sessions, tokens, models.
    """
    projects_dir = config_dir / "projects"
    if not projects_dir.exists():
        return {"messages": 0, "sessions": 0, "tokens": 0, "models": {}}

    total_tokens = 0
    total_messages = 0
    sessions: set[str] = set()
    models: dict[str, dict[str, int]] = {}

    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()

    for proj in projects_dir.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            # Skip files not modified on or after target date
            mtime = datetime.fromtimestamp(f.stat().st_mtime).date()
            if mtime < target_date:
                continue

            session_counted = False
            try:
                with open(f) as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except Exception:
                            continue

                        if entry.get("type") != "assistant":
                            continue
                        ts = entry.get("timestamp", "")
                        if not ts.startswith(date_str):
                            continue

                        msg = entry.get("message", {})
                        usage = msg.get("usage", {})
                        model = msg.get("model", "unknown")

                        inp = usage.get("input_tokens", 0)
                        out = usage.get("output_tokens", 0)
                        cache_read = usage.get("cache_read_input_tokens", 0)
                        cache_create = usage.get("cache_creation_input_tokens", 0)

                        total_tokens += inp + out
                        total_messages += 1

                        short = model.replace("claude-", "").split("-2025")[0]
                        if short not in models:
                            models[short] = {
                                "input_tokens": 0,
                                "output_tokens": 0,
                                "cache_read": 0,
                                "cache_create": 0,
                                "total": 0,
                            }
                        models[short]["input_tokens"] += inp
                        models[short]["output_tokens"] += out
                        models[short]["cache_read"] += cache_read
                        models[short]["cache_create"] += cache_create
                        models[short]["total"] += inp + out

                        if not session_counted:
                            sessions.add(entry.get("sessionId", f.stem))
                            session_counted = True
            except Exception:
                continue

    return {
        "messages": total_messages,
        "sessions": len(sessions),
        "tokens": total_tokens,
        "models": models,
    }


def _aggregate_claude_stats(stats: dict, config_dir: Path | None = None) -> dict:
    """Aggregate stats-cache.json into summary.

    If stats-cache.json is stale (lastComputedDate < today), computes
    live stats from session .jsonl files for today.
    """
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    daily_activity = stats.get("dailyActivity", [])
    daily_model_tokens = stats.get("dailyModelTokens", [])
    model_usage = stats.get("modelUsage", {})
    last_computed = stats.get("lastComputedDate", "")

    # Check if stats-cache.json is stale for today
    need_live = last_computed < today and config_dir is not None

    # Today's activity — prefer live data if cache is stale
    if need_live:
        live = _compute_live_stats(config_dir, today)
        today_messages = live["messages"]
        today_sessions = live["sessions"]
        today_tokens = live["tokens"]
        today_models = live["models"]
    else:
        today_entry = next((d for d in daily_activity if d["date"] == today), None)
        today_messages = today_entry["messageCount"] if today_entry else 0
        today_sessions = today_entry["sessionCount"] if today_entry else 0
        today_tokens_entry = next((d for d in daily_model_tokens if d["date"] == today), None)
        today_tokens = sum((today_tokens_entry.get("tokensByModel", {}) or {}).values()) if today_tokens_entry else 0
        today_models = None

    # Weekly totals (from cache)
    week_messages = sum(d["messageCount"] for d in daily_activity if d["date"] >= week_ago)
    week_sessions = sum(d["sessionCount"] for d in daily_activity if d["date"] >= week_ago)
    week_tokens = 0
    for d in daily_model_tokens:
        if d["date"] >= week_ago:
            week_tokens += sum((d.get("tokensByModel", {}) or {}).values())

    # Add live today data to weekly totals if cache is stale
    if need_live:
        week_messages += today_messages
        week_sessions += today_sessions
        week_tokens += today_tokens

    # All-time model breakdown
    model_breakdown = {}
    for model_id, usage in model_usage.items():
        short_name = model_id.replace("claude-", "").split("-2025")[0]
        total_out = usage.get("outputTokens", 0)
        total_in = usage.get("inputTokens", 0)
        cache_read = usage.get("cacheReadInputTokens", 0)
        cache_create = usage.get("cacheCreationInputTokens", 0)
        model_breakdown[short_name] = {
            "input_tokens": total_in,
            "output_tokens": total_out,
            "cache_read": cache_read,
            "cache_create": cache_create,
            "total": total_in + total_out,
        }

    # Merge today's live model data into all-time breakdown
    if today_models:
        for short_name, m in today_models.items():
            if short_name in model_breakdown:
                for k in ("input_tokens", "output_tokens", "cache_read", "cache_create", "total"):
                    model_breakdown[short_name][k] += m[k]
            else:
                model_breakdown[short_name] = m

    # Build daily activity with tokens
    tokens_map = {}
    for d in daily_model_tokens:
        tokens_map[d["date"]] = sum((d.get("tokensByModel", {}) or {}).values())
    daily_with_tokens = [
        {"date": d["date"], "messages": d["messageCount"], "tokens": tokens_map.get(d["date"], 0)}
        for d in daily_activity[-14:]
    ]
    # Append today's live data if stale
    if need_live and today_messages > 0:
        daily_with_tokens.append({
            "date": today,
            "messages": today_messages,
            "tokens": today_tokens,
        })

    return {
        "total_sessions": stats.get("totalSessions", 0) + (today_sessions if need_live else 0),
        "total_messages": stats.get("totalMessages", 0) + (today_messages if need_live else 0),
        "first_session": stats.get("firstSessionDate"),
        "last_computed": last_computed,
        "live_today": need_live,
        "today": {
            "messages": today_messages,
            "sessions": today_sessions,
            "tokens": today_tokens,
        },
        "week": {
            "messages": week_messages,
            "sessions": week_sessions,
            "tokens": week_tokens,
        },
        "models": model_breakdown,
        "daily_activity": daily_with_tokens,
    }


@router.get("/claude-usage")
async def get_claude_usage(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_or_create_user),
):
    """Get real Claude CLI usage stats per user from stats-cache.json."""
    require_admin(user)

    users_result = await db.execute(
        select(User).where(User.is_active == True).order_by(User.created_at)
    )
    users = users_result.scalars().all()

    result = []
    for u in users:
        # Try SSO user_id (used as config dir name)
        stats = _read_claude_stats(str(u.id))
        config_dir = _get_claude_config_dir(str(u.id))
        if not stats:
            # Try with sso_user_id if stored differently
            stats = _read_claude_stats(u.username)
            config_dir = _get_claude_config_dir(u.username)

        if stats:
            summary = _aggregate_claude_stats(stats, config_dir)
            summary["user_id"] = str(u.id)
            summary["username"] = u.username
            summary["email"] = u.email
            summary["claude_authenticated"] = u.claude_authenticated
            result.append(summary)
        else:
            result.append({
                "user_id": str(u.id),
                "username": u.username,
                "email": u.email,
                "claude_authenticated": u.claude_authenticated,
                "total_sessions": 0,
                "total_messages": 0,
                "first_session": None,
                "today": {"messages": 0, "sessions": 0, "tokens": 0},
                "week": {"messages": 0, "sessions": 0, "tokens": 0},
                "models": {},
                "daily_activity": [],
            })

    return result
