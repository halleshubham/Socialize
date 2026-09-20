"""public signup and superadmin

Revision ID: 69029159530b
Revises: f3a8c1d29b4e
Create Date: 2026-09-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '69029159530b'
down_revision: Union[str, None] = 'f3a8c1d29b4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('is_superadmin', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column('users', sa.Column('full_name', sa.String(length=255), nullable=True))
    op.add_column('users', sa.Column('contact_number', sa.String(length=30), nullable=True))
    op.add_column('users', sa.Column('company_name', sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'company_name')
    op.drop_column('users', 'contact_number')
    op.drop_column('users', 'full_name')
    op.drop_column('users', 'is_superadmin')
