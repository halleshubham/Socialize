"""carousel format support

Revision ID: b79087a1bc9a
Revises: 8a2906014675
Create Date: 2026-09-08 21:54:54.744793

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b79087a1bc9a'
down_revision: Union[str, None] = '8a2906014675'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('content_items', sa.Column('carousel_script', sa.Text(), nullable=True))
    op.add_column(
        'content_items',
        sa.Column('carousel_slides', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
    )
    op.add_column('media_assets', sa.Column('slide_index', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('media_assets', 'slide_index')
    op.drop_column('content_items', 'carousel_slides')
    op.drop_column('content_items', 'carousel_script')
