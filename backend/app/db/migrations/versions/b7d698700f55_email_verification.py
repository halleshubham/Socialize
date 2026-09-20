"""email verification

Revision ID: b7d698700f55
Revises: 69029159530b
Create Date: 2026-09-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d698700f55'
down_revision: Union[str, None] = '69029159530b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('email_verified', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column('users', sa.Column('email_verification_token', sa.String(length=64), nullable=True))
    op.add_column('users', sa.Column('email_verification_sent_at', sa.DateTime(timezone=True), nullable=True))
    op.create_unique_constraint(
        'uq_users_email_verification_token', 'users', ['email_verification_token']
    )


def downgrade() -> None:
    op.drop_constraint('uq_users_email_verification_token', 'users', type_='unique')
    op.drop_column('users', 'email_verification_sent_at')
    op.drop_column('users', 'email_verification_token')
    op.drop_column('users', 'email_verified')
