"""postiz brand settings

Revision ID: 027a01ee2a6a
Revises: 63a43ab704ee
Create Date: 2026-09-07 00:00:07.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '027a01ee2a6a'
down_revision: Union[str, None] = '63a43ab704ee'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('brand_kit', sa.Column('postiz_api_key_encrypted', sa.Text(), nullable=True))
    op.add_column(
        'brand_kit',
        sa.Column('postiz_channels', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
    )


def downgrade() -> None:
    op.drop_column('brand_kit', 'postiz_channels')
    op.drop_column('brand_kit', 'postiz_api_key_encrypted')
