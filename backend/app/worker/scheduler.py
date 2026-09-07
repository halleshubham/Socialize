import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from backend.app.db.models import OAuthCredential
from backend.app.db.session import SessionLocal
from backend.app.worker.pipeline import run_daily_fetch

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _daily_fetch_job() -> None:
    """One brand's Gmail failing (quota, revoked consent, ...) shouldn't
    block the rest - each brand with a connected Gmail account gets its own
    try/except, sequentially (fine at small brand counts; worth staggering/
    parallelizing later if that changes)."""
    db = SessionLocal()
    try:
        brand_ids = [
            row.brand_kit_id
            for row in db.query(OAuthCredential.brand_kit_id)
            .filter(OAuthCredential.provider == "gmail", OAuthCredential.brand_kit_id.isnot(None))
            .all()
        ]
    finally:
        db.close()

    for brand_id in brand_ids:
        try:
            count = run_daily_fetch(brand_id)
            logger.info("Daily Gmail fetch for brand %s: %d new email(s) ingested", brand_id, count)
        except Exception:
            logger.exception("Daily Gmail fetch job failed for brand %s", brand_id)


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler()
    # 06:00 local time daily, per the plan. Swap for Celery Beat in Phase 3
    # once there's a real job queue.
    _scheduler.add_job(_daily_fetch_job, CronTrigger(hour=6, minute=0), id="daily_gmail_fetch")
    _scheduler.start()
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
