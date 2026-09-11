import uuid

from sqlalchemy.orm import Session

from backend.agents.json_utils import extract_json
from backend.agents.researcher.product_angle_prompts import SYSTEM_PROMPT, build_user_prompt
from backend.app.llm.prompt_overrides import resolve_prompt
from backend.app.llm.provider import ChatProvider


def extract_angles(
    db: Session,
    brand_kit_id: uuid.UUID,
    product_name: str,
    product_url: str,
    grounding_text: str,
    extra_context: str = "",
) -> list[dict]:
    """The product-catalog sibling of researcher/github_angles.py's
    extract_angles - same output contract ({title, url, summary,
    suitable_for_social, priority_score, rationale} per item) so it plugs
    directly into orchestrator.py's create_content_items, just grounded in
    a real WooCommerce product listing instead of a repo's README. Every
    returned item's url is set to product_url."""
    provider = ChatProvider(db, brand_kit_id)
    system_prompt = resolve_prompt(db, brand_kit_id, "researcher_product_angles", SYSTEM_PROMPT)
    result = provider.complete(
        agent_task="researcher_product_angles",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": build_user_prompt(product_name, extra_context, grounding_text)},
        ],
    )
    parsed = extract_json(result.text)
    angles = parsed.get("articles", [])
    return [{**a, "url": product_url} for a in angles if isinstance(a, dict) and a.get("title")]
