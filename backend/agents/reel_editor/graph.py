"""Reel Editor pipeline: script -> shot list -> character reference image ->
per-scene generation (Veo for "video" scenes with subject-reference +
previous-clip continuity, Pillow+ffmpeg for "text_card" scenes) -> ffmpeg
stitch -> one MediaAsset. Enforces a hard per-reel cost cap (Veo costs real
money per second; text_card scenes are free/local) - stops generating
further video scenes once the next one would exceed it, rather than
blowing past a budget or failing the whole reel.
"""

import logging

from sqlalchemy.orm import Session

from backend.agents.json_utils import extract_json, sanitize_llm_json
from backend.agents.languages import DEFAULT_LANGUAGE
from backend.agents.source_attribution import resolve_source_name
from backend.agents.reel_editor.prompts import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_LOCALIZED,
    build_user_prompt,
)
from backend.agents.reel_editor.templates import (
    DEFAULT_TEMPLATE,
    TEMPLATE_CHOICES,
    TEMPLATE_GUIDANCE,
    VOICE_DIRECTION,
)
from backend.agents.reel_editor.text_card import render_stat_reveal_clip, render_text_card_clip
from backend.app.db.models import BrandKit, ContentItem, IngestedEmail, MediaAsset, SyncState
from backend.app.ffmpeg.stitch import stitch_clips
from backend.app.llm.image_provider import IMAGE_MODEL, get_image_provider
from backend.app.llm.provider import ChatProvider
from backend.app.llm.user_keys import resolve_api_key
from backend.app.llm.video_provider import (
    DEFAULT_NEGATIVE_PROMPT,
    DEFAULT_VIDEO_MODEL_KEY,
    VIDEO_MODEL_CHOICES,
    estimate_clip_cost_usd,
    get_video_provider,
)
from backend.app.storage.local_disk import get_storage_backend

logger = logging.getLogger(__name__)

DEFAULT_COST_CAP_USD = 2.50
COST_CAP_KEY = "reel_cost_cap_usd"
VIDEO_MODEL_KEY_SETTING = "reel_video_model_key"
CHARACTER_REF_COST_USD = 0.039  # same gemini-2.5-flash-image pricing as posters
# hi/mr reels get narration written in Devanagari script by the shot-lister
# (see prompts.py's NARRATION_GUIDANCE_LOCALIZED) and spoken by Veo directly,
# per an explicit user request to try it live rather than defaulting to
# silent video scenes - UNVERIFIED quality, hence text_card scenes are kept
# alongside narrated video scenes regardless, as a Devanagari-accurate
# fallback no matter how the spoken audio turns out. The English name here
# (not the Devanagari display name from languages.py) is what actually goes
# into the Veo prompt text below - Veo takes plain English instructions
# describing what to speak and in what language, e.g. "speaks in Marathi".
_LOCALIZED_NARRATION_LANGUAGES = {"hi", "mr"}
_NARRATION_LANGUAGE_NAME = {"hi": "Hindi", "mr": "Marathi"}


def get_cost_cap(db: Session, brand_kit_id) -> float:
    row = db.get(SyncState, (COST_CAP_KEY, brand_kit_id))
    return float(row.value) if row else DEFAULT_COST_CAP_USD


def set_cost_cap(db: Session, brand_kit_id, cap_usd: float) -> None:
    row = db.get(SyncState, (COST_CAP_KEY, brand_kit_id))
    if row:
        row.value = str(cap_usd)
    else:
        db.add(SyncState(key=COST_CAP_KEY, brand_kit_id=brand_kit_id, value=str(cap_usd)))
    db.commit()


def get_video_model_key(db: Session, brand_kit_id) -> str:
    row = db.get(SyncState, (VIDEO_MODEL_KEY_SETTING, brand_kit_id))
    return row.value if row and row.value in VIDEO_MODEL_CHOICES else DEFAULT_VIDEO_MODEL_KEY


def set_video_model_key(db: Session, brand_kit_id, model_key: str) -> None:
    if model_key not in VIDEO_MODEL_CHOICES:
        model_key = DEFAULT_VIDEO_MODEL_KEY
    row = db.get(SyncState, (VIDEO_MODEL_KEY_SETTING, brand_kit_id))
    if row:
        row.value = model_key
    else:
        db.add(SyncState(key=VIDEO_MODEL_KEY_SETTING, brand_kit_id=brand_kit_id, value=model_key))
    db.commit()


def _build_shotlist(db: Session, content_item: ContentItem) -> list[dict]:
    if content_item.reel_scenes:
        return content_item.reel_scenes  # already built (e.g. a prior partial run)

    language = content_item.language or DEFAULT_LANGUAGE
    system_prompt = SYSTEM_PROMPT_LOCALIZED if language in _LOCALIZED_NARRATION_LANGUAGES else SYSTEM_PROMPT
    reel_template = content_item.reel_template or DEFAULT_TEMPLATE
    article_text = content_item.article_full_text or content_item.article_summary or ""

    provider = ChatProvider(db, content_item.brand_kit_id)
    result = provider.complete(
        agent_task="reel_shotlist",
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": build_user_prompt(
                    content_item.reel_script or "",
                    content_item.character_description or "",
                    reel_template,
                    TEMPLATE_GUIDANCE.get(reel_template, ""),
                    content_item.article_title,
                    article_text,
                ),
            },
        ],
        content_item_id=content_item.id,
    )
    scenes = sanitize_llm_json(extract_json(result.text)).get("scenes", [])
    content_item.reel_scenes = scenes
    db.commit()
    return scenes


def generate_reel(db: Session, content_item: ContentItem) -> MediaAsset:
    if not content_item.reel_script:
        raise ValueError("No reel_script on this content item - Content Writer must run first")

    cost_cap = get_cost_cap(db, content_item.brand_kit_id)
    video_model_key = get_video_model_key(db, content_item.brand_kit_id)
    video_model = VIDEO_MODEL_CHOICES[video_model_key]
    reel_template = content_item.reel_template or DEFAULT_TEMPLATE
    scenes = _build_shotlist(db, content_item)
    if not scenes:
        raise RuntimeError("Shot-listing produced no scenes")

    brand_kit = db.get(BrandKit, content_item.brand_kit_id)
    font_key = brand_kit.font_choice if brand_kit else "inter"
    source_name = None
    if content_item.source_email_id:
        email = db.get(IngestedEmail, content_item.source_email_id)
        source_name = resolve_source_name(content_item.article_url, email.sender if email else None)

    google_api_key = resolve_api_key(db, content_item.brand_kit_id, "google")
    image_provider = get_image_provider(google_api_key)
    video_provider = get_video_provider(google_api_key)
    if not video_provider:
        raise RuntimeError("No video provider configured (no Google API key set - see Account settings)")

    total_cost = 0.0

    # Character reference image, once, reused as a subject reference on the
    # first video scene for identity consistency across the reel. Only
    # relevant for explainer_influencer (or any template that ended up with
    # a real character_description) - faceless/animated posts often have
    # none, which is fine, generate_scene just starts from nothing.
    #
    # Reused across retries rather than regenerated every time: found live
    # that a single reel item paid for FOUR character-reference generations
    # across repeated retries (same $0.039 each time) because this used to
    # always call generate_background() with no check for an existing one -
    # unlike reel_scenes, which was already cached on the content item.
    storage = get_storage_backend()
    character_ref_bytes = None
    existing_ref = (
        db.query(MediaAsset)
        .filter(
            MediaAsset.content_item_id == content_item.id,
            MediaAsset.asset_type == "character_reference",
        )
        .order_by(MediaAsset.created_at.desc())
        .first()
    )
    if existing_ref:
        try:
            character_ref_bytes = storage.load(existing_ref.storage_uri)
        except Exception:
            logger.exception("Could not load cached character reference, will regenerate")
    if character_ref_bytes is None and image_provider and content_item.character_description:
        try:
            character_ref_bytes = image_provider.generate_background(
                prompt=(
                    f"Character reference sheet, single subject, neutral background, "
                    f"front-facing: {content_item.character_description}"
                ),
                width=1024,
                height=1024,
            )
            total_cost += CHARACTER_REF_COST_USD
            ref_uri = storage.save(character_ref_bytes, f"{content_item.id}_character_ref.png")
            db.add(
                MediaAsset(
                    content_item_id=content_item.id,
                    asset_type="character_reference",
                    storage_uri=ref_uri,
                    generation_model=IMAGE_MODEL,
                    generation_prompt=content_item.character_description,
                    cost_usd=CHARACTER_REF_COST_USD,
                )
            )
            db.commit()
        except Exception:
            logger.exception("Character reference generation failed, continuing without it")

    clip_bytes_list: list[bytes] = []
    last_frame_bytes = None  # only carried forward across consecutive "video" scenes
    seen_video_scene = False
    scenes_generated = 0
    video_error: str | None = None  # set on a failed video scene; see the try/except below

    for scene in scenes:
        scene_type = scene.get("type", "video")

        if scene_type == "text_card":
            # Free/local - never counted against the cost cap, never breaks
            # video-to-video continuity (last_frame_bytes is left untouched).
            # A before/after stat reveal (e.g. a debt figure cut down to a
            # settlement figure) is still this same scene type, just with
            # before_text/before_label present - see text_card.py's
            # render_stat_reveal_clip docstring for why that's Pillow-drawn
            # rather than described to Veo (same "Veo can't render exact
            # figures reliably" reasoning as a plain text_card's quote/stat).
            try:
                if scene.get("before_text"):
                    clip_bytes = render_stat_reveal_clip(
                        before_text=scene.get("before_text", ""),
                        before_label=scene.get("before_label") or None,
                        after_text=scene.get("text", ""),
                        after_label=scene.get("label") or None,
                        font_key=font_key,
                        source_name=source_name,
                        highlight_text=scene.get("highlight_text") or None,
                        tone=scene.get("tone", "neutral"),
                    )
                else:
                    clip_bytes = render_text_card_clip(
                        text=scene.get("text", ""),
                        label=scene.get("label") or None,
                        font_key=font_key,
                        source_name=source_name,
                    )
                clip_bytes_list.append(clip_bytes)
                scenes_generated += 1
            except Exception:
                logger.exception("Text-card render failed for %s, skipping this scene", content_item.id)
            continue

        next_cost = estimate_clip_cost_usd(video_model)
        if total_cost + next_cost > cost_cap:
            logger.warning(
                "Reel %s hit cost cap ($%.2f) after %d/%d scenes - stopping here",
                content_item.id, cost_cap, scenes_generated, len(scenes),
            )
            break

        # A "host" scene (the on-camera presenter, explainer_influencer
        # template) always re-anchors to the character reference image
        # rather than chaining from whatever the previous scene's last frame
        # was - cutting back to the host after a graphics/B-roll scene
        # should look like the same person/setting again, not a visual
        # continuation of the unrelated scene before it.
        location = scene.get("location", "scene")
        if location == "host" and character_ref_bytes:
            starting_image = character_ref_bytes
        else:
            starting_image = last_frame_bytes or (character_ref_bytes if not seen_video_scene else None)

        description = scene.get("description", "")
        narration = scene.get("narration") or ""
        language = content_item.language or DEFAULT_LANGUAGE
        # Voice-character direction is folded into the prompt text as a
        # plain-language description, since Veo has no dedicated
        # voice-selection parameter - UNVERIFIED how reliably this actually
        # steers the generated voice, included as a best-effort attempt per
        # an explicit user request (young/energetic female Marathi narrator).
        voice_direction = VOICE_DIRECTION.get(reel_template, VOICE_DIRECTION[DEFAULT_TEMPLATE])
        if narration and language in _LOCALIZED_NARRATION_LANGUAGES:
            lang_name = _NARRATION_LANGUAGE_NAME.get(language, "")
            prompt = (
                f'{description} A narrator ({voice_direction}) speaks the following line aloud '
                f'in {lang_name}: "{narration}"'
            )
        elif narration:
            prompt = f'{description} A narrator ({voice_direction}) says: "{narration}"'
        else:
            prompt = description
        # Folded into the prompt text too, not just the negative_prompt
        # config field - that field 400s on the Lite tier (see
        # video_provider.py), so this is what actually reaches Lite reels.
        prompt = f"{prompt} (no on-screen text, no subtitles, no captions, no readable signage)"

        try:
            clip = video_provider.generate_scene(
                prompt=prompt,
                starting_image=starting_image,
                model=video_model,
                negative_prompt=DEFAULT_NEGATIVE_PROMPT,
            )
        except Exception as exc:
            # Unlike the cost-cap check above, this wasn't caught before -
            # a live 429 surfaced Google's raw JSON error verbatim as
            # last_error, unreadable and with no indication it was a
            # rate-limit/quota issue rather than an app bug. Translated
            # below - NOT assumed to be depleted billing: user-confirmed
            # live that account credits were still available when this
            # fired, so RESOURCE_EXHAUSTED here is Google's per-model
            # request-rate cap, not an empty wallet. veo-3.1-lite-generate-
            # preview is a preview model - those commonly carry strict
            # per-day/per-minute request caps independent of credit
            # balance. Also treated like the cost cap (stop, don't discard
            # whatever already rendered - including preceding text_cards,
            # which are free and often carry real article content) since
            # this will keep failing identically on every remaining scene
            # until the rate window resets, not just this one.
            error_text = str(exc)
            if "RESOURCE_EXHAUSTED" in error_text or "429" in error_text:
                video_error = (
                    "Gemini/Veo rate limit or quota exceeded (RESOURCE_EXHAUSTED) - likely a "
                    "per-day/per-minute request cap on this preview model, not depleted billing. "
                    "Check https://ai.dev/rate-limit for your actual usage/limits; "
                    "https://ai.studio/projects has billing details if it turns out to be that instead."
                )
            else:
                video_error = f"Video scene generation failed: {error_text[:300]}"
            logger.exception(
                "Reel %s: video scene generation failed after %d/%d scenes - stopping here",
                content_item.id, scenes_generated, len(scenes),
            )
            break

        clip_bytes_list.append(clip.video_bytes)
        # A host scene's last frame isn't a useful continuity anchor for
        # whatever comes next (a cut away from the presenter shouldn't look
        # like it's visually continuing from their face) - only non-host
        # scenes chain into each other.
        last_frame_bytes = None if location == "host" else clip.last_frame_bytes
        seen_video_scene = True
        total_cost += next_cost
        scenes_generated += 1

    if not clip_bytes_list:
        if video_error:
            raise RuntimeError(video_error)
        raise RuntimeError(
            f"Cost cap (${cost_cap:.2f}) is too low to generate even one scene "
            f"(~${estimate_clip_cost_usd(video_model):.2f}/scene at the {video_model_key} tier) - "
            f"raise it in settings."
        )

    final_video_bytes = stitch_clips(clip_bytes_list)

    storage = get_storage_backend()
    storage_uri = storage.save(final_video_bytes, f"{content_item.id}.mp4")

    if scenes_generated >= len(scenes):
        truncated_note = ""
    elif video_error:
        truncated_note = f" (stopped at {scenes_generated}/{len(scenes)} scenes - {video_error})"
    else:
        truncated_note = f" (cost-capped at {scenes_generated}/{len(scenes)} scenes)"
    asset = MediaAsset(
        content_item_id=content_item.id,
        asset_type="reel",
        storage_uri=storage_uri,
        generation_model=video_model,
        generation_prompt=(content_item.reel_script or "") + truncated_note,
        cost_usd=total_cost,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset
