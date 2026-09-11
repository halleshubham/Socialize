"""agent prompt config

Revision ID: 8799456d99fc
Revises: e0bc1e03996a
Create Date: 2026-09-08 18:01:04.178729

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '8799456d99fc'
down_revision: Union[str, None] = 'e0bc1e03996a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'agent_prompt_config',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('brand_kit_id', sa.UUID(), nullable=False),
        sa.Column('prompt_key', sa.String(length=100), nullable=False),
        sa.Column('prompt_text', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['brand_kit_id'], ['brand_kit.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('brand_kit_id', 'prompt_key', name='uq_agent_prompt_config_brand_key'),
    )


def downgrade() -> None:
    op.drop_table('agent_prompt_config')
