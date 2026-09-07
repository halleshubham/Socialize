import json
import logging

from sqlalchemy.orm import Session

from backend.agents.content_writer.prompts import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_FORMAT_ONLY,
    SYSTEM_PROMPT_LOCALIZED,
    build_user_prompt,
    build_user_prompt_format_only,
)
from backend.agents.graphic_designer.templates import DEFAULT_TEMPLATE, TEMPLATE_CHOICES
from backend.agents.json_utils import extract_json, sanitize_llm_json
from backend.agents.languages import DEFAULT_LANGUAGE, display_name
from backend.agents.reel_editor.templates import DEFAULT_TEMPLATE as DEFAULT_REEL_TEMPLATE
from backend.agents.reel_editor.templates import TEMPLATE_CHOICES as REEL_TEMPLATE_CHOICES
from backend.app.db.models import BrandKit, ContentItem
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


def write_copy(db: Session, content_item: ContentItem, revision_feedback: str | None = None) -> dict:
    brand_kit = db.get(BrandKit, content_item.brand_kit_id)
    tone = brand_kit.tone_of_voice_prompt if brand_kit else ""
    language = content_item.language or (brand_kit.default_language if brand_kit else DEFAULT_LANGUAGE)

    article_text = content_item.article_full_text or content_item.article_summary or ""

    format_ = content_item.format or "text_only"
    agent_task = _agent_task_for_language(language)
    system_prompt = SYSTEM_PROMPT if agent_task == "content_writer" else SYSTEM_PROMPT_LOCALIZED
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


def write_format_fields(db: Session, content_item: ContentItem, format_: str) -> dict:
    """Derives just the field(s) a new format needs (poster_headline, or
    reel_script + character_description) for a post whose copy_text is
    already finalized - used to add a poster/reel to something originally
    written as text_only (or that already has one format and is getting
    another), without regenerating or touching the approved caption. See
    routes_board.py's generate_media."""
    if not content_item.copy_text:
        raise ValueError("No existing copy_text to derive format fields from")

    article_text = content_item.article_full_text or content_item.article_summary or ""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_FORMAT_ONLY},
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
    agent_task = _agent_task_for_language(content_item.language or DEFAULT_LANGUAGE)
    result = provider.complete(agent_task=agent_task, messages=messages, content_item_id=content_item.id)
    parsed = sanitize_llm_json(extract_json(result.text))

    if format_ == "poster":
        content_item.poster_headline = (parsed.get("poster_headline") or "")[:300]
        template = parsed.get("poster_template")
        content_item.poster_template = template if template in TEMPLATE_CHOICES else DEFAULT_TEMPLATE
        content_item.poster_content = parsed.get("poster_content") or {}
    elif format_ == "reel":
        content_item.reel_script = parsed.get("reel_script") or None
        content_item.character_description = parsed.get("character_description") or None
        reel_template = parsed.get("reel_template")
        content_item.reel_template = (
            reel_template if reel_template in REEL_TEMPLATE_CHOICES else DEFAULT_REEL_TEMPLATE
        )
    db.commit()
    return parsed
