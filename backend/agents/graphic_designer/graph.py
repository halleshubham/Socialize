import logging
import re

from sqlalchemy.orm import Session

from backend.agents.graphic_designer.layout import add_brand_strip, add_source_line, render_fallback_poster
from backend.agents.graphic_designer.prompts import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_FULL_DESIGN,
    SYSTEM_PROMPT_PRODUCT_PHOTO_DESIGN,
    build_user_prompt,
    build_user_prompt_full_design,
    build_user_prompt_product_photo_design,
)
from backend.agents.graphic_designer.templates import TEMPLATE_CHOICES, TEMPLATE_MOODS
from backend.agents.languages import DEFAULT_LANGUAGE, display_name
from backend.agents.source_attribution import resolve_source_name
from backend.app.db.models import BrandKit, ContentItem, IngestedEmail, MediaAsset
from backend.app.llm.image_provider import (
    IMAGE_GEN_COST_USD,
    IMAGE_MODEL_CHOICES,
    get_image_model_key,
    get_image_provider,
)
from backend.app.llm.prompt_library import (
    DEFAULT_CATEGORY,
    PRODUCT_PHOTO_CATEGORY,
    TEMPLATE_CATEGORY,
    format_examples_block,
    get_example_prompts,
)
from backend.app.llm.prompt_overrides import resolve_prompt
from backend.app.llm.provider import ChatProvider
from backend.app.storage.local_disk import get_storage_backend

logger = logging.getLogger(__name__)


def _generic_prompt_fallback(headline: str) -> str:
    return (
        f'Poster background art with the text "{headline}" rendered directly into the image as '
        "bold, high-contrast display typography, styled to match the mood of the scene. No other "
        "text or words anywhere in the image. Leave the bottom ~10% of the frame calm/low-detail."
    )


def _craft_image_prompt(db: Session, content_item: ContentItem, headline: str, image_model_key: str) -> str:
    """Sonnet writes a specific, story-grounded prompt instead of a fixed
    template wrapped around the headline - a rigid per-article prompt makes
    every background look like the same generic stock-photo mood, and a
    plain white-text-on-dark-panel treatment every time looks monotonous
    even once backgrounds vary.

    Every language, including Devanagari, gets the full AI-rendered-
    typography treatment - the image model is instructed to render the
    headline verbatim (see SYSTEM_PROMPT), and the Media Review approval
    gate (regenerate if wrong) is the accepted safety net for cases where
    it doesn't. Falls back to a generic (but still headline-aware) prompt
    if this call fails."""
    article_text = content_item.article_full_text or content_item.article_summary or ""
    if not article_text:
        return _generic_prompt_fallback(headline)

    examples = get_example_prompts(image_model_key, DEFAULT_CATEGORY, headline)
    user_prompt = build_user_prompt(headline, content_item.article_title, article_text) + format_examples_block(
        examples
    )

    try:
        provider = ChatProvider(db, content_item.brand_kit_id)
        system_prompt = resolve_prompt(db, content_item.brand_kit_id, "graphic_designer_headline", SYSTEM_PROMPT)
        result = provider.complete(
            agent_task="graphic_designer_direction",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            content_item_id=content_item.id,
        )
        return result.text.strip() or _generic_prompt_fallback(headline)
    except Exception:
        logger.exception("Creative-direction prompt failed, using generic fallback")
        return _generic_prompt_fallback(headline)


def _generic_full_design_prompt(poster_content: dict) -> str:
    fields = ", ".join(f'{k}: "{v}"' for k, v in poster_content.items() if v)
    return (
        f"Design a complete social media poster including all of this text, composed and styled "
        f"however fits best, with real typographic hierarchy: {fields}. No other invented text. "
        "Leave the bottom ~10% of the frame calm/low-detail."
    )


def _craft_full_design_prompt(
    db: Session, content_item: ContentItem, template: str, poster_content: dict, image_model_key: str
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
    language_name = display_name(content_item.language or DEFAULT_LANGUAGE)
    tone = brand_kit.tone_of_voice_prompt if brand_kit else ""
    template_label = TEMPLATE_CHOICES.get(template, template)
    template_mood = TEMPLATE_MOODS.get(template, "")

    category = TEMPLATE_CATEGORY.get(template, DEFAULT_CATEGORY)
    query_text = " ".join(str(v) for v in poster_content.values() if v)
    examples = get_example_prompts(image_model_key, category, query_text)
    user_prompt = build_user_prompt_full_design(
        template_label,
        template_mood,
        poster_content,
        language_name,
        tone,
        content_item.article_title,
        article_text,
    ) + format_examples_block(examples)

    try:
        provider = ChatProvider(db, content_item.brand_kit_id)
        system_prompt = resolve_prompt(
            db, content_item.brand_kit_id, "graphic_designer_full_design", SYSTEM_PROMPT_FULL_DESIGN
        )
        result = provider.complete(
            agent_task="graphic_designer_direction",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            content_item_id=content_item.id,
        )
        return result.text.strip() or _generic_full_design_prompt(poster_content)
    except Exception:
        logger.exception("Full-design creative-direction prompt failed, using generic fallback")
        return _generic_full_design_prompt(poster_content)


_PRICE_LINE_RE = re.compile(r"^Price:\s*(.+)$", re.MULTILINE)


def _extract_real_price(article_full_text: str | None) -> str | None:
    """WooCommerce's grounding text (integrations/woocommerce/source.py's
    build_grounding_text) always writes the real listing price as its own
    "Price: {symbol}{amount}" line - pull it back out rather than ever
    asking an LLM to restate a number, so what reaches the image prompt is
    the exact real figure, not a paraphrase."""
    if not article_full_text:
        return None
    match = _PRICE_LINE_RE.search(article_full_text)
    return match.group(1).strip() if match else None


def _craft_product_photo_design_prompt(
    db: Session, content_item: ContentItem, poster_content: dict, price: str | None, image_model_key: str
) -> str:
    """Same idea as _craft_full_design_prompt, but for a product-photo
    poster: the real product photo is passed to the image model as a
    reference image (see image_provider.py's generate_full_design), so the
    creative-direction prompt asks it to design promotional elements around
    that real photo rather than a from-scratch scene. See prompts.py's
    SYSTEM_PROMPT_PRODUCT_PHOTO_DESIGN for what this is modeled on."""
    article_text = content_item.article_full_text or content_item.article_summary or ""
    if not article_text:
        return _generic_full_design_prompt(poster_content)

    brand_kit = db.get(BrandKit, content_item.brand_kit_id)
    language_name = display_name(content_item.language or DEFAULT_LANGUAGE)
    tone = brand_kit.tone_of_voice_prompt if brand_kit else ""

    query_text = " ".join(str(v) for v in poster_content.values() if v)
    examples = get_example_prompts(image_model_key, PRODUCT_PHOTO_CATEGORY, query_text)
    user_prompt = build_user_prompt_product_photo_design(
        poster_content,
        price,
        language_name,
        tone,
        content_item.article_title,
        article_text,
    ) + format_examples_block(examples)

    try:
        provider = ChatProvider(db, content_item.brand_kit_id)
        system_prompt = resolve_prompt(
            db, content_item.brand_kit_id, "graphic_designer_product_photo", SYSTEM_PROMPT_PRODUCT_PHOTO_DESIGN
        )
        result = provider.complete(
            agent_task="graphic_designer_direction",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            content_item_id=content_item.id,
        )
        return result.text.strip() or _generic_full_design_prompt(poster_content)
    except Exception:
        logger.exception("Product-photo creative-direction prompt failed, using generic fallback")
        return _generic_full_design_prompt(poster_content)


def generate_poster(db: Session, content_item: ContentItem) -> MediaAsset:
    brand_kit = db.get(BrandKit, content_item.brand_kit_id)
    headline = content_item.poster_headline or content_item.article_title
    font_key = brand_kit.font_choice if brand_kit else "inter"
    brand_name = brand_kit.brand_name if brand_kit else ""
    social_handles = brand_kit.social_handles if brand_kit else {}
    website_url = brand_kit.website_url if brand_kit else None
    logo_bytes = None
    if brand_kit and brand_kit.logo_asset_path and brand_kit.show_logo_on_posters:
        try:
            logo_bytes = get_storage_backend().load(brand_kit.logo_asset_path)
        except Exception:
            logger.exception("Could not load brand logo, rendering poster without it")

    template = content_item.poster_template
    poster_content = content_item.poster_content or {}
    use_full_design = bool(template and poster_content)
    show_brand_name = brand_kit.show_brand_name_on_posters if brand_kit else True

    # WooCommerce-sourced items (researcher/product_angles.py) get a real
    # product photo saved as a MediaAsset at creation time (see
    # routes_board.py's draft_from_website).
    product_photo_asset = (
        db.query(MediaAsset)
        .filter(MediaAsset.content_item_id == content_item.id, MediaAsset.asset_type == "product_photo")
        .order_by(MediaAsset.created_at.desc())
        .first()
    )
    product_photo_bytes = None
    if product_photo_asset:
        try:
            product_photo_bytes = get_storage_backend().load(product_photo_asset.storage_uri)
        except Exception:
            logger.exception("Could not load product photo, falling back to AI generation")

    image_model_key = get_image_model_key(db, content_item.brand_kit_id)
    image_model = IMAGE_MODEL_CHOICES[image_model_key]
    image_model_cost = IMAGE_GEN_COST_USD[image_model_key]
    provider = get_image_provider(db, content_item.brand_kit_id, image_model_key)
    full_image_bytes = None  # AI-designed complete poster (text included) - templated posters only
    background_bytes = None  # AI background, headline baked in by the model - legacy single-headline path
    generation_model = None
    image_prompt = None
    cost_usd = 0.0

    # If a provider IS configured but the actual generation call fails
    # (including a silent safety-filter refusal - Gemini returns a normal
    # response with no image data rather than an error, see
    # image_provider.py's _generate), this is let to propagate rather than
    # caught-and-silently-replaced with a blank gradient: a poster that
    # looks finished but has no content is worse than a visible retry-needed
    # state (routes_board.py's _run_with_processing_state catches this and
    # sets content_item.last_error, surfacing it on the board) - EXCEPT for
    # the product-photo branch just below, where there's a real photo to
    # fall back to instead of nothing, so that one stays a soft failure.
    # Only the creative-direction prompt steps (_craft_full_design_prompt/
    # _craft_image_prompt/_craft_product_photo_design_prompt) still have
    # their own internal fallback - a generic prompt is a much lower-stakes
    # degradation than no image at all.
    if provider and product_photo_bytes and use_full_design:
        # The real photo IS the product being sold - passed to the image
        # model as a reference image (image_provider.py's generate_full_design)
        # so it composes a real promotional design (headline, real price if
        # known) around the actual photo instead of ignoring it, matching a
        # real Wisdom Wear ad the user provided as a target (see
        # prompts.py's SYSTEM_PROMPT_PRODUCT_PHOTO_DESIGN). Soft-failure: if
        # this errors, fall through to the plain real-photo-only poster
        # below rather than propagating - a real, if plainer, photo is
        # strictly better than a blank card, unlike the no-photo cases.
        price = _extract_real_price(content_item.article_full_text)
        image_prompt = _craft_product_photo_design_prompt(
            db, content_item, poster_content, price, image_model_key
        )
        try:
            full_image_bytes = provider.generate_full_design(
                prompt=image_prompt,
                width=1080,
                height=1080,
                model=image_model,
                reference_image=product_photo_bytes,
            )
            generation_model = image_model
            cost_usd = image_model_cost
        except Exception:
            logger.exception("Product-photo full-design generation failed, falling back to the plain photo")
    elif provider and use_full_design:
        # Full creative control: the image model designs background, layout,
        # and every text field itself (see _craft_full_design_prompt).
        image_prompt = _craft_full_design_prompt(db, content_item, template, poster_content, image_model_key)
        full_image_bytes = provider.generate_full_design(
            prompt=image_prompt, width=1080, height=1080, model=image_model
        )
        generation_model = image_model
        cost_usd = image_model_cost
    elif provider and product_photo_bytes:
        # A product photo but no real poster_template/poster_content to
        # design around - nothing worth spending an AI call on, stays free:
        # just the real photo + brand strip + source, no caption overlay.
        pass
    elif provider:
        # Legacy path (no poster_template/poster_content saved) - the image
        # model is asked to render the headline itself, verbatim, in every
        # language including Devanagari (see _craft_image_prompt). If it
        # gets the glyphs wrong, Media Review's regenerate button is the
        # safety net - no Pillow-drawn text fallback.
        #
        # generate_full_design, NOT generate_background - this was a real
        # bug: generate_background unconditionally appends "no text or
        # words in the image" to whatever prompt it's given (correct for
        # its other two callers - reel_editor's character reference and
        # carousel_editor's shared background, both of which genuinely want
        # no text), but _craft_image_prompt's whole job is to write a
        # prompt that explicitly instructs the model to render the headline
        # verbatim. Feeding that through generate_background meant the
        # final prompt handed to the image model directly contradicted
        # itself ("...render the headline 'X'... no text or words in the
        # image."), which generate_full_design doesn't do.
        image_prompt = _craft_image_prompt(db, content_item, headline, image_model_key)
        background_bytes = provider.generate_full_design(
            prompt=image_prompt, width=1080, height=1080, model=image_model
        )
        generation_model = image_model
        cost_usd = image_model_cost

    if full_image_bytes:
        png_bytes = add_brand_strip(
            full_image_bytes,
            font_key=font_key,
            brand_name=brand_name,
            social_handles=social_handles,
            website_url=website_url,
            logo_bytes=logo_bytes,
            show_brand_name=show_brand_name,
        )
    elif background_bytes:
        # Image model already drew the headline (or attempted to) - just
        # stamp the brand strip, no Pillow-drawn content text.
        png_bytes = add_brand_strip(
            background_bytes,
            font_key=font_key,
            brand_name=brand_name,
            social_handles=social_handles,
            website_url=website_url,
            logo_bytes=logo_bytes,
            show_brand_name=show_brand_name,
        )
    else:
        # Either the real product photo (product_photo_bytes), no image
        # provider configured, or the AI call failed - background (real
        # photo, or a neutral gradient) + brand strip only. No headline.
        png_bytes = render_fallback_poster(
            font_key=font_key,
            brand_name=brand_name,
            social_handles=social_handles,
            website_url=website_url,
            background_bytes=product_photo_bytes,
            logo_bytes=logo_bytes,
            show_brand_name=show_brand_name,
        )

    # Applied last, regardless of which branch above produced png_bytes, so
    # attribution to the original publication is guaranteed rather than
    # dependent on the image model remembering to include it (full-design
    # AI prompts don't ask for this - it's stamped on deterministically here
    # instead, same reasoning as the brand strip). Independently toggleable
    # per brand, and no longer limited to email-sourced items.
    if brand_kit and brand_kit.show_source_attribution and content_item.article_url:
        email = db.get(IngestedEmail, content_item.source_email_id) if content_item.source_email_id else None
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
