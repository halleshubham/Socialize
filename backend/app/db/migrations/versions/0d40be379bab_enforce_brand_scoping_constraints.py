"""enforce brand scoping constraints

Revision ID: 0d40be379bab
Revises: 6a6bb0e0ca70
Create Date: 2026-09-07 00:00:02.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0d40be379bab'
down_revision: Union[str, None] = '6a6bb0e0ca70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('brand_kit', 'owner_user_id', nullable=False)
    op.alter_column('niche_config', 'brand_kit_id', nullable=False)
    op.alter_column('ingested_emails', 'brand_kit_id', nullable=False)
    op.alter_column('content_items', 'brand_kit_id', nullable=False)
    op.alter_column('sync_state', 'brand_kit_id', nullable=False)

    op.create_index('ix_ingested_emails_brand_kit_id', 'ingested_emails', ['brand_kit_id'])
    op.create_index('ix_content_items_brand_kit_id', 'content_items', ['brand_kit_id'])
    op.create_index('ix_brand_members_user_id', 'brand_members', ['user_id'])

    op.create_unique_constraint(
        'uq_oauth_credentials_provider_brand', 'oauth_credentials', ['provider', 'brand_kit_id']
    )

    # sync_state's PK grows to (key, brand_kit_id) - every setting it holds
    # is per-brand, so brand_kit_id is never NULL (Postgres disallows
    # nullable PK columns anyway). Callers switch to
    # db.get(SyncState, (key, brand_kit_id)).
    op.drop_constraint('sync_state_pkey', 'sync_state', type_='primary')
    op.create_primary_key('sync_state_pkey', 'sync_state', ['key', 'brand_kit_id'])


def downgrade() -> None:
    op.drop_constraint('sync_state_pkey', 'sync_state', type_='primary')
    op.create_primary_key('sync_state_pkey', 'sync_state', ['key'])

    op.drop_constraint('uq_oauth_credentials_provider_brand', 'oauth_credentials', type_='unique')

    op.drop_index('ix_brand_members_user_id', table_name='brand_members')
    op.drop_index('ix_content_items_brand_kit_id', table_name='content_items')
    op.drop_index('ix_ingested_emails_brand_kit_id', table_name='ingested_emails')

    op.alter_column('sync_state', 'brand_kit_id', nullable=True)
    op.alter_column('content_items', 'brand_kit_id', nullable=True)
    op.alter_column('ingested_emails', 'brand_kit_id', nullable=True)
    op.alter_column('niche_config', 'brand_kit_id', nullable=True)
    op.alter_column('brand_kit', 'owner_user_id', nullable=True)
