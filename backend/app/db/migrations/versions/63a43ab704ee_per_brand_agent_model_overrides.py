"""per brand agent model overrides

Revision ID: 63a43ab704ee
Revises: 4984acc706ea
Create Date: 2026-09-07 00:00:06.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '63a43ab704ee'
down_revision: Union[str, None] = '4984acc706ea'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # agent_task stops being globally unique on its own - a brand can now
    # have its own override row for the same agent_task alongside the
    # global (brand_kit_id NULL) row.
    op.drop_constraint('agent_model_config_agent_task_key', 'agent_model_config', type_='unique')
    op.add_column('agent_model_config', sa.Column('brand_kit_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_agent_model_config_brand_kit_id', 'agent_model_config', 'brand_kit', ['brand_kit_id'], ['id']
    )


def downgrade() -> None:
    op.drop_constraint('fk_agent_model_config_brand_kit_id', 'agent_model_config', type_='foreignkey')
    op.drop_column('agent_model_config', 'brand_kit_id')
    op.create_unique_constraint('agent_model_config_agent_task_key', 'agent_model_config', ['agent_task'])
