"""Top-level per-content-item (= per-article) graph: subgraph nodes +
interrupt() approval gates + a Postgres checkpointer, per the architecture in
docs/architecture.md.

The Researcher's article extraction happens ONCE per email, outside this
graph (see process_email below) - it can produce many ContentItems from one
newsletter, which doesn't fit the "one graph run per item" model. Each
resulting ContentItem then gets its own graph run, starting at fetch_article.

Full pipeline (Phase 3):
    fetch_article -> analytical -> [interrupt: analytical_review]
      -> content_writer -> [interrupt: content_review]
           -> (poster format)    graphic_designer -> [interrupt: media_review] -> END
           -> (reel format)      reel_editor      -> [interrupt: media_review] -> END
           -> (carousel format)  carousel_editor  -> [interrupt: media_review] -> END
           -> (text_only format) END
      -> END (discarded/write_myself at any gate)

brand_kit.combined_drafting brands take a shortcut off "analytical" instead:
    fetch_article -> analytical (drafts brief + copy TOGETHER, one cheap call)
      -> [interrupt: combined_review] -> (poster/reel format) graphic_designer/reel_editor -> [interrupt: media_review] -> END
                                       -> (text_only format) END
                                       -> END (discarded/write_myself)
No separate content_writer/content_review step for these items - see
analytical/graph.py::write_brief_and_copy and _route_after_analytical below.

reel_editor can take minutes (Veo per-scene generation) - routes_board.py
dispatches the resume_content_item call that would enter it to a background
thread (see app/worker/background.py) rather than blocking the HTTP request.

The checkpointer and compiled graph are process-wide singletons backed by a
psycopg ConnectionPool (rather than one-off connections per run), since both
the web app and the scheduled worker need to invoke/resume runs throughout
the process lifetime.

Usage:
    new_item_ids = process_email(SessionLocal, email_id)
    # ... later, once the user acts in the UI ...
    resume_content_item(content_item_id, {"decision": "send_to_content_writer", "format": "poster"})
"""

import logging
import uuid

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import update
from sqlalchemy.orm import Session

from backend.agents.analytical.graph import write_brief, write_brief_and_copy
from backend.agents.auto_mode import DEFAULT_FORMAT as DEFAULT_AUTO_FORMAT
from backend.agents.auto_mode import VALID_FORMATS, get_auto_mode_settings
from backend.agents.carousel_editor.graph import _build_carousel_shotlist, generate_carousel
from backend.agents.content_writer.graph import write_copy
from backend.agents.graphic_designer.graph import generate_poster
from backend.agents.languages import LANGUAGE_CHOICES
from backend.agents.reel_editor.graph import _build_shotlist, generate_reel
from backend.agents.researcher.graph import extract_articles
from backend.agents.researcher.rss_angles import extract_angles as extract_rss_angles
from backend.agents.state import ContentItemState, Format, Stage
from backend.app.config import get_settings
from backend.app.db.models import BrandKit, ContentItem, IngestedEmail, IngestedRssItem
from backend.app.db.session import SessionLocal
from backend.app.integrations.article_fetch import fetch_article_text
from backend.app.integrations.botsab.send import send_content_item

settings = get_settings()
logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None
_checkpointer: PostgresSaver | None = None
_compiled_graph = None


def _conninfo() -> str:
    # PostgresSaver/psycopg want a plain conninfo string, not SQLAlchemy's
    # "+psycopg" driver marker.
    return settings.database_url.replace("postgresql+psycopg://", "postgresql://")


def get_checkpointer() -> PostgresSaver:
    global _pool, _checkpointer
    if _checkpointer is None:
        _pool = ConnectionPool(
            _conninfo(),
            max_size=5,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        _checkpointer = PostgresSaver(_pool)
        _checkpointer.setup()
    return _checkpointer


# --- Nodes -------------------------------------------------------------


def _fetch_article_node(state: ContentItemState) -> dict:
    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, state["content_item_id"])
        # A GitHub-sourced item (see create_content_items below) already has
        # article_full_text set at creation time (the repo's real README/
        # docs/commits) - skip the readability scrape, which would either
        # fail or extract garbage off a github.com repo page. A Gmail-
        # sourced item always starts with this empty (the Researcher only
        # sets article_summary), so this is a no-op for existing behavior.
        if content_item.article_url and not content_item.article_full_text:
            result = fetch_article_text(content_item.article_url)
            content_item.article_full_text = result.text
            if result.resolved_url:
                # Replaces click-tracking wrapper links (Mailchimp's
                # list-manage.com, Substack, etc.) with the real
                # destination - source_attribution.py and the "Source:
                # {article_url}" caption link both read this field later.
                content_item.article_url = result.resolved_url
            db.commit()
        return {}
    finally:
        db.close()


def _analytical_node(state: ContentItemState) -> dict:
    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, state["content_item_id"])
        email = db.get(IngestedEmail, state["source_email_id"]) if state.get("source_email_id") else None
        brand_kit = db.get(BrandKit, content_item.brand_kit_id)

        if brand_kit and brand_kit.combined_drafting:
            # Skips the separate Content Writer call entirely - one cheap
            # call drafts brief + copy/hashtags + the brand's configured
            # default format's fields together. Both format and language
            # only default here (never overwritten) - so a regenerate cycle
            # back through this same node doesn't reset a value the user
            # already changed, AND so an explicit per-batch override set at
            # creation time (create_content_items' format_override/
            # language_override - e.g. routes_board.py's draft_from_website
            # letting a WooCommerce-sourced batch request a reel/Marathi
            # poster even though the brand's own combined-drafting default
            # is an English poster) survives this node instead of being
            # silently reset back to the brand default.
            if not content_item.format:
                content_item.format = (
                    brand_kit.combined_drafting_format
                    if brand_kit.combined_drafting_format in VALID_FORMATS
                    else DEFAULT_AUTO_FORMAT
                )
            if not content_item.language:
                content_item.language = brand_kit.default_language
            write_brief_and_copy(
                db, content_item, email, content_item.format, revision_feedback=state.get("user_feedback")
            )
            _prebuild_shotlist_for_review(db, content_item)
            content_item.stage = Stage.DRAFTED
            content_item.user_feedback = None
            db.commit()
            return {
                "stage": Stage.DRAFTED,
                "format": content_item.format,
                "combined_drafting": True,
                "user_feedback": None,
            }

        write_brief(db, content_item, email)
        content_item.stage = Stage.ANALYZED
        db.commit()
        return {"stage": Stage.ANALYZED, "combined_drafting": False}
    finally:
        db.close()


def _route_after_analytical(state: ContentItemState) -> str:
    return "combined_review_gate" if state.get("combined_drafting") else "analytical_review_gate"


def _analytical_review_gate(state: ContentItemState) -> dict:
    decision = interrupt(
        {
            "gate": "analytical_review",
            "content_item_id": str(state["content_item_id"]),
            "prompt": "write_myself | send_to_content_writer | discard",
        }
    )
    choice = decision.get("decision")
    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, state["content_item_id"])
        content_item.user_feedback = decision.get("feedback")
        if choice == "discard":
            content_item.stage = Stage.DISCARDED
        elif choice == "write_myself":
            # No copy_text yet - routes_board.py's write_copy_manually
            # captures it, then generate_media can still add a poster/reel.
            content_item.stage = Stage.APPROVED
        elif choice == "send_to_content_writer":
            content_item.format = decision.get("format") or Format.TEXT_ONLY
            language = decision.get("language")
            if language in LANGUAGE_CHOICES:
                content_item.language = language
        db.commit()
        return {
            "stage": content_item.stage,
            "format": content_item.format,
            "user_feedback": decision.get("feedback"),
            "last_gate_decision": choice,
        }
    finally:
        db.close()


def _route_after_review(state: ContentItemState) -> str:
    if state.get("stage") in (Stage.DISCARDED, Stage.APPROVED):
        return END
    return "content_writer"


def _combined_review_gate(state: ContentItemState) -> dict:
    """combined_drafting brands land here instead of analytical_review_gate
    - brief + copy are already drafted (see _analytical_node), so this
    shows both together and skips straight past what would otherwise be a
    separate content_review gate. Same decision shape as content_review_gate
    plus write_myself (analytical_review_gate's discard-to-self-write
    option), since this is this item's only gate before media generation."""
    decision = interrupt(
        {
            "gate": "combined_review",
            "content_item_id": str(state["content_item_id"]),
            "prompt": "approve | regenerate | write_myself | discard",
        }
    )
    choice = decision.get("decision")
    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, state["content_item_id"])
        content_item.user_feedback = decision.get("feedback")
        if choice == "discard":
            content_item.stage = Stage.DISCARDED
        elif choice == "write_myself":
            content_item.stage = Stage.APPROVED
        elif choice == "approve" and content_item.format not in (Format.POSTER, Format.REEL, Format.CAROUSEL):
            content_item.stage = Stage.APPROVED
        # "regenerate", and "approve" for poster/reel format (still needs a
        # media-generation step), leave stage=DRAFTED - the router below
        # uses last_gate_decision to tell them apart, same as content_review_gate.
        db.commit()
        return {
            "stage": content_item.stage,
            "format": content_item.format,
            "combined_drafting": True,
            "user_feedback": decision.get("feedback"),
            "last_gate_decision": choice,
        }
    finally:
        db.close()


def _route_after_combined_review(state: ContentItemState) -> str:
    if state.get("stage") in (Stage.DISCARDED, Stage.APPROVED):
        return END
    if state.get("last_gate_decision") == "approve":
        if state.get("format") == Format.POSTER:
            return "graphic_designer"
        if state.get("format") == Format.REEL:
            return "reel_editor"
        if state.get("format") == Format.CAROUSEL:
            return "carousel_editor"
    return "analytical"  # regenerate - brief+copy are one call, redo both


# Reel: Hindi/Marathi text quality from the AI (both the narrative script
# AND whatever the shot-listing step derives from it) isn't reliable enough
# yet (user-reported) - for these two languages only, reel_scenes' narration
# (the exact final text that ends up spoken) is built right after drafting,
# BEFORE the item reaches its content-review gate, so the board can offer it
# for direct editing pre-generation instead of only after paying for a real
# Veo call. English (and any other language) keeps the original lazy
# behavior for reels - shot-listing happens at generation time only, inside
# generate_reel - no extra cost/latency added to that path.
#
# Carousel: unlike reel, this runs for EVERY language (not just hi/mr) -
# carousel slide wording often benefits from a human tweak regardless of
# language, not just an AI-quality problem specific to Hindi/Marathi (user
# request). No added cost either way: shot-listing has to happen once
# regardless, this only changes WHEN (drafting time vs. generation time),
# not whether.
#
# Best-effort throughout: a failure here is logged and swallowed, not
# propagated - worst case the item reaches content_review without a
# pre-built list to edit, same as before this existed, and generation still
# builds one lazily as always.
_TEXT_REVIEW_LANGUAGES = {"hi", "mr"}


def _prebuild_shotlist_for_review(db: Session, content_item: ContentItem) -> None:
    try:
        if (
            content_item.format == Format.REEL
            and content_item.language in _TEXT_REVIEW_LANGUAGES
            and content_item.reel_script
        ):
            content_item.reel_scenes = None  # force a rebuild from the just-drafted script
            db.commit()
            _build_shotlist(db, content_item)
        elif content_item.format == Format.CAROUSEL and content_item.carousel_script:
            content_item.carousel_slides = []
            db.commit()
            _build_carousel_shotlist(db, content_item)
    except Exception:
        logger.exception(
            "Pre-build shot list for review failed for %s, will build lazily at generation instead",
            content_item.id,
        )


def _content_writer_node(state: ContentItemState) -> dict:
    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, state["content_item_id"])
        write_copy(db, content_item, revision_feedback=state.get("user_feedback"))
        _prebuild_shotlist_for_review(db, content_item)
        content_item.stage = Stage.DRAFTED
        content_item.user_feedback = None  # consumed
        db.commit()
        return {"stage": Stage.DRAFTED, "user_feedback": None}
    finally:
        db.close()


def _content_review_gate(state: ContentItemState) -> dict:
    decision = interrupt(
        {
            "gate": "content_review",
            "content_item_id": str(state["content_item_id"]),
            "prompt": "approve | regenerate | discard",
        }
    )
    choice = decision.get("decision")
    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, state["content_item_id"])
        content_item.user_feedback = decision.get("feedback")
        if choice == "discard":
            content_item.stage = Stage.DISCARDED
        elif choice == "approve" and content_item.format not in (Format.POSTER, Format.REEL, Format.CAROUSEL):
            content_item.stage = Stage.APPROVED
        # "regenerate", and "approve" for poster/reel format (still needs a
        # media-generation step), leave stage=DRAFTED - the router below
        # uses last_gate_decision to tell them apart.
        db.commit()
        return {
            "stage": content_item.stage,
            "user_feedback": decision.get("feedback"),
            "last_gate_decision": choice,
        }
    finally:
        db.close()


def _route_after_content_review(state: ContentItemState) -> str:
    if state.get("stage") in (Stage.DISCARDED, Stage.APPROVED):
        return END
    if state.get("last_gate_decision") == "approve":
        if state.get("format") == Format.POSTER:
            return "graphic_designer"
        if state.get("format") == Format.REEL:
            return "reel_editor"
        if state.get("format") == Format.CAROUSEL:
            return "carousel_editor"
    return "content_writer"  # regenerate


def _graphic_designer_node(state: ContentItemState) -> dict:
    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, state["content_item_id"])
        generate_poster(db, content_item)
        content_item.stage = Stage.MEDIA_GENERATED
        db.commit()
        return {"stage": Stage.MEDIA_GENERATED}
    finally:
        db.close()


def _reel_editor_node(state: ContentItemState) -> dict:
    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, state["content_item_id"])
        try:
            generate_reel(db, content_item)
            content_item.stage = Stage.MEDIA_GENERATED
            content_item.last_error = None
        except Exception as exc:
            # Leaves stage unchanged (still DRAFTED/MEDIA_GENERATED) - no
            # LangGraph interrupt is pending at this point though (it was
            # already consumed to get here), so routes_board.py's
            # retry_reel calls generate_reel directly rather than another
            # resume_content_item. last_error is what lets the board tell
            # this apart from a card genuinely paused at its gate.
            content_item.last_error = str(exc)[:2000]
            raise
        finally:
            content_item.is_processing = False
            db.commit()
        return {"stage": Stage.MEDIA_GENERATED}
    finally:
        db.close()


def _carousel_editor_node(state: ContentItemState) -> dict:
    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, state["content_item_id"])
        try:
            generate_carousel(db, content_item)
            content_item.stage = Stage.MEDIA_GENERATED
            content_item.last_error = None
        except Exception as exc:
            # Same as _reel_editor_node - no pending interrupt at this point,
            # so routes_board.py's retry route calls generate_carousel
            # directly rather than another resume_content_item.
            content_item.last_error = str(exc)[:2000]
            raise
        finally:
            content_item.is_processing = False
            db.commit()
        return {"stage": Stage.MEDIA_GENERATED}
    finally:
        db.close()


def _media_review_gate(state: ContentItemState) -> dict:
    decision = interrupt(
        {
            "gate": "media_review",
            "content_item_id": str(state["content_item_id"]),
            "prompt": "approve | regenerate | discard",
        }
    )
    choice = decision.get("decision")
    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, state["content_item_id"])
        content_item.user_feedback = decision.get("feedback")
        if choice == "discard":
            content_item.stage = Stage.DISCARDED
        elif choice == "approve":
            content_item.stage = Stage.APPROVED
        # "regenerate" leaves stage=MEDIA_GENERATED; router sends it back.
        db.commit()
        return {
            "stage": content_item.stage,
            "user_feedback": decision.get("feedback"),
            "last_gate_decision": choice,
        }
    finally:
        db.close()


def _route_after_media_review(state: ContentItemState) -> str:
    if state.get("last_gate_decision") == "regenerate":
        if state.get("format") == Format.REEL:
            return "reel_editor"
        if state.get("format") == Format.CAROUSEL:
            return "carousel_editor"
        return "graphic_designer"
    return END


def build_graph(checkpointer):
    graph = StateGraph(ContentItemState)

    graph.add_node("fetch_article", _fetch_article_node)
    graph.add_node("analytical", _analytical_node)
    graph.add_node("analytical_review_gate", _analytical_review_gate)
    graph.add_node("combined_review_gate", _combined_review_gate)
    graph.add_node("content_writer", _content_writer_node)
    graph.add_node("content_review_gate", _content_review_gate)
    graph.add_node("graphic_designer", _graphic_designer_node)
    graph.add_node("reel_editor", _reel_editor_node)
    graph.add_node("carousel_editor", _carousel_editor_node)
    graph.add_node("media_review_gate", _media_review_gate)

    graph.add_edge(START, "fetch_article")
    graph.add_edge("fetch_article", "analytical")
    graph.add_conditional_edges(
        "analytical", _route_after_analytical, ["analytical_review_gate", "combined_review_gate"]
    )
    graph.add_conditional_edges(
        "analytical_review_gate", _route_after_review, ["content_writer", END]
    )
    graph.add_conditional_edges(
        "combined_review_gate",
        _route_after_combined_review,
        ["analytical", "graphic_designer", "reel_editor", "carousel_editor", END],
    )
    graph.add_edge("content_writer", "content_review_gate")
    graph.add_conditional_edges(
        "content_review_gate",
        _route_after_content_review,
        ["content_writer", "graphic_designer", "reel_editor", "carousel_editor", END],
    )
    graph.add_edge("graphic_designer", "media_review_gate")
    graph.add_edge("reel_editor", "media_review_gate")
    graph.add_edge("carousel_editor", "media_review_gate")
    graph.add_conditional_edges(
        "media_review_gate",
        _route_after_media_review,
        ["graphic_designer", "reel_editor", "carousel_editor", END],
    )

    return graph.compile(checkpointer=checkpointer)


def get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph(get_checkpointer())
    return _compiled_graph


def auto_advance_content_item(content_item_id: uuid.UUID) -> None:
    """Auto-mode: for an item whose priority_score clears the configured
    threshold, resumes the analytical_review gate (send_to_content_writer,
    using the brand's default language), once that lands it at
    content_review resumes that too (approve), and - per an explicit user
    decision to extend auto mode past generation - once that lands it at
    media_review resumes that too (approve), landing it at APPROVED (or
    straight to APPROVED/DISCARDED earlier for a text_only/unsuitable
    outcome). If `auto_mode.auto_send` is also on, a fresh approve out of
    media_review additionally triggers the actual WhatsApp send - the one
    remaining manual checkpoint is then just this same auto_send toggle,
    not a click. Safe/cheap to call unconditionally (checked by
    process_email after every new item, and swept over the existing
    backlog when auto-mode is first turned on from routes_board.py) - it's
    a no-op whenever auto-mode is off, the item doesn't clear the score
    threshold, or it's not sitting at a gate this covers."""
    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, content_item_id)
        if not content_item:
            return
        auto_mode = get_auto_mode_settings(db, content_item.brand_kit_id)
        if not auto_mode.enabled:
            return
        brand_kit = db.get(BrandKit, content_item.brand_kit_id)
        language = brand_kit.default_language if brand_kit else None
    finally:
        db.close()

    # At most three resumes: past analytical_review, past content_review,
    # then past media_review - one full lifecycle, so a plain range covers
    # it without needing to loop indefinitely. Each stage's resume can
    # itself land further downstream synchronously (e.g. content_review's
    # approve runs graphic_designer/reel_editor and reaches media_review
    # within that one resume_content_item call), so this loop just keeps
    # nudging forward from wherever the item actually ended up.
    for _ in range(3):
        db = SessionLocal()
        try:
            content_item = db.get(ContentItem, content_item_id)
            if not content_item:
                return
            stage, score = content_item.stage, content_item.priority_score
        finally:
            db.close()

        if score is None or score < auto_mode.min_score:
            return

        if stage == Stage.ANALYZED:
            payload = {"decision": "send_to_content_writer", "feedback": "", "format": auto_mode.format}
            if language:
                payload["language"] = language
        elif stage == Stage.DRAFTED:
            payload = {"decision": "approve", "feedback": ""}
        elif stage == Stage.MEDIA_GENERATED:
            if content_item.format == "reel":
                # Reels always stop here for a real human look before
                # APPROVED/auto-send, even with auto-mode + auto_send both
                # on - unlike a poster/carousel, a reel can be silently
                # truncated (the per-reel cost cap can stop generation
                # mid-story) or land on a rough/incoherent Veo clip with
                # nothing else catching it before it would otherwise go
                # straight out over WhatsApp. Posters/carousels keep the
                # existing auto-approve behavior.
                break
            payload = {"decision": "approve", "feedback": ""}
        else:
            break  # APPROVED, DISCARDED, or anything else this function doesn't drive

        try:
            resume_content_item(content_item_id, payload)
        except Exception:
            logger.exception("Auto-mode resume failed for %s at stage %s", content_item_id, stage)
            return

    if not auto_mode.auto_send:
        return

    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, content_item_id)
        if not content_item or content_item.stage != Stage.APPROVED:
            return
        try:
            send_content_item(db, content_item)
        except Exception as exc:
            content_item.last_error = f"Auto-send via WhatsApp failed: {exc}"
            db.commit()
            logger.exception("Auto-send failed for %s", content_item_id)
    finally:
        db.close()


# --- Public entry points -------------------------------------------------


def create_content_items(
    db_session_factory,
    brand_kit_id: uuid.UUID,
    articles: list[dict],
    source_email_id: uuid.UUID | None = None,
    format_override: str | None = None,
    language_override: str | None = None,
) -> list[uuid.UUID]:
    """Creates one ContentItem per article/angle dict and drives a graph run
    (through to the first interrupt) for each suitable one. Shared by
    process_email (Gmail - one email can hold many articles) and the
    GitHub-/Website-source routes (routes_board.py's draft_from_github/
    draft_from_website - one repo/product produces several distinct
    angles, same shape). Each dict: {title, url, summary,
    suitable_for_social, priority_score, rationale}, optionally
    {full_text: ...} - when present, sets content_item.article_full_text
    at creation, which makes _fetch_article_node skip its own re-fetch
    (see there).

    format_override/language_override, when given, are set directly on
    each created item - for a combined_drafting brand, this is the only
    way to get anything other than the brand's own combined_drafting_format/
    default_language for a given batch (see _analytical_node, which only
    ever DEFAULTS format/language, never overwrites an already-set value) -
    e.g. drafting one specific run of reels/Marathi posters from a
    WooCommerce catalog whose brand otherwise always drafts English
    posters. Ignored (both None) for the normal per-item review flow, where
    the user picks format/language at the analytical_review gate instead.

    Unsuitable items are persisted as stage=DISCARDED with their rationale
    but never get a graph run - there's nothing left to do with them, and
    this keeps them visible on the board instead of silently vanishing.

    Returns the ids of the ContentItems that were sent into the pipeline
    (i.e. excludes the discarded ones)."""
    db = db_session_factory()
    try:
        created: list[tuple[uuid.UUID, bool]] = []  # (content_item_id, suitable)
        for article in articles:
            suitable = bool(article.get("suitable_for_social", False))
            content_item = ContentItem(
                brand_kit_id=brand_kit_id,
                source_email_id=source_email_id,
                stage=Stage.RESEARCHED if suitable else Stage.DISCARDED,
                article_title=str(article.get("title", ""))[:500],
                article_url=article.get("url") or None,
                article_summary=article.get("summary", ""),
                article_full_text=article.get("full_text") or "",
                priority_score=article.get("priority_score"),
                priority_rationale=article.get("rationale", ""),
                format=format_override,
                language=language_override,
            )
            db.add(content_item)
            db.flush()  # populate content_item.id without a full commit yet
            created.append((content_item.id, suitable))
        db.commit()
    finally:
        db.close()

    started_ids: list[uuid.UUID] = []
    for content_item_id, suitable in created:
        if not suitable:
            continue
        config = {"configurable": {"thread_id": str(content_item_id)}}
        try:
            get_graph().invoke(
                {
                    "content_item_id": content_item_id,
                    "brand_kit_id": brand_kit_id,
                    "source_email_id": source_email_id,
                    "stage": Stage.RESEARCHED,
                },
                config,
            )
            started_ids.append(content_item_id)
            auto_advance_content_item(content_item_id)
        except Exception as exc:
            # One bad article (fetch/LLM failure) shouldn't block the rest
            # of the batch (10-30 articles for a newsletter, several angles
            # for a GitHub source). last_error IS still set (a fresh
            # session - the one from the block above is already closed) so
            # the item shows a real reason on the board instead of sitting
            # at RESEARCHED looking silently stuck, matching how every other
            # node in this pipeline (_reel_editor_node, _carousel_editor_node,
            # etc.) surfaces its own failures.
            logger.exception("Pipeline run failed for content_item %s", content_item_id)
            err_db = SessionLocal()
            try:
                item = err_db.get(ContentItem, content_item_id)
                if item:
                    item.last_error = f"Pipeline run failed: {exc}"[:2000]
                    err_db.commit()
            finally:
                err_db.close()

    return started_ids


def process_email(db_session_factory, email_id: uuid.UUID) -> list[uuid.UUID]:
    """Runs the Researcher once over the whole email (extracting every
    article it contains), then hands the results to create_content_items.

    Claims the email atomically (status "new" -> "processing") before doing
    any work, and bails out immediately if it couldn't - `email.status` used
    to only flip to "processed" at the very end, after `extract_articles`
    (an LLM call) and the ContentItem inserts, so two overlapping calls for
    the same email (the 06:00 scheduled fetch racing a manual "Fetch now"
    click, or a double-click on it before the first click's background run
    had gotten far enough to flip the status) would both see status="new"
    and both run the Researcher + create duplicate ContentItems for the same
    articles - real duplicate LLM/image/video cost, not just a UI glitch."""
    db = db_session_factory()
    try:
        claim = db.execute(
            update(IngestedEmail)
            .where(IngestedEmail.id == email_id, IngestedEmail.status == "new")
            .values(status="processing")
        )
        db.commit()
        if claim.rowcount == 0:
            logger.info("Email %s already claimed/processed by another run, skipping", email_id)
            return []

        email = db.get(IngestedEmail, email_id)
        brand_kit_id = email.brand_kit_id
        articles = extract_articles(db, email)

        email.status = "processed"
        email.articles_found = len(articles)
        db.commit()
    finally:
        db.close()

    return create_content_items(db_session_factory, brand_kit_id, articles, source_email_id=email_id)


def process_rss_batch(db_session_factory, brand_kit_id: uuid.UUID, item_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Scores a batch of newly-fetched RSS entries in one call (unlike
    Gmail, where each email needs its own call to find articles embedded in
    it, RSS entries are already discrete - batching several into one
    triage call is the natural cost saving), then hands the results to
    create_content_items. Same atomic claim-before-work race guard as
    process_email."""
    if not item_ids:
        return []
    db = db_session_factory()
    try:
        claim = db.execute(
            update(IngestedRssItem)
            .where(IngestedRssItem.id.in_(item_ids), IngestedRssItem.status == "new")
            .values(status="processing")
        )
        db.commit()
        if claim.rowcount == 0:
            return []

        items = (
            db.query(IngestedRssItem)
            .filter(IngestedRssItem.id.in_(item_ids), IngestedRssItem.status == "processing")
            .all()
        )
        entries = [{"title": it.title, "link": it.link, "summary": it.summary} for it in items]
        articles = extract_rss_angles(db, brand_kit_id, entries)

        for it in items:
            it.status = "processed"
        db.commit()
    finally:
        db.close()

    return create_content_items(db_session_factory, brand_kit_id, articles, source_email_id=None)


def resume_content_item(content_item_id: uuid.UUID, resume_payload: dict) -> None:
    config = {"configurable": {"thread_id": str(content_item_id)}}
    get_graph().invoke(Command(resume=resume_payload), config)


def sync_format_to_graph_state(content_item_id: uuid.UUID, format_: str) -> None:
    """routes_board.py's generate_media/approve_and_generate_media change
    content_item.format directly in the DB, bypassing the graph entirely
    (this item already reached its media_review interrupt or END, so there's
    no pending interrupt to resume into) - but the graph's OWN checkpointed
    state still has whatever format content_review_gate last set, and that
    stale value is what a later "regenerate" click at media_review_gate
    actually reads (_route_after_media_review), not the DB. Found live: a
    reel item switched to poster via "Generate poster instead" kept silently
    regenerating REELS (burning real Veo cost) every time Regenerate was
    clicked afterward, because the checkpoint never learned about the
    switch, while content_item.format in the DB stayed correctly on
    "poster" the whole time - the two were simply out of sync. Call this
    right after every out-of-graph format change to keep them in sync.
    Best-effort: a missing/exhausted checkpoint thread is logged, not
    raised, since the format change to the DB itself already succeeded."""
    config = {"configurable": {"thread_id": str(content_item_id)}}
    try:
        get_graph().update_state(config, {"format": format_})
    except Exception:
        logger.exception("Could not sync format to graph checkpoint for %s", content_item_id)


def _park_at_gate(content_item_id: uuid.UUID, as_node: str, state_patch: dict) -> bool:
    """Fast-forwards the graph checkpoint to look like `as_node` just ran,
    then runs forward from there - lands paused at whatever gate follows
    as_node's edge, re-triggering its interrupt(). Same out-of-graph
    checkpoint-sync pattern as sync_format_to_graph_state above."""
    config = {"configurable": {"thread_id": str(content_item_id)}}
    try:
        get_graph().update_state(config, state_patch, as_node=as_node)
        get_graph().invoke(None, config)
        return True
    except Exception:
        logger.exception("Could not park graph checkpoint at %s for %s", as_node, content_item_id)
        return False


def send_to_content_review(content_item_id: uuid.UUID, format_: str, language: str | None = None) -> bool:
    """Fast-forwards a Researched/Analyzed card straight into Content
    Review - for Researched (no interrupt reached yet, or a failed run) and
    for an Analyzed item whose brand has since turned on combined_drafting
    (its pending interrupt is the wrong one - analytical_review_gate, not
    combined_review_gate). Does the real work directly (bypasses
    Command(resume=...), same reasoning as retry_content_writer), then
    parks the checkpoint via _park_at_gate."""
    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, content_item_id)
        if not content_item or content_item.stage not in (Stage.RESEARCHED, Stage.ANALYZED):
            return False
        brand_kit = db.get(BrandKit, content_item.brand_kit_id)
        email = db.get(IngestedEmail, content_item.source_email_id) if content_item.source_email_id else None

        if content_item.article_url and not content_item.article_full_text:
            try:
                result = fetch_article_text(content_item.article_url)
                content_item.article_full_text = result.text
                if result.resolved_url:
                    content_item.article_url = result.resolved_url
            except Exception:
                logger.exception("Article fetch failed for %s, continuing without it", content_item_id)

        content_item.format = format_ if format_ in VALID_FORMATS else DEFAULT_AUTO_FORMAT
        if language in LANGUAGE_CHOICES:
            content_item.language = language

        combined = bool(brand_kit and brand_kit.combined_drafting)
        if combined:
            if not content_item.language:
                content_item.language = brand_kit.default_language
            write_brief_and_copy(db, content_item, email, content_item.format)
        else:
            if not content_item.brief:
                write_brief(db, content_item, email)
            write_copy(db, content_item, revision_feedback=None)
        _prebuild_shotlist_for_review(db, content_item)
        content_item.stage = Stage.DRAFTED
        db.commit()
        format_result = content_item.format
        brand_kit_id = content_item.brand_kit_id
        source_email_id = content_item.source_email_id
    finally:
        db.close()

    as_node = "analytical" if combined else "content_writer"
    return _park_at_gate(
        content_item_id,
        as_node,
        {
            # A Researched item's checkpoint thread may never have had an
            # initial state to merge into (unlike an already-Analyzed one) -
            # content_item_id must always be here, not just the changed keys.
            "content_item_id": content_item_id,
            "brand_kit_id": brand_kit_id,
            "source_email_id": source_email_id,
            "stage": Stage.DRAFTED,
            "format": format_result,
            "combined_drafting": combined,
            "user_feedback": None,
        },
    )


def rewind_to_content_review(content_item_id: uuid.UUID) -> bool:
    """Sends a Media Generated/Approved card back to Content Review for
    re-editing - existing copy_text/media stay as history, "regenerate"
    from there writes fresh ones. No-op if there's no copy_text to review."""
    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, content_item_id)
        if not content_item or content_item.stage not in (Stage.MEDIA_GENERATED, Stage.APPROVED):
            return False
        if not content_item.copy_text:
            return False
        content_item.stage = Stage.DRAFTED
        content_item.is_processing = False
        content_item.last_error = None
        db.commit()
        format_ = content_item.format
        brand_kit_id = content_item.brand_kit_id
        source_email_id = content_item.source_email_id
    finally:
        db.close()

    return _park_at_gate(
        content_item_id,
        "content_writer",
        {
            "content_item_id": content_item_id,
            "brand_kit_id": brand_kit_id,
            "source_email_id": source_email_id,
            "stage": Stage.DRAFTED,
            "format": format_,
            "user_feedback": None,
        },
    )


def rewind_to_needs_review(content_item_id: uuid.UUID) -> bool:
    """Sends a Content Review/Media Review/Approved/Discarded card back to
    Needs Review (analytical_review_gate) - e.g. undoing a "write myself"
    choice, or restoring a discarded item that got at least as far as
    having a brief (see restore_content_item below for the no-brief case,
    which this doesn't cover). Always the non-combined gate (regardless of
    the brand's current combined_drafting setting), since that's Needs
    Review's own interrupt. No-op if there's no brief to review."""
    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, content_item_id)
        if not content_item or content_item.stage not in (
            Stage.DRAFTED, Stage.MEDIA_GENERATED, Stage.APPROVED, Stage.DISCARDED,
        ):
            return False
        if not content_item.brief:
            return False
        content_item.stage = Stage.ANALYZED
        content_item.is_processing = False
        content_item.last_error = None
        db.commit()
        brand_kit_id = content_item.brand_kit_id
        source_email_id = content_item.source_email_id
    finally:
        db.close()

    return _park_at_gate(
        content_item_id,
        "analytical",
        {
            "content_item_id": content_item_id,
            "brand_kit_id": brand_kit_id,
            "source_email_id": source_email_id,
            "stage": Stage.ANALYZED,
            "combined_drafting": False,
            "user_feedback": None,
        },
    )


def restore_content_item(content_item_id: uuid.UUID) -> bool:
    """Brings a discarded item back for reconsideration. There was
    previously no way to do this at all - a discard was permanent.

    Two cases, depending on how far the item got before being discarded:
    - It has a brief (discarded from Content Review/Media Review/Approved,
      or straight from Needs Review): just rewind_to_needs_review, which
      now also accepts Stage.DISCARDED (see its own docstring).
    - It has no brief at all - discarded straight out of Researcher triage
      (suitable_for_social=False in create_content_items), which never even
      ran the graph once, so there's no existing checkpoint thread to
      re-park. Restarts the SAME graph invocation create_content_items uses
      for a freshly-suitable article (fetch article -> write brief -> land
      at Needs Review) instead."""
    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, content_item_id)
        if not content_item or content_item.stage != Stage.DISCARDED:
            return False
        has_brief = bool(content_item.brief)
        brand_kit_id = content_item.brand_kit_id
        source_email_id = content_item.source_email_id
    finally:
        db.close()

    if has_brief:
        return rewind_to_needs_review(content_item_id)

    config = {"configurable": {"thread_id": str(content_item_id)}}
    try:
        get_graph().invoke(
            {
                "content_item_id": content_item_id,
                "brand_kit_id": brand_kit_id,
                "source_email_id": source_email_id,
                "stage": Stage.RESEARCHED,
            },
            config,
        )
        return True
    except Exception:
        logger.exception("Restore-from-discard graph run failed for %s", content_item_id)
        return False


def park_at_media_review(
    content_item_id: uuid.UUID, format_: str, brand_kit_id: uuid.UUID | None, source_email_id: uuid.UUID | None
) -> bool:
    """Parks the checkpoint at media_review_gate after routes_board.py's
    generate_media/approve_and_generate_media generate a poster/reel
    out-of-band (that item's graph thread had already reached END) - without
    this, the resulting card's own Approve/Regenerate/Discard buttons
    silently no-op, since Command(resume=...) has no pending interrupt to
    resume into."""
    if format_ == "reel":
        as_node = "reel_editor"
    elif format_ == "carousel":
        as_node = "carousel_editor"
    else:
        as_node = "graphic_designer"
    return _park_at_gate(
        content_item_id,
        as_node,
        {
            "content_item_id": content_item_id,
            "brand_kit_id": brand_kit_id,
            "source_email_id": source_email_id,
            "stage": Stage.MEDIA_GENERATED,
            "format": format_,
            "user_feedback": None,
        },
    )
