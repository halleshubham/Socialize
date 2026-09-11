"""Deletes DISCARDED content items - and their MediaAsset/LlmCallLog rows
and stored media files - once they've sat discarded longer than a
per-brand-configurable retention window (default 2 days). Runs daily via
worker/scheduler.py, same pattern as the Gmail/RSS fetch jobs.

`content_item.updated_at` (auto-refreshed on every stage change, including
the transition into DISCARDED) is used as "when it was discarded" - good
enough without a dedicated timestamp column, since nothing else legitimately
re-touches a discarded item afterward.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from backend.agents.state import Stage
from backend.app.db.models import BrandKit, ContentItem, ContentItemVersion, LlmCallLog, MediaAsset, PostizPost, SyncState
from backend.app.db.session import SessionLocal
from backend.app.storage.local_disk import get_storage_backend

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 2
_RETENTION_KEY = "discarded_retention_days"


def get_retention_days(db: Session, brand_kit_id) -> int:
    row = db.get(SyncState, (_RETENTION_KEY, brand_kit_id))
    if not row:
        return DEFAULT_RETENTION_DAYS
    try:
        return max(0, int(row.value))
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS


def set_retention_days(db: Session, brand_kit_id, days: int) -> None:
    days = max(0, int(days))
    row = db.get(SyncState, (_RETENTION_KEY, brand_kit_id))
    if row:
        row.value = str(days)
    else:
        db.add(SyncState(key=_RETENTION_KEY, brand_kit_id=brand_kit_id, value=str(days)))
    db.commit()


def _delete_content_item(db: Session, content_item: ContentItem) -> None:
    """Every table with a content_item_id FK (MediaAsset, LlmCallLog,
    ContentItemVersion, PostizPost) has to be gone before the ContentItem
    row itself is deleted. No ORM `relationship()` exists anywhere in this
    schema (every table here uses a plain FK column instead), so SQLAlchemy
    has no dependency info to auto-order cross-table deletes within one
    flush - queuing `db.delete()` on both a child and its parent in the same
    flush emits them in an arbitrary order and hits a real FK violation
    (confirmed live: every discarded item with a MediaAsset failed this way
    before this fix). Bulk `.delete()` calls sidestep the issue entirely -
    each executes as an immediate DELETE, not deferred to a later flush, so
    by the time the ContentItem row itself is deleted, its children are
    already gone from the database."""
    storage = get_storage_backend()
    assets = db.query(MediaAsset).filter(MediaAsset.content_item_id == content_item.id).all()
    for asset in assets:
        try:
            storage.delete(asset.storage_uri)
        except Exception:
            logger.exception("Could not delete stored file for asset %s, continuing", asset.id)

    db.query(MediaAsset).filter(MediaAsset.content_item_id == content_item.id).delete()
    db.query(LlmCallLog).filter(LlmCallLog.content_item_id == content_item.id).delete()
    db.query(ContentItemVersion).filter(ContentItemVersion.content_item_id == content_item.id).delete()
    db.query(PostizPost).filter(PostizPost.content_item_id == content_item.id).delete()
    db.delete(content_item)


def run_discarded_cleanup() -> int:
    """Sweeps every brand's DISCARDED items against its own configured
    retention window (a brand with `discarded_retention_days=0` is
    effectively opted out - nothing is ever old enough). Best-effort per
    item - one bad row's deletion failing doesn't block the rest. Returns
    how many content items were deleted, for the scheduler's own log line."""
    db = SessionLocal()
    deleted = 0
    try:
        brand_ids = [row.id for row in db.query(BrandKit.id).all()]
        for brand_id in brand_ids:
            retention_days = get_retention_days(db, brand_id)
            if retention_days <= 0:
                continue
            cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
            items = (
                db.query(ContentItem)
                .filter(
                    ContentItem.brand_kit_id == brand_id,
                    ContentItem.stage == Stage.DISCARDED,
                    ContentItem.updated_at < cutoff,
                )
                .all()
            )
            for item in items:
                try:
                    _delete_content_item(db, item)
                    db.commit()
                    deleted += 1
                except Exception:
                    db.rollback()
                    logger.exception("Could not delete discarded content item %s, skipping", item.id)
    finally:
        db.close()
    return deleted
