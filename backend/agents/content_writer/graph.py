import json
import logging

from sqlalchemy.orm import Session

from backend.agents.content_writer.prompts import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_FORMAT_ONLY,
    SYSTEM_PROMPT_FORMAT_ONLY_PERSONAL,
    SYSTEM_PROMPT_FORMAT_ONLY_PRODUCT,
    SYSTEM_PROMPT_LOCALIZED,
    SYSTEM_PROMPT_PERSONAL,
    SYSTEM_PROMPT_PRODUCT,
    build_user_prompt,
    build_user_prompt_format_only,
)
from backend.agents.graphic_designer.templates import DEFAULT_TEMPLATE, TEMPLATE_CHOICES
from backend.agents.json_utils import extract_json, normalize_reel_script, sanitize_llm_json, truncate_on_word_boundary
from backend.agents.languages import DEFAULT_LANGUAGE, display_name
from backend.agents.reel_editor.templates import DEFAULT_TEMPLATE as DEFAULT_REEL_TEMPLATE
from backend.agents.reel_editor.templates import TEMPLATE_CHOICES as REEL_TEMPLATE_CHOICES
from backend.app.db.models import BrandKit, ContentItem
from backend.app.llm.prompt_overrides import resolve_prompt
from backend.app.llm.provider import ChatProvider

logger = logging.getLogger(__name__)

# Anthropic's server-side web search tool - executed by Anthropic itself
# (not a client-side tool loop we need to handle), used for current
# trending-hashtag context. See docs/architecture.md Trending Hashtags.
# Anthropic-specific tool schema, so it's only ever passed on the
# "content_writer" (Claude) path below, never "content_writer_localized".
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}

# User-reported: Claude's Marathi writing quality wasn't good enough - GPT-5.4
# (via _agent_task_for_language below) writes hi/mr copy instead; every other
# language keeps using Claude. See llm/provider.py's DEFAULT_MODELS for the
# actual model routing - this only picks which agent_task name to ask for.
_LOCALIZED_LANGUAGES = {"hi", "mr"}


def _agent_task_for_language(language: str) -> str:
    return "content_writer_localized" if language in _LOCALIZED_LANGUAGES else "content_writer"


# brand_kit.content_voice == "personal"/"product" (the GitHub- and
# WooCommerce-sourced brands) overrides the language-based selection above
# entirely - both voices are English-only for now (no localized variant
# built), same "brand default, no per-item override yet" scope as
# content_voice itself.
def _select_agent_task(language: str, content_voice: str) -> str:
    if content_voice == "personal":
        return "content_writer_personal"
    if content_voice == "product":
        return "content_writer_product"
    return _agent_task_for_language(language)


_SYSTEM_PROMPTS = {
    "content_writer": SYSTEM_PROMPT,
    "content_writer_localized": SYSTEM_PROMPT_LOCALIZED,
    "content_writer_personal": SYSTEM_PROMPT_PERSONAL,
    "content_writer_product": SYSTEM_PROMPT_PRODUCT,
}


def write_copy(db: Session, content_item: ContentItem, revision_feedback: str | None = None) -> dict:
    brand_kit = db.get(BrandKit, content_item.brand_kit_id)
    tone = brand_kit.tone_of_voice_prompt if brand_kit else ""
    language = content_item.language or (brand_kit.default_language if brand_kit else DEFAULT_LANGUAGE)
    content_voice = brand_kit.content_voice if brand_kit else "newsletter"

    article_text = content_item.article_full_text or content_item.article_summary or ""

    format_ = content_item.format or "text_only"
    agent_task = _select_agent_task(language, content_voice)
    # agent_task's string values double as prompt_key here - content
    # writer's 4 variants happen to have a 1:1 name match between the
    # model-routing task and the prompt-override slot (see
    # agents/prompt_registry.py), unlike graphic_designer/reel_editor.
    system_prompt = resolve_prompt(db, content_item.brand_kit_id, agent_task, _SYSTEM_PROMPTS[agent_task])
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": build_user_prompt(
                content_item.brief or "",
                content_item.article_title,
                article_text,
                tone,
                display_name(language),
                format_,
                revision_feedback,
            ),
        },
    ]

    provider = ChatProvider(db, content_item.brand_kit_id)
    if agent_task == "content_writer":
        # Claude path only - WEB_SEARCH_TOOL is an Anthropic-specific tool
        # schema, would error against the OpenAI localized path below.
        try:
            result = provider.complete(
                agent_task=agent_task,
                messages=messages,
                content_item_id=content_item.id,
                tools=[WEB_SEARCH_TOOL],
            )
            parsed = sanitize_llm_json(extract_json(result.text))
        except (Exception, json.JSONDecodeError):
            # Retry without the tool if either the call itself fails, OR it
            # succeeds but yields no parseable JSON - observed in practice: a
            # long reel-format response with multiple web_search rounds
            # occasionally comes back with empty/unparseable final text despite
            # real completion tokens being spent, even though finish_reason was
            # "stop". web_search is a nice-to-have for live hashtag relevance;
            # don't let either failure mode take down copy generation.
            logger.exception("content_writer call with web_search failed/unparseable, retrying without it")
            result = provider.complete(
                agent_task=agent_task, messages=messages, content_item_id=content_item.id
            )
            parsed = sanitize_llm_json(extract_json(result.text))
    else:
        result = provider.complete(agent_task=agent_task, messages=messages, content_item_id=content_item.id)
        parsed = sanitize_llm_json(extract_json(result.text))

    copy_text = parsed.get("copy_text", "")
    if content_item.article_url:
        # Deterministic, not left to the model to remember - every post is
        # ultimately a repost of someone else's reporting, so the original
        # article link always goes in the caption itself, not just relied
        # on via the "open article" link in the board UI.
        copy_text = f"{copy_text}\n\nSource: {content_item.article_url}"
    content_item.copy_text = copy_text
    content_item.hashtags = parsed.get("hashtags", [])
    content_item.poster_headline = truncate_on_word_boundary(parsed.get("poster_headline") or "", 300)
    template = parsed.get("poster_template")
    content_item.poster_template = template if template in TEMPLATE_CHOICES else DEFAULT_TEMPLATE
    content_item.poster_content = parsed.get("poster_content") or {}
    content_item.reel_script = normalize_reel_script(parsed.get("reel_script"))
    content_item.character_description = parsed.get("character_description") or None
    reel_template = parsed.get("reel_template")
    content_item.reel_template = reel_template if reel_template in REEL_TEMPLATE_CHOICES else DEFAULT_REEL_TEMPLATE
    content_item.carousel_script = parsed.get("carousel_script") or None
    content_item.language = language
    db.commit()
    return parsed


# format-only prompt variant per content_voice, same idea as _SYSTEM_PROMPTS
# above but for write_format_fields - keeps a personal/product brand's
# added-later reel/poster/carousel fields in the same voice as the caption
# they're attached to, instead of always falling back to the generic
# newsletter-summary framing.
_FORMAT_ONLY_SYSTEM_PROMPTS = {
    "content_writer_format_only": SYSTEM_PROMPT_FORMAT_ONLY,
    "content_writer_format_only_personal": SYSTEM_PROMPT_FORMAT_ONLY_PERSONAL,
    "content_writer_format_only_product": SYSTEM_PROMPT_FORMAT_ONLY_PRODUCT,
}


def _format_only_prompt_key(content_voice: str) -> str:
    if content_voice == "personal":
        return "content_writer_format_only_personal"
    if content_voice == "product":
        return "content_writer_format_only_product"
    return "content_writer_format_only"


def write_format_fields(db: Session, content_item: ContentItem, format_: str) -> dict:
    """Derives just the field(s) a new format needs (poster_headline, or
    reel_script + character_description) for a post whose copy_text is
    already finalized - used to add a poster/reel to something originally
    written as text_only (or that already has one format and is getting
    another), without regenerating or touching the approved caption. See
    routes_board.py's generate_media.

    Routes through the same content_voice selection as write_copy for BOTH
    the prompt text (personal/product get their own format-only variant,
    see _FORMAT_ONLY_SYSTEM_PROMPTS) and the model/agent_task (personal/
    product/localized all have their own DEFAULT_MODELS entry) - previously
    this always used the generic newsletter voice and content_writer/
    content_writer_localized regardless of content_voice, so a personal or
    product brand adding a reel/poster to an already-approved post got
    fields written in a mismatched voice from the caption itself."""
    if not content_item.copy_text:
        raise ValueError("No existing copy_text to derive format fields from")

    brand_kit = db.get(BrandKit, content_item.brand_kit_id)
    content_voice = brand_kit.content_voice if brand_kit else "newsletter"
    language = content_item.language or DEFAULT_LANGUAGE

    article_text = content_item.article_full_text or content_item.article_summary or ""
    prompt_key = _format_only_prompt_key(content_voice)
    system_prompt = resolve_prompt(
        db, content_item.brand_kit_id, prompt_key, _FORMAT_ONLY_SYSTEM_PROMPTS[prompt_key]
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": build_user_prompt_format_only(
                format_,
                content_item.copy_text,
                content_item.brief or "",
                content_item.article_title,
                article_text,
            ),
        },
    ]
    provider = ChatProvider(db, content_item.brand_kit_id)
    agent_task = _select_agent_task(language, content_voice)
    result = provider.complete(agent_task=agent_task, messages=messages, content_item_id=content_item.id)
    parsed = sanitize_llm_json(extract_json(result.text))

    if format_ == "poster":
        content_item.poster_headline = truncate_on_word_boundary(parsed.get("poster_headline") or "", 300)
        template = parsed.get("poster_template")
        content_item.poster_template = template if template in TEMPLATE_CHOICES else DEFAULT_TEMPLATE
        content_item.poster_content = parsed.get("poster_content") or {}
    elif format_ == "reel":
        content_item.reel_script = normalize_reel_script(parsed.get("reel_script"))
        content_item.character_description = parsed.get("character_description") or None
        reel_template = parsed.get("reel_template")
        content_item.reel_template = (
            reel_template if reel_template in REEL_TEMPLATE_CHOICES else DEFAULT_REEL_TEMPLATE
        )
    elif format_ == "carousel":
        content_item.carousel_script = parsed.get("carousel_script") or None
    db.commit()
    return parsed
