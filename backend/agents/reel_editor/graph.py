"""Reel Editor pipeline: script -> shot list -> character reference image ->
per-scene generation (Veo, with subject-reference + previous-clip
continuity) -> ffmpeg stitch -> one MediaAsset. Enforces a hard per-reel
cost cap (Veo costs real money per second) - stops generating further
scenes once the next one would exceed it, rather than blowing past a
budget or failing the whole reel.

Nothing in a generated reel is ever Pillow-drawn - no on-screen text, no
closing source card. Every fact/quote/attribution that matters is carried
through narration alone; anything that can't be said aloud (source credit
included) just isn't part of a reel. See prompts.py's module docstring for
why on-screen text specifically was tried and reverted (garbled glyphs,
wrong framing, live-tested on a real reel).
"""

import logging

from sqlalchemy.orm import Session

from backend.agents.json_utils import extract_json, sanitize_llm_json
from backend.agents.languages import DEFAULT_LANGUAGE
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
from backend.app.db.models import ContentItem, MediaAsset, SyncState
from backend.app.ffmpeg.stitch import stitch_clips
from backend.app.llm.image_provider import (
    IMAGE_GEN_COST_USD,
    IMAGE_MODEL_CHOICES,
    get_image_model_key,
    get_image_provider,
)
from backend.app.llm.prompt_library import format_examples_block, get_reel_example_prompts
from backend.app.llm.prompt_overrides import resolve_prompt
from backend.app.llm.provider import ChatProvider
from backend.app.llm.user_keys import resolve_api_key
from backend.app.llm.video_provider import (
    CLIP_DURATION_SECONDS,
    DEFAULT_NEGATIVE_PROMPT,
    DEFAULT_VIDEO_MODEL_KEY,
    VIDEO_MODEL_CHOICES,
    estimate_clip_cost_usd,
    get_video_provider,
)
from backend.app.storage.local_disk import get_storage_backend

logger = logging.getLogger(__name__)

# Sized to actually afford the shot-listing prompt's own upper bound (up to
# 8 scenes for a dense story, see reel_editor/prompts.py) at either
# brand-selectable tier that isn't Standard: 8 scenes x 8s x $0.12/s (Fast,
# the new default) = $7.68, or 8 x 8 x $0.08 (Lite) = $5.12. The old $2.50
# only covered ~3-4 scenes even at Lite - most "dense" reels were hitting
# this cap and stopping mid-story, before their planned resolution scene,
# regardless of how good the per-clip generation itself was.
DEFAULT_COST_CAP_USD = 8.00
COST_CAP_KEY = "reel_cost_cap_usd"
VIDEO_MODEL_KEY_SETTING = "reel_video_model_key"
# hi/mr reels get narration written in Devanagari script by the shot-lister
# (see prompts.py's NARRATION_GUIDANCE_LOCALIZED) and spoken by Veo directly,
# and any on-screen text Veo is asked to render also uses this script -
# UNVERIFIED quality for either (Veo's spoken/rendered Devanagari accuracy
# hasn't been confirmed live the way English has), accepted per an explicit
# user request rather than falling back to a Pillow-drawn safety net. The
# English name here (not the Devanagari display name from languages.py) is
# what actually goes into the Veo prompt text below - Veo takes plain
# English instructions describing what to speak and in what language, e.g.
# "speaks in Marathi".
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
    localized = language in _LOCALIZED_NARRATION_LANGUAGES
    prompt_key = "reel_shotlist_localized" if localized else "reel_shotlist"
    default_prompt = SYSTEM_PROMPT_LOCALIZED if localized else SYSTEM_PROMPT
    system_prompt = resolve_prompt(db, content_item.brand_kit_id, prompt_key, default_prompt)
    reel_template = content_item.reel_template or DEFAULT_TEMPLATE
    article_text = content_item.article_full_text or content_item.article_summary or ""

    examples = get_reel_example_prompts(reel_template, content_item.reel_script or "")
    user_prompt = build_user_prompt(
        content_item.reel_script or "",
        content_item.character_description or "",
        reel_template,
        TEMPLATE_GUIDANCE.get(reel_template, ""),
        content_item.article_title,
        article_text,
    ) + format_examples_block(examples, subject="this kind of visual scene (borrowed from an image-prompt library, adapt to video)")

    provider = ChatProvider(db, content_item.brand_kit_id)
    result = provider.complete(
        agent_task="reel_shotlist",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        content_item_id=content_item.id,
    )
    scenes = sanitize_llm_json(extract_json(result.text)).get("scenes", [])
    # timeframe/text_on_visual are computed here, not asked of the model -
    # timeframe is pure arithmetic (every scene is a fixed
    # CLIP_DURATION_SECONDS), and text_on_visual is always empty by policy
    # (see prompts.py's module docstring on why on-screen text is never
    # generated). Both exist purely so the board's reel-review table can
    # show the classic timeframe/visual/text-on-visual/music/voiceover
    # shot-list shape per user request, without ever asking the model to
    # fill in a field that would contradict the no-on-screen-text policy.
    for i, scene in enumerate(scenes):
        scene["timeframe"] = f"{i * CLIP_DURATION_SECONDS}s–{(i + 1) * CLIP_DURATION_SECONDS}s"
        scene["text_on_visual"] = ""
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

    # WooCommerce-sourced items (researcher/product_angles.py) get a real
    # product photo saved as a MediaAsset at creation time - passed in below
    # as the first video scene's starting image, the same mechanism
    # character_ref_bytes already uses for continuity. Grounds Veo's
    # generation in the real product visually without hard-requiring it -
    # Veo may still not reproduce the exact print (known, accepted
    # trade-off - no AI model can reproduce an exact print/design anyway).
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
            logger.exception("Could not load product photo, continuing without it as a visual reference")

    image_model_key = get_image_model_key(db, content_item.brand_kit_id)
    image_model = IMAGE_MODEL_CHOICES[image_model_key]
    image_model_cost = IMAGE_GEN_COST_USD[image_model_key]
    image_provider = get_image_provider(db, content_item.brand_kit_id, image_model_key)
    video_provider = get_video_provider(resolve_api_key(db, content_item.brand_kit_id, "google"))
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
                model=image_model,
            )
            total_cost += image_model_cost
            ref_uri = storage.save(character_ref_bytes, f"{content_item.id}_character_ref.png")
            db.add(
                MediaAsset(
                    content_item_id=content_item.id,
                    asset_type="character_reference",
                    storage_uri=ref_uri,
                    generation_model=image_model,
                    generation_prompt=content_item.character_description,
                    cost_usd=image_model_cost,
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
        elif not seen_video_scene:
            starting_image = last_frame_bytes or character_ref_bytes or product_photo_bytes
        else:
            starting_image = last_frame_bytes

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
        # Same best-effort plain-language approach as voice_direction above -
        # Veo has no dedicated music/score parameter, so the shot-listing
        # step's per-scene "music" direction (see prompts.py's
        # MUSIC_GUIDANCE) is folded into the prompt text instead. "none"/
        # empty means the scene relies on ambient/diegetic sound alone, so
        # nothing is added to the prompt in that case rather than asking for
        # silence explicitly (which Veo generates audio unconditionally
        # regardless of what's asked - see video_provider.py).
        music = (scene.get("music") or "").strip()
        if music and music.lower() not in ("none", "no music", "n/a", "silence"):
            prompt = f"{prompt} Background music: {music}."
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
            # whatever already rendered) since this will keep failing
            # identically on every remaining scene until the rate window
            # resets, not just this one.
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
