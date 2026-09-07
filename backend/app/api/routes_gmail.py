"""In-app Gmail OAuth connect flow for the active brand - the "Connect
Gmail" button on the Brand Kit page. Any user with access to the brand can
connect/reconnect/disconnect it (operational action, not owner-gated, same
as the reel/auto-mode settings). See integrations/gmail/oauth.py for the
actual Flow construction and credential storage.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from backend.app.auth.brand_deps import get_active_brand
from backend.app.auth.deps import get_current_user
from backend.app.db.models import BrandKit, User
from backend.app.db.session import get_db
from backend.app.integrations.gmail.oauth import build_authorization_flow, disconnect, save_credentials

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/brand-kit/gmail")

_STATE_KEY = "gmail_oauth_state"
_VERIFIER_KEY = "gmail_oauth_code_verifier"
_BRAND_KEY = "gmail_oauth_brand_id"


def _callback_redirect_uri(request: Request) -> str:
    return str(request.url_for("gmail_oauth_callback"))


@router.get("/connect")
def connect(
    request: Request,
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    try:
        flow = build_authorization_flow(_callback_redirect_uri(request))
    except FileNotFoundError:
        return RedirectResponse(
            url="/brand-kit?error=Gmail OAuth client not configured (GMAIL_CREDENTIALS_PATH) - see docs/architecture.md",
            status_code=303,
        )
    auth_url, state = flow.authorization_url(prompt="consent", include_granted_scopes="true")

    # PKCE's code_verifier lives only on this Flow instance - a fresh Flow()
    # gets built for the callback request, so it has to round-trip through
    # the session same as state does, or the token exchange fails.
    request.session[_STATE_KEY] = state
    request.session[_VERIFIER_KEY] = flow.code_verifier
    request.session[_BRAND_KEY] = str(brand.id)
    return RedirectResponse(url=auth_url, status_code=303)


@router.get("/callback", name="gmail_oauth_callback")
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stored_state = request.session.pop(_STATE_KEY, None)
    code_verifier = request.session.pop(_VERIFIER_KEY, None)
    brand_kit_id = request.session.pop(_BRAND_KEY, None)

    if error:
        return RedirectResponse(url=f"/brand-kit?error=Gmail connection cancelled ({error})", status_code=303)
    if not (code and state and stored_state and brand_kit_id) or state != stored_state:
        return RedirectResponse(
            url="/brand-kit?error=Gmail connection failed a security check - try connecting again",
            status_code=303,
        )

    try:
        flow = build_authorization_flow(
            _callback_redirect_uri(request), state=stored_state, code_verifier=code_verifier
        )
        flow.fetch_token(code=code)
    except Exception as exc:
        logger.exception("Gmail OAuth token exchange failed")
        return RedirectResponse(
            url=f"/brand-kit?error=Gmail connection failed: {exc}",
            status_code=303,
        )

    save_credentials(db, uuid.UUID(brand_kit_id), flow.credentials)
    return RedirectResponse(url="/brand-kit?gmail=connected", status_code=303)


@router.post("/disconnect")
def disconnect_gmail(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    disconnect(db, brand.id)
    return RedirectResponse(url="/brand-kit", status_code=303)
