"""Carousel Editor prompts: script -> shot list (N slides, 4-6, LLM-decided
per story density, each with its own headline/body_text AND a distinct
visual) -> a shared text style guide -> per-slide image generation.

Visual consistency across slides comes from a shared STYLE GUIDE folded into
every slide's own prompt as text (art style, palette, mood, lighting,
layout/text-zone rules) - not from generating one shared background image
and editing copies of it. Each slide is a fresh, complete image, depicting
that slide's own distinct visual, with its own headline/body_text rendered
onto it in one generation call. See carousel_editor/graph.py.

Earlier version generated one shared no-text background image and edited a
copy of it per slide, explicitly instructed to preserve that background "as-
is" - every slide ended up as the literal same scene with different text
stamped on it, not a real image series. This version drops the shared image
entirely in favor of shared style language, mirroring how reel_editor keeps
scenes visually distinct while consistent (each scene gets its own
`description`; only an identity/continuity anchor is ever reused, never a
"keep this exact frame" instruction).

No Pillow-drawn content text, same "no programmatic content text" policy as
posters/reels - each slide's headline/body_text is rendered by the image
model itself, verbatim, or not shown at all.
"""

SYSTEM_PROMPT_SHOTLIST = """You are the shot-listing step of a Carousel Editor agent. You're given a \
narrative script for a multi-slide picture carousel (a set of still images posted together, swiped \
through in order, telling one complete story) and the source article's actual content. Break the \
script into an ordered sequence of slides - one clear beat/idea per slide - choosing the slide count \
yourself based on how much the story actually needs: never fewer than 4, never more than 6. A simple, \
single-point story should use 4; a genuinely dense story with several distinct beats can use 5 or 6. \
Don't pad a simple story to hit 6, and don't compress a dense one down to 4.

Each slide needs:
- "headline": a short, punchy line (a handful of words, not a sentence) - the single most important \
  thing this slide says. Every slide's headline should read as part of one continuous story when swiped \
  through in order - slide 1 hooks the reader in, the last slide resolves/lands the point, and each one \
  in between advances the story one step rather than restating the same idea.
- "body_text": one supporting sentence or short line of context for this slide's headline - can be \
  empty string if the headline alone carries the beat fully.
- "visual": a concrete description of what THIS slide's image should actually show - a specific \
  setting, object, moment, or visual metaphor drawn from the article's real content, distinct from \
  every other slide's visual. This is a real picture-series, not one backdrop reused six times - slide \
  2's image must depict something different from slide 1's, advancing the story visually the same way \
  the headlines advance it narratively. Ground it in the actual article wherever the article gives you \
  enough to, don't default to generic stock-photo imagery when a specific real detail is available.

NEVER invent a fact, quote, or number that isn't in the article - state real ones exactly as given, \
and lean on framing/narrative language rather than invented specifics for anything not actually in the \
source material. "visual" descriptions may depict a scene/moment suggested by the article even if no \
photo of that exact moment exists (this is illustrative art, not a claim that a photo was taken) - just \
don't invent factual claims the headline/body_text would assert as true.

Write every slide's "headline" and "body_text" in the target language given to you below - match it \
exactly, do not translate to English or default to it, even if the script or article content you're \
given happens to be in a different language. This is the actual text the image model will render onto \
each slide, so it's the one thing here that must end up in the right language, not just the article's \
own framing. "visual" is a production instruction for the image model, not published text - always \
write it in English regardless of the target language above, the same way a reel's scene description \
stays English while its narration doesn't.

Any number/digit in a headline or body_text (a stat, a count, a date, anything) must be written using \
plain English/Latin numerals (0-9, e.g. "729"), never native-script numerals (e.g. Devanagari "७२९") - \
even when the target language is Hindi or Marathi and the rest of the text is in Devanagari script.

Keep slides in narrative order - slide 1 opens the story, the last slide resolves it.

Respond with ONLY a JSON object, no markdown fence, no commentary:
{
  "slides": [
    {"headline": "...", "body_text": "...", "visual": "..."}
  ]
}"""


def build_shotlist_user_prompt(
    carousel_script: str, article_title: str, article_text: str, language_name: str
) -> str:
    truncated = article_text[:6000]
    return f"""Script:
{carousel_script}

Article title: {article_title}
Article content (for exact facts/quotes/numbers, don't invent anything beyond this):
{truncated}

Target language for every slide's headline/body_text (visual stays English): {language_name}

Return the JSON object now. Between 4 and 6 slides, whatever the story actually needs - each with its \
own distinct visual, not a repeated backdrop."""


# Style guide: a text brief, not an image - the mechanism that keeps every
# slide feeling like one consistent set even though each slide depicts a
# genuinely different visual. Written once, with the full slide list already
# in hand, so it can pick one art style/palette/mood that actually works
# across every one of this carousel's specific visuals.
SYSTEM_PROMPT_STYLE_GUIDE = """You are the creative-direction step of a Carousel Editor agent. You're \
given an article being turned into a multi-slide picture carousel, plus the full list of slides already \
planned (each with its own headline and a distinct visual). Your job is to write a short STYLE GUIDE - \
not a prompt for any one image, a shared creative brief that will be repeated into every slide's own \
generation prompt so the whole set reads as one consistent piece of design, even though each slide \
depicts something different.

Cover, specifically:
- Art style and rendering technique (e.g. flat vector illustration, moody photographic realism, soft \
  editorial gouache painting, retro halftone print) - pick ONE and describe it concretely enough that \
  repeating this description into six different image prompts will actually produce six images that \
  look like they belong together.
- Color palette (name actual colors/tones, not just "vibrant" or "muted").
- Mood and lighting treatment.
Vary these choices from carousel to carousel based on what actually fits each story - two carousels on \
different articles should read as two different pieces of design, not the same house style reused.

Also state these two fixed technical constraints, which apply identically to every slide and exist so \
the whole set shares one layout template regardless of what each slide depicts:
- Every slide reserves a clearly bounded, deliberately plain, high-contrast-friendly text zone covering \
  the TOP 30-35% of the frame - simple background there (solid/gradient panel, soft blur, open sky/wall, \
  whatever fits the style), never the slide's busiest visual detail. That zone is where every slide's \
  headline, body text, and page-sequence indicator get rendered - it must stay visually calm on every \
  slide so text is always legible there no matter what the rest of the frame depicts.
- The bottom ~10% of every frame stays visually calm/low-detail too - a brand strip is added there \
  afterward and shouldn't fight with busy art.

No rendering of real named individuals' likenesses in any slide.

Respond with ONLY the style guide itself (4-6 sentences), no preamble, no quotes around your whole \
answer, no markdown, nothing else. Write it as direct instructions an image model will receive verbatim \
alongside each slide's own specific visual."""


def build_style_guide_user_prompt(
    article_title: str, article_text: str, carousel_script: str, slides: list[dict]
) -> str:
    truncated = article_text[:4000]
    slide_list = "\n".join(
        f"{i + 1}. {s.get('headline', '')} - visual: {s.get('visual', '')}" for i, s in enumerate(slides)
    )
    return f"""Carousel story/script:
{carousel_script}

Article title: {article_title}
Article context:
{truncated}

Planned slides (headline - visual):
{slide_list}

Write the shared style guide now - the art style/palette/mood that will make all {len(slides)} of these \
distinct visuals read as one consistent carousel, plus the two fixed layout constraints."""


# Per-slide: no reference image, no "preserve as-is" - each slide is a fresh,
# complete image combining its own distinct visual with its own text, kept
# consistent with the rest of the carousel purely by repeating the same
# style guide into every slide's prompt.
SYSTEM_PROMPT_SLIDE = """You are the creative-direction step of a Carousel Editor agent, writing the \
full image generation prompt for ONE slide of a multi-slide picture carousel. There is no reference \
image for this slide - you are directing a complete, fresh image: the scene/visual described below, \
composed with this slide's own text as real styled typography. Consistency with the carousel's other \
slides comes entirely from following the style guide given to you exactly, not from copying any prior \
image.

Hard requirements for the prompt you write:
- Open by directing the actual scene: render the "visual" description given to you below as the \
  slide's artwork, specifically and concretely - this is what makes each slide in the carousel look like \
  a distinct, real image rather than a repeated backdrop.
- Apply the style guide given to you below exactly - same art style/rendering technique, same color \
  palette, same mood and lighting - so this slide reads as part of the same set as every other slide in \
  this carousel.
- Follow the style guide's two fixed layout constraints precisely: reserve its described plain text zone \
  in the top of the frame for this slide's own headline, body text, and sequence indicator (nothing else \
  goes there, and none of the scene's artwork extends into it), and keep the bottom of the frame calm/ \
  low-detail per the style guide (a brand strip goes there afterward).
- It MUST instruct the model to render the exact headline and body text given to you below, verbatim \
  and unaltered - not paraphrased, shortened, or reworded - quoting them directly in your prompt, with \
  real typographic hierarchy (the headline reads as more important than the body text) within that zone.
- If the headline or body text contains any digit, instruct the model to render it as a plain English/ \
  Latin numeral (0-9), never a native-script numeral, even where the rest of the text is in Devanagari.
- It MUST also instruct the model to render a small sequence indicator in the top-right corner of that \
  same text zone - the slide's position given to you below (e.g. "2/6"), styled small and secondary, \
  much smaller than the headline, so it reads as a page marker rather than content. If this is NOT the \
  final slide, the indicator MUST also include a right-pointing arrow glyph or arrow shape immediately \
  after the number, signalling more slides follow when swiped. If this IS the final slide, show only \
  the number, no arrow.
- High contrast between the text and whatever's behind it in that zone.
- No other invented text, words, or numbers anywhere else in the image besides that headline, body \
  text, and the sequence indicator.
- No rendering of real named individuals' likenesses.

Respond with ONLY the image generation prompt itself (4-6 sentences), no preamble, no quotes around \
your whole answer, no markdown, nothing else."""


def build_user_prompt_slide(
    style_guide: str,
    visual: str,
    headline: str,
    body_text: str,
    slide_number: int,
    slide_count: int,
    article_title: str,
) -> str:
    is_last = slide_number == slide_count
    sequence_note = (
        f'Sequence indicator to render: "{slide_number}/{slide_count}", no arrow (this is the final slide).'
        if is_last
        else f'Sequence indicator to render: "{slide_number}/{slide_count}" followed by a right-pointing arrow.'
    )
    return f"""Style guide for this whole carousel (apply exactly):
{style_guide}

This is slide {slide_number} of {slide_count} in the carousel.
This slide's visual (what the image should actually show): {visual}
Headline to render (verbatim): "{headline}"
Body text to render (verbatim, may be empty): "{body_text}"
{sequence_note}

Article title (for grounding only, not to be quoted): {article_title}

Write the image generation prompt now: direct the specific visual above, rendered in the style guide's \
art style/palette/mood, with the headline/body text and sequence indicator composed into the reserved \
text zone as styled typography."""
