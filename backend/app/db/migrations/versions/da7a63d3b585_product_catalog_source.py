"""product catalog source

Revision ID: da7a63d3b585
Revises: 6ac650a2c293
Create Date: 2026-09-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'da7a63d3b585'
down_revision: Union[str, None] = '6ac650a2c293'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('brand_kit', sa.Column('product_catalog_url', sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column('brand_kit', 'product_catalog_url')
