"""backfill brand 1

Revision ID: 6a6bb0e0ca70
Revises: da36ce3f1e10
Create Date: 2026-09-07 00:00:01.000000

Data-only migration: turns the app's single pre-existing implicit BrandKit
row (if any) into "Brand 1", owned by the admin user, and backfills
brand_kit_id onto every row of niche_config/ingested_emails/content_items/
sync_state plus the "gmail" oauth_credentials row.

Precondition: ADMIN_EMAIL (and ADMIN_PASSWORD, only if that user doesn't
already exist yet - e.g. a fresh install where this runs before the app's
own bootstrap_admin_user() has ever booted) must be set in the environment
this migration runs in. On the existing production DB the admin user
already exists, so this is a no-op precondition there.
"""
import uuid
from typing import Sequence, Union

import bcrypt
import sqlalchemy as sa
from alembic import op

from backend.app.config import get_settings

# revision identifiers, used by Alembic.
revision: str = '6a6bb0e0ca70'
down_revision: Union[str, None] = 'da36ce3f1e10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    settings = get_settings()

    if not settings.admin_email:
        raise RuntimeError(
            "ADMIN_EMAIL must be set in the environment before running this migration "
            "(same variable backend/app/main.py's bootstrap_admin_user() reads)."
        )

    admin_row = bind.execute(
        sa.text("SELECT id FROM users WHERE email = :e"), {"e": settings.admin_email}
    ).first()
    if admin_row:
        admin_id = admin_row.id
        bind.execute(sa.text("UPDATE users SET is_admin = true WHERE id = :id"), {"id": admin_id})
    else:
        # Fresh install: bootstrap_admin_user() only runs at app boot, which
        # happens AFTER migrations per main.py's lifespan - so on a brand
        # new DB this user doesn't exist yet. Create it here the same way
        # (bcrypt, matching auth/basic.py's hashing) so login still works
        # once the app actually boots; bootstrap_admin_user()'s own
        # "does a user with this email already exist" check makes this
        # idempotent against that later boot-time call.
        if not settings.admin_password:
            raise RuntimeError(
                "No user with ADMIN_EMAIL exists yet and ADMIN_PASSWORD is unset - "
                "both are needed to create the admin account this migration backfills onto."
            )
        admin_id = uuid.uuid4()
        hashed = bcrypt.hashpw(settings.admin_password.encode(), bcrypt.gensalt()).decode()
        bind.execute(
            sa.text(
                "INSERT INTO users (id, email, hashed_password, auth_provider, is_admin) "
                "VALUES (:id, :e, :h, 'basic', true)"
            ),
            {"id": admin_id, "e": settings.admin_email, "h": hashed},
        )

    brand_row = bind.execute(sa.text("SELECT id, name FROM brand_kit ORDER BY created_at ASC LIMIT 1")).first()
    if not brand_row:
        # Genuinely fresh install with no brand yet - nothing to backfill.
        # The next migration's NOT NULL tightening is safe since every
        # dependent table is empty too.
        return

    brand_id = brand_row.id
    if brand_row.name == "default":
        bind.execute(sa.text("UPDATE brand_kit SET name = 'Brand 1' WHERE id = :b"), {"b": brand_id})
    bind.execute(sa.text("UPDATE brand_kit SET owner_user_id = :u WHERE id = :b"), {"u": admin_id, "b": brand_id})

    bind.execute(
        sa.text(
            "INSERT INTO brand_members (id, brand_kit_id, user_id, role) "
            "VALUES (:id, :b, :u, 'owner') "
            "ON CONFLICT (brand_kit_id, user_id) DO NOTHING"
        ),
        {"id": uuid.uuid4(), "b": brand_id, "u": admin_id},
    )

    for table in ("niche_config", "ingested_emails", "content_items", "sync_state"):
        bind.execute(sa.text(f"UPDATE {table} SET brand_kit_id = :b WHERE brand_kit_id IS NULL"), {"b": brand_id})

    bind.execute(
        sa.text(
            "UPDATE oauth_credentials SET brand_kit_id = :b "
            "WHERE provider = 'gmail' AND brand_kit_id IS NULL"
        ),
        {"b": brand_id},
    )


def downgrade() -> None:
    # Data-only migration - nothing structural to reverse. Leaving the
    # backfilled brand_kit_id values in place on downgrade is harmless
    # (the columns themselves are dropped by the prior migration's
    # downgrade), and blanking them back to NULL here isn't meaningfully
    # "undoing" anything a re-upgrade couldn't just redo.
    pass
