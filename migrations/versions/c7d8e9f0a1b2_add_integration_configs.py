"""add integration_configs table

Revision ID: c7d8e9f0a1b2
Revises: b5f7a9c1d2e3
Create Date: 2026-03-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision: str = 'c7d8e9f0a1b2'
down_revision: Union[str, None] = 'b5f7a9c1d2e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'integration_configs',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('service_name', sa.String(100), unique=True, nullable=False),
        sa.Column('credentials', JSONB, server_default='{}'),
        sa.Column('is_enabled', sa.Boolean, server_default=sa.text('true')),
        sa.Column('updated_by', UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('integration_configs')
