"""Fetches new mail since the last successful sync and stores it in
ingested_emails. Called by the daily scheduled job (worker/scheduler.py) and
by the manual "Fetch now" button in the inbox UI.
"""

import time
import uuid

from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from backend.app.db.models import IngestedEmail, SyncState
from backend.app.integrations.gmail import parse
from backend.app.integrations.gmail.oauth import load_credentials

SYNC_STATE_KEY = "gmail_last_fetch_epoch"
SEARCH_QUERY_KEY = "gmail_search_query"
DEFAULT_LOOKBACK_SECONDS = 24 * 60 * 60  # first-ever run: last 24h only


def get_last_fetch_epoch(db: Session, brand_kit_id: uuid.UUID) -> int:
    """Public (not just fetch_new_emails' internal cursor read) so the board
    UI can show "last fetched at" and so set_last_fetch_epoch can be used to
    deliberately rewind it - "refetch since this date" lets a user re-pull
    older mail on demand instead of only ever moving forward."""
    row = db.get(SyncState, (SYNC_STATE_KEY, brand_kit_id))
    if row:
        return int(row.value)
    return int(time.time()) - DEFAULT_LOOKBACK_SECONDS


def set_last_fetch_epoch(db: Session, brand_kit_id: uuid.UUID, epoch: int) -> None:
    row = db.get(SyncState, (SYNC_STATE_KEY, brand_kit_id))
    if row:
        row.value = str(epoch)
    else:
        db.add(SyncState(key=SYNC_STATE_KEY, brand_kit_id=brand_kit_id, value=str(epoch)))
    db.commit()


def get_search_query(db: Session, brand_kit_id: uuid.UUID) -> str:
    """Extra Gmail search terms ANDed onto the after: date filter, e.g.
    "category:promotions OR category:updates", "label:Newsletters", or
    "from:newsletter@example.com". Empty means "all mail" (Gmail's default
    search behavior already excludes Spam/Trash)."""
    row = db.get(SyncState, (SEARCH_QUERY_KEY, brand_kit_id))
    return row.value if row else ""


def set_search_query(db: Session, brand_kit_id: uuid.UUID, query: str) -> None:
    row = db.get(SyncState, (SEARCH_QUERY_KEY, brand_kit_id))
    if row:
        row.value = query
    else:
        db.add(SyncState(key=SEARCH_QUERY_KEY, brand_kit_id=brand_kit_id, value=query))
    db.commit()


def fetch_new_emails(db: Session, brand_kit_id: uuid.UUID) -> list[uuid.UUID]:
    """Returns the ids of the newly ingested emails (already committed).
    Returns plain ids rather than ORM objects: fetch_new_emails does another
    commit afterward (to advance the sync cursor), which expires every
    object's attributes, and the caller may use these ids after this
    function's Session is closed - a DetachedInstanceError trap otherwise."""
    creds = load_credentials(db, brand_kit_id)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    since_epoch = get_last_fetch_epoch(db, brand_kit_id)
    fetch_started_at = int(time.time())
    # No category filter by default: newsletters - exactly what this tool
    # wants - are usually auto-sorted into Gmail's Promotions/Updates tabs,
    # not Primary. User-configurable via set_search_query / the board UI if
    # they want to narrow this (e.g. "category:promotions", "label:Newsletters").
    extra_query = get_search_query(db, brand_kit_id).strip()
    query = f"after:{since_epoch} {extra_query}".strip()

    new_emails: list[IngestedEmail] = []
    page_token = None
    while True:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, pageToken=page_token, maxResults=100)
            .execute()
        )
        for msg_ref in resp.get("messages", []):
            gmail_message_id = msg_ref["id"]
            if db.query(IngestedEmail).filter(
                IngestedEmail.gmail_message_id == gmail_message_id,
                IngestedEmail.brand_kit_id == brand_kit_id,
            ).first():
                continue

            full = (
                service.users()
                .messages()
                .get(userId="me", id=gmail_message_id, format="full")
                .execute()
            )
            email = IngestedEmail(
                brand_kit_id=brand_kit_id,
                gmail_message_id=gmail_message_id,
                sender=parse.header(full, "From"),
                subject=parse.header(full, "Subject"),
                received_at=parse.received_at(full),
                snippet=full.get("snippet", ""),
                body_text=parse.extract_body_text(full),
                body_with_links=parse.extract_body_with_links(full),
                status="new",
            )
            db.add(email)
            new_emails.append(email)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    db.commit()
    new_email_ids = [email.id for email in new_emails]
    set_last_fetch_epoch(db, brand_kit_id, fetch_started_at)
    return new_email_ids
