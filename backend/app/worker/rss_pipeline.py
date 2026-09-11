"""Glue between RSS fetch and the orchestrator - the RSS sibling of
worker/pipeline.py. One brand at a time: fetch new entries across all its
configured feeds, then score the whole pending batch in one LLM call and
create ContentItems for the suitable ones.
"""

import logging
import uuid

from backend.agents.orchestrator import process_rss_batch
from backend.app.db.models import IngestedRssItem
from backend.app.db.session import SessionLocal
from backend.app.integrations.rss.fetch import fetch_new_items

logger = logging.getLogger(__name__)


def fetch_pending_rss_ids(brand_kit_id: uuid.UUID) -> tuple[list[uuid.UUID], int]:
    db = SessionLocal()
    try:
        new_ids = fetch_new_items(db, brand_kit_id)
        pending_ids = [
            row.id
            for row in db.query(IngestedRssItem.id)
            .filter(IngestedRssItem.brand_kit_id == brand_kit_id, IngestedRssItem.status == "new")
            .all()
        ]
        return pending_ids, len(new_ids)
    finally:
        db.close()


def run_rss_fetch(brand_kit_id: uuid.UUID) -> int:
    """Returns how many entries were run through the pipeline this call."""
    pending_ids, _new_count = fetch_pending_rss_ids(brand_kit_id)
    if not pending_ids:
        return 0
    try:
        process_rss_batch(SessionLocal, brand_kit_id, pending_ids)
    except Exception:
        logger.exception("RSS pipeline run failed for brand %s", brand_kit_id)
    return len(pending_ids)
