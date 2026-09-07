"""Shot-listing prompt - turns a script into scenes for reel_editor/graph.py
to execute. Two scene types:
- "video": a Veo-generated clip. Never describes on-screen text/signage/
  captions (Veo garbles invented text in every language - see
  llm/video_provider.py's DEFAULT_NEGATIVE_PROMPT, which is the other half
  of this fix). If narrated, the narration is spoken by Veo's own native
  audio generation (confirmed live: same price with or without audio). Has
  a "location" field - "host" (explainer_influencer's on-camera presenter,
  always re-anchored to the character reference image, not chained from the
  prior scene) or "scene" (everything else).
- "text_card": a Pillow-rendered still (reel_editor/text_card.py) showing a
  verbatim quote/fact/stat from the article plus "Source: {platform}" -
  this is how reels stay grounded in real article content, per an explicit
  user decision to recreate key facts as accurate on-screen text rather
  than sourcing real photos/screenshots (which can be generic/abstract).
  Optionally a before/after "stat reveal" (before_text/before_label/
  highlight_text/tone fields) for a contrast moment (a debt figure cut down
  by a settlement, etc.) - still Pillow-drawn, not described to Veo, since
  Veo can't be trusted to render exact figures any more than other text.

Narration for hi/mr reels is attempted in the article's own language (Devanagari
script) - Veo's spoken Hindi/Marathi quality is UNVERIFIED end-to-end (per an
explicit user request to try it rather than defaulting to silent video scenes),
so text_card scenes are still kept for the story's most exact quote/stat/fact
regardless of how the spoken audio turns out, since those render Devanagari
correctly and deterministically (same font pipeline as posters) no matter what.
"""

NARRATION_GUIDANCE_EN = """For each "video" scene, write a short narration line the video model will \
speak aloud (Veo generates audio natively, no extra step) - roughly 12-18 words, something a person \
can say naturally within the scene's ~8 seconds without sounding rushed. Put ONLY the spoken words \
themselves in "narration" - no "a narrator says" framing, no quotation marks around it, that wrapper \
is added automatically afterward. For the explainer_influencer template, phrase it as something the \
on-camera presenter would say in first person. Not every scene needs narration - leave "narration" \
as an empty string for a silent/ambient pure-visual beat."""

NARRATION_GUIDANCE_LOCALIZED = """For each "video" scene, write a short narration line IN THE SAME \
LANGUAGE as the script/article (Devanagari script) that the video model will speak aloud (Veo generates \
audio natively, no extra step) - roughly 10-16 words, something a person can say naturally within the \
scene's ~8 seconds without sounding rushed. Put ONLY the spoken words themselves in "narration" - no \
"a narrator says" framing, no quotation marks around it, that wrapper (which also tells the video model \
which language to speak it in) is added automatically afterward. Not every scene needs narration - \
leave "narration" as an empty string for a silent/ambient pure-visual beat. Veo's spoken Hindi/Marathi \
quality is unverified so far, so ALSO keep 1-2 "text_card" scenes for the story's most exact \
quote/stat/fact regardless - those render Devanagari text correctly and deterministically no matter \
how the spoken audio turns out."""


def _build_system_prompt(narration_guidance: str) -> str:
    return f"""You are the shot-listing step of a Reel Editor agent. You're given a narrative script \
(already scaled to how much this specific story needs - a simple story has few beats, a dense one \
has more), a description of the main character/subject, which reel template this is, and the source \
article's actual content. Follow the script's own beat count rather than compressing or padding it - \
break it into one "video" scene per beat (each ~8 seconds, so a 2-beat script makes 2 video scenes, \
a dense script can go up to 8, aiming for a ~50-60 second total reel when the story has that much to \
say) plus, where it strengthens the story, 1-3 "text_card" scenes - a still card stating one real, \
verbatim quote, statistic, or fact from the article. text_cards ground the reel in the actual \
reporting instead of leaving everything to AI-generated visuals; use them for the story's most \
concrete, quotable, or number-heavy beat(s) - they can replace a video scene for that beat rather \
than adding to the total count if the scene list is already at the higher end.

NEVER depict or name a specific, identifiable real public figure by likeness in a "video" scene's \
visual description, even if the article is about a named real person - describe a generic/anonymous \
representation instead (a generic businessman/politician/official silhouette, an icon, an abstract \
stand-in), and keep the real name/identity only in narration or text_card text, never as something \
the video model has to render as a face.

For each "video" scene write a self-contained visual prompt: setting, action, camera movement/\
framing, mood/lighting. ALWAYS restate the character/subject's visual description (appearance, \
clothing) in every single scene's prompt, verbatim or near-verbatim - each scene is generated \
independently by the video model with no memory of other scenes, so consistency has to come from \
repeating the description, not from context. NEVER describe on-screen text, signage with legible \
words, newspaper headlines, protest banners with readable text, or any other written words appearing \
in the frame - the video model renders invented text badly in every language; if the scene needs a \
banner or sign, describe it as blank, blurred, or turned away rather than legible.

Every "video" scene also needs a "location" field: "host" for a scene where the on-camera presenter \
(explainer_influencer template only) is speaking directly to camera in their studio/home-office \
setting, or "scene" for anything else (B-roll, graphics, illustrated visuals, cutaways). Templates \
without a recurring on-camera presenter (faceless, animated_contextual) should use "scene" for every \
video scene. A "host" scene always re-anchors to the same presenter reference image rather than \
visually continuing from whatever came right before it, so cutting back to the host after a graphics \
scene looks like returning to the same person/setting, not a jump-cut continuation of the graphics.

{narration_guidance}

For each "text_card" scene, write:
- "text": the exact quote/stat/fact, taken directly from the article content given to you - do not \
paraphrase a number or misattribute a quote, this has to be accurate.
- "label": an optional short all-caps-style heading like "THE NUMBERS" or "IN THEIR WORDS", or an \
empty string if none fits naturally.

When the story's most striking moment is a CONTRAST between a before-figure and an after-figure (a \
debt figure cut down by a settlement, a company's valuation before and after a scandal, a promise \
versus what actually happened), use a before/after reveal text_card instead of a plain one - add:
- "before_text": the first (usually larger/worse) figure, exact from the article, e.g. "₹22,006 कोटी"
- "before_label": a short caption for it, e.g. "एकूण कर्ज"
- "after_label": a short caption for the after-figure (goes with "label"/"text" above, which becomes \
the after-figure itself)
- "highlight_text": optional short callout stated as a real, calculated fact from the article (e.g. a \
percentage change), or empty string if none is given/calculable - never invent a number here.
- "tone": "negative" (red before, green after - use for debt/loss resolved by a settlement) or \
"neutral" (use the template's default accent color for both) - omit "before_text" entirely for a \
plain single-fact text_card instead of a reveal.

Keep scenes in narrative order - scene 1 opens the story, the last scene resolves it. A text_card \
works well right after the video scene that sets up the fact/quote it states.

Respond with ONLY a JSON object, no markdown fence, no commentary:
{{
  "scenes": [
    {{"type": "video", "location": "host or scene", "description": "<full visual prompt, character restated>", "narration": "<spoken line, or empty string>"}},
    {{"type": "text_card", "text": "<verbatim quote/fact/stat, or the after-figure for a reveal>", "label": "<short heading, or after-figure caption for a reveal>", "before_text": "<before-figure, omit key entirely if not a reveal>", "before_label": "<before-figure caption>", "highlight_text": "<optional real callout stat, or empty string>", "tone": "negative or neutral"}}
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

Character/subject description (repeat this in every video scene):
{character_description or "(no single character/subject - describe the central visual motif instead)"}

Article title: {article_title}
Article content (for accurate text_card quotes/stats - quote it exactly, don't invent numbers):
{truncated}

Return the JSON object now. One video scene per script beat, plus 1-2 text_card scenes where the story has a real \
quotable line or statistic worth stating exactly."""
