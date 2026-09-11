import uuid

from sqlalchemy.orm import Session

from backend.agents.json_utils import extract_json
from backend.agents.niche import get_active_niche_config
from backend.agents.researcher.rss_angle_prompts import SYSTEM_PROMPT, build_user_prompt
from backend.app.llm.prompt_overrides import resolve_prompt
from backend.app.llm.provider import ChatProvider


def extract_angles(db: Session, brand_kit_id: uuid.UUID, entries: list[dict]) -> list[dict]:
    """Scores a batch of already-fetched RSS entries ({title, link, summary}
    each) against the brand's niche in one call - the RSS sibling of
    researcher/graph.py's extract_articles, but entries are already
    discrete (no "find articles embedded in this blob" step needed)."""
    niche = get_active_niche_config(db, brand_kit_id)
    niche_prompt = niche.prompt_text if niche else "(no niche configured yet - be generically selective)"
    keywords = niche.keywords if niche else []

    provider = ChatProvider(db, brand_kit_id)
    system_prompt = resolve_prompt(db, brand_kit_id, "researcher_rss_triage", SYSTEM_PROMPT)
    result = provider.complete(
        agent_task="researcher_rss_triage",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": build_user_prompt(niche_prompt, keywords, entries)},
        ],
    )
    parsed = extract_json(result.text)
    articles = parsed.get("articles", [])
    return [a for a in articles if isinstance(a, dict) and a.get("title")]
