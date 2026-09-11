"""github source and content voice

Revision ID: 344d778779cd
Revises: c24b37ebca01
Create Date: 2026-09-07 14:10:57.172245

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '344d778779cd'
down_revision: Union[str, None] = 'c24b37ebca01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('brand_kit', sa.Column('github_token_encrypted', sa.Text(), nullable=True))
    op.add_column(
        'brand_kit',
        sa.Column('github_repos', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
    )
    op.add_column(
        'brand_kit',
        sa.Column('content_voice', sa.String(length=20), nullable=False, server_default='newsletter'),
    )


def downgrade() -> None:
    op.drop_column('brand_kit', 'content_voice')
    op.drop_column('brand_kit', 'github_repos')
    op.drop_column('brand_kit', 'github_token_encrypted')
