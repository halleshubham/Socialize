"""Brand and user (account) deletion - previously absent from the app
entirely (confirmed via a full grep of backend/app/api/*.py for delete/
remove routes before this was added - only BrandMember removal and RSS
feed removal existed). Required for GDPR-style "right to erasure" once
there are unrelated public tenants, but a real gap regardless.

No `relationship()` exists anywhere in this schema (every table uses a
plain FK column instead - see worker/cleanup.py's own docstring on the
same point), so deletion order has to be spelled out explicitly: children
before parents, in dependency order, via bulk `.delete()` calls (each
executes immediately, not deferred to a later flush) rather than queuing
`db.delete()` on parent and child in the same flush, which hits a real FK
violation from SQLAlchemy having no dependency graph to order by.
"""

import logging
import uuid

from sqlalchemy.orm import Session

from backend.app.db.models import (
    AgentModelConfig,
    AgentPromptConfig,
    BrandKit,
    BrandMember,
    ContentItem,
    IngestedEmail,
    IngestedRssItem,
    NicheConfig,
    OAuthCredential,
    SyncState,
    User,
    UserApiKey,
)
from backend.app.storage.local_disk import get_storage_backend
from backend.app.worker.cleanup import _delete_content_item

logger = logging.getLogger(__name__)


def delete_brand(db: Session, brand_kit_id: uuid.UUID) -> None:
    """Permanently deletes a BrandKit and everything scoped to it. Content
    items go first (reusing worker/cleanup.py's already-live-tested
    per-item ordering - MediaAsset + its stored file, LlmCallLog,
    ContentItemVersion, PostizPost, then the item itself), since
    ContentItem.source_email_id references ingested_emails - those can't be
    deleted while content items still point at them. Every other
    brand_kit_id-FK table follows, then the brand's own logo file and row
    last."""
    storage = get_storage_backend()

    items = db.query(ContentItem).filter(ContentItem.brand_kit_id == brand_kit_id).all()
    for item in items:
        _delete_content_item(db, item)
    # _delete_content_item's final step is an ORM-tracked db.delete(item),
    # not a bulk .delete() - deferred to whenever the session next flushes,
    # same as this function's own db.delete(brand) below. With no
    # relationship() anywhere in this schema, SQLAlchemy has no dependency
    # graph to order those two deferred deletes by, so without this
    # explicit flush a single end-of-function commit could emit DELETE
    # brand_kit before the content_items DELETEs actually landed - confirmed
    # live: exactly this FK violation, caught in testing before it shipped.
    # Flushing here (not deferring to the caller's commit) forces every
    # content_item DELETE to actually execute first.
    db.flush()

    db.query(NicheConfig).filter(NicheConfig.brand_kit_id == brand_kit_id).delete()
    db.query(BrandMember).filter(BrandMember.brand_kit_id == brand_kit_id).delete()
    db.query(AgentModelConfig).filter(AgentModelConfig.brand_kit_id == brand_kit_id).delete()
    db.query(AgentPromptConfig).filter(AgentPromptConfig.brand_kit_id == brand_kit_id).delete()
    db.query(SyncState).filter(SyncState.brand_kit_id == brand_kit_id).delete()
    db.query(OAuthCredential).filter(OAuthCredential.brand_kit_id == brand_kit_id).delete()
    # Safe only now that every ContentItem (whose source_email_id/nothing
    # else still points at these) is already gone.
    db.query(IngestedEmail).filter(IngestedEmail.brand_kit_id == brand_kit_id).delete()
    db.query(IngestedRssItem).filter(IngestedRssItem.brand_kit_id == brand_kit_id).delete()

    brand = db.get(BrandKit, brand_kit_id)
    if brand:
        if brand.logo_asset_path:
            try:
                storage.delete(brand.logo_asset_path)
            except Exception:
                logger.exception("Could not delete stored logo for brand %s, continuing", brand_kit_id)
        db.delete(brand)


def delete_user(db: Session, user_id: uuid.UUID) -> tuple[bool, str]:
    """Refuses if the user still owns any brand - cascading through a
    brand's real content (and every OTHER member's access to it) just
    because its owner deleted their own account would be a surprising,
    destructive side effect for everyone else on that brand, not something
    to do silently as a side effect of one person's account deletion.
    Returns (True, "") on success, (False, reason) if refused - callers
    show `reason` back to whoever asked for the deletion."""
    owned = db.query(BrandKit.id).filter(BrandKit.owner_user_id == user_id).count()
    if owned:
        return False, (
            f"This user still owns {owned} brand(s) - delete those brands (or transfer ownership, "
            "once that exists) before deleting the account."
        )
    db.query(UserApiKey).filter(UserApiKey.user_id == user_id).delete()
    db.query(BrandMember).filter(BrandMember.user_id == user_id).delete()
    user = db.get(User, user_id)
    if user:
        db.delete(user)
    return True, ""
