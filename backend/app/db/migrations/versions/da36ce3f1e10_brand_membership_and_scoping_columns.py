"""brand membership and scoping columns

Revision ID: da36ce3f1e10
Revises: 88d80bbc19fe
Create Date: 2026-09-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'da36ce3f1e10'
down_revision: Union[str, None] = '88d80bbc19fe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Pure schema: every new brand_kit_id FK is added nullable here - the
    # next migration backfills them onto existing data, and the one after
    # that tightens to NOT NULL. Splitting it this way means a failure
    # partway (e.g. ADMIN_EMAIL unset for the backfill) leaves the DB in a
    # clean, still-nullable, still-rollback-able state.

    op.add_column('users', sa.Column('is_admin', sa.Boolean(), nullable=False, server_default='false'))

    op.create_table(
        'brand_members',
        sa.Column('id', sa.UUID(), primary_key=True),
        sa.Column('brand_kit_id', sa.UUID(), sa.ForeignKey('brand_kit.id'), nullable=False),
        sa.Column('user_id', sa.UUID(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False, server_default='member'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('brand_kit_id', 'user_id', name='uq_brand_members_brand_user'),
    )

    op.add_column('brand_kit', sa.Column('owner_user_id', sa.UUID(), nullable=True))
    op.create_foreign_key('fk_brand_kit_owner_user_id', 'brand_kit', 'users', ['owner_user_id'], ['id'])

    op.add_column('ingested_emails', sa.Column('brand_kit_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_ingested_emails_brand_kit_id', 'ingested_emails', 'brand_kit', ['brand_kit_id'], ['id']
    )

    op.add_column('content_items', sa.Column('brand_kit_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_content_items_brand_kit_id', 'content_items', 'brand_kit', ['brand_kit_id'], ['id']
    )

    op.add_column('oauth_credentials', sa.Column('brand_kit_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_oauth_credentials_brand_kit_id', 'oauth_credentials', 'brand_kit', ['brand_kit_id'], ['id']
    )
    op.drop_constraint('oauth_credentials_provider_key', 'oauth_credentials', type_='unique')

    op.add_column('sync_state', sa.Column('brand_kit_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_sync_state_brand_kit_id', 'sync_state', 'brand_kit', ['brand_kit_id'], ['id']
    )


def downgrade() -> None:
    op.drop_constraint('fk_sync_state_brand_kit_id', 'sync_state', type_='foreignkey')
    op.drop_column('sync_state', 'brand_kit_id')

    op.create_unique_constraint('oauth_credentials_provider_key', 'oauth_credentials', ['provider'])
    op.drop_constraint('fk_oauth_credentials_brand_kit_id', 'oauth_credentials', type_='foreignkey')
    op.drop_column('oauth_credentials', 'brand_kit_id')

    op.drop_constraint('fk_content_items_brand_kit_id', 'content_items', type_='foreignkey')
    op.drop_column('content_items', 'brand_kit_id')

    op.drop_constraint('fk_ingested_emails_brand_kit_id', 'ingested_emails', type_='foreignkey')
    op.drop_column('ingested_emails', 'brand_kit_id')

    op.drop_constraint('fk_brand_kit_owner_user_id', 'brand_kit', type_='foreignkey')
    op.drop_column('brand_kit', 'owner_user_id')

    op.drop_table('brand_members')

    op.drop_column('users', 'is_admin')
