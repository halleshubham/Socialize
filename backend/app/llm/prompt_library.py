"""Dynamic few-shot grounding for the Graphic Designer's creative-direction
prompts, sourced from https://github.com/YouMind-OpenLab's curated,
community-sourced image-generation prompt libraries (per-model "awesome
prompts" collections, each backing a Claude Code / OpenClaw skill).

These are NOT live APIs - each repo's `references/*.json` files are plain
data checked into the repo, refreshed by the org's own GitHub Actions
roughly twice daily (confirmed by inspecting nano-banana-pro-prompts-
recommend-skill's SKILL.md and scripts/setup.js). This module fetches them
the same way that skill does (raw.githubusercontent.com), caches each
category file locally with a ~20h TTL (inside that twice-daily cadence,
so we're never more than half a cycle stale) and does a lightweight
keyword match against this specific poster's own real content to surface
1-2 relevant real example prompts - injected into the USER prompt (not the
system prompt, which is the stable, brand-editable rules text) as
"here's what a real successful prompt for this model/category looks like,
for structural inspiration" - never copied verbatim, never treated as
something to imitate word-for-word.

Verified live (Sept 2026): the org's video-prompt libraries (Seedance 2.0,
Grok Imagine) have NO equivalent static data source - the seedance skill's
own documented "Live API" (https://youmind.com/youhome-api/video-prompts)
returns a bare 404 today. So this module only covers image generation
(Graphic Designer); Reel Editor's shot-listing prompt is NOT wired to this
- see reel_editor/graph.py, which has no import from here.

Best-effort throughout: any fetch/parse failure just means no example
prompts get injected this time (logged, not raised) - this is inspiration,
never a hard dependency for poster generation to succeed.
"""

import json
import logging
import re
import time
from pathlib import Path

import httpx

from backend.app.config import get_settings

logger = logging.getLogger(__name__)

# image_model_key (image_provider.py's IMAGE_MODEL_CHOICES) -> which repo's
# library to draw from. The three Gemini/"Nano Banana" tiers share one
# library (per that repo's own README: prompts are Nano-Banana-Pro-tuned
# but explicitly "work with Nano Banana 2... any text-to-image AI model") -
# GPT Image 2 gets its own repo, built the same way, one model earlier.
_LIBRARY_REPO_BY_MODEL_KEY: dict[str, str] = {
    "gpt_image_2": "gpt-image-2-prompts-search",
    "nano_banana_2_lite": "nano-banana-pro-prompts-recommend-skill",
    "nano_banana": "nano-banana-pro-prompts-recommend-skill",
    "nano_banana_pro": "nano-banana-pro-prompts-recommend-skill",
}
_DEFAULT_LIBRARY_REPO = "nano-banana-pro-prompts-recommend-skill"

# graphic_designer poster_template -> library category slug. Both repos
# share the same 11-category taxonomy (confirmed by diffing their
# manifest.json files) - see each repo's references/manifest.json for the
# authoritative live list; this is just OUR templates' best mapping onto it.
TEMPLATE_CATEGORY: dict[str, str] = {
    "quote": "social-media-post",
    "tribute": "social-media-post",
    "narrative": "social-media-post",
    "fact_critique": "infographic-edu-visual",
    "trivia": "infographic-edu-visual",
    "event": "poster-flyer",
}
PRODUCT_PHOTO_CATEGORY = "ecommerce-main-image"
DEFAULT_CATEGORY = "social-media-post"  # legacy headline path, or an unmapped template

# Reel Editor's shot-listing prompt (reel_editor/graph.py) also borrows from
# here - NOT because these are video prompts (they're not), but because the
# org's actual Seedance/Grok Imagine video-prompt sources have no usable
# data: the Seedance skill's documented source is a live API
# (https://youmind.com/youhome-api/video-prompts) that returns a bare 404
# as of Sept 2026 (confirmed directly, plus checked for a newer endpoint in
# both video skills' commit history/issues - unmaintained since March 2026,
# nothing newer). So this borrows structural/descriptive-language
# inspiration (composition, framing, mood, sequential storytelling) from
# the same image-prompt libraries instead, explicitly not Seedance/Veo
# syntax - see reel_editor/graph.py's usage and format_examples_block's own
# "structural inspiration only" framing, which applies just as much here.
REEL_TEMPLATE_CATEGORY: dict[str, str] = {
    "explainer_influencer": "social-media-post",
    "faceless": "social-media-post",
    "animated_contextual": "comic-storyboard",
}
REEL_DEFAULT_CATEGORY = "social-media-post"
REEL_LIBRARY_REPO = "nano-banana-pro-prompts-recommend-skill"

_CACHE_TTL_SECONDS = 20 * 3600  # inside the org's own ~twice-daily refresh cadence
_MAX_EXAMPLES = 2
_FETCH_TIMEOUT_SECONDS = 30.0  # the largest category files run ~20MB+


def _cache_dir() -> Path:
    path = Path(get_settings().local_storage_dir).parent / "prompt_library_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fetch_category(repo: str, category_slug: str) -> list[dict]:
    """One category's example prompts - a local disk cache first (if not
    stale), else a live fetch from raw.githubusercontent.com, same URL
    shape the skill itself resolves at runtime."""
    cache_path = _cache_dir() / repo / f"{category_slug}.json"
    if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < _CACHE_TTL_SECONDS:
        try:
            return json.loads(cache_path.read_text())
        except Exception:
            logger.exception("Cached prompt library file unreadable, refetching: %s", cache_path)

    url = f"https://raw.githubusercontent.com/YouMind-OpenLab/{repo}/main/references/{category_slug}.json"
    response = httpx.get(url, timeout=_FETCH_TIMEOUT_SECONDS)
    response.raise_for_status()
    data = response.json()

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(data))
    return data


_WORD_RE = re.compile(r"[a-zA-Z]{4,}")


def _keywords(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "")}


def _match_examples(repo: str, category_slug: str, query_text: str) -> list[dict]:
    """Up to _MAX_EXAMPLES {title, content} dicts from the given
    repo/category, ranked by keyword overlap with query_text - or [] if the
    fetch fails, the category is empty, or nothing meaningfully overlaps (a
    forced irrelevant example is worse than none)."""
    try:
        entries = _fetch_category(repo, category_slug)
    except Exception:
        logger.exception("Prompt library fetch failed for %s/%s, continuing without examples", repo, category_slug)
        return []

    query_words = _keywords(query_text)
    if not query_words or not entries:
        return []

    scored = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("content"):
            continue
        haystack = _keywords(f"{entry.get('title', '')} {entry.get('description', '')}")
        overlap = len(query_words & haystack)
        if overlap:
            scored.append((overlap, entry))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {"title": entry.get("title", ""), "content": entry["content"]}
        for _, entry in scored[:_MAX_EXAMPLES]
    ]


def get_example_prompts(image_model_key: str, category_slug: str, query_text: str) -> list[dict]:
    """Graphic Designer's entry point - repo picked by which image model
    this brand has selected (IMAGE_MODEL_CHOICES)."""
    repo = _LIBRARY_REPO_BY_MODEL_KEY.get(image_model_key, _DEFAULT_LIBRARY_REPO)
    return _match_examples(repo, category_slug, query_text)


def get_reel_example_prompts(reel_template: str, query_text: str) -> list[dict]:
    """Reel Editor's entry point - see REEL_TEMPLATE_CATEGORY's docstring
    for why this borrows from an image-prompt library rather than a
    video-specific one (Seedance's real data source is confirmed dead)."""
    category = REEL_TEMPLATE_CATEGORY.get(reel_template, REEL_DEFAULT_CATEGORY)
    return _match_examples(REEL_LIBRARY_REPO, category, query_text)


def format_examples_block(examples: list[dict], subject: str = "this image model") -> str:
    """Rendered for injection into a creative-direction USER prompt (not
    the system prompt) - real successful prompts as structural/stylistic
    inspiration, explicitly NOT something to copy verbatim, since they're
    about a different subject entirely. `subject` names what the examples
    are grounding (an image model for Graphic Designer's own calls, or a
    generic "visual composition" phrasing for Reel Editor's borrowed use -
    see get_reel_example_prompts)."""
    if not examples:
        return ""
    lines = [
        f"\nFor structural/stylistic inspiration ONLY (these are about a completely different subject - "
        f"do not reuse their specific content, just notice how they're built: composition, framing, "
        f"level of detail, how text/typography is integrated), here are real prompts that worked well "
        f"for {subject}:"
    ]
    for i, example in enumerate(examples, 1):
        title = example["title"]
        content = example["content"][:600]
        lines.append(f'{i}. "{title}": {content}')
    return "\n".join(lines)
