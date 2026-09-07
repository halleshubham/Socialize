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

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.db.models import OAuthCredential
from backend.app.util.crypto import decrypt, encrypt

GMAIL_READONLY_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

PROVIDER = "gmail"


class GmailNotConnected(Exception):
    """Raised when no oauth_credentials row exists yet for 'gmail' scoped to
    this brand - connect it from that brand's Brand Kit page."""


def build_authorization_flow(redirect_uri: str, state: str | None = None, code_verifier: str | None = None) -> Flow:
    """Shared by both connect (state=None, fresh flow) and the callback
    (state/code_verifier restored from the session so the SAME PKCE
    exchange started in connect() can be completed here - a new Flow()
    object is constructed per request, so nothing else carries the
    code_verifier across the redirect round-trip). Reads the same OAuth
    client secrets file as the local gmail_authorize.py script
    (GMAIL_CREDENTIALS_PATH) - works whether that's a "web" or "installed"
    (Desktop app) type client (google_auth_oauthlib auto-detects which),
    though an "installed" client only accepts a localhost redirect_uri per
    Google's own rules - fine for local dev, but a real deployment needs a
    "Web application" type OAuth client with this app's actual domain
    registered as an authorized redirect URI."""
    settings = get_settings()
    return Flow.from_client_secrets_file(
        settings.gmail_credentials_path,
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
        creds.refresh(GoogleAuthRequest())
        save_credentials(db, brand_kit_id, creds)  # persist the rotated access token

    return creds
