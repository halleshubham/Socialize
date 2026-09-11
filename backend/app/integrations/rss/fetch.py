"""Fetches new entries from a brand's configured RSS/Atom feeds and stores
them in ingested_rss_items - the RSS equivalent of gmail/fetch.py. One feed
failing (dead URL, malformed XML) doesn't block the others.
"""

import logging
import uuid

from sqlalchemy.orm import Session

from backend.app.db.models import BrandKit, IngestedRssItem
from backend.app.integrations.rss.client import RssError, fetch_feed

logger = logging.getLogger(__name__)


def list_brand_feeds(brand_kit: BrandKit | None) -> list[dict]:
    return brand_kit.rss_feeds if brand_kit else []


def add_feed(db: Session, brand_kit: BrandKit, url: str, name: str) -> None:
    feeds = list(brand_kit.rss_feeds)
    feeds.append({"url": url.strip(), "name": name.strip() or url.strip()})
    brand_kit.rss_feeds = feeds
    db.commit()


def remove_feed(db: Session, brand_kit: BrandKit, url: str) -> None:
    brand_kit.rss_feeds = [f for f in brand_kit.rss_feeds if f.get("url") != url]
    db.commit()


def fetch_new_items(db: Session, brand_kit_id: uuid.UUID) -> list[uuid.UUID]:
    """Returns the ids of newly ingested items (already committed) - plain
    ids, not ORM objects, since this commits internally and the caller may
    use these after this function's Session is closed."""
    brand_kit = db.get(BrandKit, brand_kit_id)
    if not brand_kit or not brand_kit.rss_feeds:
        return []

    new_items: list[IngestedRssItem] = []
    for feed in brand_kit.rss_feeds:
        url = feed.get("url")
        if not url:
            continue
        try:
            entries = fetch_feed(url)
        except RssError:
            logger.exception("RSS fetch failed for %s (brand %s)", url, brand_kit_id)
            continue

        for entry in entries:
            exists = (
                db.query(IngestedRssItem)
                .filter(
                    IngestedRssItem.entry_id == entry["entry_id"],
                    IngestedRssItem.brand_kit_id == brand_kit_id,
                )
                .first()
            )
            if exists:
                continue
            item = IngestedRssItem(
                brand_kit_id=brand_kit_id,
                feed_url=url,
                entry_id=entry["entry_id"],
                title=entry["title"][:500],
                link=entry["link"][:1000],
                summary=entry["summary"],
                published_at=entry["published_at"],
                status="new",
            )
            db.add(item)
            new_items.append(item)

    db.commit()
    return [item.id for item in new_items]
