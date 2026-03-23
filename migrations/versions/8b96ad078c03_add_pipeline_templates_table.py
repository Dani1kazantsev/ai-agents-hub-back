"""add pipeline_templates table

Revision ID: 8b96ad078c03
Revises: c419eaa35d76
Create Date: 2026-03-09 16:52:29.635482

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '8b96ad078c03'
down_revision: Union[str, None] = 'c419eaa35d76'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'pipeline_templates',
        sa.Column('id', sa.UUID(), nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('slug', sa.String(length=255), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('human_loop', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('orchestrator_prompt', sa.Text(), nullable=True),
        sa.Column('agents', postgresql.JSONB(), nullable=True, server_default=sa.text("'[]'::jsonb")),
        sa.Column('steps', postgresql.JSONB(), nullable=True, server_default=sa.text("'[]'::jsonb")),
        sa.Column('steps_description', postgresql.JSONB(), nullable=True, server_default=sa.text("'[]'::jsonb")),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_by', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
    )
    op.create_index('ix_pipeline_templates_slug', 'pipeline_templates', ['slug'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_pipeline_templates_slug', table_name='pipeline_templates')
    op.drop_table('pipeline_templates')
