"""Glue between Gmail fetch and the orchestrator: fetch new mail, then run
every unprocessed email (newly fetched, plus any left over status='new' from
a prior run that got interrupted mid-batch - e.g. a server restart) through
the Researcher's article extraction and a graph run for every suitable
article found. Used by the daily scheduled job, and by the manual "Fetch
now" button (routes_board.py) - the latter calls fetch_pending_email_ids and
process_pending_emails separately so it can raise GmailNotConnected
synchronously (a fast, actionable check worth blocking on) while
backgrounding the slow per-email pipeline runs.
"""

import logging
import uuid

from backend.agents.orchestrator import process_email
from backend.app.db.models import IngestedEmail
from backend.app.db.session import SessionLocal
from backend.app.integrations.gmail.fetch import fetch_new_emails

logger = logging.getLogger(__name__)


def fetch_pending_email_ids(brand_kit_id: uuid.UUID) -> tuple[list[uuid.UUID], int]:
    """Fetches new mail from this brand's Gmail (raises GmailNotConnected if
    not set up) and returns (every email of theirs still awaiting a pipeline
    run - newly fetched ones plus any backlog left over from a prior
    interrupted run, dedup means those never get re-fetched, only re-swept
    here; how many were actually new THIS call - what "Fetch now" shows the
    user, since the pending list alone can't tell a fresh fetch of 5 apart
    from a backlog sweep finding the same 5 left over from before)."""
    db = SessionLocal()
    try:
        new_email_ids = fetch_new_emails(db, brand_kit_id)
        pending_ids = [
            row.id
            for row in db.query(IngestedEmail.id)
            .filter(IngestedEmail.brand_kit_id == brand_kit_id, IngestedEmail.status == "new")
            .all()
        ]
        return pending_ids, len(new_email_ids)
    finally:
        db.close()


def process_pending_emails(email_ids: list[uuid.UUID]) -> None:
    for email_id in email_ids:
        try:
            process_email(SessionLocal, email_id)
        except Exception:
            logger.exception("Pipeline run failed for email %s", email_id)


def run_daily_fetch(brand_kit_id: uuid.UUID) -> int:
    """Returns the number of emails run through the pipeline this call."""
    pending_ids, _new_count = fetch_pending_email_ids(brand_kit_id)
    process_pending_emails(pending_ids)
    return len(pending_ids)
