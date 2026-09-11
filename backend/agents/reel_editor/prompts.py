"""Shot-listing prompt - turns a script into scenes for reel_editor/graph.py
to execute. Every scene is a Veo-generated "video" clip - there is no
Pillow-rendered scene type for content text (see docs/architecture.md's
"no programmatic content text" policy).

On-screen text is explicitly NOT used, in any language - the shot-listing
prompt below never asks for it, and video_provider.py's negative_prompt/
per-scene prompt suffix actively suppress it. This reverses an earlier
attempt at letting Veo render on-screen text itself: live-tested on a real
sensitive story, it came back completely garbled (nonsense glyphs, not
just imperfect) and the framing was often wrong too (Veo composited the
scene inside a bordered "poster" box rather than full-bleed video) - a
worse result than not having text at all. Any exact quote/stat/fact from
the article is carried through narration only, never on-screen text.

Each scene has a "location" field - "host" (explainer_influencer's
on-camera presenter, always re-anchored to the character reference image,
not chained from the prior scene) or "scene" (everything else).

Each scene also carries a "music" field (this module - short mood/genre
direction, folded into the Veo prompt by reel_editor/graph.py) and, once
built, a "timeframe" and "text_on_visual" field - both of those two are
added deterministically by reel_editor/graph.py's _build_shotlist AFTER
parsing the model's response, not asked of the model: timeframe is pure
arithmetic (scene index * CLIP_DURATION_SECONDS), and text_on_visual is
always the empty string by policy (see above - never asking the model to
fill it keeps this on-screen-text-off policy from ever being second-guessed
per scene). All five fields together (timeframe, description, text_on_visual,
music, narration) are what the board's reel-review table shows per user
request - a classic shot-list/script table shape.
"""

NARRATION_GUIDANCE_EN = """For each scene, write a short narration line the video model will \
speak aloud (Veo generates audio natively, no extra step) - roughly 12-18 words, something a person \
can say naturally within the scene's ~8 seconds without sounding rushed. Put ONLY the spoken words \
themselves in "narration" - no "a narrator says" framing, no quotation marks around it, that wrapper \
is added automatically afterward. For the explainer_influencer template, phrase it as something the \
on-camera presenter would say in first person. This is the ONLY way facts/quotes/stats reach the \
viewer - there is no on-screen text, so narration must carry them, stated exactly (quote or number) \
as given in the article, never paraphrased or invented. Not every scene needs narration - leave \
"narration" as an empty string only for a silent/ambient pure-visual beat where nothing needs saying."""

NARRATION_GUIDANCE_LOCALIZED = """For each scene, write a short narration line IN THE SAME \
LANGUAGE as the script/article (Devanagari script) that the video model will speak aloud (Veo generates \
audio natively, no extra step) - roughly 10-16 words, something a person can say naturally within the \
scene's ~8 seconds without sounding rushed. Put ONLY the spoken words themselves in "narration" - no \
"a narrator says" framing, no quotation marks around it, that wrapper (which also tells the video model \
which language to speak it in) is added automatically afterward. This is the ONLY way facts/quotes/stats \
reach the viewer - there is no on-screen text, so narration must carry them, stated exactly as given in \
the article, never paraphrased or invented. Not every scene needs narration - leave "narration" as an \
empty string only for a silent/ambient pure-visual beat where nothing needs saying."""


MUSIC_GUIDANCE = """For each scene, also write a short "music" direction - the mood/genre of \
background music or score that fits this scene (a few words, e.g. "tense low synth pulse", "warm \
acoustic guitar, upbeat", "orchestral swell, hopeful"). The video model has no dedicated music \
control, so this becomes a plain-language direction folded into the generation prompt alongside the \
narration - keep it consistent with the story's overall mood rather than swinging wildly scene to \
scene, unless the story's own arc genuinely shifts tone (e.g. tense build-up resolving into an upbeat \
ending). Use "none" only for a scene meant to run on ambient/diegetic sound alone, no added score."""


def _build_system_prompt(narration_guidance: str) -> str:
    return f"""You are the shot-listing step of a Reel Editor agent. You're given a narrative script \
(already scaled to how much this specific story needs - a simple story has few beats, a dense one \
has more), a description of the main character/subject, which reel template this is, and the source \
article's actual content. Follow the script's own beat count rather than compressing or padding it - \
break it into one scene per beat (each ~8 seconds, so a 2-beat script makes 2 scenes, a dense script \
can go up to 8, aiming for a ~50-60 second total reel when the story has that much to say).

NEVER describe on-screen text, captions, subtitles, signage with legible words, or any other written \
words appearing in the frame - the video model renders invented/on-screen text badly and unreliably in \
every language, and any real quote/stat/fact from the article belongs in narration instead (see below), \
never as something drawn into the shot. If a scene would naturally have a sign or banner, describe it \
as blank, blurred, or turned away rather than legible.

NEVER depict or name a specific, identifiable real public figure by likeness in a scene's visual \
description, even if the article is about a named real person - describe a generic/anonymous \
representation instead (a generic businessman/politician/official silhouette, an icon, an abstract \
stand-in), and keep the real name/identity only in narration, never as something the video model has \
to render as a face.

For each scene write a self-contained visual prompt: setting, action, camera movement/framing, \
mood/lighting. ALWAYS restate the character/subject's visual description (appearance, clothing) in \
every single scene's prompt, verbatim or near-verbatim - each scene is generated independently by the \
video model with no memory of other scenes, so consistency has to come from repeating the description, \
not from context.

Every scene also needs a "location" field: "host" for a scene where the on-camera presenter \
(explainer_influencer template only) is speaking directly to camera in their studio/home-office \
setting, or "scene" for anything else (B-roll, graphics, illustrated visuals, cutaways). Templates \
without a recurring on-camera presenter (faceless, animated_contextual) should use "scene" for every \
scene. A "host" scene always re-anchors to the same presenter reference image rather than visually \
continuing from whatever came right before it, so cutting back to the host after a graphics scene \
looks like returning to the same person/setting, not a jump-cut continuation of the graphics.

{narration_guidance}

{MUSIC_GUIDANCE}

Keep scenes in narrative order - scene 1 opens the story, the last scene resolves it.

Respond with ONLY a JSON object, no markdown fence, no commentary:
{{
  "scenes": [
    {{"type": "video", "location": "host or scene", "description": "<full visual prompt, character restated, NO on-screen text>", "narration": "<spoken line, or empty string>", "music": "<short mood/genre direction, or \\"none\\">"}}
  ]
}}"""


SYSTEM_PROMPT = _build_system_prompt(NARRATION_GUIDANCE_EN)
SYSTEM_PROMPT_LOCALIZED = _build_system_prompt(NARRATION_GUIDANCE_LOCALIZED)


def build_user_prompt(
    reel_script: str,
    character_description: str,
    reel_template: str,
    template_guidance: str,
    article_title: str,
    article_text: str,
) -> str:
    truncated = article_text[:6000]
    return f"""Reel template: {reel_template} - {template_guidance}

Script:
{reel_script}

Character/subject description (repeat this in every scene):
{character_description or "(no single character/subject - describe the central visual motif instead)"}

Article title: {article_title}
Article content (for accurate narration of quotes/stats - state it exactly, don't invent numbers):
{truncated}

Return the JSON object now. One scene per script beat. No on-screen text anywhere."""
