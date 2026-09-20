"""Carousel Editor pipeline: script -> shot list (4-6 slides, LLM-decided,
each with its own headline/body_text AND a distinct visual) -> a shared text
style guide -> per-slide image generation, each slide a fresh, complete
image (scene + its own text) rather than a shared background edited per
slide.

Nothing here is ever Pillow-drawn beyond branding (add_brand_strip) and
source attribution (add_source_line) - each slide's headline/body_text is
rendered by the image model itself, verbatim, or not shown at all. See
docs/architecture.md's "no programmatic content text" policy.

Error-handling policy (mirrors graphic_designer/graph.py::generate_poster):
- No image provider configured: propagate. There is nothing to build the
  carousel from.
- The style guide LLM call failing: propagate - without it, slides would
  have nothing keeping them visually consistent with each other.
- An individual slide's image generation failing: soft failure, falls back
  to a neutral-gradient branded card for that one slide (no AI content
  text, same as a poster's "no provider" fallback) rather than discarding
  the whole carousel over one bad slide.
"""

import logging

from sqlalchemy.orm import Session

from backend.agents.carousel_editor.prompts import (
    SYSTEM_PROMPT_SHOTLIST,
    SYSTEM_PROMPT_SLIDE,
    SYSTEM_PROMPT_STYLE_GUIDE,
    build_shotlist_user_prompt,
    build_style_guide_user_prompt,
    build_user_prompt_slide,
)
from backend.agents.graphic_designer.layout import add_brand_strip, add_source_line, render_fallback_poster
from backend.agents.json_utils import extract_json, sanitize_llm_json
from backend.agents.languages import DEFAULT_LANGUAGE, display_name
from backend.agents.source_attribution import resolve_source_name
from backend.app.db.models import BrandKit, ContentItem, IngestedEmail, MediaAsset
from backend.app.llm.image_provider import (
    IMAGE_GEN_COST_USD,
    IMAGE_MODEL_CHOICES,
    get_image_model_key,
    get_image_provider,
)
from backend.app.llm.prompt_library import DEFAULT_CATEGORY, format_examples_block, get_example_prompts
from backend.app.llm.prompt_overrides import resolve_prompt
from backend.app.llm.provider import ChatProvider
from backend.app.storage.local_disk import get_storage_backend

logger = logging.getLogger(__name__)

MIN_SLIDES = 4
MAX_SLIDES = 6


def _build_carousel_shotlist(db: Session, content_item: ContentItem) -> list[dict]:
    if content_item.carousel_slides:
        return content_item.carousel_slides  # already built (e.g. a prior partial run)

    if not content_item.carousel_script:
        raise ValueError("No carousel_script on this content item - Content Writer must run first")

    article_text = content_item.article_full_text or content_item.article_summary or ""
    language_name = display_name(content_item.language or DEFAULT_LANGUAGE)
    system_prompt = resolve_prompt(db, content_item.brand_kit_id, "carousel_shotlist", SYSTEM_PROMPT_SHOTLIST)
    user_prompt = build_shotlist_user_prompt(
        content_item.carousel_script, content_item.article_title, article_text, language_name
    )

    provider = ChatProvider(db, content_item.brand_kit_id)
    result = provider.complete(
        agent_task="carousel_shotlist",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        content_item_id=content_item.id,
    )
    slides = sanitize_llm_json(extract_json(result.text)).get("slides", [])
    # Defensive cap - the prompt already asks for 4-6, this just guards a
    # misbehaving model rather than being the primary enforcement mechanism.
    slides = slides[:MAX_SLIDES]
    if len(slides) < MIN_SLIDES:
        # No retry here - a hard requirement would just trade "too few
        # slides" for "raise on a borderline story," and a thin carousel is
        # still a real, postable carousel. Logged so an under-shooting model
        # is at least visible/debuggable rather than silently accepted.
        logger.warning(
            "Carousel shotlist for content_item %s returned only %d slide(s), fewer than the "
            "requested minimum of %d",
            content_item.id, len(slides), MIN_SLIDES,
        )
    content_item.carousel_slides = slides
    db.commit()
    return slides


def _craft_style_guide(db: Session, content_item: ContentItem, slides: list[dict]) -> str:
    article_text = content_item.article_full_text or content_item.article_summary or ""
    user_prompt = build_style_guide_user_prompt(
        content_item.article_title, article_text, content_item.carousel_script or "", slides
    )

    provider = ChatProvider(db, content_item.brand_kit_id)
    system_prompt = resolve_prompt(
        db, content_item.brand_kit_id, "carousel_style_guide", SYSTEM_PROMPT_STYLE_GUIDE
    )
    result = provider.complete(
        agent_task="carousel_direction",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        content_item_id=content_item.id,
    )
    return result.text.strip() or (
        "Flat, high-contrast editorial illustration style, muted neutral palette, soft even lighting. "
        "Every slide reserves a plain top 30-35% text zone and keeps the bottom 10% calm for a brand strip."
    )


def _craft_slide_prompt(
    db: Session,
    content_item: ContentItem,
    style_guide: str,
    visual: str,
    headline: str,
    body_text: str,
    slide_number: int,
    slide_count: int,
    image_model_key: str,
) -> str:
    examples = get_example_prompts(image_model_key, DEFAULT_CATEGORY, headline)
    user_prompt = build_user_prompt_slide(
        style_guide, visual, headline, body_text, slide_number, slide_count, content_item.article_title
    ) + format_examples_block(examples)

    provider = ChatProvider(db, content_item.brand_kit_id)
    system_prompt = resolve_prompt(db, content_item.brand_kit_id, "carousel_slide", SYSTEM_PROMPT_SLIDE)
    result = provider.complete(
        agent_task="carousel_direction",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        content_item_id=content_item.id,
    )
    return result.text.strip() or (
        f"{visual} Rendered in the carousel's shared style. Add the headline \"{headline}\" and body "
        f'text "{body_text}" as styled typography in the top text zone. No other text.'
    )


def generate_carousel(db: Session, content_item: ContentItem) -> list[MediaAsset]:
    brand_kit = db.get(BrandKit, content_item.brand_kit_id)
    font_key = brand_kit.font_choice if brand_kit else "inter"
    brand_name = brand_kit.brand_name if brand_kit else ""
    social_handles = brand_kit.social_handles if brand_kit else {}
    website_url = brand_kit.website_url if brand_kit else None
    show_brand_name = brand_kit.show_brand_name_on_posters if brand_kit else True
    logo_bytes = None
    if brand_kit and brand_kit.logo_asset_path and brand_kit.show_logo_on_posters:
        try:
            logo_bytes = get_storage_backend().load(brand_kit.logo_asset_path)
        except Exception:
            logger.exception("Could not load brand logo, rendering carousel without it")

    source_name = None
    if brand_kit and brand_kit.show_source_attribution and content_item.article_url:
        email = (
            db.get(IngestedEmail, content_item.source_email_id) if content_item.source_email_id else None
        )
        source_name = resolve_source_name(content_item.article_url, email.sender if email else None)

    slides = _build_carousel_shotlist(db, content_item)
    if not slides:
        raise RuntimeError("Shot-listing produced no slides")

    image_model_key = get_image_model_key(db, content_item.brand_kit_id)
    image_model = IMAGE_MODEL_CHOICES[image_model_key]
    image_model_cost = IMAGE_GEN_COST_USD[image_model_key]
    provider = get_image_provider(db, content_item.brand_kit_id, image_model_key)
    if not provider:
        raise RuntimeError("No image provider configured (set an API key in Account settings)")

    storage = get_storage_backend()
    total_cost = 0.0

    # One shared brief, computed once per run and repeated into every
    # slide's own prompt below - this (not a shared reference image) is
    # what keeps the set feeling like one consistent carousel even though
    # each slide depicts a genuinely different visual. No fallback here -
    # if this fails, propagate; without it slides have nothing tying their
    # styles together (routes_board.py's _run_with_processing_state
    # surfaces it via content_item.last_error).
    style_guide = _craft_style_guide(db, content_item, slides)

    assets: list[MediaAsset] = []
    slide_count = len(slides)
    for i, slide in enumerate(slides):
        headline = slide.get("headline", "")
        body_text = slide.get("body_text", "")
        visual = slide.get("visual", "")

        try:
            slide_prompt = _craft_slide_prompt(
                db, content_item, style_guide, visual, headline, body_text, i + 1, slide_count, image_model_key
            )
            full_slide_bytes = provider.generate_full_design(
                prompt=slide_prompt,
                width=1080,
                height=1080,
                model=image_model,
            )
            total_cost += image_model_cost
            slide_cost = image_model_cost
            generation_prompt = slide_prompt
        except Exception:
            # Soft failure - fall back to a neutral branded card for this
            # one slide (no AI content text, same as a poster's "no
            # provider" fallback) rather than discarding the whole carousel
            # over one bad slide.
            logger.exception("Carousel slide %d/%d generation failed, falling back to a blank slide", i + 1, slide_count)
            full_slide_bytes = None
            slide_cost = 0.0
            generation_prompt = f"{headline} (fallback - slide generation failed)"

        if full_slide_bytes:
            png_bytes = add_brand_strip(
                full_slide_bytes,
                font_key=font_key,
                brand_name=brand_name,
                social_handles=social_handles,
                website_url=website_url,
                logo_bytes=logo_bytes,
                show_brand_name=show_brand_name,
            )
        else:
            png_bytes = render_fallback_poster(
                font_key=font_key,
                brand_name=brand_name,
                social_handles=social_handles,
                website_url=website_url,
                logo_bytes=logo_bytes,
                show_brand_name=show_brand_name,
            )

        if source_name:
            png_bytes = add_source_line(png_bytes, font_key, source_name)

        storage_uri = storage.save(png_bytes, f"{content_item.id}_slide_{i}.png")
        asset = MediaAsset(
            content_item_id=content_item.id,
            asset_type="carousel_slide",
            slide_index=i,
            storage_uri=storage_uri,
            generation_model=image_model,
            generation_prompt=generation_prompt,
            cost_usd=slide_cost,
        )
        db.add(asset)
        assets.append(asset)

    db.commit()
    for asset in assets:
        db.refresh(asset)
    return assets
