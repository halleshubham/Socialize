"""Reel template registry - the three-way split confirmed (via live research,
not invented) as a real, recognized short-form-video framework: faceless,
presenter/on-camera, and motion-graphics/animated. Mirrors
graphic_designer/templates.py's pattern: single source of truth for the
choice list (UI dropdown) and each template's generation behavior, inferred
by the Content Writer, user-overridable on the board before generation.

A fact/quote "text card" cutaway (see reel_editor/text_card.py) is a
cross-cutting scene type available within any of the three templates, not a
separate template of its own - confirmed against the same research as a
better fit than a 4th template.
"""

TEMPLATE_CHOICES: dict[str, str] = {
    "explainer_influencer": "Explainer with presenter",
    "faceless": "Faceless (graphics + voiceover)",
    "animated_contextual": "Animated / contextual",
}
DEFAULT_TEMPLATE = "faceless"

# Style guidance fed to the shot-listing prompt (reel_editor/prompts.py) -
# what actually differs per template: whether a consistent on-camera
# character carries the story, or voiceover-over-B-roll does, and whether
# the visual language is photorealistic or illustrated/motion-graphic.
TEMPLATE_GUIDANCE: dict[str, str] = {
    "explainer_influencer": (
        "A single consistent presenter appears on camera for the intro and outro host scenes "
        "(restate their visual description verbatim in every host scene, as usual for character "
        "consistency) - modern, minimal studio or home-office setting, soft warm lighting, plant "
        "or bookshelf softly out of focus behind them, casual-professional look. Between the host "
        "bookends, cut to data/graphics scenes that carry the "
        "host merely talks about - the numbers should be ON SCREEN, not just narrated. Quick, "
        "punchy cuts between host and graphics; no slow fades. For financial/statistical stories, "
        "use a deep navy/charcoal background language for graphics scenes with red for "
        "debt/loss/before-figures and green for settlement/resolution/after-figures, for visual "
        "contrast a viewer reads at a glance."
    ),
    "faceless": (
        "No recurring on-camera presenter - visuals are B-roll/scene-setting shots illustrating the "
        "story, carried by an unseen narrator's voiceover. Do not invent a character; if the article "
        "has a natural visual subject (a place, an object, a crowd), use that instead."
    ),
    "animated_contextual": (
        "Illustrated/motion-graphic visual language, not photorealistic - flat-color scenes, "
        "graphic shapes, icons, simple character illustrations, or data/map/timeline visuals that "
        "move. Carried by voiceover narration. Explicitly describe the illustration style in every "
        "scene (e.g. 'flat vector illustration, muted palette, subtle parallax motion') so it stays "
        "consistent across scenes."
    ),
}

# Folded into the Veo prompt as plain-language voice-character direction
# (graph.py's generate_reel) - Veo has no dedicated voice-selection
# parameter, so this is a best-effort description, not a guarantee. Only
# explainer_influencer gets an opinionated persona since it has an actual
# on-camera host to characterize; the other templates keep a neutral
# narrator description.
VOICE_DIRECTION: dict[str, str] = {
    "explainer_influencer": "a young, energetic Indian woman, confident and clear news-explainer tone",
    "faceless": "a calm, clear narrator",
    "animated_contextual": "a calm, clear narrator",
}
