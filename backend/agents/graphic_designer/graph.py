import logging

from sqlalchemy.orm import Session

from backend.agents.graphic_designer.fonts import FONT_CHOICES, contains_devanagari
from backend.agents.graphic_designer.layout import add_brand_strip, add_source_line, render_fallback_poster
from backend.agents.graphic_designer.poster_render import RENDERERS
from backend.agents.graphic_designer.prompts import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_BACKGROUND_ONLY,
    SYSTEM_PROMPT_FULL_DESIGN,
    build_user_prompt,
    build_user_prompt_background_only,
    build_user_prompt_full_design,
)
from backend.agents.graphic_designer.templates import TEMPLATE_CHOICES, TEMPLATE_MOODS
from backend.agents.languages import DEFAULT_LANGUAGE, display_name
from backend.agents.source_attribution import resolve_source_name
from backend.app.db.models import BrandKit, ContentItem, IngestedEmail, MediaAsset
from backend.app.llm.image_provider import (
    IMAGE_GEN_COST_USD_PRO,
    IMAGE_GEN_COST_USD_STANDARD,
    IMAGE_MODEL_PRO,
    IMAGE_MODEL_STANDARD,
    get_image_provider,
)
from backend.app.llm.provider import ChatProvider
from backend.app.llm.user_keys import resolve_api_key
from backend.app.storage.local_disk import get_storage_backend

logger = logging.getLogger(__name__)


def _generic_prompt_fallback(headline: str, background_only: bool) -> str:
    if background_only:
        return (
            "Poster background art matching the mood of the story, no text or words anywhere in "
            "the image. Leave the middle third and bottom ~15% of the frame calm/low-detail."
        )
    return (
        f'Poster background art with the text "{headline}" rendered directly into the image as '
        "bold, high-contrast display typography, styled to match the mood of the scene. No other "
        "text or words anywhere in the image. Leave the bottom ~10% of the frame calm/low-detail."
    )


def _craft_image_prompt(db: Session, content_item: ContentItem, headline: str, background_only: bool) -> str:
    """Sonnet writes a specific, story-grounded prompt instead of a fixed
    template wrapped around the headline - a rigid per-article prompt makes
    every background look like the same generic stock-photo mood, and Pillow
    drawing the same plain white-text-on-dark-panel treatment every time
    looks monotonous even once backgrounds vary.

    background_only=True skips asking the image model to render the
    headline at all - used for Devanagari (Hindi/Marathi) headlines, since a
    live test showed Gemini's image model renders Devanagari as garbled,
    wrong conjuncts/matras rather than the actual text. Pillow (with a real
    Devanagari font, see fonts.py) draws it correctly afterward instead.
    English headlines still get the full AI-rendered-typography treatment,
    which tested well. Falls back to a generic (but still headline-aware)
    prompt if this call fails."""
    article_text = content_item.article_full_text or content_item.article_summary or ""
    if not article_text:
        return _generic_prompt_fallback(headline, background_only)

    system_prompt = SYSTEM_PROMPT_BACKGROUND_ONLY if background_only else SYSTEM_PROMPT
    user_prompt = (
        build_user_prompt_background_only(headline, content_item.article_title, article_text)
        if background_only
        else build_user_prompt(headline, content_item.article_title, article_text)
    )

    try:
        provider = ChatProvider(db, content_item.brand_kit_id)
        result = provider.complete(
            agent_task="graphic_designer_direction",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            content_item_id=content_item.id,
        )
        return result.text.strip() or _generic_prompt_fallback(headline, background_only)
    except Exception:
        logger.exception("Creative-direction prompt failed, using generic fallback")
        return _generic_prompt_fallback(headline, background_only)


def _generic_full_design_prompt(poster_content: dict) -> str:
    fields = ", ".join(f'{k}: "{v}"' for k, v in poster_content.items() if v)
    return (
        f"Design a complete social media poster including all of this text, composed and styled "
        f"however fits best, with real typographic hierarchy: {fields}. No other invented text. "
        "Leave the bottom ~10% of the frame calm/low-detail."
    )


def _craft_full_design_prompt(
    db: Session, content_item: ContentItem, template: str, poster_content: dict
) -> str:
    """Full creative control handed to the image model - it designs the
    complete poster (background, layout, all typography) for every field in
    poster_content, in whatever language the copy is in, including
    Devanagari. See prompts.py's SYSTEM_PROMPT_FULL_DESIGN docstring for the
    accepted trade-off this represents (user-confirmed: no Pillow safety net
    for Devanagari text accuracy here, in exchange for genuinely varied,
    freely-styled designs instead of a fixed per-template layout)."""
    article_text = content_item.article_full_text or content_item.article_summary or ""
    if not article_text:
        return _generic_full_design_prompt(poster_content)

    brand_kit = db.get(BrandKit, content_item.brand_kit_id)
    font_names = ", ".join(name for name, _ in FONT_CHOICES.values())
    language_name = display_name(content_item.language or DEFAULT_LANGUAGE)
    tone = brand_kit.tone_of_voice_prompt if brand_kit else ""
    template_label = TEMPLATE_CHOICES.get(template, template)
    template_mood = TEMPLATE_MOODS.get(template, "")

    try:
        provider = ChatProvider(db, content_item.brand_kit_id)
        result = provider.complete(
            agent_task="graphic_designer_direction",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_FULL_DESIGN.format(font_names=font_names)},
                {
                    "role": "user",
                    "content": build_user_prompt_full_design(
                        template_label,
                        template_mood,
                        poster_content,
                        language_name,
                        tone,
                        content_item.article_title,
                        article_text,
                    ),
                },
            ],
            content_item_id=content_item.id,
        )
        return result.text.strip() or _generic_full_design_prompt(poster_content)
    except Exception:
        logger.exception("Full-design creative-direction prompt failed, using generic fallback")
        return _generic_full_design_prompt(poster_content)


def generate_poster(db: Session, content_item: ContentItem) -> MediaAsset:
    brand_kit = db.get(BrandKit, content_item.brand_kit_id)
    headline = content_item.poster_headline or content_item.article_title
    font_key = brand_kit.font_choice if brand_kit else "inter"
    brand_name = brand_kit.brand_name if brand_kit else ""
    social_handles = brand_kit.social_handles if brand_kit else {}
    website_url = brand_kit.website_url if brand_kit else None
    logo_bytes = None
    if brand_kit and brand_kit.logo_asset_path:
        try:
            logo_bytes = get_storage_backend().load(brand_kit.logo_asset_path)
        except Exception:
            logger.exception("Could not load brand logo, rendering poster without it")

    template = content_item.poster_template
    poster_content = content_item.poster_content or {}
    use_template = template in RENDERERS and bool(poster_content)

    api_key = resolve_api_key(db, content_item.brand_kit_id, "google")
    provider = get_image_provider(api_key)
    full_image_bytes = None  # AI-designed complete poster (text included) - templated posters only
    background_bytes = None  # AI background only - legacy single-headline path
    generation_model = None
    image_prompt = None
    cost_usd = 0.0

    if provider and use_template:
        # Full creative control: the image model designs background, layout,
        # and every text field itself (see _craft_full_design_prompt), via
        # the higher-accuracy Pro tier - see image_provider.py's docstring.
        image_prompt = _craft_full_design_prompt(db, content_item, template, poster_content)
        try:
            full_image_bytes = provider.generate_full_design(prompt=image_prompt, width=1080, height=1080)
            generation_model = IMAGE_MODEL_PRO
            cost_usd = IMAGE_GEN_COST_USD_PRO
        except Exception:
            logger.exception("Full-design generation failed, falling back to the deterministic template layout")
    elif provider:
        # Legacy path (no poster_template/poster_content saved) - unchanged:
        # AI can't reliably render Devanagari, so Hindi/Marathi headlines
        # only get an AI background and Pillow draws the (correct) text.
        pillow_draws_text = contains_devanagari(headline)
        image_prompt = _craft_image_prompt(db, content_item, headline, background_only=pillow_draws_text)
        try:
            background_bytes = provider.generate_background(prompt=image_prompt, width=1080, height=1080)
            generation_model = IMAGE_MODEL_STANDARD
            cost_usd = IMAGE_GEN_COST_USD_STANDARD
        except Exception:
            logger.exception("AI background generation failed, falling back to neutral gradient")

    if full_image_bytes:
        png_bytes = add_brand_strip(
            full_image_bytes,
            font_key=font_key,
            brand_name=brand_name,
            social_handles=social_handles,
            website_url=website_url,
            logo_bytes=logo_bytes,
        )
    elif use_template:
        # No provider configured, or the full-design call failed - fall back
        # to the deterministic per-template Pillow layout rather than
        # producing nothing.
        png_bytes = RENDERERS[template](
            poster_content,
            headline,
            font_key,
            None,
            brand_name,
            social_handles,
            website_url,
            logo_bytes,
        )
    elif background_bytes and not contains_devanagari(headline):
        # Image model already drew the headline - just stamp the brand strip.
        png_bytes = add_brand_strip(
            background_bytes,
            font_key=font_key,
            brand_name=brand_name,
            social_handles=social_handles,
            website_url=website_url,
            logo_bytes=logo_bytes,
        )
    else:
        # Either no AI background at all, or there is one but Pillow still
        # needs to draw the (Devanagari) headline over it. Also the path for
        # legacy items with no poster_template/poster_content saved.
        png_bytes = render_fallback_poster(
            headline,
            font_key=font_key,
            brand_name=brand_name,
            social_handles=social_handles,
            website_url=website_url,
            background_bytes=background_bytes,
            logo_bytes=logo_bytes,
        )

    # Applied last, regardless of which branch above produced png_bytes, so
    # attribution to the original publication is guaranteed rather than
    # dependent on the image model remembering to include it (full-design
    # AI prompts don't ask for this - it's stamped on deterministically here
    # instead, same reasoning as the brand strip).
    if content_item.source_email_id:
        email = db.get(IngestedEmail, content_item.source_email_id)
        source_name = resolve_source_name(content_item.article_url, email.sender if email else None)
        png_bytes = add_source_line(png_bytes, font_key, source_name)

    storage = get_storage_backend()
    storage_uri = storage.save(png_bytes, f"{content_item.id}.png")

    asset = MediaAsset(
        content_item_id=content_item.id,
        asset_type="poster",
        storage_uri=storage_uri,
        generation_model=generation_model,
        generation_prompt=image_prompt or headline,
        cost_usd=cost_usd,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset
