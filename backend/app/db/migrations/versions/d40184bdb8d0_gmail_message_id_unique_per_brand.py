"""gmail message id unique per brand

Revision ID: d40184bdb8d0
Revises: 9e72ddf60868
Create Date: 2026-09-07 00:00:04.000000

Two brands can connect the same physical Gmail account - gmail_message_id
must be unique per brand, not globally, or the second brand's fetch sees
the first brand's already-ingested rows and silently skips everything.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd40184bdb8d0'
down_revision: Union[str, None] = '9e72ddf60868'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('ingested_emails_gmail_message_id_key', 'ingested_emails', type_='unique')
    op.create_unique_constraint(
        'uq_ingested_emails_message_brand', 'ingested_emails', ['gmail_message_id', 'brand_kit_id']
    )


def downgrade() -> None:
    op.drop_constraint('uq_ingested_emails_message_brand', 'ingested_emails', type_='unique')
    op.create_unique_constraint('ingested_emails_gmail_message_id_key', 'ingested_emails', ['gmail_message_id'])
