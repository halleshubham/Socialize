"""Veo video generation for the Reel Editor agent, via the Gemini Developer
API (plain GOOGLE_API_KEY - confirmed reachable this way, not only through
Vertex AI, by inspecting the installed google-genai SDK directly in Sept
2026). Mirrors llm/image_provider.py's shape: a small protocol, one
implementation, real USD pricing baked in so the Reel Editor can enforce a
hard per-reel cost cap before generating each scene.
"""

import logging
import time
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

# Three tiers, model chosen per generate_reel call (see reel_editor/graph.py's
# SyncState-backed "reel video model" setting) rather than a single hardcoded
# constant - Lite stays the default (this is a personal tool and Veo costs
# add up fast), Fast and Standard are available as explicit opt-ins for a
# specific reel. ai.google.dev pricing confirmed live, Sept 2026 (fetched the
# official pricing page directly): all three tiers' listed per-second price
# already includes audio ("video with audio price (default)") - audio
# generation isn't even togglable on this API surface (see generate_scene's
# config_kwargs comment), so this is just informational.
VIDEO_MODEL_LITE = "veo-3.1-lite-generate-preview"
VIDEO_MODEL_FAST = "veo-3.1-fast-generate-preview"
VIDEO_MODEL_STANDARD = "veo-3.1-generate-preview"
VIDEO_MODEL = VIDEO_MODEL_LITE  # back-compat default
VIDEO_MODEL_CHOICES = {"lite": VIDEO_MODEL_LITE, "fast": VIDEO_MODEL_FAST, "standard": VIDEO_MODEL_STANDARD}
DEFAULT_VIDEO_MODEL_KEY = "lite"

COST_PER_SECOND_USD = {
    VIDEO_MODEL_LITE: 0.08,
    VIDEO_MODEL_FAST: 0.12,
    VIDEO_MODEL_STANDARD: 0.40,
}  # 1080p
CLIP_DURATION_SECONDS = 8
POLL_INTERVAL_SECONDS = 10
POLL_TIMEOUT_SECONDS = 360  # Veo docs: up to ~6 min at peak load

# Veo garbles on-screen text/subtitles it invents unprompted, in every
# language, not just Devanagari (confirmed via research citing Google's own
# Veo 3.1 prompting guide - "no subtitles" as an explicit negative prompt is
# the documented fix). Combined with the shot-listing prompt never asking
# for on-screen text in scenes at all - real quotes/facts go in text_card
# scenes instead, rendered correctly by Pillow (see reel_editor/text_card.py).
DEFAULT_NEGATIVE_PROMPT = "subtitles, captions, on-screen text, written words, watermark, garbled text"


@dataclass
class GeneratedClip:
    video_bytes: bytes
    last_frame_bytes: bytes | None  # fed to the next scene for continuity


class VideoGenProvider(Protocol):
    def generate_scene(
        self,
        prompt: str,
        starting_image: bytes | None = None,
        model: str = VIDEO_MODEL_LITE,
        negative_prompt: str = DEFAULT_NEGATIVE_PROMPT,
    ) -> GeneratedClip: ...


class GeminiVideoProvider:
    def __init__(self, api_key: str):
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._genai = genai

    def generate_scene(
        self,
        prompt: str,
        starting_image: bytes | None = None,
        model: str = VIDEO_MODEL_LITE,
        negative_prompt: str = DEFAULT_NEGATIVE_PROMPT,
    ) -> GeneratedClip:
        """starting_image animates FROM that image (the character reference
        for scene 1, or the previous clip's last frame for scene 2+ -
        continuity). NOTE: config.reference_images (Veo 3.1's dedicated
        subject-reference feature) is NOT used here - confirmed via a live
        400 error that veo-3.1-lite-generate-preview (our cost-conscious
        default) doesn't support it, only the ~5x pricier standard tier
        does. Consistency instead comes from (a) this starting-image chain
        and (b) the shot-list step repeating the character description in
        every scene's text prompt.

        negative_prompt has the SAME lite-tier restriction, confirmed via
        another live 400 error ("negativePrompt isn't supported by this
        model") - only attached on Standard; on Lite (and Fast, untested but
        treated the same out of caution - it's a preview tier like Lite, not
        confirmed to accept negativePrompt) the caller (reel_editor/graph.py)
        instead folds the same constraint into the prompt text itself as a
        plain-language suffix, which every tier accepts (weaker than a
        dedicated negative-prompt field, but the shot-listing prompt already
        avoids describing legible text in the first place, so this is
        defense in depth, not the primary fix)."""
        from google.genai import types

        config_kwargs: dict = {
            "duration_seconds": CLIP_DURATION_SECONDS,
            "resolution": "1080p",
            "aspect_ratio": "9:16",  # vertical, standard for reels
            # generate_audio is deliberately NOT set here - confirmed via a
            # live 400 error that it's rejected outright on the Gemini
            # Developer API surface ("only supported in Gemini Enterprise
            # Agent Platform mode"). Audio is generated unconditionally on
            # this API instead (confirmed separately: a clip made before
            # this file ever mentioned audio already had an AAC track).
        }
        if model == VIDEO_MODEL_STANDARD:
            config_kwargs["negative_prompt"] = negative_prompt
        image_arg = (
            types.Image(image_bytes=starting_image, mime_type="image/png") if starting_image else None
        )

        operation = self._client.models.generate_videos(
            model=model,
            prompt=prompt,
            image=image_arg,
            config=types.GenerateVideosConfig(**config_kwargs),
        )

        started = time.monotonic()
        while not operation.done:
            if time.monotonic() - started > POLL_TIMEOUT_SECONDS:
                raise TimeoutError(f"Veo generation exceeded {POLL_TIMEOUT_SECONDS}s")
            time.sleep(POLL_INTERVAL_SECONDS)
            operation = self._client.operations.get(operation)

        if operation.error:
            raise RuntimeError(f"Veo generation failed: {operation.error}")

        # A prompt Google's Responsible AI (RAI) safety filter rejects comes
        # back as a "successful" operation (operation.error is empty) but
        # with response.generated_videos left None rather than a real
        # video - confirmed live: this previously crashed as a bare
        # "'NoneType' object is not subscriptable" on the [0] below, with no
        # indication of why. response.rai_media_filtered_reasons carries
        # Google's actual reason when this happens (per the SDK's
        # GenerateVideosResponse type) - surfaced here so a rejected prompt
        # is diagnosable instead of looking like an app bug. Real risk for
        # this app specifically: its content leans on real-world social-
        # conflict subject matter (caste discrimination, protest, communal
        # violence) that safety filters are more likely to flag.
        response = operation.response
        if not response or not response.generated_videos:
            reasons = (response.rai_media_filtered_reasons if response else None) or []
            reason_text = "; ".join(reasons) if reasons else "no reason given by the API"
            raise RuntimeError(
                f"Veo returned no video for this scene - rejected by Google's content safety "
                f"filter (RAI), not a generation failure. Reason: {reason_text}"
            )

        generated = response.generated_videos[0]
        video_bytes = self._client.files.download(file=generated.video)

        last_frame_bytes = None
        try:
            last_frame_bytes = _extract_last_frame(video_bytes)
        except Exception:
            logger.exception("Could not extract last frame for scene continuity")

        return GeneratedClip(video_bytes=video_bytes, last_frame_bytes=last_frame_bytes)


def _extract_last_frame(video_bytes: bytes) -> bytes:
    """One JPEG frame from the very end of the clip, via ffmpeg - used as
    the next scene's starting image so consecutive clips actually connect
    instead of jump-cutting to an unrelated frame."""
    import subprocess
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        in_path = Path(tmp) / "clip.mp4"
        out_path = Path(tmp) / "last_frame.jpg"
        in_path.write_bytes(video_bytes)
        subprocess.run(
            ["ffmpeg", "-y", "-sseof", "-1", "-i", str(in_path), "-update", "1", "-q:v", "2", str(out_path)],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return out_path.read_bytes()


def get_video_provider(api_key: str | None) -> VideoGenProvider | None:
    """api_key is resolved by the caller (llm/user_keys.py's resolve_api_key)."""
    if api_key:
        return GeminiVideoProvider(api_key)
    return None


def estimate_clip_cost_usd(model: str = VIDEO_MODEL_LITE) -> float:
    return CLIP_DURATION_SECONDS * COST_PER_SECOND_USD.get(model, COST_PER_SECOND_USD[VIDEO_MODEL_LITE])
