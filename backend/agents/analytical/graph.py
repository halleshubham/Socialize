from sqlalchemy.orm import Session

from backend.agents.analytical.prompts import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_COMBINED,
    build_user_prompt,
    build_user_prompt_combined,
)
from backend.agents.graphic_designer.templates import DEFAULT_TEMPLATE, TEMPLATE_CHOICES
from backend.agents.json_utils import extract_json, sanitize_llm_json
from backend.agents.languages import DEFAULT_LANGUAGE, display_name
from backend.agents.niche import get_active_niche_config
from backend.agents.reel_editor.templates import DEFAULT_TEMPLATE as DEFAULT_REEL_TEMPLATE
from backend.agents.reel_editor.templates import TEMPLATE_CHOICES as REEL_TEMPLATE_CHOICES
from backend.app.db.models import BrandKit, ContentItem, IngestedEmail
from backend.app.llm.provider import ChatProvider


def write_brief(db: Session, content_item: ContentItem, email: IngestedEmail | None) -> str:
    niche = get_active_niche_config(db, content_item.brand_kit_id)
    niche_prompt = niche.prompt_text if niche else ""

    article_text = content_item.article_full_text or content_item.article_summary or ""
    source_context = f"newsletter {email.sender!r}, email subject {email.subject!r}" if email else "unknown"

    provider = ChatProvider(db, content_item.brand_kit_id)
    result = provider.complete(
        agent_task="analytical_brief",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_user_prompt(
                    niche_prompt, content_item.article_title, article_text, source_context
                ),
            },
        ],
        content_item_id=content_item.id,
    )

    content_item.brief = result.text
    db.commit()
    return result.text


def write_brief_and_copy(
    db: Session,
    content_item: ContentItem,
    email: IngestedEmail | None,
    format_: str,
    revision_feedback: str | None = None,
) -> dict:
    """Combined-drafting path (brand_kit.combined_drafting) - one cheap call
    (agent_task="analytical_combined_draft", Sonnet 5 per DEFAULT_MODELS)
    produces the brief AND the drafted post together, instead of a separate
    Content Writer call. format_ is the brand's combined_drafting_format
    default (agents/orchestrator.py's _analytical_node sets
    content_item.format from it before calling this) - drafts only that
    format's fields, same "omit the other format's fields" contract
    content_writer/graph.py::write_copy's prompt already uses. No
    web_search tool (cost-conscious path, hashtags from the model's own
    knowledge)."""
    niche = get_active_niche_config(db, content_item.brand_kit_id)
    niche_prompt = niche.prompt_text if niche else ""

    brand_kit = db.get(BrandKit, content_item.brand_kit_id)
    tone = brand_kit.tone_of_voice_prompt if brand_kit else ""
    language = content_item.language or DEFAULT_LANGUAGE

    article_text = content_item.article_full_text or content_item.article_summary or ""
    source_context = f"newsletter {email.sender!r}, email subject {email.subject!r}" if email else "unknown"

    provider = ChatProvider(db, content_item.brand_kit_id)
    result = provider.complete(
        agent_task="analytical_combined_draft",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_COMBINED},
            {
                "role": "user",
                "content": build_user_prompt_combined(
                    niche_prompt,
                    content_item.article_title,
                    article_text,
                    source_context,
                    tone,
                    display_name(language),
                    format_,
                    revision_feedback,
                ),
            },
        ],
        content_item_id=content_item.id,
    )
    parsed = sanitize_llm_json(extract_json(result.text))

    content_item.brief = parsed.get("brief", "")
    copy_text = parsed.get("copy_text", "")
    if content_item.article_url:
        # Same deterministic source-link append as write_copy - a repost
        # should always carry the original article link in the caption.
        copy_text = f"{copy_text}\n\nSource: {content_item.article_url}"
    content_item.copy_text = copy_text
    content_item.hashtags = parsed.get("hashtags", [])
    content_item.poster_headline = (parsed.get("poster_headline") or "")[:300]
    template = parsed.get("poster_template")
    content_item.poster_template = template if template in TEMPLATE_CHOICES else DEFAULT_TEMPLATE
    content_item.poster_content = parsed.get("poster_content") or {}
    content_item.reel_script = parsed.get("reel_script") or None
    content_item.character_description = parsed.get("character_description") or None
    reel_template = parsed.get("reel_template")
    content_item.reel_template = reel_template if reel_template in REEL_TEMPLATE_CHOICES else DEFAULT_REEL_TEMPLATE
    content_item.language = language
    db.commit()
    return parsed
