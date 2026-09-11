"""rss feeds and ingested rss items

Revision ID: 8a2906014675
Revises: 3df9c458707e
Create Date: 2026-09-08 20:14:44.703025

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '8a2906014675'
down_revision: Union[str, None] = '3df9c458707e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ingested_rss_items',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('brand_kit_id', sa.UUID(), nullable=False),
        sa.Column('feed_url', sa.String(length=500), nullable=False),
        sa.Column('entry_id', sa.String(length=500), nullable=False),
        sa.Column('title', sa.String(length=500), nullable=False),
        sa.Column('link', sa.String(length=1000), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['brand_kit_id'], ['brand_kit.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('entry_id', 'brand_kit_id', name='uq_ingested_rss_items_entry_brand'),
    )
    op.create_index(op.f('ix_ingested_rss_items_brand_kit_id'), 'ingested_rss_items', ['brand_kit_id'], unique=False)
    op.add_column(
        'brand_kit',
        sa.Column('rss_feeds', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
    )


def downgrade() -> None:
    op.drop_column('brand_kit', 'rss_feeds')
    op.drop_index(op.f('ix_ingested_rss_items_brand_kit_id'), table_name='ingested_rss_items')
    op.drop_table('ingested_rss_items')
