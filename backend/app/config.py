from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    # "development" (default - safe for local docker-compose/localhost use)
    # or "production" - gates behavior that would otherwise break local dev
    # (forcing session cookies to HTTPS-only would lock you out of plain
    # http://localhost) but is required once this is reachable by anyone
    # other than the operator - see main.py's use of this for the session
    # cookie and the app_secret_key startup check below.
    app_env: str = "development"
    app_secret_key: str = "dev-secret-change-me"
    auth_backend: str = "basic"  # "basic" | "google_oauth"
    app_encryption_key: str = ""
    # Comma-separated OLDER encryption keys, still accepted for decrypting
    # existing secrets but never used for new encryption - the rotation
    # path for app_encryption_key (see util/crypto.py's MultiFernet use):
    # generate a new key, move the current app_encryption_key's value here
    # (append if there were already older ones), set app_encryption_key to
    # the new value, redeploy. Every UserApiKey/OAuthCredential/Botsab-
    # Postiz-GitHub-token row still encrypted under an old key keeps
    # decrypting correctly; anything saved fresh (or re-saved) after
    # rotation uses the new one. Empty by default - rotation is opt-in,
    # not required for the single-key setup this app ships with.
    app_encryption_key_previous: str = ""

    admin_email: str = ""
    admin_password: str = ""

    # Database
    database_url: str = "postgresql+psycopg://socialize:socialize@localhost:5432/socialize"

    # Redis - shared state across app replicas. Nothing reads this yet
    # (login-attempt throttling and rate limiting are still in-process
    # dicts, correct only for today's single-replica deployment - see
    # auth/login_throttle.py and middleware/rate_limit.py); added ahead of
    # that migration so the infrastructure exists to build against.
    # docker-compose overrides this to redis:6379 automatically - this
    # value is for running the app locally (outside docker) against the
    # compose redis, whose port is mapped to 6381 on the host to avoid
    # colliding with a local Redis install (same reasoning as
    # DATABASE_URL's 5434 for Postgres).
    redis_url: str = "redis://localhost:6381/0"

    # LLM providers (read by litellm from env directly too; kept here for
    # explicit config/validation and for the provider-check script)
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    google_api_key: str = ""
    google_cloud_project: str = ""
    google_cloud_location: str = "us-central1"
    google_application_credentials: str = ""

    # Storage
    storage_backend: str = "local_disk"  # "local_disk" | "r2"
    local_storage_dir: str = "./data/media"

    # Cloudflare R2 - S3-compatible object storage, used via boto3's S3
    # client with a custom endpoint_url. Bucket stays PRIVATE - url_for()
    # hands out short-lived presigned URLs instead of permanent public
    # links, preserving the same "must be logged in to get a working link"
    # behavior /media/{filename} already gives local_disk (see storage/r2.py).
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""
    r2_presigned_url_expiry_seconds: int = 3600

    # Gmail (Phase 1) - ONE shared OAuth client for the whole app (only the
    # resulting per-brand token differs, see oauth_credentials). Set
    # gmail_client_id + gmail_client_secret (a "Web application" client) for
    # env-only deploys like Coolify; otherwise the client JSON file at
    # gmail_credentials_path is used (local dev's "Desktop app" client).
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_credentials_path: str = "./secrets/gmail_credentials.json"

    # GitHub - a second content source (integrations/github/), alongside
    # Gmail. A brand's own PAT (BrandKit.github_token_encrypted) overrides
    # this shared fallback, same pattern as Botsab/Postiz. Needs "repo"
    # scope for private repos; public-only works unauthenticated but at
    # GitHub's much lower 60 req/hr anonymous rate limit.
    github_token: str = ""

    # Postiz - self-hosted at postiz_base_url. postiz_api_key is the shared
    # fallback key (same "brand override, else this" pattern as Botsab/LLM
    # keys) - a brand's own key lives encrypted on its BrandKit row instead.
    postiz_base_url: str = "https://social.shackyapps.in"
    postiz_api_key: str = ""

    # Botsab (WhatsApp) - self-hosted at botsab_base_url (local repo
    # ../Botsab). Posters and reels both send via upload+fileId (see
    # integrations/botsab/client.py) - no public reachability needed for
    # this app itself, Botsab is the one being called, not the other way
    # around.
    botsab_base_url: str = "https://botsab.shackyapps.in"
    botsab_api_key: str = ""
    botsab_instance_id: str = ""

    # Resend (email) - the only email-sending capability in this app,
    # currently just self-serve signup's verification link
    # (auth/email_verification.py). resend_from_email must be an address on
    # a domain verified in the Resend dashboard, or every send fails -
    # Resend's own onboarding domain (onboarding@resend.dev) works for
    # testing without a custom domain, but only ever delivers to the
    # account's own verified email. Unset means "email verification isn't
    # configured" - signup then falls back to auto-verifying accounts
    # rather than locking new users out with no way to receive the link
    # (see auth/email_verification.py's send_verification_email).
    resend_api_key: str = ""
    resend_from_email: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
