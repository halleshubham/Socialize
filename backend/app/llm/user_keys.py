"""Per-user LLM provider API keys, encrypted at rest. When a brand generates
content, the BRAND OWNER's key is used (not whoever's operating it, if it's
shared) - see resolve_api_key.

Bring-Your-Own-Key is the enforced model (SaaS-readiness Phase 2, confirmed
by the user): a brand-scoped call with no owner-configured key for a
provider gets None back, not the process-wide ANTHROPIC_API_KEY/
OPENAI_API_KEY/GOOGLE_API_KEY env vars - those three are checked every
existing brand already has its own key set for anthropic/openai/google
(confirmed against the live DB before this change shipped), so this closes
the gap for every brand going forward without changing behavior for any
brand today. See llm/provider.py's ChatProvider.complete for why brand-
scoped calls also need an explicit check on top of this, not just a None
return - litellm has its own independent env-var fallback that this
function's return value alone doesn't stop.

The shared .env fallback still applies ONLY when there's no brand context
at all (brand_kit_id=None) - a genuinely brand-less system call, e.g.
scripts/check_providers.py, which has no brand to have configured a key on
in the first place.
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
    the brand owner's own key if they've set one, else None (BYOK is the
    enforced model - see this module's docstring). The image/video
    providers (llm/image_provider.py, llm/video_provider.py) already treat
    None as "not configured" and fall back to a non-AI path on their own,
    so no further change was needed there; ChatProvider.complete needs an
    explicit check instead of just reading this return value, since
    litellm has its own independent env-var fallback (see there).

    brand_kit_id=None (no brand context at all, e.g. scripts/
    check_providers.py) is the one case that still uses the shared .env
    key - there's no brand that could have configured its own here."""
    if brand_kit_id is not None:
        brand = db.get(BrandKit, brand_kit_id)
        if not brand:
            return None
        row = (
            db.query(UserApiKey)
            .filter(UserApiKey.user_id == brand.owner_user_id, UserApiKey.provider == provider)
            .first()
        )
        return decrypt(row.encrypted_key) if row else None

    settings = get_settings()
    attr = _ENV_FALLBACK_ATTR.get(provider)
    return (getattr(settings, attr, "") or None) if attr else None
