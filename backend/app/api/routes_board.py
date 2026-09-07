import logging
import time
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.agents.auto_mode import DEFAULT_FORMAT, DEFAULT_MIN_SCORE, get_auto_mode_settings, set_auto_mode_settings
from backend.agents.content_writer.graph import write_copy, write_format_fields
from backend.agents.graphic_designer.graph import generate_poster
from backend.agents.graphic_designer.templates import TEMPLATE_CHOICES
from backend.agents.languages import DEFAULT_LANGUAGE, LANGUAGE_CHOICES
from backend.agents.orchestrator import (
    auto_advance_content_item,
    resume_content_item,
    sync_format_to_graph_state,
)
from backend.agents.reel_editor.graph import (
    generate_reel,
    get_cost_cap,
    get_video_model_key,
    set_cost_cap,
    set_video_model_key,
)
from backend.agents.reel_editor.templates import TEMPLATE_CHOICES as REEL_TEMPLATE_CHOICES
from backend.agents.state import Format, Stage
from backend.app.auth.brand_deps import get_accessible_brands, get_active_brand
from backend.app.auth.deps import get_current_user
from backend.app.db.models import BrandKit, ContentItem, IngestedEmail, LlmCallLog, MediaAsset, PostizPost, User
from backend.app.db.session import SessionLocal, get_db
from backend.app.integrations.botsab.send import WhatsAppSendError, send_content_item
from backend.app.integrations.gmail.fetch import get_last_fetch_epoch, get_search_query, set_last_fetch_epoch, set_search_query
from backend.app.integrations.gmail.oauth import GmailNotConnected
from backend.app.integrations.postiz.auto_schedule import build_schedule_slots
from backend.app.integrations.postiz.client import PostizError
from backend.app.integrations.postiz.send import PostizSendError, get_postiz_client
from backend.app.integrations.postiz.send import send_content_item as send_postiz_content_item
from backend.app.llm.video_provider import VIDEO_MODEL_CHOICES
from backend.app.storage.local_disk import get_storage_backend
from backend.app.worker.background import run_in_background
from backend.app.worker.pipeline import fetch_pending_email_ids, process_pending_emails

logger = logging.getLogger(__name__)

# Handy starting points for the free-text Gmail search box - Gmail ANDs
# space-separated terms, ORs need explicit parens/"OR".
SEARCH_QUERY_PRESETS = [
    ("", "All mail"),
    ("category:promotions OR category:updates", "Promotions + Updates (most newsletters)"),
    ("category:primary", "Primary only"),
    ("label:Newsletters", "Newsletters label"),
]

def _cost_by_item_id(db: Session, item_ids: list) -> dict:
    """Total spend per content_item - every LLM call (brief, copy, creative
    direction, shot-listing) plus every generated media asset (poster/reel/
    character-reference), so a card's cost reflects everything that went
    into it, not just the most visible/expensive step."""
    if not item_ids:
        return {}
    costs: dict = {}
    llm_rows = (
        db.query(LlmCallLog.content_item_id, func.sum(LlmCallLog.cost_usd))
        .filter(LlmCallLog.content_item_id.in_(item_ids))
        .group_by(LlmCallLog.content_item_id)
        .all()
    )
    for item_id, total in llm_rows:
        costs[item_id] = costs.get(item_id, 0.0) + (total or 0.0)
    media_rows = (
        db.query(MediaAsset.content_item_id, func.sum(MediaAsset.cost_usd))
        .filter(MediaAsset.content_item_id.in_(item_ids))
        .group_by(MediaAsset.content_item_id)
        .all()
    )
    for item_id, total in media_rows:
        costs[item_id] = costs.get(item_id, 0.0) + (total or 0.0)
    return costs


router = APIRouter(prefix="/board")
templates = Jinja2Templates(directory="backend/app/templates")


def _accessible_content_item(db: Session, user: User, content_item_id: str) -> ContentItem | None:
    """Every board action below takes a content_item_id from the URL/form -
    this checks it actually belongs to a brand the current user can access
    (owned or shared) before any route touches it, so knowing/guessing
    another brand's content_item_id isn't enough to act on it."""
    content_item = db.get(ContentItem, content_item_id)
    if not content_item:
        return None
    accessible_ids = {b.id for b in get_accessible_brands(db, user)}
    if content_item.brand_kit_id not in accessible_ids:
        return None
    return content_item

# One card = one ContentItem (article/post-in-making). Columns are the
# pipeline stages a user cares about watching; Discarded is shown separately,
# collapsed, so a noisy newsletter doesn't dominate the board while still
# being visible ("which mails have actually been looked at").
BOARD_COLUMNS = [
    ("researched", "Researching", Stage.RESEARCHED),
    ("analyzed", "Needs Review", Stage.ANALYZED),
    ("drafted", "Content Review", Stage.DRAFTED),
    ("media_generated", "Media Review", Stage.MEDIA_GENERATED),
    ("approved", "Approved", Stage.APPROVED),
]


@router.get("", response_class=HTMLResponse)
def show_board(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
    error: str | None = None,
    fetched: int | None = None,
    pending: int | None = None,
):
    items_by_stage: dict[str, list[ContentItem]] = {}
    for key, _label, stage in BOARD_COLUMNS:
        items_by_stage[key] = (
            db.query(ContentItem)
            .filter(ContentItem.brand_kit_id == brand.id, ContentItem.stage == stage)
            .order_by(ContentItem.updated_at.desc())
            .limit(50)
            .all()
        )
    discarded = (
        db.query(ContentItem)
        .filter(ContentItem.brand_kit_id == brand.id, ContentItem.stage == Stage.DISCARDED)
        .order_by(ContentItem.updated_at.desc())
        .limit(50)
        .all()
    )

    all_items = [i for items in items_by_stage.values() for i in items] + discarded
    email_ids = {i.source_email_id for i in all_items if i.source_email_id}
    emails_by_id = (
        {e.id: e for e in db.query(IngestedEmail).filter(IngestedEmail.id.in_(email_ids)).all()}
        if email_ids
        else {}
    )

    item_ids = [i.id for i in all_items]
    format_by_item_id = {i.id: i.format for i in all_items}
    storage = get_storage_backend()
    media_by_item_id: dict = {}
    if item_ids:
        assets = (
            db.query(MediaAsset)
            .filter(MediaAsset.content_item_id.in_(item_ids))
            .filter(MediaAsset.asset_type.in_(["poster", "reel"]))
            .order_by(MediaAsset.created_at.desc())
            .all()
        )
        for asset in assets:
            # Newest-matching-the-item's-CURRENT-format, not just newest
            # overall - an item can hold both a poster and a reel asset in
            # its history (switching format via generate-media leaves the
            # old asset in place), and a stray leftover of the wrong type
            # would otherwise shadow the real one just by being more
            # recent. asset_type excludes character_reference rows - those
            # aren't "the" media for a card.
            if asset.asset_type != format_by_item_id.get(asset.content_item_id):
                continue
            media_by_item_id.setdefault(
                asset.content_item_id, (storage.url_for(asset.storage_uri), asset.asset_type)
            )

    # Transparency panel: every email actually fetched/looked at, whether or
    # not it produced any cards - answers "which mails have been traced".
    recent_emails = (
        db.query(IngestedEmail)
        .filter(IngestedEmail.brand_kit_id == brand.id)
        .order_by(IngestedEmail.created_at.desc())
        .limit(20)
        .all()
    )

    cost_by_item_id = _cost_by_item_id(db, item_ids)

    # Approved items with a still-pending Postiz schedule move out of
    # Approved into their own "Scheduled" column - keeps Approved for what's
    # still awaiting a send/schedule decision. An immediate ("now") send
    # leaves scheduled_at NULL and the item stays in Approved untouched.
    approved_ids = [i.id for i in items_by_stage.get("approved", [])]
    scheduled_posts_by_item_id: dict = {}
    if approved_ids:
        pending_posts = (
            db.query(PostizPost)
            .filter(PostizPost.content_item_id.in_(approved_ids), PostizPost.scheduled_at.isnot(None))
            .order_by(PostizPost.scheduled_at.asc())
            .all()
        )
        for post in pending_posts:
            scheduled_posts_by_item_id.setdefault(post.content_item_id, []).append(post)
    scheduled_items = [i for i in items_by_stage["approved"] if i.id in scheduled_posts_by_item_id]
    scheduled_items.sort(key=lambda i: scheduled_posts_by_item_id[i.id][0].scheduled_at)
    items_by_stage["approved"] = [i for i in items_by_stage["approved"] if i.id not in scheduled_posts_by_item_id]

    return templates.TemplateResponse(
        request,
        "board.html",
        {
            "columns": BOARD_COLUMNS,
            "items_by_stage": items_by_stage,
            "scheduled_items": scheduled_items,
            "scheduled_posts_by_item_id": scheduled_posts_by_item_id,
            "discarded": discarded,
            "emails_by_id": emails_by_id,
            "media_by_item_id": media_by_item_id,
            "cost_by_item_id": cost_by_item_id,
            "recent_emails": recent_emails,
            "search_query": get_search_query(db, brand.id),
            "search_presets": SEARCH_QUERY_PRESETS,
            "gmail_last_fetch_at": datetime.fromtimestamp(get_last_fetch_epoch(db, brand.id), tz=timezone.utc),
            "reel_cost_cap": get_cost_cap(db, brand.id),
            "reel_video_model_key": get_video_model_key(db, brand.id),
            "video_model_choices": VIDEO_MODEL_CHOICES,
            "auto_mode": get_auto_mode_settings(db, brand.id),
            "language_choices": LANGUAGE_CHOICES,
            "default_language": brand.default_language or DEFAULT_LANGUAGE,
            "template_choices": TEMPLATE_CHOICES,
            "reel_template_choices": REEL_TEMPLATE_CHOICES,
            "accessible_brands": get_accessible_brands(db, user),
            "active_brand": brand,
            "user": user,
            "error": error,
            "fetched": fetched,
            "pending": pending,
            "active_nav": "board",
        },
    )


def _fetch_and_redirect(brand_kit_id) -> RedirectResponse:
    """Shared by fetch_now and refetch_since below - checking/listing Gmail
    is a single fast API call and worth blocking on (GmailNotConnected is
    exactly the kind of setup error the user needs to see immediately, not
    after a background thread swallows it); running every pending email
    through the Researcher/Analytical/(auto-mode) pipeline is the slow,
    unbounded part (more so with auto-mode - since a qualifying item now
    runs all the way to media generation without stopping), so that part
    backgrounds."""
    try:
        pending_ids, new_count = fetch_pending_email_ids(brand_kit_id)
    except GmailNotConnected as exc:
        return RedirectResponse(url=f"/board?error={exc}", status_code=303)

    run_in_background(process_pending_emails, pending_ids)
    return RedirectResponse(url=f"/board?fetched={new_count}&pending={len(pending_ids)}", status_code=303)


@router.post("/fetch")
def fetch_now(
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    return _fetch_and_redirect(brand.id)


@router.post("/gmail-refetch-since")
def refetch_since(
    since_date: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """Deliberately rewinds this brand's Gmail fetch cursor to midnight UTC
    on the given date, then immediately fetches - lets you re-pull older
    mail (a backlog that predates when this brand connected Gmail, or mail
    you want re-swept with a wider/changed search query) on demand, instead
    of the cursor only ever moving forward. Safe to run repeatedly: fetch's
    own gmail_message_id-per-brand dedup means anything already ingested for
    THIS brand is skipped either way, so widening the window just surfaces
    whatever's actually new to this brand within it."""
    try:
        since = datetime.strptime(since_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return RedirectResponse(url="/board?error=Invalid date", status_code=303)
    set_last_fetch_epoch(db, brand.id, int(since.timestamp()))
    return _fetch_and_redirect(brand.id)


@router.post("/search-query")
def update_search_query(
    query: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """Extra Gmail search terms used on every future fetch (see
    integrations/gmail/fetch.py:get_search_query for syntax). Doesn't affect
    emails already fetched, and doesn't re-fetch anything by itself - just
    click "Fetch now" after saving to apply it."""
    set_search_query(db, brand.id, query.strip())
    return RedirectResponse(url="/board", status_code=303)


def _start_processing(content_item_id: str) -> bool:
    """Marks a card as busy (loader on the card, other actions unaffected -
    every route below returns immediately after this and does the actual
    work in a background thread) and clears any stale error from a previous
    attempt. Returns False if the item no longer exists."""
    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, content_item_id)
        if not content_item:
            return False
        content_item.is_processing = True
        content_item.last_error = None
        db.commit()
        return True
    finally:
        db.close()


def _run_with_processing_state(content_item_id: str, work_fn) -> None:
    """Runs work_fn(db, content_item) in the background, then always clears
    is_processing and sets last_error (None on success, the exception
    message on failure) - the single place every slow board action's
    loader/error-card bookkeeping lives, so each route below only has to
    supply what actually changes (write_copy, generate_poster, ...)."""
    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, content_item_id)
        if not content_item:
            return
        try:
            work_fn(db, content_item)
            content_item.last_error = None
        except Exception as exc:
            logger.exception("Background board action failed for %s", content_item_id)
            content_item.last_error = str(exc)[:2000]
        finally:
            content_item.is_processing = False
            db.commit()
    finally:
        db.close()


def _decide_enters_slow_node(content_item: ContentItem, decision: str) -> bool:
    """Whether resuming with this decision will run content_writer/
    graphic_designer/reel_editor before the graph hits its next interrupt -
    all three make an LLM and/or image/video generation call, so those
    resumes go to a background thread instead of blocking the request.
    Mirrors orchestrator.py's _route_after_review/_route_after_content_review/
    _route_after_media_review - keep in sync if that routing logic changes."""
    if content_item.stage == Stage.ANALYZED:
        return decision == "send_to_content_writer"
    if content_item.stage == Stage.DRAFTED:
        if decision == "approve":
            return content_item.format in (Format.POSTER, Format.REEL)
        return decision == "regenerate"
    if content_item.stage == Stage.MEDIA_GENERATED:
        return decision == "regenerate"
    return False


def _resume_in_background(content_item_id: str, payload: dict) -> None:
    def _work(db: Session, content_item: ContentItem) -> None:
        resume_content_item(content_item_id, payload)
        # resume_content_item's graph nodes each open their own SessionLocal
        # and commit through it - this function's `db`/`content_item` (from
        # _run_with_processing_state) needs an explicit refresh to see any
        # of that, or last_error/is_processing below would write back over
        # stale, pre-resume field values (see approve_and_generate_media's
        # docstring for the same SQLAlchemy identity-map gotcha).
        db.expire_all()

    _run_with_processing_state(content_item_id, _work)


@router.post("/{content_item_id}/decide")
def decide(
    content_item_id: str,
    decision: str = Form(...),
    feedback: str = Form(""),
    format: str = Form(""),
    language: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Resumes whichever interrupt this content item's graph thread is
    currently paused at - analytical_review (write_myself | send_to_content_writer
    | discard, the latter takes `format`/`language`), content_review, or
    media_review (approve | regenerate | discard). LangGraph resumes
    whatever's actually pending, so one route handles all three gates.

    Resumes that enter a slow node (content_writer/graphic_designer/
    reel_editor - see _decide_enters_slow_node) go to a background thread
    instead of blocking the request; everything else (discard, write_myself,
    approve on a text_only post) is just a DB stage flip and stays
    synchronous since there's no meaningful wait to show a loader for."""
    payload = {"decision": decision, "feedback": feedback}
    if format:
        payload["format"] = format
    if language:
        payload["language"] = language

    if not _accessible_content_item(db, user, content_item_id):
        return RedirectResponse(url="/board?error=Not found", status_code=303)

    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, content_item_id)
        slow = bool(content_item) and _decide_enters_slow_node(content_item, decision)
        if slow:
            content_item.is_processing = True
            content_item.last_error = None
            db.commit()
    finally:
        db.close()

    if slow:
        run_in_background(_resume_in_background, content_item_id, payload)
    else:
        resume_content_item(content_item_id, payload)
    return RedirectResponse(url="/board", status_code=303)


_APPROVE_ALL_STAGES = {
    "analyzed": Stage.ANALYZED,
    "drafted": Stage.DRAFTED,
    "media_generated": Stage.MEDIA_GENERATED,
}


@router.post("/approve-all")
def approve_all(
    stage: str = Form(...),
    format: str = Form(""),
    language: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """Bulk version of decide() - applies its "send to Content Writer"
    (Needs Review) or "approve" (Content Review / Media Review) decision to
    every card currently in one column, for the active brand only. Each
    item goes through the exact same synchronous-vs-backgrounded check
    decide() uses per item (_decide_enters_slow_node) - the shared bg-task
    thread pool (app/worker/background.py, max_workers=4) throttles how
    many actually run at once rather than firing all of them simultaneously.
    A card that changes stage mid-loop (another tab, another user on a
    shared brand) is simply skipped, same as decide()'s own not-found guard."""
    target_stage = _APPROVE_ALL_STAGES.get(stage)
    if not target_stage:
        return RedirectResponse(url="/board?error=Invalid stage", status_code=303)

    decision = "send_to_content_writer" if target_stage == Stage.ANALYZED else "approve"
    base_payload = {"decision": decision, "feedback": ""}
    if target_stage == Stage.ANALYZED:
        base_payload["format"] = format or DEFAULT_FORMAT
        if language in LANGUAGE_CHOICES:
            base_payload["language"] = language

    item_ids = [
        str(row.id)
        for row in db.query(ContentItem.id)
        .filter(ContentItem.brand_kit_id == brand.id, ContentItem.stage == target_stage)
        .all()
    ]

    for content_item_id in item_ids:
        payload = dict(base_payload)
        db2 = SessionLocal()
        try:
            content_item = db2.get(ContentItem, content_item_id)
            if not content_item or content_item.stage != target_stage:
                continue
            slow = _decide_enters_slow_node(content_item, decision)
            if slow:
                content_item.is_processing = True
                content_item.last_error = None
                db2.commit()
        finally:
            db2.close()

        if slow:
            run_in_background(_resume_in_background, content_item_id, payload)
        else:
            resume_content_item(content_item_id, payload)

    return RedirectResponse(url="/board", status_code=303)


@router.post("/{content_item_id}/retry-content-writer")
def retry_content_writer(
    content_item_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """For a content_writer step stuck after a failed generation - same
    reasoning as retry_reel below: once a node fails mid-flight, the graph's
    checkpoint isn't cleanly resumable via Command(resume=...) anymore, so
    this calls write_copy directly instead of going through the graph."""
    if not _accessible_content_item(db, user, content_item_id) or not _start_processing(content_item_id):
        return RedirectResponse(url="/board", status_code=303)

    def _work(db: Session, content_item: ContentItem) -> None:
        write_copy(db, content_item, revision_feedback=content_item.user_feedback)
        content_item.stage = Stage.DRAFTED
        content_item.user_feedback = None

    run_in_background(_run_with_processing_state, content_item_id, _work)
    return RedirectResponse(url="/board", status_code=303)


@router.post("/{content_item_id}/retry-poster")
def retry_poster(
    content_item_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Poster equivalent of retry_reel below - same "graph checkpoint isn't
    resumable after a mid-node failure" reasoning."""
    if not _accessible_content_item(db, user, content_item_id) or not _start_processing(content_item_id):
        return RedirectResponse(url="/board", status_code=303)

    def _work(db: Session, content_item: ContentItem) -> None:
        generate_poster(db, content_item)
        content_item.stage = Stage.MEDIA_GENERATED

    run_in_background(_run_with_processing_state, content_item_id, _work)
    return RedirectResponse(url="/board", status_code=303)


@router.post("/{content_item_id}/retry-reel")
def retry_reel(
    content_item_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """For a reel stuck after a failed generation (network blip, Veo quota,
    safety filter, cost cap too low) - resume_content_item doesn't apply
    since the graph isn't paused at an interrupt once content_review's
    "approve" already resumed once. Calls generate_reel directly instead,
    still in the background."""
    if not _accessible_content_item(db, user, content_item_id) or not _start_processing(content_item_id):
        return RedirectResponse(url="/board", status_code=303)

    def _work(db: Session, content_item: ContentItem) -> None:
        generate_reel(db, content_item)
        content_item.stage = Stage.MEDIA_GENERATED

    run_in_background(_run_with_processing_state, content_item_id, _work)
    return RedirectResponse(url="/board", status_code=303)


def _generate_media_work(target_format: str):
    """Shared by generate_media and approve_and_generate_media below -
    derives the new format's field(s) via write_format_fields if not already
    present (existing copy_text/hashtags are always left untouched) then
    runs the matching media generator."""

    def _work(db: Session, item: ContentItem) -> None:
        if target_format == "poster":
            if not item.poster_headline:
                write_format_fields(db, item, "poster")
            generate_poster(db, item)
        else:
            if not item.reel_script:
                write_format_fields(db, item, "reel")
            generate_reel(db, item)
        item.stage = Stage.MEDIA_GENERATED

    return _work


@router.post("/{content_item_id}/generate-media")
def generate_media(
    content_item_id: str,
    target_format: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Adds a poster or reel to a post that already has an approved
    copy_text (typically one originally written as text_only) - bypasses the
    LangGraph graph entirely, same reasoning as retry_reel: this item's
    original run already reached END, so there's no interrupt to resume
    into. Only valid once a post is actually Approved (stage=APPROVED) - see
    approve_and_generate_media for the drafted/Content-Review-column
    equivalent, which resumes the graph properly first."""
    if target_format not in ("poster", "reel"):
        return RedirectResponse(url="/board?error=Invalid format", status_code=303)

    content_item = _accessible_content_item(db, user, content_item_id)
    if not content_item or not content_item.copy_text:
        return RedirectResponse(
            url="/board?error=No existing content to generate media from", status_code=303
        )

    content_item.format = target_format
    content_item.is_processing = True
    content_item.last_error = None
    db.commit()
    sync_format_to_graph_state(content_item_id, target_format)

    run_in_background(_run_with_processing_state, content_item_id, _generate_media_work(target_format))
    return RedirectResponse(url="/board", status_code=303)


@router.post("/{content_item_id}/approve-and-generate-media")
def approve_and_generate_media(
    content_item_id: str,
    target_format: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """One-click version of Approve (from the drafted/Content Review card)
    followed by Generate poster/reel, for a text_only draft you've decided
    you also want a poster or reel for - without a manual approve-then-
    click-again round trip. Unlike generate_media, this item is still
    genuinely paused at the content_review LangGraph interrupt, so it MUST
    go through resume_content_item first (decision="approve") to close that
    interrupt out correctly - only then is it safe to bypass the graph for
    media generation the same way generate_media does. Synchronous
    resume_content_item call is safe here: for a text_only item, approving
    is just a stage flip with no LLM/media call in the graph itself."""
    if target_format not in ("poster", "reel"):
        return RedirectResponse(url="/board?error=Invalid format", status_code=303)

    content_item = _accessible_content_item(db, user, content_item_id)
    if not content_item or content_item.stage != Stage.DRAFTED or not content_item.copy_text:
        return RedirectResponse(url="/board?error=Nothing to approve here", status_code=303)

    resume_content_item(content_item_id, {"decision": "approve", "feedback": ""})

    # resume_content_item commits the stage change through its own,
    # separate SessionLocal (the graph node opens its own session) - this
    # route's `db` already has this row cached in its identity map from the
    # check above, and Session.get() returns that cached copy without
    # re-querying, so without expiring it here this would incorrectly still
    # read the pre-approval "drafted" row and bail out below every time.
    db.expire_all()
    content_item = db.get(ContentItem, content_item_id)
    if not content_item or content_item.stage != Stage.APPROVED:
        # Didn't land where expected (e.g. this wasn't actually a pending
        # interrupt) - bail out rather than force media generation on an
        # inconsistent state.
        return RedirectResponse(url="/board?error=Could not approve this item", status_code=303)

    content_item.format = target_format
    content_item.is_processing = True
    content_item.last_error = None
    db.commit()
    sync_format_to_graph_state(content_item_id, target_format)

    run_in_background(_run_with_processing_state, content_item_id, _generate_media_work(target_format))
    return RedirectResponse(url="/board", status_code=303)


@router.post("/reel-cost-cap")
def update_reel_cost_cap(
    cap_usd: float = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    set_cost_cap(db, brand.id, max(0.0, cap_usd))
    return RedirectResponse(url="/board", status_code=303)


@router.post("/reel-video-model")
def update_reel_video_model(
    model_key: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """Which Veo tier the next reel generation uses - see
    llm/video_provider.py's VIDEO_MODEL_CHOICES ("lite", cheap default, vs
    "standard", ~5x pricier). Applies to whichever reel gets generated next,
    same "configure, then trigger" pattern as the reel cost cap."""
    set_video_model_key(db, brand.id, model_key)
    return RedirectResponse(url="/board", status_code=303)


def _sweep_auto_mode(content_item_ids: list) -> None:
    for content_item_id in content_item_ids:
        auto_advance_content_item(content_item_id)


@router.post("/auto-mode")
def update_auto_mode(
    enabled: str = Form(""),
    min_score: float = Form(DEFAULT_MIN_SCORE),
    format: str = Form(DEFAULT_FORMAT),
    auto_send: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """Auto mode skips the analytical_review, content_review, AND
    media_review human gates for items whose priority_score clears
    `min_score`, running them straight through to APPROVED - and, if
    `auto_send` is also on, straight through an actual WhatsApp send (see
    agents/auto_mode.py's module docstring for why this is a separate
    toggle, off by default, rather than bundled into `enabled`). Turning
    `enabled` on also sweeps every item currently sitting in Needs
    Review/Content Review/Media Review, in the background, so it applies to
    your existing backlog too, not just items fetched from here on -
    including any already-generated poster/reel sitting in Media Review
    that clears the score threshold, which this sweep will now auto-approve
    (and auto-send, if that's on) without you having looked at it first."""
    was_enabled = get_auto_mode_settings(db, brand.id).enabled
    is_enabled = enabled == "on"
    set_auto_mode_settings(
        db,
        brand.id,
        enabled=is_enabled,
        min_score=max(0.0, min(1.0, min_score)),
        format_=format,
        auto_send=auto_send == "on",
    )

    if is_enabled and not was_enabled:
        pending_ids = [
            row.id
            for row in db.query(ContentItem.id)
            .filter(
                ContentItem.brand_kit_id == brand.id,
                ContentItem.stage.in_([Stage.ANALYZED, Stage.DRAFTED, Stage.MEDIA_GENERATED]),
            )
            .all()
        ]
        run_in_background(_sweep_auto_mode, pending_ids)

    return RedirectResponse(url="/board", status_code=303)


@router.post("/{content_item_id}/poster-template")
def update_poster_template(
    content_item_id: str,
    poster_template: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Lets the user override the Content Writer's inferred poster_template
    before generation - poster_content itself stays as written, only the
    layout it's rendered into changes. Only meaningful pre-generation
    (drafted column); regenerating the poster after this re-reads whichever
    template is saved here at that time."""
    content_item = _accessible_content_item(db, user, content_item_id)
    if content_item and content_item.poster_template in TEMPLATE_CHOICES:
        if poster_template in TEMPLATE_CHOICES:
            content_item.poster_template = poster_template
            db.commit()
    return RedirectResponse(url="/board", status_code=303)


@router.post("/{content_item_id}/reel-template")
def update_reel_template(
    content_item_id: str,
    reel_template: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Poster-template's reel equivalent - overrides the Content Writer's
    inferred explainer_influencer/faceless/animated_contextual choice before
    generation. reel_script/character_description stay as written; only
    which visual style the shot-listing step aims for changes.

    Also usable from an already-generated reel (Media Review card) to try a
    different style - clears the cached reel_scenes shot list so the next
    generation rebuilds it against the new template instead of silently
    reusing the old template's scenes (reel_editor/graph.py's
    _build_shotlist returns the cached list unconditionally otherwise)."""
    content_item = _accessible_content_item(db, user, content_item_id)
    if content_item and content_item.reel_template in REEL_TEMPLATE_CHOICES:
        if reel_template in REEL_TEMPLATE_CHOICES:
            content_item.reel_template = reel_template
            content_item.reel_scenes = None
            db.commit()
    return RedirectResponse(url="/board", status_code=303)


@router.post("/{content_item_id}/send-whatsapp")
def send_whatsapp(
    content_item_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Pushes this post's caption + latest poster/reel to the WhatsApp
    recipient configured on the Brand Kit page, via Botsab. Backgrounded:
    the upload/send calls are outbound HTTP to a third-party server and can
    stall for the full client timeout on a slow connection (observed live -
    a multi-second httpx.WriteTimeout blocked the request), so this behaves
    like every other slow board action now - immediate response, loader on
    the card, error surfaced via the same last_error/Retry flow."""
    if not _accessible_content_item(db, user, content_item_id) or not _start_processing(content_item_id):
        return RedirectResponse(url="/board?error=Not found", status_code=303)

    def _work(db: Session, content_item: ContentItem) -> None:
        try:
            send_content_item(db, content_item)
        except WhatsAppSendError:
            raise
        except Exception as exc:
            raise WhatsAppSendError(f"WhatsApp send failed: {exc}") from exc

    run_in_background(_run_with_processing_state, content_item_id, _work)
    return RedirectResponse(url="/board", status_code=303)


@router.post("/{content_item_id}/send-postiz")
def send_postiz(
    content_item_id: str,
    schedule_at: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Pushes this post's caption + latest poster/reel to every Postiz
    channel configured on the Brand Kit page. schedule_at is an optional
    ISO-8601 UTC timestamp (blank = publish now) - the board's JS converts
    the datetime-local picker's local wall-clock value to UTC before
    submitting, since a naive value here would otherwise be misread by
    Postiz as already being UTC. Backgrounded for the same reason
    send_whatsapp is - outbound HTTP to a third-party server, real
    upload+multi-channel POST latency."""
    if not _accessible_content_item(db, user, content_item_id) or not _start_processing(content_item_id):
        return RedirectResponse(url="/board?error=Not found", status_code=303)

    parsed_schedule = None
    if schedule_at:
        try:
            parsed_schedule = datetime.fromisoformat(schedule_at)
        except ValueError:
            pass

    def _work(db: Session, content_item: ContentItem) -> None:
        try:
            send_postiz_content_item(db, content_item, schedule_at=parsed_schedule)
        except PostizSendError:
            raise
        except Exception as exc:
            raise PostizSendError(f"Postiz send failed: {exc}") from exc

    run_in_background(_run_with_processing_state, content_item_id, _work)
    return RedirectResponse(url="/board", status_code=303)


@router.post("/{content_item_id}/postiz-posts/{postiz_post_id}/cancel")
def cancel_postiz_post(
    content_item_id: str,
    postiz_post_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Deletes one still-pending scheduled Postiz post (one row/channel) -
    the card drops out of the Scheduled column and back into Approved once
    it has no pending rows left, since that column is derived live from
    PostizPost, not a stage of its own (see show_board)."""
    content_item = _accessible_content_item(db, user, content_item_id)
    if not content_item:
        return RedirectResponse(url="/board?error=Not found", status_code=303)
    post = (
        db.query(PostizPost)
        .filter(PostizPost.content_item_id == content_item.id, PostizPost.postiz_post_id == postiz_post_id)
        .first()
    )
    if not post:
        return RedirectResponse(url="/board?error=Not found", status_code=303)

    brand_kit = db.get(BrandKit, content_item.brand_kit_id)
    client = get_postiz_client(brand_kit)
    try:
        if client:
            client.delete_post(postiz_post_id)
    except PostizError as exc:
        return RedirectResponse(url=f"/board?error=Couldn't cancel in Postiz: {exc}", status_code=303)

    db.delete(post)
    db.commit()
    return RedirectResponse(url="/board", status_code=303)


_AUTO_SCHEDULE_ITEM_DELAY_SECONDS = 5
# A gentle pace between items, not a guarantee of staying under Postiz's
# documented 30 req/hour self-hosted cap (each item is 1-2 calls, so a large
# batch can still exceed it - failures surface per-card via last_error the
# same as any other board action, and re-running Auto-schedule only retries
# items that don't already have a PostizPost row, so it's a safe retry).


def _run_auto_schedule(pairs: list[tuple[str, datetime]]) -> None:
    for i, (content_item_id, slot) in enumerate(pairs):
        db = SessionLocal()
        try:
            content_item = db.get(ContentItem, content_item_id)
            if not content_item or content_item.stage != Stage.APPROVED:
                continue
            content_item.is_processing = True
            content_item.last_error = None
            db.commit()
            try:
                send_postiz_content_item(db, content_item, schedule_at=slot)
            except Exception as exc:
                logger.exception("Auto-schedule send failed for %s", content_item_id)
                content_item.last_error = str(exc)[:2000]
            finally:
                content_item.is_processing = False
                db.commit()
        finally:
            db.close()
        if i < len(pairs) - 1:
            time.sleep(_AUTO_SCHEDULE_ITEM_DELAY_SECONDS)


@router.post("/auto-schedule")
def auto_schedule(
    range_preset: str = Form("week"),
    start_date: str = Form(""),
    end_date: str = Form(""),
    posts_per_day: int = Form(1),
    day_start_hour: float = Form(9.0),
    day_end_hour: float = Form(21.0),
    tz_offset_minutes: int = Form(0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """Bulk-schedules every Approved item that isn't already pending a
    Postiz schedule, oldest-approved-first, one per slot across the chosen
    date range at posts_per_day per day. Runs as one sequential background
    job (see _run_auto_schedule) rather than one run_in_background task per
    item - items are spaced out to be gentle on Postiz's rate limit instead
    of firing the whole batch at once."""
    today = datetime.now(timezone.utc).date()
    if range_preset == "2weeks":
        start, end = today, today + timedelta(days=13)
    elif range_preset == "custom":
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
        except ValueError:
            return RedirectResponse(url="/board?error=Invalid date range", status_code=303)
    else:
        start, end = today, today + timedelta(days=6)
    if end < start:
        return RedirectResponse(url="/board?error=End date is before start date", status_code=303)

    posts_per_day = max(1, min(posts_per_day, 20))
    day_start_hour = max(0.0, min(day_start_hour, 23.0))
    day_end_hour = max(day_start_hour + 0.5, min(day_end_hour, 24.0))

    already_scheduled_ids = {
        row.content_item_id
        for row in db.query(PostizPost.content_item_id)
        .join(ContentItem, ContentItem.id == PostizPost.content_item_id)
        .filter(ContentItem.brand_kit_id == brand.id, PostizPost.scheduled_at.isnot(None))
        .all()
    }
    query = db.query(ContentItem.id).filter(
        ContentItem.brand_kit_id == brand.id, ContentItem.stage == Stage.APPROVED
    )
    if already_scheduled_ids:
        query = query.filter(ContentItem.id.notin_(already_scheduled_ids))
    item_ids = [str(row.id) for row in query.order_by(ContentItem.updated_at.asc()).all()]
    if not item_ids:
        return RedirectResponse(url="/board?error=No unscheduled Approved posts to schedule", status_code=303)

    slots = build_schedule_slots(start, end, posts_per_day, day_start_hour, day_end_hour, tz_offset_minutes)
    if not slots:
        return RedirectResponse(url="/board?error=No time slots in that range", status_code=303)

    pairs = list(zip(item_ids, slots))
    run_in_background(_run_auto_schedule, pairs)
    return RedirectResponse(
        url=f"/board?error=Scheduling {len(pairs)} post(s) in the background - watch the Scheduled column",
        status_code=303,
    )


@router.post("/{content_item_id}/discard")
def discard(
    content_item_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Direct discard for cards NOT sitting at a pending interrupt
    (researched, approved) - those graph threads have already either
    finished or errored out, so resume_content_item's Command(resume=...)
    doesn't apply. Every other column (analyzed/drafted/media_generated) is
    genuinely paused at a gate and uses decide(decision="discard") instead,
    to close its graph thread out properly."""
    content_item = _accessible_content_item(db, user, content_item_id)
    if content_item:
        content_item.stage = Stage.DISCARDED
        db.commit()
    return RedirectResponse(url="/board", status_code=303)
