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
from backend.app.worker.background import run_in_background

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


def _process_one_email(email_id: uuid.UUID) -> None:
    try:
        process_email(SessionLocal, email_id)
    except Exception:
        logger.exception("Pipeline run failed for email %s", email_id)


def process_pending_emails(email_ids: list[uuid.UUID]) -> None:
    """Dispatches each email as its OWN background task rather than looping
    through them on one thread - found live: one email whose auto-advance
    reached reel generation (minutes, via the graph's own synchronous
    content_review -> reel_editor resume chain) blocked every other email in
    the same fetch behind it, since they all sat on one thread waiting their
    turn in this loop. The board showed the whole batch stuck "processing"
    for however long that one reel took, with nothing wrong reported - every
    email really was being worked, just serialized behind the slowest one.
    Each email claims and runs independently anyway (status 'new' ->
    'processing' per-row), so nothing about correctness depends on doing
    this in one thread - only throughput does, and this fixes that."""
    for email_id in email_ids:
        run_in_background(_process_one_email, email_id)


def run_daily_fetch(brand_kit_id: uuid.UUID) -> int:
    """Returns the number of emails run through the pipeline this call."""
    pending_ids, _new_count = fetch_pending_email_ids(brand_kit_id)
    process_pending_emails(pending_ids)
    return len(pending_ids)
