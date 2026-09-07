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
           -> (poster format) graphic_designer -> [interrupt: media_review] -> END
           -> (reel format)   reel_editor      -> [interrupt: media_review] -> END
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

from backend.agents.analytical.graph import write_brief, write_brief_and_copy
from backend.agents.auto_mode import DEFAULT_FORMAT as DEFAULT_AUTO_FORMAT
from backend.agents.auto_mode import VALID_FORMATS, get_auto_mode_settings
from backend.agents.content_writer.graph import write_copy
from backend.agents.graphic_designer.graph import generate_poster
from backend.agents.languages import LANGUAGE_CHOICES
from backend.agents.reel_editor.graph import generate_reel
from backend.agents.researcher.graph import extract_articles
from backend.agents.state import ContentItemState, Format, Stage
from backend.app.config import get_settings
from backend.app.db.models import BrandKit, ContentItem, IngestedEmail
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
        if content_item.article_url:
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
            # default format's fields together. format_ only defaults here
            # (not overwritten) so a regenerate cycle back through this same
            # node doesn't reset a format the user already changed.
            if not content_item.format:
                content_item.format = (
                    brand_kit.combined_drafting_format
                    if brand_kit.combined_drafting_format in VALID_FORMATS
                    else DEFAULT_AUTO_FORMAT
                )
            content_item.language = brand_kit.default_language
            write_brief_and_copy(
                db, content_item, email, content_item.format, revision_feedback=state.get("user_feedback")
            )
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
            # User is handling this post outside the system using the
            # brief; nothing more for the graph to do with it.
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
        elif choice == "approve" and content_item.format not in (Format.POSTER, Format.REEL):
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
    return "analytical"  # regenerate - brief+copy are one call, redo both


def _content_writer_node(state: ContentItemState) -> dict:
    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, state["content_item_id"])
        write_copy(db, content_item, revision_feedback=state.get("user_feedback"))
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
        elif choice == "approve" and content_item.format not in (Format.POSTER, Format.REEL):
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
        return "reel_editor" if state.get("format") == Format.REEL else "graphic_designer"
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
        ["analytical", "graphic_designer", "reel_editor", END],
    )
    graph.add_edge("content_writer", "content_review_gate")
    graph.add_conditional_edges(
        "content_review_gate",
        _route_after_content_review,
        ["content_writer", "graphic_designer", "reel_editor", END],
    )
    graph.add_edge("graphic_designer", "media_review_gate")
    graph.add_edge("reel_editor", "media_review_gate")
    graph.add_conditional_edges(
        "media_review_gate", _route_after_media_review, ["graphic_designer", "reel_editor", END]
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


def process_email(db_session_factory, email_id: uuid.UUID) -> list[uuid.UUID]:
    """Runs the Researcher once over the whole email (extracting every
    article it contains), creates one ContentItem per article, and drives a
    graph run (through to the first interrupt) for each suitable one.
    Unsuitable articles are persisted as stage=DISCARDED with their rationale
    but never get a graph run - there's nothing left to do with them, and
    this keeps them visible on the board instead of silently vanishing.

    Returns the ids of the ContentItems that were sent into the pipeline
    (i.e. excludes the discarded ones).

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

        created: list[tuple[uuid.UUID, bool]] = []  # (content_item_id, suitable)
        for article in articles:
            suitable = bool(article.get("suitable_for_social", False))
            content_item = ContentItem(
                brand_kit_id=brand_kit_id,
                source_email_id=email_id,
                stage=Stage.RESEARCHED if suitable else Stage.DISCARDED,
                article_title=str(article.get("title", ""))[:500],
                article_url=article.get("url") or None,
                article_summary=article.get("summary", ""),
                priority_score=article.get("priority_score"),
                priority_rationale=article.get("rationale", ""),
            )
            db.add(content_item)
            db.flush()  # populate content_item.id without a full commit yet
            created.append((content_item.id, suitable))

        email.status = "processed"
        email.articles_found = len(articles)
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
                    "source_email_id": email_id,
                    "stage": Stage.RESEARCHED,
                },
                config,
            )
            started_ids.append(content_item_id)
            auto_advance_content_item(content_item_id)
        except Exception:
            # One bad article (fetch/LLM failure) shouldn't block the other
            # 10-30 articles a newsletter can contain.
            logger.exception("Pipeline run failed for content_item %s", content_item_id)

    return started_ids


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


def redraft_via_combined_drafting(content_item_id: uuid.UUID) -> bool:
    """For an item sitting at Stage.ANALYZED - checkpointed paused at
    analytical_review_gate, from before its brand turned on
    combined_drafting - redoes the analytical step through
    write_brief_and_copy instead, landing it at Stage.DRAFTED so the human
    never needs to click through a separate Content Writer step. Returns
    False (no-op) if the item isn't at Stage.ANALYZED or its brand doesn't
    have combined_drafting on.

    Bypasses Command(resume=...) entirely, same reasoning as retry_reel/
    retry_content_writer - this item's checkpoint is paused at a DIFFERENT
    interrupt (analytical_review_gate) than the one it needs to land at
    (combined_review_gate), so a normal resume can't get there; there's no
    "switch which gate I'm paused at" resume payload. Instead: do the work
    directly, then use LangGraph's update_state(..., as_node="analytical")
    to tell the checkpoint "pretend the analytical node just produced this
    state" - its outgoing edge (_route_after_analytical) then correctly
    sends the NEXT invoke to combined_review_gate instead of
    analytical_review_gate. update_state alone doesn't run anything further,
    so a following invoke(None, config) is what actually executes forward
    to (and pauses at) that interrupt, keeping the checkpoint's real
    position and content_item.stage in sync - the same "DB write + graph
    checkpoint write" pairing sync_format_to_graph_state above uses for a
    different out-of-graph change."""
    db = SessionLocal()
    try:
        content_item = db.get(ContentItem, content_item_id)
        if not content_item or content_item.stage != Stage.ANALYZED:
            return False
        brand_kit = db.get(BrandKit, content_item.brand_kit_id)
        if not brand_kit or not brand_kit.combined_drafting:
            return False

        email = db.get(IngestedEmail, content_item.source_email_id) if content_item.source_email_id else None
        if not content_item.format:
            content_item.format = (
                brand_kit.combined_drafting_format
                if brand_kit.combined_drafting_format in VALID_FORMATS
                else DEFAULT_AUTO_FORMAT
            )
        content_item.language = brand_kit.default_language
        write_brief_and_copy(db, content_item, email, content_item.format)
        content_item.stage = Stage.DRAFTED
        format_ = content_item.format
        db.commit()
    finally:
        db.close()

    config = {"configurable": {"thread_id": str(content_item_id)}}
    try:
        get_graph().update_state(
            config,
            {"stage": Stage.DRAFTED, "format": format_, "combined_drafting": True, "user_feedback": None},
            as_node="analytical",
        )
        get_graph().invoke(None, config)
    except Exception:
        logger.exception("Could not fast-forward graph checkpoint for %s", content_item_id)
        return False
    return True
