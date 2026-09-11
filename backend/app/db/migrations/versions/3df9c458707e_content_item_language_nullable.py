"""content item language nullable

Revision ID: 3df9c458707e
Revises: 8799456d99fc
Create Date: 2026-09-08 19:04:19.286684

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '3df9c458707e'
down_revision: Union[str, None] = '8799456d99fc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('content_items', 'language', existing_type=sa.VARCHAR(length=20), nullable=True)


def downgrade() -> None:
    op.alter_column('content_items', 'language', existing_type=sa.VARCHAR(length=20), nullable=False)
