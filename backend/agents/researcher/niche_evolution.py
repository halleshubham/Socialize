"""Periodic (user-triggered in Phase 1; could be scheduled later) reflection:
looks at what the Researcher has actually been triaging and suggests a
revised niche prompt. Never auto-activates - it's saved as an inactive,
created_by="llm_suggested" NicheConfig version for the user to review and
explicitly activate from the /niche page.
"""

import uuid

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.agents.json_utils import extract_json
from backend.agents.niche import get_active_niche_config
from backend.app.db.models import BrandKit, ContentItem, NicheConfig
from backend.app.integrations.article_fetch import fetch_article_text
from backend.app.llm.provider import ChatProvider

SYSTEM_PROMPT = """You help refine a "niche" prompt that tells a Researcher agent what \
kind of email content is worth turning into social media posts. You'll be shown the \
current niche prompt and a sample of recent triage decisions it produced. Suggest a \
revised prompt that would have done a better job - e.g. tightening scope away from \
things that turned out low-priority, or broadening it if too much on-topic material \
was being marked unsuitable.

If the current prompt already looks well-calibrated from the sample, it's fine to \
suggest only a minor tweak (or repeat it close to unchanged) - don't force a big rewrite.

Respond with ONLY a JSON object, no markdown fence, no commentary:
{
  "suggested_prompt": "<revised niche prompt text>",
  "suggested_keywords": ["<keyword>", ...],
  "rationale": "<why this revision, referencing the sample>"
}"""


def _recent_sample(db: Session, brand_kit_id: uuid.UUID, limit: int = 60) -> list[ContentItem]:
    # Every ContentItem (article) the Researcher has scored, suitable or not -
    # discarded ones are exactly the signal the niche-evolution agent needs
    # to see ("too much on-topic material was being marked unsuitable").
    return (
        db.query(ContentItem)
        .filter(ContentItem.brand_kit_id == brand_kit_id, ContentItem.priority_score.isnot(None))
        .order_by(ContentItem.created_at.desc())
        .limit(limit)
        .all()
    )


def suggest_niche_update(db: Session, brand_kit_id: uuid.UUID) -> NicheConfig:
    active = get_active_niche_config(db, brand_kit_id)
    if not active:
        raise ValueError("No active niche_config to evolve from - set one up on /niche first.")

    sample = _recent_sample(db, brand_kit_id)
    sample_lines = "\n".join(
        f"- suitable={item.stage != 'discarded'} priority={item.priority_score} "
        f"title={item.article_title!r} rationale={item.priority_rationale!r}"
        for item in sample
    ) or "(no scored articles yet)"

    user_prompt = f"""Current niche prompt:
{active.prompt_text}

Current keywords: {", ".join(active.keywords) or "(none)"}

Recent triage sample ({len(sample)} articles):
{sample_lines}

Return the JSON object now."""

    provider = ChatProvider(db, brand_kit_id)
    result = provider.complete(
        agent_task="researcher_niche_evolution",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    parsed = extract_json(result.text)

    max_version = (
        db.query(func.max(NicheConfig.version)).filter(NicheConfig.brand_kit_id == brand_kit_id).scalar() or 0
    )
    suggestion = NicheConfig(
        version=max_version + 1,
        prompt_text=parsed["suggested_prompt"],
        keywords=parsed.get("suggested_keywords", []),
        parent_version_id=active.id,
        brand_kit_id=brand_kit_id,
        created_by="llm_suggested",
        is_active=False,
        rationale=parsed.get("rationale", ""),
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)
    return suggestion


DRAFT_FROM_BRAND_SYSTEM_PROMPT = """You help write a "niche" prompt that tells a Researcher \
agent what kind of email newsletter content is worth turning into social media posts for a \
specific brand. You're given the brand's stated industry/focus, optionally some real text \
pulled from their website, and their social media handles - use these to ground a SPECIFIC, \
actionable niche prompt, not a generic one. The Researcher will use this prompt verbatim to \
triage a real inbox, so it needs concrete scope: what topics/angles count as on-niche, what to \
skip, and what tone or angle to prioritize when several on-topic articles compete.

If the website text reveals specifics (a sub-focus, a stated mission, a recurring theme, a \
regional focus, etc.), reflect those - don't just restate the industry label back generically.

Respond with ONLY a JSON object, no markdown fence, no commentary:
{
  "suggested_prompt": "<niche prompt text>",
  "suggested_keywords": ["<keyword>", ...],
  "rationale": "<one sentence on what you grounded this in>"
}"""


def draft_niche_from_brand(db: Session, brand_kit_id: uuid.UUID) -> NicheConfig:
    """One-off draft (distinct from suggest_niche_update's periodic
    triage-history reflection above) grounded in the brand's own stated
    industry/website/handles instead of past triage outcomes - meant for a
    brand-new or still-vague niche, not a refinement of an established one.
    Same never-auto-activates contract: saved inactive, created_by=
    "brand_drafted", for the user to review on /brand-kit before activating."""
    brand_kit = db.get(BrandKit, brand_kit_id)
    if not brand_kit or not (brand_kit.industry or brand_kit.website_url):
        raise ValueError("Add an industry or website to your brand kit first.")

    website_text = fetch_article_text(brand_kit.website_url).text if brand_kit.website_url else None
    handles = ", ".join(f"{platform}: {handle}" for platform, handle in (brand_kit.social_handles or {}).items())

    user_prompt = f"""Brand name: {brand_kit.brand_name or "(not set)"}
Industry/focus: {brand_kit.industry or "(not set)"}
Social handles: {handles or "(none)"}
Tone of voice: {brand_kit.tone_of_voice_prompt or "(not set)"}

Website text (best-effort extraction, may be partial or absent):
{website_text[:6000] if website_text else "(could not fetch or no website set)"}

Return the JSON object now."""

    provider = ChatProvider(db, brand_kit_id)
    result = provider.complete(
        agent_task="researcher_niche_evolution",
        messages=[
            {"role": "system", "content": DRAFT_FROM_BRAND_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    parsed = extract_json(result.text)

    active = get_active_niche_config(db, brand_kit_id)
    max_version = (
        db.query(func.max(NicheConfig.version)).filter(NicheConfig.brand_kit_id == brand_kit_id).scalar() or 0
    )
    draft = NicheConfig(
        version=max_version + 1,
        prompt_text=parsed["suggested_prompt"],
        keywords=parsed.get("suggested_keywords", []),
        parent_version_id=active.id if active else None,
        brand_kit_id=brand_kit.id,
        created_by="brand_drafted",
        is_active=False,
        rationale=parsed.get("rationale", ""),
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft
