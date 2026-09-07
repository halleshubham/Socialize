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
    storage_backend: str = "local_disk"  # "local_disk" | "gcs"
    local_storage_dir: str = "./data/media"
    gcs_bucket: str = ""

    # Gmail (Phase 1)
    gmail_credentials_path: str = "./secrets/gmail_credentials.json"

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
