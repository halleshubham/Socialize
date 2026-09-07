from sqlalchemy.orm import Session

from backend.agents.json_utils import extract_json
from backend.agents.niche import get_active_niche_config
from backend.agents.researcher.prompts import SYSTEM_PROMPT, build_user_prompt
from backend.app.db.models import IngestedEmail
from backend.app.llm.provider import ChatProvider


def extract_articles(db: Session, email: IngestedEmail) -> list[dict]:
    """Runs the whole email (which may be a newsletter with many articles)
    through the Researcher once, returning one dict per article found:
    {title, url, summary, suitable_for_social, priority_score, rationale}.
    Called once per email in worker/pipeline.py, BEFORE any ContentItem
    exists - one gets created per returned article (see orchestrator.py's
    process_email)."""
    niche = get_active_niche_config(db, email.brand_kit_id)
    niche_prompt = niche.prompt_text if niche else "(no niche configured yet - be generically selective)"
    keywords = niche.keywords if niche else []

    body = email.body_with_links or email.body_text

    provider = ChatProvider(db, email.brand_kit_id)
    result = provider.complete(
        agent_task="researcher_triage",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_user_prompt(niche_prompt, keywords, email.sender, email.subject, body),
            },
        ],
    )
    parsed = extract_json(result.text)
    articles = parsed.get("articles", [])
    return [a for a in articles if isinstance(a, dict) and a.get("title")]
