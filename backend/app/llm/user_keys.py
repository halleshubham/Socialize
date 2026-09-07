"""Per-user LLM provider API keys, encrypted at rest. When a brand generates
content, the BRAND OWNER's key is used (not whoever's operating it, if it's
shared) - see resolve_api_key. Falls back to the process-wide
ANTHROPIC_API_KEY/OPENAI_API_KEY/GOOGLE_API_KEY env vars when the owner
hasn't set their own key for that provider, so .env stays a valid way to
run this app without every user configuring their own keys first.
"""

import uuid

from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.db.models import BrandKit, UserApiKey
from backend.app.util.crypto import decrypt, encrypt

PROVIDERS = ("anthropic", "openai", "google")

_ENV_FALLBACK_ATTR = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "google": "google_api_key",
}


def get_user_api_keys(db: Session, user_id: uuid.UUID) -> dict[str, str | None]:
    """Decrypted keys this user has set, keyed by provider - missing/None
    for a provider they haven't configured. Used to render the account
    settings page (masked, never echoing the raw value back verbatim)."""
    rows = db.query(UserApiKey).filter(UserApiKey.user_id == user_id).all()
    by_provider = {row.provider: decrypt(row.encrypted_key) for row in rows}
    return {provider: by_provider.get(provider) for provider in PROVIDERS}


def set_user_api_key(db: Session, user_id: uuid.UUID, provider: str, raw_key: str) -> None:
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider {provider!r}")
    row = db.query(UserApiKey).filter(UserApiKey.user_id == user_id, UserApiKey.provider == provider).first()
    encrypted = encrypt(raw_key)
    if row:
        row.encrypted_key = encrypted
    else:
        db.add(UserApiKey(user_id=user_id, provider=provider, encrypted_key=encrypted))
    db.commit()


def clear_user_api_key(db: Session, user_id: uuid.UUID, provider: str) -> None:
    db.query(UserApiKey).filter(UserApiKey.user_id == user_id, UserApiKey.provider == provider).delete()
    db.commit()


def resolve_api_key(db: Session, brand_kit_id: uuid.UUID | None, provider: str) -> str | None:
    """The key to actually use for a generation call against this brand -
    the brand owner's own key if they've set one, else the shared .env
    fallback (settings.<provider>_api_key), else None (caller decides how
    to handle a truly unconfigured provider - ChatProvider lets litellm's
    own error surface; the image/video providers return None and the
    caller falls back to a non-AI path)."""
    if brand_kit_id is not None:
        brand = db.get(BrandKit, brand_kit_id)
        if brand:
            row = (
                db.query(UserApiKey)
                .filter(UserApiKey.user_id == brand.owner_user_id, UserApiKey.provider == provider)
                .first()
            )
            if row:
                return decrypt(row.encrypted_key)

    settings = get_settings()
    attr = _ENV_FALLBACK_ATTR.get(provider)
    return (getattr(settings, attr, "") or None) if attr else None
