"""Add agent memory, context tracking, and subagent runs

Revision ID: a1b2c3d4e5f6
Revises: 3fd3558a7f8b
Create Date: 2026-03-16 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "3fd3558a7f8b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Agent memory fields
    op.add_column("agents", sa.Column("memory_enabled", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("agents", sa.Column("memory_scope", sa.String(50), nullable=False, server_default="personal"))

    # Context tracking on chat_sessions
    op.add_column("chat_sessions", sa.Column("context_input_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("chat_sessions", sa.Column("context_output_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("chat_sessions", sa.Column("context_limit", sa.Integer(), nullable=False, server_default="200000"))

    # Agent memories table
    op.create_table(
        "agent_memories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=False, index=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True, index=True),
        sa.Column("scope", sa.String(50), nullable=False, server_default="personal"),
        sa.Column("key", sa.String(500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tags", postgresql.JSONB(), nullable=True),
        sa.Column("source", sa.String(100), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Subagent runs table
    op.create_table(
        "subagent_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("parent_session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chat_sessions.id"), nullable=False, index=True),
        sa.Column("child_session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chat_sessions.id"), nullable=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("subagent_runs")
    op.drop_table("agent_memories")
    op.drop_column("chat_sessions", "context_limit")
    op.drop_column("chat_sessions", "context_output_tokens")
    op.drop_column("chat_sessions", "context_input_tokens")
    op.drop_column("agents", "memory_scope")
    op.drop_column("agents", "memory_enabled")
