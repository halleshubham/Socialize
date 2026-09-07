"""user api keys and brand botsab

Revision ID: 9e72ddf60868
Revises: 0d40be379bab
Create Date: 2026-09-07 00:00:03.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9e72ddf60868'
down_revision: Union[str, None] = '0d40be379bab'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_api_keys',
        sa.Column('id', sa.UUID(), primary_key=True),
        sa.Column('user_id', sa.UUID(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('provider', sa.String(length=30), nullable=False),
        sa.Column('encrypted_key', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('user_id', 'provider', name='uq_user_api_keys_user_provider'),
    )
    op.add_column('brand_kit', sa.Column('botsab_instance_id', sa.String(length=100), nullable=True))
    op.add_column('brand_kit', sa.Column('botsab_api_key_encrypted', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('brand_kit', 'botsab_api_key_encrypted')
    op.drop_column('brand_kit', 'botsab_instance_id')
    op.drop_table('user_api_keys')
