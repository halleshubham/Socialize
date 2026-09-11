import uuid

from sqlalchemy.orm import Session

from backend.agents.json_utils import extract_json
from backend.agents.researcher.github_angle_prompts import SYSTEM_PROMPT, build_user_prompt
from backend.app.llm.prompt_overrides import resolve_prompt
from backend.app.llm.provider import ChatProvider


def extract_angles(
    db: Session,
    brand_kit_id: uuid.UUID,
    repo_full_name: str,
    repo_url: str,
    grounding_text: str,
    feature_description: str,
    extra_context: str = "",
) -> list[dict]:
    """The GitHub-sourced sibling of researcher/graph.py's extract_articles -
    same output contract ({title, url, summary, suitable_for_social,
    priority_score, rationale} per item) so it plugs directly into
    orchestrator.py's create_content_items, just grounded in a repo's real
    README/docs/commits instead of a newsletter email. Every returned
    item's url is set to repo_url (there's no per-angle link, unlike a
    newsletter's per-article links)."""
    provider = ChatProvider(db, brand_kit_id)
    system_prompt = resolve_prompt(db, brand_kit_id, "researcher_github_angles", SYSTEM_PROMPT)
    result = provider.complete(
        agent_task="researcher_github_angles",
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": build_user_prompt(repo_full_name, feature_description, extra_context, grounding_text),
            },
        ],
    )
    parsed = extract_json(result.text)
    angles = parsed.get("articles", [])
    return [{**a, "url": repo_url} for a in angles if isinstance(a, dict) and a.get("title")]
