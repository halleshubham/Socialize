from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    app_secret_key: str = "dev-secret-change-me"
    auth_backend: str = "basic"  # "basic" | "google_oauth"
    app_encryption_key: str = ""

    admin_email: str = ""
    admin_password: str = ""

    # Database
    database_url: str = "postgresql+psycopg://socialize:socialize@localhost:5432/socialize"

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

    # Gmail (Phase 1)
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
