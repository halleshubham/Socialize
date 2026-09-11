"""Image generation for the Graphic Designer agent (posters) and Reel
Editor (character reference images). Separate from llm/provider.py's
ChatProvider: LiteLLM's image-gen coverage is inconsistent across
providers, so this is its own small protocol wrapping each provider's
native SDK directly (per the approved plan).

A brand picks ONE model (IMAGE_MODEL_CHOICES) that's used for every image
call this app makes - full-design posters, product-photo-reference
posters, legacy background-only posters, and reel character references -
there's no separate "cheap tier" model any more; see get_image_model_key/
set_image_model_key below (SyncState-backed, same per-brand pattern as
reel_editor/graph.py's get_video_model_key).

Two providers:
- GeminiImageProvider: the Gemini Developer API (a plain GOOGLE_API_KEY,
  not Vertex AI), via the google-genai SDK's generate_content with
  response_modalities=["IMAGE"]. Covers all three Gemini/"Nano Banana"
  tiers - which one is used is passed in per-call as `model`, not fixed at
  construction.
- OpenAIImageProvider: gpt-image-2 via the openai SDK's images.generate
  (text-to-image) / images.edit (reference-image-grounded, used for the
  product-photo path).

If neither the brand's own key nor the shared env-var fallback is
configured for whichever provider the selected model needs (see
llm/user_keys.py's resolve_api_key), get_image_provider returns None and
callers fall back to a brand-color gradient background (or, for a
product-photo poster, the real photo with no AI design) instead of
failing the whole poster.
"""

import logging
from typing import Protocol

from sqlalchemy.orm import Session

from backend.app.db.models import SyncState
from backend.app.llm.user_keys import resolve_api_key

logger = logging.getLogger(__name__)

IMAGE_MODEL_NANO_BANANA = "gemini-2.5-flash-image"
IMAGE_MODEL_NANO_BANANA_PRO = "gemini-3-pro-image"
IMAGE_MODEL_NANO_BANANA_2_LITE = "gemini-3.1-flash-lite-image"
IMAGE_MODEL_GPT_IMAGE_2 = "gpt-image-2"
IMAGE_MODEL = IMAGE_MODEL_NANO_BANANA  # back-compat alias

# Selectable per brand (get_image_model_key/set_image_model_key below).
# Nano Banana Pro is the default - per ai.google.dev/gemini-api/docs/pricing
# (fetched directly, Sept 2026) it's ~4x Nano Banana 2 Lite's price
# ($0.134 vs $0.0336/image, IMAGE_GEN_COST_USD below), but Lite's own prior
# justification here ("reportedly close to Pro-tier quality") was this
# app's own untested assumption, not a benchmark run against real posters -
# and poster engagement/typography quality is a live, explicit ask, so the
# real ~10 cents/poster delta is worth it as the default rather than
# something a brand has to discover and opt into. Lite-2 stays available
# (and still is what reel_editor's character-reference image uses whenever
# a brand explicitly selects it, since it reads this same per-brand
# setting) for anyone who wants to trade quality back for cost.
IMAGE_MODEL_CHOICES: dict[str, str] = {
    "nano_banana_2_lite": IMAGE_MODEL_NANO_BANANA_2_LITE,
    "nano_banana": IMAGE_MODEL_NANO_BANANA,
    "nano_banana_pro": IMAGE_MODEL_NANO_BANANA_PRO,
    "gpt_image_2": IMAGE_MODEL_GPT_IMAGE_2,
}
IMAGE_MODEL_LABELS: dict[str, str] = {
    "nano_banana_2_lite": "Nano Banana 2 Lite (Google)",
    "nano_banana": "Nano Banana (Google)",
    "nano_banana_pro": "Nano Banana Pro (Google, default)",
    "gpt_image_2": "GPT Image 2 (OpenAI)",
}
DEFAULT_IMAGE_MODEL_KEY = "nano_banana_pro"

# Which llm/user_keys.py provider name (see PROVIDERS there) each model's
# API key comes from.
IMAGE_MODEL_PROVIDER: dict[str, str] = {
    "nano_banana_2_lite": "google",
    "nano_banana": "google",
    "nano_banana_pro": "google",
    "gpt_image_2": "openai",
}

# Approximate per-image USD cost at this app's usual size (~1024-1080px
# square), standard (non-batch) pricing - used only for this app's own
# cost-tracking badge, not billed by us. Gemini figures confirmed directly
# off ai.google.dev/gemini-api/docs/pricing, Sept 2026. gpt-image-2 prices
# per output token ($30/1M) rather than a flat per-image rate - $0.06 is an
# approximate medium-quality, 1024x1024 estimate (OpenAI publishes no
# single official per-image figure), not a guaranteed exact charge.
IMAGE_GEN_COST_USD: dict[str, float] = {
    "nano_banana_2_lite": 0.0336,
    "nano_banana": 0.039,
    "nano_banana_pro": 0.134,
    "gpt_image_2": 0.06,
}

IMAGE_MODEL_KEY_SETTING = "poster_image_model_key"


def get_image_model_key(db: Session, brand_kit_id) -> str:
    row = db.get(SyncState, (IMAGE_MODEL_KEY_SETTING, brand_kit_id))
    return row.value if row and row.value in IMAGE_MODEL_CHOICES else DEFAULT_IMAGE_MODEL_KEY


def set_image_model_key(db: Session, brand_kit_id, model_key: str) -> None:
    if model_key not in IMAGE_MODEL_CHOICES:
        model_key = DEFAULT_IMAGE_MODEL_KEY
    row = db.get(SyncState, (IMAGE_MODEL_KEY_SETTING, brand_kit_id))
    if row:
        row.value = model_key
    else:
        db.add(SyncState(key=IMAGE_MODEL_KEY_SETTING, brand_kit_id=brand_kit_id, value=model_key))
    db.commit()


class ImageGenProvider(Protocol):
    def generate_background(self, prompt: str, width: int, height: int, model: str) -> bytes: ...
    def generate_full_design(
        self, prompt: str, width: int, height: int, model: str, reference_image: bytes | None = None
    ) -> bytes: ...


def _sniff_mime_type(image_bytes: bytes) -> str:
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"  # WooCommerce product photos are typically JPEG


class GeminiImageProvider:
    def __init__(self, api_key: str):
        # Imported lazily so the google-genai dependency is only touched
        # when a key is actually configured.
        from google import genai

        self._client = genai.Client(api_key=api_key)

    def _generate(self, model: str, contents) -> bytes:
        from google.genai import types

        response = self._client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio="1:1"),
            ),
        )
        for candidate in response.candidates or []:
            for part in candidate.content.parts or []:
                if part.inline_data and part.inline_data.data:
                    return part.inline_data.data

        # Found live: a prompt Google's safety filter rejects doesn't error -
        # it comes back as a normal response with no inline_data anywhere,
        # indistinguishable from a generic failure unless the refusal reason
        # is dug out explicitly (same shape as video_provider.py's RAI-filter
        # handling). This app's real content (caste violence, assassinated
        # journalists, communal conflict) is exactly what safety filters are
        # more likely to flag, so surfacing WHY matters here, not just THAT.
        block_reason = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
        finish_reasons = [
            str(c.finish_reason) for c in (response.candidates or []) if getattr(c, "finish_reason", None)
        ]
        text_parts = [
            part.text
            for c in (response.candidates or [])
            for part in ((c.content.parts or []) if c.content else [])
            if getattr(part, "text", None)
        ]
        details = "; ".join(
            filter(
                None,
                [
                    f"block_reason={block_reason}" if block_reason else None,
                    f"finish_reason={', '.join(finish_reasons)}" if finish_reasons else None,
                    f"model said: {' '.join(text_parts)[:300]}" if text_parts else None,
                ],
            )
        ) or "no reason given by the API - possibly a content safety filter"
        raise RuntimeError(f"{model} returned no image data ({details})")

    def generate_background(self, prompt: str, width: int, height: int, model: str) -> bytes:
        return self._generate(
            model,
            f"{prompt}. Photographic or illustrative background art suitable for a "
            f"social media poster, no text or words in the image.",
        )

    def generate_full_design(
        self, prompt: str, width: int, height: int, model: str, reference_image: bytes | None = None
    ) -> bytes:
        # No "no text" suffix here (unlike generate_background above) - the
        # whole point is the model renders every text field itself. See
        # graphic_designer/prompts.py's SYSTEM_PROMPT_FULL_DESIGN for what's
        # already baked into the prompt this receives.
        #
        # reference_image (a real product photo) is passed as an attached
        # image part ahead of the text prompt - the same image+text input
        # shape Gemini's own image-editing examples use - so the model
        # anchors the design to the real photo (see
        # prompts.py's SYSTEM_PROMPT_PRODUCT_PHOTO_DESIGN, which explicitly
        # instructs it to preserve the photo rather than repaint it) instead
        # of generating a from-scratch scene that ignores it.
        if reference_image:
            from google.genai import types

            contents = [
                types.Part.from_bytes(data=reference_image, mime_type=_sniff_mime_type(reference_image)),
                f"{prompt}. Render this as a complete, finished social media poster image, "
                f"using the attached photo as the real visual anchor - preserve it as-is.",
            ]
        else:
            contents = f"{prompt}. Render this as a complete, finished social media poster image."
        return self._generate(model, contents)


class OpenAIImageProvider:
    def __init__(self, api_key: str):
        # Imported lazily so the openai dependency is only touched when a
        # key is actually configured.
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)

    def _extract_bytes(self, model: str, response) -> bytes:
        import base64

        data = (response.data or [None])[0]
        b64 = getattr(data, "b64_json", None) if data else None
        if not b64:
            raise RuntimeError(f"{model} returned no image data")
        return base64.b64decode(b64)

    def generate_background(self, prompt: str, width: int, height: int, model: str) -> bytes:
        response = self._client.images.generate(
            model=model,
            prompt=(
                f"{prompt}. Photographic or illustrative background art suitable for a "
                f"social media poster, no text or words in the image."
            ),
            size="1024x1024",
            quality="medium",
        )
        return self._extract_bytes(model, response)

    def generate_full_design(
        self, prompt: str, width: int, height: int, model: str, reference_image: bytes | None = None
    ) -> bytes:
        full_prompt = f"{prompt}. Render this as a complete, finished social media poster image."
        if reference_image:
            full_prompt += (
                " Use the attached photo as the real visual anchor - preserve it as-is, don't repaint it."
            )
            mime = _sniff_mime_type(reference_image)
            ext = "png" if mime == "image/png" else "jpg"
            response = self._client.images.edit(
                model=model,
                image=(f"reference.{ext}", reference_image, mime),
                prompt=full_prompt,
                size="1024x1024",
                quality="high",
            )
        else:
            response = self._client.images.generate(
                model=model, prompt=full_prompt, size="1024x1024", quality="high"
            )
        return self._extract_bytes(model, response)


def get_image_provider(db: Session, brand_kit_id, model_key: str) -> ImageGenProvider | None:
    """Resolves the right provider class AND the right API key for
    whichever model this brand has selected (IMAGE_MODEL_PROVIDER) - the
    brand's own key for that provider if set, else the shared env-var
    fallback (llm/user_keys.py's resolve_api_key). None means neither is
    configured for that provider; callers fall back to a non-AI path
    instead of failing the whole poster."""
    provider_name = IMAGE_MODEL_PROVIDER.get(model_key, "google")
    api_key = resolve_api_key(db, brand_kit_id, provider_name)
    if not api_key:
        return None
    if provider_name == "openai":
        return OpenAIImageProvider(api_key)
    return GeminiImageProvider(api_key)
