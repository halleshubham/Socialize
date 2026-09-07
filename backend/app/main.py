from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
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
from backend.app.auth.brand_deps import NoBrandAccess
from backend.app.auth.deps import NotAuthenticated, get_current_user
from backend.app.config import get_settings
from backend.app.db.models import ContentItem, IngestedEmail, User
from backend.app.db.session import SessionLocal
from backend.app.worker.scheduler import start_scheduler, stop_scheduler

settings = get_settings()


def bootstrap_admin_user() -> None:
    """Creates the single admin user from ADMIN_EMAIL/ADMIN_PASSWORD if no
    user with that email exists yet. Idempotent, safe to run on every boot."""
    if not settings.admin_email or not settings.admin_password:
        return
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == settings.admin_email).first()
        if existing:
            return
        db.add(
            User(
                email=settings.admin_email,
                hashed_password=hash_password(settings.admin_password),
                auth_provider="basic",
                is_admin=True,
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap_admin_user()
    recover_orphaned_processing_items()
    recover_orphaned_email_claims()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Socialize", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.app_secret_key)
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
def media(filename: str, user: User = Depends(get_current_user)):
    """Auth-gated file serving for generated media (posters, later reels) -
    this is the user's own generated content, not public static assets, so
    it goes through get_current_user rather than an unauthenticated
    StaticFiles mount like /static."""
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = Path(settings.local_storage_dir) / filename
    if not path.is_file():
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
