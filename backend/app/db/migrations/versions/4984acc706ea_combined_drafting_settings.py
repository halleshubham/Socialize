"""combined drafting settings

Revision ID: 4984acc706ea
Revises: d40184bdb8d0
Create Date: 2026-09-07 00:00:05.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4984acc706ea'
down_revision: Union[str, None] = 'd40184bdb8d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('brand_kit', sa.Column('combined_drafting', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column(
        'brand_kit', sa.Column('combined_drafting_format', sa.String(length=20), nullable=False, server_default='poster')
    )


def downgrade() -> None:
    op.drop_column('brand_kit', 'combined_drafting_format')
    op.drop_column('brand_kit', 'combined_drafting')
