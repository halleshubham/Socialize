"""Background-art generation for the Graphic Designer agent. Separate from
llm/provider.py's ChatProvider: LiteLLM's image-gen coverage is inconsistent
across providers, so this is its own small protocol wrapping each provider's
native SDK directly (per the approved plan).

GeminiImageProvider uses the Gemini Developer API (a plain GOOGLE_API_KEY,
not Vertex AI / a GCP project) via the google-genai SDK's generate_content
with response_modalities=["IMAGE"]. If GOOGLE_API_KEY isn't set,
get_image_provider returns None and the Graphic Designer falls back to a
brand-color gradient background instead of failing the whole poster.

Two models, picked per call site by which matters more:
- IMAGE_MODEL_STANDARD (gemini-2.5-flash-image, "Nano Banana", $0.039/image):
  background-only art with no text to get right - used for the legacy
  single-headline poster path's background, and reel character references.
- IMAGE_MODEL_PRO (gemini-3-pro-image, "Nano Banana Pro", ~$0.134/image at
  1K/2K per ai.google.dev/gemini-api/docs/pricing, confirmed Sept 2026):
  Google's higher-fidelity, better text-rendering tier - used for the
  full-design poster path (generate_full_design), where the model is
  composing and rendering every text field itself and accuracy matters far
  more than the ~3.4x cost difference.
"""

import logging
from typing import Protocol

logger = logging.getLogger(__name__)

IMAGE_MODEL_STANDARD = "gemini-2.5-flash-image"
IMAGE_MODEL_PRO = "gemini-3-pro-image"
IMAGE_MODEL = IMAGE_MODEL_STANDARD  # back-compat alias - background-only call sites

IMAGE_GEN_COST_USD_STANDARD = 0.039
IMAGE_GEN_COST_USD_PRO = 0.134


class ImageGenProvider(Protocol):
    def generate_background(self, prompt: str, width: int, height: int) -> bytes: ...
    def generate_full_design(self, prompt: str, width: int, height: int) -> bytes: ...


class GeminiImageProvider:
    def __init__(self, api_key: str):
        # Imported lazily so the google-genai dependency is only touched
        # when a key is actually configured.
        from google import genai

        self._client = genai.Client(api_key=api_key)

    def _generate(self, model: str, contents: str) -> bytes:
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
        raise RuntimeError(f"{model} returned no image data")

    def generate_background(self, prompt: str, width: int, height: int) -> bytes:
        return self._generate(
            IMAGE_MODEL_STANDARD,
            f"{prompt}. Photographic or illustrative background art suitable for a "
            f"social media poster, no text or words in the image.",
        )

    def generate_full_design(self, prompt: str, width: int, height: int) -> bytes:
        # No "no text" suffix here (unlike generate_background above) - the
        # whole point is the model renders every text field itself. See
        # graphic_designer/prompts.py's SYSTEM_PROMPT_FULL_DESIGN for what's
        # already baked into the prompt this receives.
        return self._generate(
            IMAGE_MODEL_PRO,
            f"{prompt}. Render this as a complete, finished social media poster image.",
        )


def get_image_provider(api_key: str | None) -> ImageGenProvider | None:
    """api_key is resolved by the caller (llm/user_keys.py's resolve_api_key
    - the brand owner's own "google" key if set, else the shared
    GOOGLE_API_KEY env fallback). None means neither is configured; the
    Graphic Designer falls back to a brand-color gradient background."""
    if api_key:
        return GeminiImageProvider(api_key)
    return None
