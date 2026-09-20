import logging
import time
from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.agents.auto_mode import (
    DEFAULT_FORMAT,
    DEFAULT_MIN_SCORE,
    VALID_FORMATS,
    get_auto_mode_settings,
    set_auto_mode_settings,
)
from backend.agents.carousel_editor.graph import generate_carousel
from backend.agents.content_writer.graph import write_copy, write_format_fields
from backend.agents.graphic_designer.graph import generate_poster
from backend.agents.json_utils import truncate_on_word_boundary
from backend.agents.graphic_designer.templates import TEMPLATE_CHOICES
from backend.agents.languages import DEFAULT_LANGUAGE, LANGUAGE_CHOICES
from backend.agents.orchestrator import (
    _prebuild_shotlist_for_review,
    auto_advance_content_item,
    create_content_items,
    park_at_media_review,
    restore_content_item,
    resume_content_item,
    rewind_to_content_review,
    rewind_to_needs_review,
    send_to_content_review,
    sync_format_to_graph_state,
)
from backend.agents.researcher.github_angles import extract_angles as extract_github_angles
from backend.agents.researcher.product_angles import extract_angles as extract_product_angles
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
from backend.app.db.models import BrandKit, ContentItem, IngestedEmail, LlmCallLog, MediaAsset, PostizPost, SyncState, User
from backend.app.db.session import SessionLocal, get_db
from backend.app.integrations.botsab.send import WhatsAppSendError, send_content_item
from backend.app.integrations.github.client import GithubError
from backend.app.integrations.github.source import fetch_grounding_text, get_github_client
from backend.app.integrations.gmail.fetch import get_last_fetch_epoch, get_search_query, set_last_fetch_epoch, set_search_query
from backend.app.integrations.gmail.oauth import GmailNotConnected
from backend.app.integrations.postiz.auto_schedule import build_schedule_slots
from backend.app.integrations.postiz.client import PostizError
from backend.app.integrations.postiz.send import PostizSendError, get_postiz_client
from backend.app.integrations.postiz.send import send_content_item as send_postiz_content_item
from backend.app.integrations.woocommerce.source import build_grounding_text, list_brand_products, primary_image_url
from backend.app.llm.image_provider import IMAGE_MODEL_LABELS, get_image_model_key, set_image_model_key
from backend.app.llm.video_provider import VIDEO_MODEL_CHOICES
from backend.app.storage.local_disk import get_storage_backend
from backend.app.worker.background import run_in_background
from backend.app.worker.cleanup import get_retention_days, set_retention_days
from backend.app.worker.pipeline import fetch_pending_email_ids, process_pending_emails
from backend.app.worker.rss_pipeline import run_rss_fetch

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

    # Carousel items hold N "carousel_slide" MediaAsset rows instead of one -
    # the poster/reel query above's "newest matching asset_type" logic
    # doesn't fit a multi-asset format, so this groups by slide_index
    # instead, taking the newest asset per (item, slide_index) pair (a
    # regenerate inserts a fresh full set of rows, all newer than whatever
    # came before). media_by_item_id's value becomes (list_of_urls,
    # "carousel") for these items - board.html's media_preview macro
    # branches on that shape.
    carousel_item_ids = [i for i in item_ids if format_by_item_id.get(i) == "carousel"]
    if carousel_item_ids:
        slide_assets = (
            db.query(MediaAsset)
            .filter(MediaAsset.content_item_id.in_(carousel_item_ids), MediaAsset.asset_type == "carousel_slide")
            .order_by(MediaAsset.created_at.desc())
            .all()
        )
        slides_by_item: dict = {}
        for asset in slide_assets:
            slides_by_item.setdefault(asset.content_item_id, {}).setdefault(asset.slide_index, asset)
        for content_item_id, idx_map in slides_by_item.items():
            urls = [storage.url_for(idx_map[idx].storage_uri) for idx in sorted(idx_map.keys())]
            media_by_item_id[content_item_id] = (urls, "carousel")

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
            "discard_retention_days": get_retention_days(db, brand.id),
            "reel_video_model_key": get_video_model_key(db, brand.id),
            "video_model_choices": VIDEO_MODEL_CHOICES,
            "image_model_key": get_image_model_key(db, brand.id),
            "image_model_labels": IMAGE_MODEL_LABELS,
            "auto_mode": get_auto_mode_settings(db, brand.id),
            "auto_schedule_running": _schedule_job_already_running(db, brand.id),
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
            "website_products": list_brand_products(brand) if brand.product_catalog_url else [],
            "active_nav": "board",
        },
    )


@router.get("/{content_item_id}/review", response_class=HTMLResponse)
def show_content_review_page(
    content_item_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Dedicated full-page alternative to the Content Review card's dialog,
    for reel-format items specifically - that dialog crams a script editor,
    an up-to-8-row shot-list table, and (for hi/mr) a per-scene narration
    editor into a ~560px-wide modal, the single most content-dense review
    surface in the app. The dialog still exists and still works exactly as
    before (this is additive, not a replacement) - this route is a bigger
    canvas for the same review, one click away via a link on the card."""
    content_item = _accessible_content_item(db, user, content_item_id)
    if not content_item or content_item.stage != Stage.DRAFTED or content_item.format != "reel":
        return RedirectResponse(url="/board", status_code=303)

    email = db.get(IngestedEmail, content_item.source_email_id) if content_item.source_email_id else None
    brand = db.get(BrandKit, content_item.brand_kit_id)

    return templates.TemplateResponse(
        request,
        "board_review.html",
        {
            "item": content_item,
            "email": email,
            "reel_template_choices": REEL_TEMPLATE_CHOICES,
            "accessible_brands": get_accessible_brands(db, user),
            "active_brand": brand,
            "user": user,
            "active_nav": "board",
        },
    )


def _display_key_for_item(db: Session, content_item: ContentItem) -> str:
    """Maps a content item's actual current stage to the column key
    board.html's card macros branch on - the same mapping show_board()
    applies when it buckets items into columns, just for one item instead
    of the whole board. "scheduled" isn't a real Stage value (see
    show_board's own comment on scheduled_items) - an approved item counts
    as scheduled here under the same rule: it has at least one PostizPost
    with a non-null scheduled_at."""
    stage_to_key = {
        Stage.RESEARCHED: "researched",
        Stage.ANALYZED: "analyzed",
        Stage.DRAFTED: "drafted",
        Stage.MEDIA_GENERATED: "media_generated",
        Stage.DISCARDED: "discarded",
    }
    if content_item.stage in stage_to_key:
        return stage_to_key[content_item.stage]
    has_scheduled_post = (
        db.query(PostizPost)
        .filter(PostizPost.content_item_id == content_item.id, PostizPost.scheduled_at.isnot(None))
        .first()
        is not None
    )
    return "scheduled" if has_scheduled_post else "approved"


def _media_for_item(storage, db: Session, content_item: ContentItem):
    """Single-item equivalent of show_board's media_by_item_id bulk query -
    same "newest asset matching the item's current format" rule for
    poster/reel, same per-slide-index grouping for carousel."""
    if content_item.format == "carousel":
        slide_assets = (
            db.query(MediaAsset)
            .filter(MediaAsset.content_item_id == content_item.id, MediaAsset.asset_type == "carousel_slide")
            .order_by(MediaAsset.created_at.desc())
            .all()
        )
        idx_map: dict = {}
        for asset in slide_assets:
            idx_map.setdefault(asset.slide_index, asset)
        if not idx_map:
            return None
        urls = [storage.url_for(idx_map[i].storage_uri) for i in sorted(idx_map.keys())]
        return (urls, "carousel")

    asset = (
        db.query(MediaAsset)
        .filter(MediaAsset.content_item_id == content_item.id, MediaAsset.asset_type == content_item.format)
        .order_by(MediaAsset.created_at.desc())
        .first()
    )
    return (storage.url_for(asset.storage_uri), asset.asset_type) if asset else None


@router.get("/{content_item_id}/card-detail", response_class=HTMLResponse)
def show_card_detail(
    content_item_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The lazy-loaded counterpart to card_face in board_card_macros.html -
    fetched by openCard() in board.html only once a card is actually
    clicked, rather than every card's full detail (script editors,
    shot-list tables, forms) being pre-rendered into the initial /board
    page load for every item in every column. Returns just the dialog's
    inner HTML fragment, not a full page."""
    content_item = _accessible_content_item(db, user, content_item_id)
    if not content_item:
        return HTMLResponse('<p class="muted">Not found, or you don\'t have access to it.</p>', status_code=404)

    key = _display_key_for_item(db, content_item)
    email = db.get(IngestedEmail, content_item.source_email_id) if content_item.source_email_id else None
    brand = db.get(BrandKit, content_item.brand_kit_id)
    storage = get_storage_backend()
    media = _media_for_item(storage, db, content_item)
    cost = _cost_by_item_id(db, [content_item.id]).get(content_item.id)
    posts = None
    if key == "scheduled":
        posts = (
            db.query(PostizPost)
            .filter(PostizPost.content_item_id == content_item.id, PostizPost.scheduled_at.isnot(None))
            .order_by(PostizPost.scheduled_at.asc())
            .all()
        )

    return templates.TemplateResponse(
        request,
        "board_card_fragment.html",
        {
            "item": content_item,
            "key": key,
            "email": email,
            "media": media,
            "cost": cost,
            "posts": posts,
            "template_choices": TEMPLATE_CHOICES,
            "reel_template_choices": REEL_TEMPLATE_CHOICES,
            "language_choices": LANGUAGE_CHOICES,
            "default_language": brand.default_language or DEFAULT_LANGUAGE,
            "combined_drafting": brand.combined_drafting,
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


@router.post("/fetch-rss")
def fetch_rss_now(
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """RSS sibling of fetch_now - unlike Gmail there's no "not connected"
    setup error worth blocking on (a dead/malformed feed just gets skipped,
    logged, per feed), so the whole fetch+triage backgrounds in one call."""
    run_in_background(run_rss_fetch, brand.id)
    return RedirectResponse(url="/board?error=Fetching RSS feeds in the background", status_code=303)


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


def _run_github_draft(
    brand_kit_id, repo_full_name: str, repo_url: str, grounding_text: str, feature_description: str, extra_context: str
) -> None:
    db = SessionLocal()
    try:
        angles = extract_github_angles(
            db, brand_kit_id, repo_full_name, repo_url, grounding_text, feature_description, extra_context
        )
    finally:
        db.close()

    for angle in angles:
        angle["full_text"] = grounding_text
    create_content_items(SessionLocal, brand_kit_id, angles, source_email_id=None)


@router.post("/draft-github-posts")
def draft_github_posts(
    repo_full_name: str = Form(...),
    feature_description: str = Form(...),
    extra_context: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """The GitHub-source sibling of "Fetch now" - instead of an inbox,
    grounds drafts in a real repo's README/docs/commit history plus what
    the user says they just built (see integrations/github/source.py,
    researcher/github_angles.py). Mirrors _fetch_and_redirect's split: the
    GitHub API calls (fetch_grounding_text) are fast and worth blocking on
    (a bad token/permissions - the kind of setup error the user needs to
    see immediately - would otherwise fail silently in a background thread
    with no way to surface it, since there's no ContentItem yet to attach
    a last_error to); only the LLM angle-extraction call backgrounds."""
    if not any(r["full_name"] == repo_full_name for r in brand.github_repos):
        return RedirectResponse(url="/board?error=Pick a repo configured on the Brand Kit page", status_code=303)

    client = get_github_client(brand)
    try:
        grounding_text = fetch_grounding_text(client, repo_full_name)
    except GithubError as exc:
        return RedirectResponse(url=f"/board?error=GitHub error: {exc}", status_code=303)
    if not grounding_text.strip():
        return RedirectResponse(
            url="/board?error=No README/docs/commits found for that repo - nothing to draft from",
            status_code=303,
        )

    repo_url = f"https://github.com/{repo_full_name}"
    run_in_background(
        _run_github_draft, brand.id, repo_full_name, repo_url, grounding_text, feature_description, extra_context
    )
    return RedirectResponse(url="/board?error=Drafting posts from GitHub in the background", status_code=303)


def _run_website_draft(
    brand_kit_id,
    product_name: str,
    product_url: str,
    grounding_text: str,
    image_url: str | None,
    extra_context: str,
    format_override: str | None = None,
    language_override: str | None = None,
) -> None:
    db = SessionLocal()
    try:
        angles = extract_product_angles(db, brand_kit_id, product_name, product_url, grounding_text, extra_context)
    finally:
        db.close()

    for angle in angles:
        angle["full_text"] = grounding_text
    started_ids = create_content_items(
        SessionLocal,
        brand_kit_id,
        angles,
        source_email_id=None,
        format_override=format_override,
        language_override=language_override,
    )

    if not image_url or not started_ids:
        return
    try:
        response = httpx.get(image_url, timeout=30)
        response.raise_for_status()
        image_bytes = response.content
    except Exception:
        logger.exception("Could not download product photo from %s", image_url)
        return

    # One MediaAsset(asset_type="product_photo") per created item - the
    # hook generate_poster/generate_reel check for to use the real photo
    # instead of generating anything (see graphic_designer/graph.py,
    # reel_editor/graph.py).
    storage = get_storage_backend()
    db = SessionLocal()
    try:
        for content_item_id in started_ids:
            storage_uri = storage.save(image_bytes, f"{content_item_id}-product.jpg")
            db.add(
                MediaAsset(
                    content_item_id=content_item_id,
                    asset_type="product_photo",
                    storage_uri=storage_uri,
                    cost_usd=0.0,
                )
            )
        db.commit()
    finally:
        db.close()


@router.post("/draft-from-website")
def draft_from_website(
    product_id: str = Form(...),
    extra_context: str = Form(""),
    format: str = Form(""),
    language: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """The product-catalog sibling of draft_github_posts - grounds drafts
    in a real product's own listing (name/category/price/description) from
    the brand's WooCommerce store (see integrations/woocommerce/,
    researcher/product_angles.py). Live-fetches the product list and
    matches by id rather than trusting a client-supplied product blob -
    same fast-fail-before-backgrounding split as draft_github_posts (a bad
    catalog URL surfaces immediately, not silently in a background
    thread).

    format/language are optional per-batch overrides ("" means "use the
    brand's own default") - the only way to get a reel or non-default
    language out of this route for a combined_drafting brand, since that
    skips the per-item review step where format/language are normally
    chosen (see orchestrator.py's create_content_items/_analytical_node)."""
    format_override = format if format in VALID_FORMATS else None
    language_override = language if language in LANGUAGE_CHOICES else None
    products = list_brand_products(brand)
    product = next((p for p in products if str(p.get("id")) == product_id), None)
    if not product:
        return RedirectResponse(
            url="/board?error=Couldn't find that product - check the catalog URL on the Brand Kit page",
            status_code=303,
        )

    grounding_text = build_grounding_text(product)
    if not grounding_text.strip():
        return RedirectResponse(url="/board?error=No description found for that product", status_code=303)

    run_in_background(
        _run_website_draft,
        brand.id,
        product.get("name", ""),
        product.get("permalink", ""),
        grounding_text,
        primary_image_url(product),
        extra_context,
        format_override,
        language_override,
    )
    return RedirectResponse(url="/board?error=Drafting posts from the catalog in the background", status_code=303)


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
            return content_item.format in (Format.POSTER, Format.REEL, Format.CAROUSEL)
        return decision == "regenerate"
    if content_item.stage == Stage.MEDIA_GENERATED:
        return decision == "regenerate"
    return False


def _resume_with_error_handling(content_item_id: str, payload: dict) -> None:
    """resume_content_item wrapped in _run_with_processing_state's
    catch-and-record-to-last_error behavior - NOT itself about threading
    (despite most of its callers routing it through run_in_background; see
    decide/approve_all/discard_all below for direct, synchronous callers).

    Needed because resume_content_item has no exception handling of its own
    and LangGraph's Command(resume=...) doesn't always land back on the
    interrupt it looks like it should: found live, a content item whose
    checkpoint was stuck mid-node from an earlier failed generation (same
    "not cleanly resumable" gotcha retry_poster/retry_reel/retry_content_writer
    exist for) re-attempted that same failing node instead of reaching
    media_review_gate - a real Gemini 429 (depleted prepaid credits) on that
    re-attempt then propagated all the way up through resume_content_item as
    an uncaught exception. For discard_all specifically that crashed the
    whole bulk request with a 500 and silently abandoned every item still
    left in the batch. Catching it here means one broken item just gets its
    last_error set (surfaced as the usual error-box on its card) instead of
    taking the rest of the batch down with it."""
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


def _send_to_content_review_in_background(content_item_id: str, format_: str, language: str) -> None:
    def _work(db: Session, content_item: ContentItem) -> None:
        ok = send_to_content_review(content_item_id, format_, language or None)
        db.expire_all()
        if not ok:
            raise RuntimeError("Could not move this card to Content Review")

    _run_with_processing_state(content_item_id, _work)


def _rewind_to_content_review_in_background(content_item_id: str) -> None:
    def _work(db: Session, content_item: ContentItem) -> None:
        ok = rewind_to_content_review(content_item_id)
        db.expire_all()
        if not ok:
            raise RuntimeError("Could not move this card back to Content Review")

    _run_with_processing_state(content_item_id, _work)


def _rewind_to_needs_review_in_background(content_item_id: str) -> None:
    def _work(db: Session, content_item: ContentItem) -> None:
        ok = rewind_to_needs_review(content_item_id)
        db.expire_all()
        if not ok:
            raise RuntimeError("Could not move this card back to Needs Review")

    _run_with_processing_state(content_item_id, _work)


def _restore_in_background(content_item_id: str) -> None:
    def _work(db: Session, content_item: ContentItem) -> None:
        ok = restore_content_item(content_item_id)
        db.expire_all()
        if not ok:
            raise RuntimeError("Could not restore this discarded item")

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
        run_in_background(_resume_with_error_handling, content_item_id, payload)
    else:
        _resume_with_error_handling(content_item_id, payload)
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
            run_in_background(_resume_with_error_handling, content_item_id, payload)
        else:
            _resume_with_error_handling(content_item_id, payload)

    return RedirectResponse(url="/board", status_code=303)


@router.post("/discard-all")
def discard_all(
    stage: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """Discard's bulk equivalent of approve_all - same column scope
    (analyzed/drafted/media_generated, wherever Approve all appears), same
    per-item resolve-the-pending-interrupt approach via
    resume_content_item(decision="discard"). Always synchronous - discard
    never enters a slow node (_decide_enters_slow_node), so there's no
    background dispatch to mirror here, unlike approve_all.

    Uses _resume_with_error_handling (not resume_content_item directly) -
    found live: one item's checkpoint was stuck mid-node from an earlier
    failed generation (see _resume_with_error_handling's own docstring),
    and resuming it re-attempted that failing generation instead of
    reaching the interrupt discard needed. That raised uncaught, which
    crashed this entire request with a 500 and silently abandoned every
    item still left in item_ids after it - one bad item took the whole
    batch down. _resume_with_error_handling catches that per item instead,
    so the rest of the batch still gets discarded."""
    target_stage = _APPROVE_ALL_STAGES.get(stage)
    if not target_stage:
        return RedirectResponse(url="/board?error=Invalid stage", status_code=303)

    item_ids = [
        str(row.id)
        for row in db.query(ContentItem.id)
        .filter(ContentItem.brand_kit_id == brand.id, ContentItem.stage == target_stage)
        .all()
    ]

    for content_item_id in item_ids:
        db2 = SessionLocal()
        try:
            content_item = db2.get(ContentItem, content_item_id)
            if not content_item or content_item.stage != target_stage:
                continue
        finally:
            db2.close()
        _resume_with_error_handling(content_item_id, {"decision": "discard", "feedback": ""})

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
        _prebuild_shotlist_for_review(db, content_item)
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


@router.post("/{content_item_id}/retry-carousel")
def retry_carousel(
    content_item_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Carousel equivalent of retry_reel/retry_poster above - same "graph
    checkpoint isn't resumable after a mid-node failure" reasoning."""
    if not _accessible_content_item(db, user, content_item_id) or not _start_processing(content_item_id):
        return RedirectResponse(url="/board", status_code=303)

    def _work(db: Session, content_item: ContentItem) -> None:
        generate_carousel(db, content_item)
        content_item.stage = Stage.MEDIA_GENERATED

    run_in_background(_run_with_processing_state, content_item_id, _work)
    return RedirectResponse(url="/board", status_code=303)


def _generate_media_work(target_format: str):
    """Shared by generate_media and approve_and_generate_media below -
    derives the new format's field(s) via write_format_fields if not already
    present (existing copy_text/hashtags are always left untouched) then
    runs the matching media generator.

    Reel is the one exception: if reel_script had to be freshly written here
    (this item skipped the normal Researcher -> Content Writer -> Content
    Review path, where a human always reviews the script before
    generate_reel spends real Veo money on it - see reel_editor/graph.py's
    per-reel cost cap), this stops right after writing it and sends the item
    back to Content Review instead of generating straight through - found
    live: this shortcut wrote a shot script and immediately spent on it in
    the same click, with no chance to fix the script/character description
    first. Poster/carousel stay one-click since their generation cost is
    much lower and doesn't carry the same "already spent, can't undo it"
    risk a bad reel script does."""

    def _work(db: Session, item: ContentItem) -> None:
        if target_format == "poster":
            if not item.poster_headline:
                write_format_fields(db, item, "poster")
            generate_poster(db, item)
        elif target_format == "carousel":
            if not item.carousel_script:
                write_format_fields(db, item, "carousel")
            generate_carousel(db, item)
        else:
            if not item.reel_script:
                write_format_fields(db, item, "reel")
                item_id = item.id
                # rewind_to_content_review manages its own SessionLocal and,
                # critically, re-parks the LangGraph checkpoint at
                # content_review_gate's interrupt (not just a DB field
                # flip) - needed so this card's own Approve/Regenerate/
                # Discard buttons work correctly afterward instead of
                # silently no-op'ing on a stale checkpoint, the same "this
                # item's graph thread already reached END" gotcha
                # park_at_media_review's own docstring describes for this
                # exact generate_media/approve_and_generate_media shortcut.
                # db.expire_all() after it so this function's own `db`/
                # `item` (from _run_with_processing_state) don't overwrite
                # that fresh state with stale pre-rewind values once the
                # caller commits.
                rewind_to_content_review(item_id)
                db.expire_all()
                return
            generate_reel(db, item)
        item.stage = Stage.MEDIA_GENERATED
        db.commit()
        park_at_media_review(item.id, target_format, item.brand_kit_id, item.source_email_id)

    return _work


@router.post("/{content_item_id}/write-copy")
def write_copy_manually(
    content_item_id: str,
    copy_text: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Sets or updates copy_text on an Approved item - either a "write
    myself" item with none yet, or editing already-approved copy."""
    content_item = _accessible_content_item(db, user, content_item_id)
    if not content_item or content_item.stage != Stage.APPROVED:
        return RedirectResponse(url="/board?error=Item not found or not approved", status_code=303)
    content_item.copy_text = copy_text.strip()
    db.commit()
    return RedirectResponse(url="/board", status_code=303)


@router.post("/{content_item_id}/send-to-content-review")
def send_to_content_review_route(
    content_item_id: str,
    format: str = Form(...),
    language: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Fast-forwards a Researched/Analyzed card into Content Review - see
    orchestrator.py's send_to_content_review."""
    if not _accessible_content_item(db, user, content_item_id) or not _start_processing(content_item_id):
        return RedirectResponse(url="/board", status_code=303)
    run_in_background(_send_to_content_review_in_background, content_item_id, format, language)
    return RedirectResponse(url="/board", status_code=303)


@router.post("/{content_item_id}/rewind-to-content-review")
def rewind_to_content_review_route(
    content_item_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Sends a Media Generated/Approved card back to Content Review - see
    orchestrator.py's rewind_to_content_review."""
    if not _accessible_content_item(db, user, content_item_id) or not _start_processing(content_item_id):
        return RedirectResponse(url="/board", status_code=303)
    run_in_background(_rewind_to_content_review_in_background, content_item_id)
    return RedirectResponse(url="/board", status_code=303)


@router.post("/{content_item_id}/rewind-to-needs-review")
def rewind_to_needs_review_route(
    content_item_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Sends a Content Review/Media Review/Approved card back to Needs
    Review - see orchestrator.py's rewind_to_needs_review."""
    if not _accessible_content_item(db, user, content_item_id) or not _start_processing(content_item_id):
        return RedirectResponse(url="/board", status_code=303)
    run_in_background(_rewind_to_needs_review_in_background, content_item_id)
    return RedirectResponse(url="/board", status_code=303)


@router.post("/{content_item_id}/restore")
def restore_route(
    content_item_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Brings a discarded card back to Needs Review (or, for one discarded
    straight out of triage with no brief yet, restarts its fetch+brief run
    first) - see orchestrator.py's restore_content_item. There was
    previously no way to undo a discard at all."""
    if not _accessible_content_item(db, user, content_item_id) or not _start_processing(content_item_id):
        return RedirectResponse(url="/board", status_code=303)
    run_in_background(_restore_in_background, content_item_id)
    return RedirectResponse(url="/board", status_code=303)


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
    if target_format not in ("poster", "reel", "carousel"):
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
    if target_format not in ("poster", "reel", "carousel"):
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


@router.post("/discard-retention")
def update_discard_retention(
    retention_days: int = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """How long a discarded item sticks around before the daily cleanup job
    (worker/cleanup.py) deletes it and its media for good - 0 opts a brand
    out of cleanup entirely (nothing is ever old enough)."""
    set_retention_days(db, brand.id, retention_days)
    return RedirectResponse(url="/board", status_code=303)


@router.post("/reel-video-model")
def update_reel_video_model(
    model_key: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """Which Veo tier the next reel generation uses - see
    llm/video_provider.py's VIDEO_MODEL_CHOICES ("fast", the default, vs
    "lite" for a cheaper/lower-quality opt-in or "standard" at ~5x "fast"'s
    price). Applies to whichever reel gets generated next, same "configure,
    then trigger" pattern as the reel cost cap."""
    set_video_model_key(db, brand.id, model_key)
    return RedirectResponse(url="/board", status_code=303)


@router.post("/poster-image-model")
def update_poster_image_model(
    model_key: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """Which image-gen model every image call this brand makes uses - see
    llm/image_provider.py's IMAGE_MODEL_CHOICES. Covers posters (full-design,
    product-photo-reference, legacy background) AND reel character
    references, not just one of them. Applies to whichever image gets
    generated next, same "configure, then trigger" pattern as the reel
    video model/cost cap."""
    set_image_model_key(db, brand.id, model_key)
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


def _review_redirect(return_to: str | None, content_item_id: str) -> RedirectResponse:
    """Sends the user back to wherever they were editing from - the board's
    own Content Review dialog, or (for reel items) the dedicated full-page
    review at /board/{id}/review. Only ever the one known-safe path for this
    exact item, never an arbitrary posted URL, so this can't be used as an
    open redirect."""
    if return_to == "review":
        return RedirectResponse(url=f"/board/{content_item_id}/review", status_code=303)
    return RedirectResponse(url="/board", status_code=303)


@router.post("/{content_item_id}/reel-template")
def update_reel_template(
    content_item_id: str,
    reel_template: str = Form(...),
    return_to: str | None = Form(None),
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
    return _review_redirect(return_to, content_item_id)


@router.post("/{content_item_id}/edit-reel-script")
def edit_reel_script(
    content_item_id: str,
    reel_script: str = Form(...),
    character_description: str = Form(""),
    return_to: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Saves a user-edited reel_script/character_description - the
    higher-level narrative and visual-subject description the shot-listing
    step turns into scenes. Previously read-only in every language - only
    the per-scene narration/music built FROM whatever script Content Writer
    wrote was ever editable (and only for hi/mr), with no way to fix the
    story itself before that shot list gets built. Unlike that hi/mr-only
    narration editing, this is open to every language, same reasoning
    copy_text editing already gets - it's your story to revise, not just an
    AI-translation-accuracy check. Every language stage: Content Review
    only (before the shot list/media exist), same as update_reel_template
    just above, whose "clear the cached shot list" reasoning this mirrors
    exactly - reel_scenes was built from the OLD script, so it's cleared
    here too rather than silently regenerating stale scenes against new
    wording."""
    content_item = _accessible_content_item(db, user, content_item_id)
    if not content_item or content_item.stage != Stage.DRAFTED or content_item.format != Format.REEL:
        return RedirectResponse(url="/board?error=Not editable", status_code=303)
    content_item.reel_script = reel_script.strip()
    content_item.character_description = character_description.strip() or None
    content_item.reel_scenes = None
    db.commit()
    return _review_redirect(return_to, content_item_id)


# Hindi/Marathi-only pre-generation text editing (routes below) - AI-drafted
# Marathi/Hindi quality isn't reliable enough yet (user-reported), so for
# these two languages the exact final text that ends up rendered/spoken is
# made directly editable at Content Review, before Approve spends real
# generation cost on it. See orchestrator.py's _prebuild_shotlist_for_review,
# which builds reel_scenes/carousel_slides at drafting time (not lazily at
# generation time, like every other language) specifically so there's
# something here to edit. All three routes below share the same guard: only
# a DRAFTED, hi/mr item is editable.
_TEXT_REVIEW_LANGUAGES = ("hi", "mr")

# poster_content's shape is template-specific (see content_writer/prompts.py's
# POSTER_TEMPLATE_GUIDE) - these are the plain scalar fields per template;
# "facts" (fact_critique) and "highlight_phrases" (trivia) are list fields,
# handled separately below with a fixed max count matching the guide's own
# "up to 3"/"up to 2" caps.
_POSTER_CONTENT_SCALAR_FIELDS = {
    "quote": ["quote_text", "attribution", "citation"],
    "tribute": ["name", "achievement", "quote_text", "tribute_line"],
    "narrative": ["body_text"],
    "fact_critique": ["critique_line"],
    "trivia": ["headline", "body_text"],
    "event": ["event_title", "datetime", "location", "cta"],
}
_MAX_FACTS = 3
_MAX_HIGHLIGHT_PHRASES = 2


@router.post("/{content_item_id}/edit-poster-content")
async def edit_poster_content(
    content_item_id: str,
    request: Request,
    poster_headline: str = Form(""),
    copy_text: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Saves user-edited poster_headline/poster_content - a plain field
    update, no LLM call, no stage change. board.html renders one form field
    per _POSTER_CONTENT_SCALAR_FIELDS[item.poster_template] plus the
    template's list fields (facts_stat_N/facts_source_N,
    highlight_phrases_N) - read generically here via request.form() since
    the field set varies by template.

    copy_text (the social caption) is included here too, alongside
    poster_content (what's actually drawn on the poster image) - found
    live: these are two separate fields from the same Content Writer draft,
    but copy_text was only ever editable much later at the Approved stage,
    by which point poster_content is no longer editable at all (the image
    is already generated). A text-review user fixing a fact/number in
    poster_content had no way to fix the same thing in copy_text at the
    same time, so the two could drift out of sync with no single place to
    reconcile them. `copy_text` is Form(None), not Form("") - None means
    the field wasn't in the submitted form at all (only board.html's
    text-review form sends it), so this route still works if some other
    future caller posts without it, rather than always blanking the
    caption to empty."""
    content_item = _accessible_content_item(db, user, content_item_id)
    if not content_item or content_item.stage != Stage.DRAFTED or content_item.language not in _TEXT_REVIEW_LANGUAGES:
        return RedirectResponse(url="/board?error=Not editable", status_code=303)

    form = await request.form()
    template = content_item.poster_template
    content = dict(content_item.poster_content or {})

    for field in _POSTER_CONTENT_SCALAR_FIELDS.get(template, []):
        if field in form:
            content[field] = str(form.get(field, "")).strip()

    if template == "fact_critique":
        facts = []
        for i in range(_MAX_FACTS):
            stat = str(form.get(f"facts_stat_{i}", "")).strip()
            source = str(form.get(f"facts_source_{i}", "")).strip()
            if stat:
                facts.append({"stat": stat, "source": source})
        content["facts"] = facts

    if template == "trivia":
        phrases = [str(form.get(f"highlight_phrases_{i}", "")).strip() for i in range(_MAX_HIGHLIGHT_PHRASES)]
        content["highlight_phrases"] = [p for p in phrases if p]

    content_item.poster_content = content
    content_item.poster_headline = truncate_on_word_boundary(poster_headline.strip(), 300)
    if copy_text is not None:
        content_item.copy_text = copy_text.strip()
    db.commit()
    return RedirectResponse(url="/board", status_code=303)


@router.post("/{content_item_id}/edit-reel-scenes")
async def edit_reel_scenes(
    content_item_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Saves user-edited per-scene narration and music direction - the exact
    text Veo will speak, and the plain-language music/mood direction folded
    into its generation prompt (see reel_editor/graph.py). Each scene's
    visual "description" is an English generation prompt regardless of the
    post's language, not user-facing content text, so it stays read-only -
    same for the deterministic "timeframe"/"text_on_visual" fields (see
    reel_editor/prompts.py's docstring), which aren't user text at all."""
    content_item = _accessible_content_item(db, user, content_item_id)
    if not content_item or content_item.stage != Stage.DRAFTED or content_item.language not in _TEXT_REVIEW_LANGUAGES:
        return RedirectResponse(url="/board?error=Not editable", status_code=303)

    form = await request.form()
    scenes = [dict(scene) for scene in (content_item.reel_scenes or [])]
    for i, scene in enumerate(scenes):
        narration_key, music_key = f"narration_{i}", f"music_{i}"
        if narration_key in form:
            scene["narration"] = str(form.get(narration_key, "")).strip()
        if music_key in form:
            scene["music"] = str(form.get(music_key, "")).strip()
    content_item.reel_scenes = scenes
    db.commit()
    return _review_redirect(str(form.get("return_to", "")) or None, content_item_id)


@router.post("/{content_item_id}/edit-carousel-slides")
async def edit_carousel_slides(
    content_item_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Saves user-edited per-slide headline/body_text - the exact text the
    image model will render onto each slide. Unlike the poster/reel edit
    routes above, this is NOT restricted to hi/mr - carousel slide wording
    often benefits from a human tweak regardless of language (user
    request), and carousel_slides is now pre-built at drafting time for
    every language (see orchestrator.py's _prebuild_shotlist_for_review)."""
    content_item = _accessible_content_item(db, user, content_item_id)
    if not content_item or content_item.stage != Stage.DRAFTED:
        return RedirectResponse(url="/board?error=Not editable", status_code=303)

    form = await request.form()
    slides = [dict(slide) for slide in (content_item.carousel_slides or [])]
    for i, slide in enumerate(slides):
        headline_key, body_key = f"headline_{i}", f"body_text_{i}"
        if headline_key in form:
            slide["headline"] = str(form.get(headline_key, "")).strip()
        if body_key in form:
            slide["body_text"] = str(form.get(body_key, "")).strip()
    content_item.carousel_slides = slides
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
    upload+multi-channel POST latency.

    A schedule_at that's present but fails to parse is a hard error, not a
    silent fall-through to "send now" - found live: a reschedule attempt
    landed with schedule_at empty/unparseable server-side (exact client
    cause unconfirmed) and got silently published immediately instead of
    at the intended time, which is a much worse failure mode than just
    rejecting the request. Parsed before _start_processing so a bad value
    redirects cleanly instead of leaving the card stuck "processing"."""
    if not _accessible_content_item(db, user, content_item_id):
        return RedirectResponse(url="/board?error=Not found", status_code=303)

    parsed_schedule = None
    if schedule_at:
        try:
            parsed_schedule = datetime.fromisoformat(schedule_at)
        except ValueError:
            logger.warning(
                "send-postiz got an unparseable schedule_at %r for %s - refusing rather than sending now",
                schedule_at, content_item_id,
            )
            return RedirectResponse(
                url="/board?error=Couldn't read that schedule time - please pick it again and retry",
                status_code=303,
            )

    if not _start_processing(content_item_id):
        return RedirectResponse(url="/board?error=Not found", status_code=303)

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


_AUTO_SCHEDULE_ITEM_DELAY_SECONDS = 240
# Paced to Postiz's documented 30 req/hour self-hosted cap - each item costs
# 1-2 calls (upload + create), so 240s/item keeps a full run to ~15 items/
# hour, comfortably inside that budget (an earlier 5s pace hit 429s almost
# immediately). A 429 specifically also gets one retry in _send_one after
# backing off by Postiz's own Retry-After value, since that failure is
# caused by our own pace rather than anything wrong with the post itself.


def _send_one(db: Session, content_item: ContentItem, slot: datetime | None) -> None:
    try:
        send_postiz_content_item(db, content_item, schedule_at=slot)
    except PostizSendError as exc:
        if exc.retry_after is None:
            raise
        wait = exc.retry_after + 5
        logger.info("Postiz rate-limited - backing off %.0fs before retrying %s", wait, content_item.id)
        time.sleep(wait)
        send_postiz_content_item(db, content_item, schedule_at=slot)


def _resolve_schedule_range(
    range_preset: str, start_date: str, end_date: str, posts_per_day: int, day_start_hour: float, day_end_hour: float
) -> tuple:
    """Shared date-range/pacing parsing for auto_schedule and
    reschedule_all - returns (start, end, posts_per_day, day_start_hour,
    day_end_hour) or raises ValueError with a user-facing message."""
    today = datetime.now(timezone.utc).date()
    if range_preset == "2weeks":
        start, end = today, today + timedelta(days=13)
    elif range_preset == "custom":
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
        except ValueError:
            raise ValueError("Invalid date range")
    else:
        start, end = today, today + timedelta(days=6)
    if end < start:
        raise ValueError("End date is before start date")

    posts_per_day = max(1, min(posts_per_day, 20))
    day_start_hour = max(0.0, min(day_start_hour, 23.0))
    day_end_hour = max(day_start_hour + 0.5, min(day_end_hour, 24.0))
    return start, end, posts_per_day, day_start_hour, day_end_hour


# Guards against two overlapping auto-schedule/reschedule-all background
# jobs running for the same brand at once - found live: clicking
# "Auto-schedule" more than once (each click's own redirect message is easy
# to miss/not realize means "still working, 4 minutes per item, could take
# over an hour" - so a second click looks like a reasonable retry) starts a
# SECOND independent background loop with its own snapshot of "which items
# still need scheduling", computed before the first loop has caught up -
# both loops then genuinely scheduled the same items, producing duplicate
# PostizPost rows (and duplicate real posts once they'd actually gone out).
# A brand-scoped SyncState row acts as the lock: present and recent enough
# means a job is (or was very recently) running. The staleness window
# covers a hard process kill leaving the lock stuck forever with no
# background thread left alive to ever clear it (background threads don't
# survive a restart - see docs/architecture.md's "Backgrounded board
# actions").
_SCHEDULE_LOCK_KEY = "schedule_job_lock"
_SCHEDULE_LOCK_STALE_SECONDS = 2 * 3600  # well past any realistic run (~20 items x 240s ~= 80min)


def _schedule_job_already_running(db: Session, brand_id) -> bool:
    row = db.get(SyncState, (_SCHEDULE_LOCK_KEY, brand_id))
    if not row:
        return False
    try:
        started_at = datetime.fromisoformat(row.value)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - started_at).total_seconds() < _SCHEDULE_LOCK_STALE_SECONDS


def _set_schedule_job_lock(db: Session, brand_id) -> None:
    row = db.get(SyncState, (_SCHEDULE_LOCK_KEY, brand_id))
    now_iso = datetime.now(timezone.utc).isoformat()
    if row:
        row.value = now_iso
    else:
        db.add(SyncState(key=_SCHEDULE_LOCK_KEY, brand_kit_id=brand_id, value=now_iso))
    db.commit()


def _clear_schedule_job_lock(brand_id) -> None:
    db = SessionLocal()
    try:
        row = db.get(SyncState, (_SCHEDULE_LOCK_KEY, brand_id))
        if row:
            db.delete(row)
            db.commit()
    finally:
        db.close()


def _run_auto_schedule(pairs: list[tuple[str, datetime]], brand_id) -> None:
    try:
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
                    _send_one(db, content_item, slot)
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
    finally:
        _clear_schedule_job_lock(brand_id)


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
    try:
        start, end, posts_per_day, day_start_hour, day_end_hour = _resolve_schedule_range(
            range_preset, start_date, end_date, posts_per_day, day_start_hour, day_end_hour
        )
    except ValueError as exc:
        return RedirectResponse(url=f"/board?error={exc}", status_code=303)

    if _schedule_job_already_running(db, brand.id):
        return RedirectResponse(
            url="/board?error=Auto-schedule is already running for this brand - it paces "
            f"~1 post every {_AUTO_SCHEDULE_ITEM_DELAY_SECONDS // 60} minutes, so a big batch can take a "
            "while. Wait for it to finish (watch the Scheduled column) before starting another run.",
            status_code=303,
        )

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
    _set_schedule_job_lock(db, brand.id)
    run_in_background(_run_auto_schedule, pairs, brand.id)
    return RedirectResponse(
        url=f"/board?error=Scheduling {len(pairs)} post(s) in the background - watch the Scheduled column",
        status_code=303,
    )


def _run_reschedule_all(pairs: list[tuple[str, datetime]], brand_id) -> None:
    """Per item: cancel every existing Postiz post (best-effort delete on
    Postiz's side, always removed locally so a dead/unreachable old post_id
    can't block the resend), then send fresh at the new slot via _send_one -
    same per-item pacing as _run_auto_schedule, since deletes count against
    Postiz's rate limit too."""
    try:
        for i, (content_item_id, slot) in enumerate(pairs):
            db = SessionLocal()
            try:
                content_item = db.get(ContentItem, content_item_id)
                if not content_item:
                    continue
                content_item.is_processing = True
                content_item.last_error = None
                db.commit()
                try:
                    brand_kit = db.get(BrandKit, content_item.brand_kit_id)
                    client = get_postiz_client(brand_kit)
                    for post in db.query(PostizPost).filter(PostizPost.content_item_id == content_item.id).all():
                        try:
                            if client:
                                client.delete_post(post.postiz_post_id)
                        except PostizError:
                            logger.warning(
                                "Couldn't delete old Postiz post %s for %s - removing locally anyway",
                                post.postiz_post_id, content_item_id,
                            )
                        db.delete(post)
                    db.commit()
                    _send_one(db, content_item, slot)
                except Exception as exc:
                    logger.exception("Reschedule-all failed for %s", content_item_id)
                    content_item.last_error = str(exc)[:2000]
                finally:
                    content_item.is_processing = False
                    db.commit()
            finally:
                db.close()
            if i < len(pairs) - 1:
                time.sleep(_AUTO_SCHEDULE_ITEM_DELAY_SECONDS)
    finally:
        _clear_schedule_job_lock(brand_id)


@router.post("/reschedule-all")
def reschedule_all(
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
    """The "start over" sibling of auto_schedule - instead of picking up
    unscheduled Approved items, this cancels every existing Postiz post for
    this brand's currently-Scheduled items and re-sends them fresh across
    the chosen range. Built for cleaning up a bad batch (e.g. the
    schedule_at=NULL bug where a reschedule silently published immediately)
    rather than a routine action. If there are more scheduled items than
    slots in the chosen range, only the earliest-scheduled ones (up to the
    slot count) are touched - widen the range or raise posts/day to cover
    everything in one run."""
    try:
        start, end, posts_per_day, day_start_hour, day_end_hour = _resolve_schedule_range(
            range_preset, start_date, end_date, posts_per_day, day_start_hour, day_end_hour
        )
    except ValueError as exc:
        return RedirectResponse(url=f"/board?error={exc}", status_code=303)

    if _schedule_job_already_running(db, brand.id):
        return RedirectResponse(
            url="/board?error=A schedule job is already running for this brand - wait for it to "
            "finish (watch the Scheduled column) before starting another run.",
            status_code=303,
        )

    rows = (
        db.query(ContentItem.id, func.min(PostizPost.scheduled_at).label("earliest"))
        .join(PostizPost, PostizPost.content_item_id == ContentItem.id)
        .filter(ContentItem.brand_kit_id == brand.id, PostizPost.scheduled_at.isnot(None))
        .group_by(ContentItem.id)
        .order_by(func.min(PostizPost.scheduled_at))
        .all()
    )
    item_ids = [str(row.id) for row in rows]
    if not item_ids:
        return RedirectResponse(url="/board?error=No scheduled posts to reschedule", status_code=303)

    slots = build_schedule_slots(start, end, posts_per_day, day_start_hour, day_end_hour, tz_offset_minutes)
    if not slots:
        return RedirectResponse(url="/board?error=No time slots in that range", status_code=303)

    pairs = list(zip(item_ids, slots))
    _set_schedule_job_lock(db, brand.id)
    run_in_background(_run_reschedule_all, pairs, brand.id)
    return RedirectResponse(
        url=f"/board?error=Rescheduling {len(pairs)} post(s) in the background - old Postiz posts are being cancelled first",
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
