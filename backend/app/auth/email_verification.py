"""Self-serve signup's email verification: a random token stored on the
user row, emailed as a one-click link via Resend. A new signup starts
email_verified=False and can't log in (BasicAuthBackend.authenticate)
until they click it.

Deliberately fails open, not closed, when Resend isn't configured
(settings.resend_api_key/resend_from_email unset): a self-hosted deployment
that hasn't set up Resend yet would otherwise let people register accounts
they can never verify and never use, with no way out except an admin
manually flipping the column. routes_auth.py's signup route checks
is_configured() and skips straight to email_verified=True when it's False,
so this feature is purely additive - the existing no-verification signup
flow (this app's actual state before Resend was wired up) keeps working
unchanged for any deployment that doesn't set these two env vars.
"""

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.db.models import User
from backend.app.integrations.resend.client import ResendClient, ResendError

RESEND_COOLDOWN_SECONDS = 60


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.resend_api_key and settings.resend_from_email)


def _get_client() -> ResendClient | None:
    settings = get_settings()
    if not is_configured():
        return None
    return ResendClient(settings.resend_api_key, settings.resend_from_email)


def _build_email_html(verify_url: str, full_name: str) -> str:
    first_name = (full_name or "").strip().split(" ")[0] or "there"
    return f"""
    <div style="font-family: -apple-system, Helvetica, Arial, sans-serif; max-width: 480px; margin: 0 auto;">
      <p>Hi {first_name},</p>
      <p>Confirm your email to finish setting up your Socialize account.</p>
      <p style="margin: 1.5rem 0;">
        <a href="{verify_url}" style="background:#2563eb; color:#fff; padding:.7rem 1.2rem; border-radius:6px; text-decoration:none; display:inline-block;">Verify email</a>
      </p>
      <p style="color:#666; font-size:.85rem;">Or paste this link into your browser: {verify_url}</p>
      <p style="color:#999; font-size:.8rem;">If you didn't sign up for Socialize, you can ignore this email.</p>
    </div>
    """


def send_verification_email(db: Session, user: User, verify_url_base: str) -> None:
    """Generates a fresh token, stores it, and emails a link built from it -
    verify_url_base is the bare route URL (e.g. from request.url_for), a
    "?token=..." query string is appended here once the real token exists,
    never before. Raises ResendError on failure - callers decide how to
    surface that (signup still succeeds either way; the account just stays
    unverified until a resend works)."""
    client = _get_client()
    if not client:
        return

    token = secrets.token_urlsafe(32)
    user.email_verification_token = token
    user.email_verification_sent_at = datetime.now(timezone.utc)
    db.commit()

    verify_url = f"{verify_url_base}?token={token}"
    client.send(
        to=user.email,
        subject="Verify your Socialize email",
        html=_build_email_html(verify_url, user.full_name or ""),
    )


def can_resend(user: User) -> bool:
    if not user.email_verification_sent_at:
        return True
    sent_at = user.email_verification_sent_at
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - sent_at > timedelta(seconds=RESEND_COOLDOWN_SECONDS)


def verify_token(db: Session, token: str) -> User | None:
    """Looks up and consumes a verification token - returns the now-verified
    user, or None if the token doesn't match anything (already used, never
    existed, or wrong). Token is cleared on success so it can't be reused."""
    if not token:
        return None
    user = db.query(User).filter(User.email_verification_token == token).first()
    if not user:
        return None
    user.email_verified = True
    user.email_verification_token = None
    db.commit()
    return user
