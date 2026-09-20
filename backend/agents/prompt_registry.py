"""Single source of truth listing every brand-overridable system prompt in
the pipeline - one entry per distinct SYSTEM_PROMPT* constant. Finer-grained
than llm/provider.py's CONFIGURABLE_AGENT_TASKS (which groups by
model-routing/cost-logging task, e.g. Graphic Designer's three different
creative-direction prompts all share one agent_task there) - here each gets
its own independently-overridable prompt_key, since llm/prompt_overrides.py
stores full-text replacements per prompt_key, not per agent_task.

Drives the Brand Kit "Prompts" panel (routes_brand_kit.py) - each entry's
"default" is also the "reset to default" target and what's shown when a
brand has no override row yet.
"""

from backend.agents.analytical.prompts import SYSTEM_PROMPT as _ANALYTICAL_BRIEF
from backend.agents.analytical.prompts import SYSTEM_PROMPT_COMBINED as _ANALYTICAL_COMBINED
from backend.agents.content_writer.prompts import SYSTEM_PROMPT as _CONTENT_WRITER
from backend.agents.content_writer.prompts import SYSTEM_PROMPT_FORMAT_ONLY as _CONTENT_WRITER_FORMAT_ONLY
from backend.agents.content_writer.prompts import (
    SYSTEM_PROMPT_FORMAT_ONLY_PERSONAL as _CONTENT_WRITER_FORMAT_ONLY_PERSONAL,
)
from backend.agents.content_writer.prompts import (
    SYSTEM_PROMPT_FORMAT_ONLY_PRODUCT as _CONTENT_WRITER_FORMAT_ONLY_PRODUCT,
)
from backend.agents.content_writer.prompts import SYSTEM_PROMPT_LOCALIZED as _CONTENT_WRITER_LOCALIZED
from backend.agents.content_writer.prompts import SYSTEM_PROMPT_PERSONAL as _CONTENT_WRITER_PERSONAL
from backend.agents.content_writer.prompts import SYSTEM_PROMPT_PRODUCT as _CONTENT_WRITER_PRODUCT
from backend.agents.graphic_designer.prompts import SYSTEM_PROMPT as _GRAPHIC_DESIGNER_HEADLINE
from backend.agents.graphic_designer.prompts import SYSTEM_PROMPT_FULL_DESIGN as _GRAPHIC_DESIGNER_FULL_DESIGN
from backend.agents.graphic_designer.prompts import (
    SYSTEM_PROMPT_PRODUCT_PHOTO_DESIGN as _GRAPHIC_DESIGNER_PRODUCT_PHOTO,
)
from backend.agents.carousel_editor.prompts import SYSTEM_PROMPT_SHOTLIST as _CAROUSEL_SHOTLIST
from backend.agents.carousel_editor.prompts import SYSTEM_PROMPT_SLIDE as _CAROUSEL_SLIDE
from backend.agents.carousel_editor.prompts import SYSTEM_PROMPT_STYLE_GUIDE as _CAROUSEL_STYLE_GUIDE
from backend.agents.reel_editor.prompts import SYSTEM_PROMPT as _REEL_SHOTLIST
from backend.agents.reel_editor.prompts import SYSTEM_PROMPT_LOCALIZED as _REEL_SHOTLIST_LOCALIZED
from backend.agents.researcher.github_angle_prompts import SYSTEM_PROMPT as _RESEARCHER_GITHUB
from backend.agents.researcher.niche_evolution import DRAFT_FROM_BRAND_SYSTEM_PROMPT as _NICHE_DRAFT_FROM_BRAND
from backend.agents.researcher.niche_evolution import SYSTEM_PROMPT as _NICHE_EVOLUTION
from backend.agents.researcher.product_angle_prompts import SYSTEM_PROMPT as _RESEARCHER_PRODUCT
from backend.agents.researcher.prompts import SYSTEM_PROMPT as _RESEARCHER_TRIAGE
from backend.agents.researcher.rss_angle_prompts import SYSTEM_PROMPT as _RESEARCHER_RSS

# {prompt_key: {"label": ..., "group": ..., "default": ...}} - group drives
# the Prompts panel's section headings, in the order defined here.
PROMPT_SLOTS: dict[str, dict[str, str]] = {
    "researcher_triage": {
        "label": "Email triage (which articles are worth posting)",
        "group": "Researcher",
        "default": _RESEARCHER_TRIAGE,
    },
    "researcher_github_angles": {
        "label": "GitHub post angles",
        "group": "Researcher",
        "default": _RESEARCHER_GITHUB,
    },
    "researcher_product_angles": {
        "label": "Product post angles",
        "group": "Researcher",
        "default": _RESEARCHER_PRODUCT,
    },
    "researcher_rss_triage": {
        "label": "RSS feed triage",
        "group": "Researcher",
        "default": _RESEARCHER_RSS,
    },
    "researcher_niche_evolution": {
        "label": "Refine niche from triage history",
        "group": "Researcher",
        "default": _NICHE_EVOLUTION,
    },
    "researcher_niche_draft_from_brand": {
        "label": "Draft niche from brand info",
        "group": "Researcher",
        "default": _NICHE_DRAFT_FROM_BRAND,
    },
    "analytical_brief": {
        "label": "Write the brief",
        "group": "Analytical",
        "default": _ANALYTICAL_BRIEF,
    },
    "analytical_combined_draft": {
        "label": "Combined brief + post (combined_drafting brands)",
        "group": "Analytical",
        "default": _ANALYTICAL_COMBINED,
    },
    "content_writer": {
        "label": "Default (English/other languages)",
        "group": "Content Writer",
        "default": _CONTENT_WRITER,
    },
    "content_writer_localized": {
        "label": "Hindi/Marathi",
        "group": "Content Writer",
        "default": _CONTENT_WRITER_LOCALIZED,
    },
    "content_writer_personal": {
        "label": "Personal/builder voice",
        "group": "Content Writer",
        "default": _CONTENT_WRITER_PERSONAL,
    },
    "content_writer_product": {
        "label": "Product/e-commerce voice",
        "group": "Content Writer",
        "default": _CONTENT_WRITER_PRODUCT,
    },
    "content_writer_format_only": {
        "label": "Add a format to an already-written post",
        "group": "Content Writer",
        "default": _CONTENT_WRITER_FORMAT_ONLY,
    },
    "content_writer_format_only_personal": {
        "label": "Add a format to an already-written post (personal/builder voice)",
        "group": "Content Writer",
        "default": _CONTENT_WRITER_FORMAT_ONLY_PERSONAL,
    },
    "content_writer_format_only_product": {
        "label": "Add a format to an already-written post (product/e-commerce voice)",
        "group": "Content Writer",
        "default": _CONTENT_WRITER_FORMAT_ONLY_PRODUCT,
    },
    "graphic_designer_headline": {
        "label": "Headline background (legacy, no template)",
        "group": "Graphic Designer",
        "default": _GRAPHIC_DESIGNER_HEADLINE,
    },
    "graphic_designer_full_design": {
        "label": "Full templated design",
        "group": "Graphic Designer",
        "default": _GRAPHIC_DESIGNER_FULL_DESIGN,
    },
    "graphic_designer_product_photo": {
        "label": "Product-photo design (WooCommerce)",
        "group": "Graphic Designer",
        "default": _GRAPHIC_DESIGNER_PRODUCT_PHOTO,
    },
    "reel_shotlist": {
        "label": "Shot-listing (default)",
        "group": "Reel Editor",
        "default": _REEL_SHOTLIST,
    },
    "reel_shotlist_localized": {
        "label": "Shot-listing (Hindi/Marathi)",
        "group": "Reel Editor",
        "default": _REEL_SHOTLIST_LOCALIZED,
    },
    "carousel_shotlist": {
        "label": "Shot-listing (script -> slides)",
        "group": "Carousel Editor",
        "default": _CAROUSEL_SHOTLIST,
    },
    "carousel_style_guide": {
        "label": "Shared style guide",
        "group": "Carousel Editor",
        "default": _CAROUSEL_STYLE_GUIDE,
    },
    "carousel_slide": {
        "label": "Per-slide design",
        "group": "Carousel Editor",
        "default": _CAROUSEL_SLIDE,
    },
}


# Best-effort "did you accidentally drop this?" nudge for the Brand Kit
# Prompts panel (routes_brand_kit.py's update_prompt_override) - full-text
# override is a deliberate trade-off with NO rules-preserving safety net
# (see llm/prompt_overrides.py's module docstring: "reset to default" is the
# only real guardrail), so every hard requirement baked into a default
# prompt (verbatim rendering, no invented facts/offers, no real people's
# likeness, no on-screen text, the JSON-only response contract) can be
# silently dropped by an edit with no warning at all. This doesn't fix that
# trade-off - it's not validation and never blocks a save - it just flags
# when a saved override no longer contains a short, distinctive fragment of
# a phrase the corresponding default relies on, so the brand admin gets a
# visible nudge instead of finding out the guardrail is gone from a bad
# generation later. Matching is a case-insensitive substring check against
# ONE fragment of the real sentence, not the whole rule - an override that
# rephrases the same rule in different words will still trip this (a false
# positive, the accepted trade-off for staying simple as a nudge, not a
# linter).
GUARDRAIL_PHRASES: dict[str, list[tuple[str, str]]] = {
    "graphic_designer_headline": [
        ("real named individuals", "no rendering of real named individuals' likeness"),
        ("verbatim", "render the headline text verbatim"),
    ],
    "graphic_designer_full_design": [
        ("real named individuals", "no rendering of real named individuals' likeness"),
        ("verbatim", "render each text field's exact wording verbatim"),
    ],
    "graphic_designer_product_photo": [
        ("invent a discount", "never invent a discount/offer/coupon/urgency claim"),
        ("real named individuals", "no rendering of real named individuals' likeness"),
    ],
    "reel_shotlist": [
        ("on-screen text", "never describe on-screen text/captions/subtitles"),
        ("real public figure", "no identifiable real public figures by likeness"),
    ],
    "reel_shotlist_localized": [
        ("on-screen text", "never describe on-screen text/captions/subtitles"),
        ("real public figure", "no identifiable real public figures by likeness"),
    ],
    "carousel_shotlist": [
        ("invent a fact", "never invent a fact, quote, or number"),
    ],
    "carousel_style_guide": [
        ("real named individuals", "no rendering of real named individuals' likeness"),
    ],
    "carousel_slide": [
        ("verbatim", "render the exact headline/body text verbatim"),
        ("real named individuals", "no rendering of real named individuals' likeness"),
    ],
    "content_writer": [("json object", "respond with only a JSON object")],
    "content_writer_localized": [("json object", "respond with only a JSON object")],
    "content_writer_personal": [("json object", "respond with only a JSON object")],
    "content_writer_product": [("json object", "respond with only a JSON object")],
    "content_writer_format_only": [("json object", "respond with only a JSON object")],
    "content_writer_format_only_personal": [("json object", "respond with only a JSON object")],
    "content_writer_format_only_product": [("json object", "respond with only a JSON object")],
    "analytical_combined_draft": [("json object", "respond with only a JSON object")],
}


def missing_guardrail_phrases(prompt_key: str, prompt_text: str) -> list[str]:
    """Human-readable descriptions of any guardrail phrase this prompt_key's
    GUARDRAIL_PHRASES entries expect but the given text doesn't contain -
    empty list if none are missing (or this prompt_key has no tracked
    guardrails). Never raises, never used to block a save."""
    text_lower = (prompt_text or "").lower()
    return [desc for phrase, desc in GUARDRAIL_PHRASES.get(prompt_key, []) if phrase not in text_lower]
