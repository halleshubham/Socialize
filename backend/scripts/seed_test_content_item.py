"""Phase 1 smoke test that doesn't require Gmail to be connected yet: inserts
one fake multi-article newsletter and runs it through Researcher extraction
-> per-article Analytical briefs, so you can verify the LLM wiring/niche
config/article fetching work before setting up Gmail OAuth.

Usage: docker compose exec app python -m backend.scripts.seed_test_content_item [brand name]
Brand name is optional if you only have one brand.
Requires ANTHROPIC_API_KEY in .env and at least one niche_config saved via /niche.
"""

import sys

from backend.agents.orchestrator import process_email
from backend.app.db.models import BrandKit, ContentItem, IngestedEmail
from backend.app.db.session import SessionLocal

FAKE_BODY_WITH_LINKS = """This week in AI engineering:

1. LangGraph 1.0 ships with native durable execution
[LangGraph's latest release](https://langchain-ai.github.io/langgraph/) adds \
first-class support for long-running, durably checkpointed agent workflows with \
human-in-the-loop interrupts.

2. New coffee subscription discount for readers
[Get 20% off](https://example.com/coffee-deal) your first order from our sponsor.

3. Google ships Veo 3.1 with subject-reference support for character consistency
[Veo 3.1](https://deepmind.google/technologies/veo/) now supports up to 3 reference \
images per generation to keep a character consistent across scenes.
"""

FAKE_EMAIL = dict(
    gmail_message_id="test-message-0001",
    sender="newsletter@example.com",
    subject="This week in AI engineering",
    body_text=FAKE_BODY_WITH_LINKS,
    body_with_links=FAKE_BODY_WITH_LINKS,
    status="new",
)


def main() -> None:
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
                print("Multiple brands exist - pass the brand name: python -m backend.scripts.seed_test_content_item <name>")
                return
            brand = brands[0]

        existing = (
            db.query(IngestedEmail)
            .filter(
                IngestedEmail.gmail_message_id == FAKE_EMAIL["gmail_message_id"],
                IngestedEmail.brand_kit_id == brand.id,
            )
            .first()
        )
        if existing:
            email_id = existing.id
            print(f"Reusing existing test email {email_id} for brand {brand.name!r}")
        else:
            email = IngestedEmail(brand_kit_id=brand.id, **FAKE_EMAIL)
            db.add(email)
            db.commit()
            db.refresh(email)
            email_id = email.id
            print(f"Inserted test email {email_id} for brand {brand.name!r}")
    finally:
        db.close()

    started_ids = process_email(SessionLocal, email_id)
    print(f"{len(started_ids)} article(s) sent into the pipeline")

    db = SessionLocal()
    try:
        items = db.query(ContentItem).filter(ContentItem.source_email_id == email_id).all()
        for item in items:
            print(f"\n--- {item.article_title!r} ---")
            print(f"stage={item.stage} priority={item.priority_score} url={item.article_url}")
            print(f"rationale: {item.priority_rationale}")
            if item.brief:
                print(f"brief: {item.brief}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
