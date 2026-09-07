"""Poster template registry - derived from analyzing a real set of 16
sample posters the user provided (Marathi social/history content: personality
tributes, quote cards, multi-slide narratives, sourced fact+critique pieces,
"did you know" trivia, and event publicity as a requested-but-unsampled type).
Single source of truth for the template list/labels (UI dropdown), the
poster_content JSON shape each template expects (Content Writer's prompt,
and each render function in layout.py), and each template's display name.

Carousels (the multi-slide narrative/quote series in the samples) are out of
scope for now - these are all single-slide renderings of the same template
patterns. See docs/architecture.md.
"""

TEMPLATE_CHOICES: dict[str, str] = {
    "quote": "Quote / poetry",
    "tribute": "Tribute / commemoration",
    "narrative": "Narrative / story",
    "fact_critique": "News fact + critique",
    "trivia": "Trivia / did-you-know",
    "event": "Event publicity",
}
DEFAULT_TEMPLATE = "quote"

# Compositional mood/purpose per template - fed to the image model as
# creative direction when it's designing the full poster freely (see
# graphic_designer/prompts.py's SYSTEM_PROMPT_FULL_DESIGN), not a rigid
# layout spec - the model decides actual arrangement/typography/color.
TEMPLATE_MOODS: dict[str, str] = {
    "quote": "a quote/poetry card - the quoted line is the visual hero, typically large and central, paired with a smaller attribution line",
    "tribute": "a tribute/commemoration poster honoring a person - their name reads as prominent, paired with what they're known for and a respectful closing line",
    "narrative": "a short story/narrative card - flowing body text telling what happened, read like a short passage rather than a headline",
    "fact_critique": "a sourced-fact poster making a critical point - the numbers/findings are visual anchors, often set apart as callouts, building to a sharp closing line",
    "trivia": "a lighter 'did you know' card - a hook, a fact, and one or two numbers or phrases worth visually emphasizing",
    "event": "an event publicity poster, laid out like a flyer - title, date/time, location, and a call to action",
}

# poster_content shape per template - what Content Writer must produce and
# what each layout.py render function reads. All string fields render
# verbatim (Pillow-drawn, or AI-drawn only for English - see graph.py) -
# Content Writer is the only place this text is ever generated.
TEMPLATE_FIELDS: dict[str, list[str]] = {
    # A quote/poem excerpt from someone, with attribution.
    "quote": ["quote_text", "attribution", "citation"],
    # Commemorating a person - name/title, what they're known for, an
    # optional short quote, and a closing respect line.
    "tribute": ["name", "achievement", "quote_text", "tribute_line"],
    # A short narrative/story beat, single-slide (a multi-slide version of
    # this is the carousel follow-up feature).
    "narrative": ["body_text"],
    # Sourced statistics + a pointed critical closing line.
    "fact_critique": ["facts", "critique_line"],  # facts: [{"stat": "...", "source": "..."}]
    # Lighter "did you know" framing, one or two numbers to visually highlight.
    "trivia": ["headline", "body_text", "highlight_phrases"],  # highlight_phrases: [str, ...]
    # Promotion for an upcoming event.
    "event": ["event_title", "datetime", "location", "cta"],
}
