"""postiz_posts table

Revision ID: c24b37ebca01
Revises: 027a01ee2a6a
Create Date: 2026-09-07 12:33:12.066339

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c24b37ebca01'
down_revision: Union[str, None] = '027a01ee2a6a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('postiz_posts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('content_item_id', sa.UUID(), nullable=False),
    sa.Column('postiz_post_id', sa.String(length=200), nullable=False),
    sa.Column('channel_id', sa.String(length=200), nullable=False),
    sa.Column('channel_name', sa.String(length=200), nullable=False),
    sa.Column('channel_identifier', sa.String(length=50), nullable=False),
    sa.Column('scheduled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['content_item_id'], ['content_items.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_postiz_posts_content_item_id'), 'postiz_posts', ['content_item_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_postiz_posts_content_item_id'), table_name='postiz_posts')
    op.drop_table('postiz_posts')
