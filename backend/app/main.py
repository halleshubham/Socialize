from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from backend.app.api import (
    routes_account,
    routes_admin,
    routes_auth,
    routes_board,
    routes_brand_kit,
    routes_brands,
    routes_gmail,
    routes_niche,
)
from backend.app.auth.basic import hash_password
from backend.app.auth.brand_deps import NoBrandAccess, get_accessible_brands
from backend.app.auth.deps import NotAuthenticated, get_current_user
from backend.app.config import get_settings
from backend.app.db.models import BrandKit, ContentItem, IngestedEmail, IngestedRssItem, MediaAsset, SyncState, User
from backend.app.db.session import SessionLocal, get_db
from backend.app.middleware.rate_limit import RateLimitMiddleware
from backend.app.worker.scheduler import start_scheduler, stop_scheduler

settings = get_settings()


def bootstrap_admin_user() -> None:
    """Creates the one superadmin user from ADMIN_EMAIL/ADMIN_PASSWORD if no
    user with that email exists yet - the sole account that can grant or
    revoke is_admin on anyone else (see auth/brand_deps.py's
    get_current_superadmin_user). Idempotent, safe to run on every boot.

    Also self-heals an existing deployment: if ADMIN_EMAIL already matched a
    user from before is_superadmin existed (this app previously bootstrapped
    a plain admin here), that row is promoted to superadmin on the next
    boot rather than leaving the deployment with zero superadmins after the
    upgrade - env config is the only lever for this account, never a
    self-serve or in-app action."""
    if not settings.admin_email or not settings.admin_password:
        return
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == settings.admin_email).first()
        if existing:
            if not existing.is_superadmin:
                existing.is_superadmin = True
                existing.is_admin = True
                db.commit()
            return
        db.add(
            User(
                email=settings.admin_email,
                hashed_password=hash_password(settings.admin_password),
                auth_provider="basic",
                is_admin=True,
                is_superadmin=True,
            )
        )
        db.commit()
    finally:
        db.close()


def recover_orphaned_processing_items() -> None:
    """Any ContentItem left with is_processing=True from before this boot
    is guaranteed dead, not just slow - background work runs in an
    in-process ThreadPoolExecutor (app/worker/background.py), which cannot
    survive a process restart, so there is no chance one of these is
    quietly still working. Found live: a reel generation's background
    thread was killed mid-run by an app restart, leaving is_processing=True
    forever with last_error never set (the cleanup code that normally
    clears it never got to run) - the card looked like it was still
    processing indefinitely. Runs on every boot, not just after a crash,
    since --reload restarts (a code change during local dev) kill
    in-flight background threads exactly the same way a crash would."""
    db = SessionLocal()
    try:
        stuck_items = db.query(ContentItem).filter(ContentItem.is_processing.is_(True)).all()
        for item in stuck_items:
            item.is_processing = False
            item.last_error = (
                "Interrupted by an app restart during generation (a background thread cannot "
                "survive a process restart) - click Retry to resume from the same shot list/draft."
            )
        if stuck_items:
            db.commit()
    finally:
        db.close()


def recover_orphaned_email_claims() -> None:
    """Same reasoning as recover_orphaned_processing_items, for
    orchestrator.py's process_email: it atomically claims an email
    (status "new" -> "processing") before running the Researcher, so two
    overlapping fetches can't both process the same email and create
    duplicate ContentItems. But that claim has the same restart risk - if
    the process is killed mid-run (crash, or a --reload restart in dev)
    after claiming but before the final "processed" flip, the email is
    stuck at "processing" forever and fetch_pending_email_ids's status="new"
    query would never pick it up again, silently dropping it. Any email
    still "processing" at boot is guaranteed orphaned (nothing survives a
    process restart), so it's safe to reset it to "new" for the next fetch
    to pick back up."""
    db = SessionLocal()
    try:
        stuck = (
            db.query(IngestedEmail)
            .filter(IngestedEmail.status == "processing")
            .update({"status": "new"}, synchronize_session=False)
        )
        if stuck:
            db.commit()
    finally:
        db.close()


def recover_orphaned_rss_claims() -> None:
    """Same reasoning as recover_orphaned_email_claims, for
    orchestrator.py's process_rss_batch - it atomically claims a batch of
    entries (status "new" -> "processing") before triaging them."""
    db = SessionLocal()
    try:
        stuck = (
            db.query(IngestedRssItem)
            .filter(IngestedRssItem.status == "processing")
            .update({"status": "new"}, synchronize_session=False)
        )
        if stuck:
            db.commit()
    finally:
        db.close()


def recover_orphaned_schedule_locks() -> None:
    """Same "nothing survives a process restart" reasoning as
    recover_orphaned_processing_items, for routes_board.py's
    auto_schedule/reschedule_all concurrency lock (a SyncState row per
    brand, guarding against two overlapping background schedule jobs
    double-scheduling the same posts - found live on a real brand). Any
    lock row present at boot is guaranteed stale, not just old - clear
    unconditionally rather than waiting out its staleness window."""
    db = SessionLocal()
    try:
        deleted = (
            db.query(SyncState)
            .filter(SyncState.key == routes_board._SCHEDULE_LOCK_KEY)
            .delete(synchronize_session=False)
        )
        if deleted:
            db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap_admin_user()
    recover_orphaned_processing_items()
    recover_orphaned_email_claims()
    recover_orphaned_rss_claims()
    recover_orphaned_schedule_locks()
    start_scheduler()
    yield
    stop_scheduler()


_DEFAULT_SECRET_KEY = "dev-secret-change-me"
if settings.app_env == "production" and settings.app_secret_key == _DEFAULT_SECRET_KEY:
    # Refuses to boot rather than warn-and-continue - this key signs every
    # session cookie, so leaving the shipped default in a reachable
    # deployment means anyone can forge a valid session for any user by
    # just knowing this same public default value. Only enforced for
    # APP_ENV=production (default is "development") so the existing local
    # docker-compose/localhost setup, which never sets APP_ENV, keeps
    # working unchanged - this is a SaaS-readiness prerequisite, not
    # something that should affect current personal/local use.
    raise RuntimeError(
        "APP_SECRET_KEY is still the default value - set a real, random secret in .env before running "
        "with APP_ENV=production. Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )

app = FastAPI(title="Socialize", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret_key,
    # https_only is conditional on APP_ENV, not unconditionally True - this
    # app's own default/local setup is plain http://localhost (docker-
    # compose), and an https_only cookie is silently never sent/accepted
    # over plain HTTP, which would lock out every existing local deployment
    # the moment this shipped. same_site/max_age are safe to apply
    # unconditionally: "lax" doesn't break normal top-level navigation
    # (login redirects, etc.), and a 14-day expiry is just better hygiene
    # than the previous "never expires" default, in every environment.
    https_only=settings.app_env == "production",
    same_site="lax",
    max_age=14 * 24 * 3600,
)
# Added after SessionMiddleware so it wraps outermost (Starlette applies
# middleware in reverse add order) - rejects an over-quota request before
# it reaches session/auth handling at all.
app.add_middleware(RateLimitMiddleware)
app.mount("/static", StaticFiles(directory="backend/app/static"), name="static")


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url="/login", status_code=303)


@app.exception_handler(NoBrandAccess)
async def no_brand_access_handler(request: Request, exc: NoBrandAccess):
    return RedirectResponse(url="/brands/new", status_code=303)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/")
def root():
    return RedirectResponse(url="/board")


@app.get("/media/{filename}")
def media(
    filename: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Auth-gated file serving for generated media (posters, reels, carousel
    slides, brand logos) - this is a specific brand's own generated content,
    not public static assets, so it goes through get_current_user AND an
    explicit brand-ownership check, not just "is logged in."

    Found live (SaaS-readiness review): this route previously only checked
    get_current_user - ANY authenticated user of the app, from ANY brand,
    could fetch ANY other brand's media by requesting its filename directly,
    with nothing but an unguessable UUID-prefixed name standing in for real
    access control. Only a real risk once the app has mutually-untrusted
    tenants (this used to be a personal/small-team tool where every user was
    already trusted), but a hard blocker before opening self-serve signup.
    R2 doesn't have this problem the same way - url_for() there only ever
    generates a presigned URL for an asset the caller's own brand-scoped
    query already confirmed access to (see routes_board.py); this route is
    local_disk's equivalent gate, applied at fetch time instead since a raw
    filename in the URL bypasses whatever query produced it.

    A requested filename is legitimately one of two things (the only two
    things url_for() is ever called on - see routes_board.py and
    routes_brand_kit.py): a MediaAsset.storage_uri (poster/reel/carousel
    slide/character-reference/product-photo, scoped via its content_item's
    brand_kit_id) or a BrandKit.logo_asset_path (scoped directly). Anything
    else - or a real match the caller's brands don't include - 404s, not
    403, so an unauthorized guess can't even confirm the file exists."""
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = Path(settings.local_storage_dir) / filename
    if not path.is_file():
        raise HTTPException(status_code=404)

    accessible_ids = {b.id for b in get_accessible_brands(db, user)}
    asset = db.query(MediaAsset).filter(MediaAsset.storage_uri == filename).first()
    if asset:
        content_item = db.get(ContentItem, asset.content_item_id)
        if not content_item or content_item.brand_kit_id not in accessible_ids:
            raise HTTPException(status_code=404)
    else:
        brand = db.query(BrandKit).filter(BrandKit.logo_asset_path == filename).first()
        if not brand or brand.id not in accessible_ids:
            raise HTTPException(status_code=404)

    return FileResponse(path)


app.include_router(routes_auth.router)
app.include_router(routes_admin.router)
app.include_router(routes_account.router)
app.include_router(routes_brands.router)
app.include_router(routes_niche.router)
app.include_router(routes_brand_kit.router)
app.include_router(routes_gmail.router)
app.include_router(routes_board.router)
