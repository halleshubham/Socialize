"""One-time interactive Gmail OAuth consent. Run this LOCALLY (not inside
Docker) since it needs a browser: it spins up a local server, opens your
browser to Google's consent screen, and stores the resulting refresh token
(encrypted) in Postgres for the app/scheduler to use afterward.

Prerequisites:
  - GMAIL_CREDENTIALS_PATH in .env points at your downloaded OAuth "Desktop
    app" client JSON (see docs/architecture.md for how to get one).
  - The Postgres container is up: `docker compose up -d db`
  - DATABASE_URL in .env resolves to that db from the host (localhost:5434
    by default - see .env.example).

Usage: source .venv/bin/activate && python -m backend.scripts.gmail_authorize [brand name]
Brand name is optional if you only have one brand set up.
"""

import sys

from google_auth_oauthlib.flow import InstalledAppFlow

from backend.app.config import get_settings
from backend.app.db.models import BrandKit
from backend.app.db.session import SessionLocal
from backend.app.integrations.gmail.oauth import GMAIL_READONLY_SCOPES, save_credentials


def main() -> None:
    settings = get_settings()
    brand_name = sys.argv[1] if len(sys.argv) > 1 else None

    db = SessionLocal()
    try:
        if brand_name:
            brand = db.query(BrandKit).filter(BrandKit.name == brand_name).first()
            if not brand:
                print(f"No brand named {brand_name!r} found.")
                return
        else:
            brands = db.query(BrandKit).all()
            if len(brands) != 1:
                print("Multiple brands exist - pass the brand name: python -m backend.scripts.gmail_authorize <name>")
                return
            brand = brands[0]

        flow = InstalledAppFlow.from_client_secrets_file(
            settings.gmail_credentials_path, GMAIL_READONLY_SCOPES
        )
        creds = flow.run_local_server(port=0)
        save_credentials(db, brand.id, creds)
    finally:
        db.close()

    print(f"Gmail connected for brand {brand.name!r}. Credentials stored (encrypted) in Postgres.")


if __name__ == "__main__":
    main()
