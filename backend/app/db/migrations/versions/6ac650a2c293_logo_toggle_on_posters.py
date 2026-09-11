"""logo toggle on posters

Revision ID: 6ac650a2c293
Revises: 344d778779cd
Create Date: 2026-09-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6ac650a2c293'
down_revision: Union[str, None] = '344d778779cd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'brand_kit',
        sa.Column('show_logo_on_posters', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('brand_kit', 'show_logo_on_posters')
