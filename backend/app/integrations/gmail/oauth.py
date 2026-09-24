"""Gmail OAuth2 credential storage, and the in-app connect flow itself
(build_authorization_flow/exchange_code below - the Brand Kit page's
"Connect Gmail" button, via routes_gmail.py). The original one-time local
script (backend/scripts/gmail_authorize.py, needs a local browser) still
works too - both paths end up calling save_credentials the same way. This
module also handles loading the stored credential and transparently
refreshing it, used by both the manual "fetch now" route and the daily
scheduled job.
"""

import json
import uuid

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.db.models import OAuthCredential
from backend.app.util.crypto import decrypt, encrypt

GMAIL_READONLY_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

PROVIDER = "gmail"


class GmailClientNotConfigured(Exception):
    """Raised when neither GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET nor a client
    JSON file at GMAIL_CREDENTIALS_PATH is available - the app itself has
    no OAuth client to start any brand's consent flow with."""


def gmail_client_config() -> dict:
    """The shared OAuth client, in google_auth_oauthlib's client-config
    shape. GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET win when both are set -
    treated as a "Web application" client, which is what any real domain
    needs - so a deploy platform only has to hold two env vars instead of
    mounting a file. Otherwise falls back to the JSON file (either "web" or
    "installed" type, as downloaded from Google Cloud Console)."""
    settings = get_settings()
    if settings.gmail_client_id and settings.gmail_client_secret:
        return {
            "web": {
                "client_id": settings.gmail_client_id,
                "client_secret": settings.gmail_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }
    try:
        with open(settings.gmail_credentials_path) as f:
            return json.load(f)
    except FileNotFoundError as exc:
        raise GmailClientNotConfigured(
            "Gmail OAuth client not configured - set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET "
            "(or GMAIL_CREDENTIALS_PATH) - see docs/architecture.md"
        ) from exc


class GmailNotConnected(Exception):
    """Raised when no oauth_credentials row exists yet for 'gmail' scoped to
    this brand - connect it from that brand's Brand Kit page."""


class GmailTokenExpired(GmailNotConnected):
    """Raised when a row DOES exist but the stored refresh token itself is
    no longer valid (revoked in the user's Google account, or - very common
    for an OAuth client still in Google's "Testing" publishing status - a
    refresh token that auto-expires after 7 days). A subclass of
    GmailNotConnected, not a sibling: found live, load_credentials' own
    creds.refresh() call had zero exception handling, so a real
    google.auth.exceptions.RefreshError (invalid_grant: Token has been
    expired or revoked) propagated all the way up through /board/fetch as
    an uncaught 500 instead of the same clear "reconnect Gmail" message
    GmailNotConnected already gets - subclassing means every existing
    `except GmailNotConnected` catch site (routes_board.py's
    _fetch_and_redirect) picks this up automatically with no separate catch
    needed, while still getting an accurate message (this brand WAS
    connected, unlike the base class's "never connected" case)."""


def build_authorization_flow(redirect_uri: str, state: str | None = None, code_verifier: str | None = None) -> Flow:
    """Shared by both connect (state=None, fresh flow) and the callback
    (state/code_verifier restored from the session so the SAME PKCE
    exchange started in connect() can be completed here - a new Flow()
    object is constructed per request, so nothing else carries the
    code_verifier across the redirect round-trip). Uses the shared OAuth
    client from gmail_client_config() (GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET,
    else the GMAIL_CREDENTIALS_PATH file) - works whether that's a "web" or "installed"
    (Desktop app) type client (google_auth_oauthlib auto-detects which),
    though an "installed" client only accepts a localhost redirect_uri per
    Google's own rules - fine for local dev, but a real deployment needs a
    "Web application" type OAuth client with this app's actual domain
    registered as an authorized redirect URI."""
    return Flow.from_client_config(
        gmail_client_config(),
        scopes=GMAIL_READONLY_SCOPES,
        redirect_uri=redirect_uri,
        state=state,
        code_verifier=code_verifier,
    )


def save_credentials(db: Session, brand_kit_id: uuid.UUID, creds: Credentials) -> None:
    payload = encrypt(creds.to_json())
    row = (
        db.query(OAuthCredential)
        .filter(OAuthCredential.provider == PROVIDER, OAuthCredential.brand_kit_id == brand_kit_id)
        .first()
    )
    if row:
        row.encrypted_payload = payload
        row.scopes = list(creds.scopes or GMAIL_READONLY_SCOPES)
    else:
        db.add(
            OAuthCredential(
                provider=PROVIDER,
                brand_kit_id=brand_kit_id,
                encrypted_payload=payload,
                scopes=list(creds.scopes or GMAIL_READONLY_SCOPES),
            )
        )
    db.commit()


def is_connected(db: Session, brand_kit_id: uuid.UUID) -> bool:
    return (
        db.query(OAuthCredential)
        .filter(OAuthCredential.provider == PROVIDER, OAuthCredential.brand_kit_id == brand_kit_id)
        .first()
        is not None
    )


def disconnect(db: Session, brand_kit_id: uuid.UUID) -> None:
    db.query(OAuthCredential).filter(
        OAuthCredential.provider == PROVIDER, OAuthCredential.brand_kit_id == brand_kit_id
    ).delete()
    db.commit()


def load_credentials(db: Session, brand_kit_id: uuid.UUID) -> Credentials:
    row = (
        db.query(OAuthCredential)
        .filter(OAuthCredential.provider == PROVIDER, OAuthCredential.brand_kit_id == brand_kit_id)
        .first()
    )
    if not row:
        raise GmailNotConnected("This brand doesn't have Gmail connected yet - connect it from its Brand Kit page.")
    info = json.loads(decrypt(row.encrypted_payload))
    creds = Credentials.from_authorized_user_info(info, GMAIL_READONLY_SCOPES)

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleAuthRequest())
        except RefreshError as exc:
            raise GmailTokenExpired(
                "This brand's Gmail connection has expired or been revoked - reconnect it from the "
                "Brand Kit page."
            ) from exc
        save_credentials(db, brand_kit_id, creds)  # persist the rotated access token

    return creds
