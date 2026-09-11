"""brand strip toggles

Revision ID: e0bc1e03996a
Revises: da7a63d3b585
Create Date: 2026-09-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e0bc1e03996a'
down_revision: Union[str, None] = 'da7a63d3b585'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'brand_kit',
        sa.Column('show_brand_name_on_posters', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        'brand_kit',
        sa.Column('show_source_attribution', sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column('brand_kit', 'show_source_attribution')
    op.drop_column('brand_kit', 'show_brand_name_on_posters')
