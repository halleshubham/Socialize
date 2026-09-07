"""Shared state that flows through the whole content pipeline. This IS the
LangGraph state object for the per-content-item graph in orchestrator.py, and
mirrors the `content_items` row it's checkpointed against (added in Phase 1's
migration) - so there's no separate serialization format between agents.

Kept intentionally minimal in Phase 0: only the fields the orchestrator
skeleton and stage enum need to exist now. Each phase adds the fields its
agent actually produces (e.g. `brief` in Phase 1, `copy_draft` in Phase 2)
rather than speculatively declaring them all here upfront.
"""

from enum import StrEnum
from typing import Annotated, TypedDict
from uuid import UUID


class Stage(StrEnum):
    RAW = "raw"
    RESEARCHED = "researched"
    ANALYZED = "analyzed"
    DRAFTED = "drafted"
    MEDIA_GENERATED = "media_generated"
    APPROVED = "approved"
    SCHEDULED = "scheduled"  # Phase 4
    POSTED = "posted"  # Phase 4
    DISCARDED = "discarded"


class Format(StrEnum):
    POSTER = "poster"
    REEL = "reel"
    TEXT_ONLY = "text_only"


def _last_write_wins(_old, new):
    return new


class ContentItemState(TypedDict, total=False):
    content_item_id: UUID
    brand_kit_id: UUID
    source_email_id: UUID | None
    stage: Annotated[Stage, _last_write_wins]
    format: Format | None
    language: str

    # Phase 1 (Researcher / Analytical / article fetch). Researcher's
    # extraction happens once per email, before any ContentItem exists (see
    # agents/orchestrator.py's process_email) - so its output lives on the
    # ContentItem DB row from the start, not passed through graph state. Each
    # node loads the row itself via content_item_id rather than duplicating
    # article_title/url/summary/brief/etc. here.

    # Phase 2 (Content Writer / Graphic Designer). Same story: copy_text,
    # hashtags, poster_headline live on the ContentItem row, not here.

    # Free-text feedback from the most recent approval gate, consumed by the
    # next node as "regenerate with feedback" input.
    user_feedback: str | None

    # Transient routing signal set by a gate node, read by the conditional
    # edge immediately after it - needed because "approve" and "regenerate"
    # can both leave `stage` unchanged (e.g. approving a poster item stays at
    # DRAFTED until the Graphic Designer runs), so `stage` alone can't always
    # tell the router what to do next.
    last_gate_decision: Annotated[str, _last_write_wins]

    # Set by _analytical_node (from the brand's combined_drafting flag at
    # the time it ran) - _route_after_analytical reads this to send the
    # item to combined_review_gate instead of analytical_review_gate,
    # without a second DB query at routing time.
    combined_drafting: Annotated[bool, _last_write_wins]
