import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db.session import Base


def _uuid_col():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_col()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    auth_provider: Mapped[str] = mapped_column(String(50), default="basic")
    # Global-admin flag (distinct from being a per-brand owner) - gates the
    # /admin/users "create a user" flow. Only the bootstrapped admin account
    # has this set, unless another admin promotes someone.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserApiKey(Base):
    """A user's own LLM provider API key, encrypted at rest (same Fernet
    scheme as OAuthCredential). When generation runs for a brand, the
    BRAND OWNER's key for that provider is used (see llm/user_keys.py's
    resolve_api_key) - a shared/member operator's own keys are never used
    for a brand they don't own, only whoever created it. Falls back to the
    process-wide ANTHROPIC_API_KEY/OPENAI_API_KEY/GOOGLE_API_KEY env vars
    when the owner hasn't set their own key for that provider, so an
    existing single-admin setup keeps working without forced migration."""

    __tablename__ = "user_api_keys"

    id: Mapped[uuid.UUID] = _uuid_col()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)  # "anthropic" | "openai" | "google"
    encrypted_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_user_api_keys_user_provider"),)


class NicheConfig(Base):
    """Versioned, append-only config describing what the Researcher agent should
    look for. Editing creates a new row; the newest is_active=True row is current."""

    __tablename__ = "niche_config"

    id: Mapped[uuid.UUID] = _uuid_col()
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    keywords: Mapped[list] = mapped_column(JSONB, default=list)
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("niche_config.id"), nullable=True
    )
    # Which brand this niche belongs to - what draft_niche_from_brand
    # (niche_evolution.py) reads industry/website_url/social_handles from,
    # and what every new version gets stamped with. Also what
    # get_active_niche_config filters on, since a niche is per-brand.
    brand_kit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brand_kit.id"), nullable=False
    )
    # "user" | "llm_suggested" (periodic reflection on triage history) |
    # "brand_drafted" (one-off draft grounded in brand_kit info)
    created_by: Mapped[str] = mapped_column(String(20), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Only set for created_by="llm_suggested": why the niche-evolution agent
    # proposed this revision, shown to the user before they activate it.
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BrandKit(Base):
    """Brand identity used on generated media - NOT a color scheme. The
    Graphic Designer prints brand_name/social_handles/website_url as a
    bottom strip on posters (and Phase 3's Reel Editor will do the same as
    an end-card). font_choice is a key into
    agents/graphic_designer/fonts.py's FONT_CHOICES (a fixed, vendored set -
    free-text font names are useless without an actual .ttf file to render)."""

    __tablename__ = "brand_kit"

    id: Mapped[uuid.UUID] = _uuid_col()
    # Who created this brand - the single source of truth for who can manage
    # sharing/removal (see BrandMember below, which is the access list of
    # everyone who can use the brand, owner included).
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), default="default")
    brand_name: Mapped[str] = mapped_column(String(200), default="")
    # Short free-text description of what the brand does/covers (e.g. "vegan
    # bakery in Austin", "grassroots political collective") - grounds
    # draft_niche_from_brand's LLM call so a vague niche prompt isn't the
    # only signal the Researcher agent has to go on.
    industry: Mapped[str] = mapped_column(String(200), default="")
    website_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # e.g. {"instagram": "@handle", "facebook": "handle", "youtube": "@handle", "x": "@handle", "tiktok": "@handle"}
    social_handles: Mapped[dict] = mapped_column(JSONB, default=dict)
    font_choice: Mapped[str] = mapped_column(String(50), default="inter")
    # WhatsApp JID to send approved posts to via Botsab - "{phone}@s.whatsapp.net"
    # for a DM or "{numeric_id}@g.us" for a group (see integrations/botsab/).
    whatsapp_recipient: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # This brand's own Botsab connection - each brand can send from a
    # different WhatsApp number/instance. botsab_base_url stays a shared
    # .env setting (one Botsab deployment endpoint); only the per-instance
    # identity is brand-scoped. Falls back to BOTSAB_API_KEY/INSTANCE_ID env
    # vars when unset, same "brand setting overrides env fallback" pattern
    # as UserApiKey.
    botsab_instance_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    botsab_api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # This brand's own Postiz connection (integrations/postiz/) - same
    # brand-override-falls-back-to-.env pattern as Botsab. postiz_channels
    # is which of this brand's connected Postiz channels "Send to Postiz"
    # targets: [{"id": ..., "identifier": <platform type>, "name": ...}] -
    # identifier is stored alongside the id so send.py doesn't need a live
    # GET /integrations call just to know each channel's platform type.
    postiz_api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    postiz_channels: Mapped[list] = mapped_column(JSONB, default=list)
    logo_asset_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reference_image_paths: Mapped[list] = mapped_column(JSONB, default=list)
    tone_of_voice_prompt: Mapped[str] = mapped_column(Text, default="")
    default_language: Mapped[str] = mapped_column(String(20), default="en")
    # Skips the separate Content Writer LLM call - one cheap Sonnet-5 call
    # in the Analytical step drafts the brief AND the post together (see
    # orchestrator.py's _analytical_node / analytical/graph.py's
    # write_brief_and_copy). Off by default so existing brands keep today's
    # two-step Analytical -> Content Writer flow unless deliberately
    # switched over. combined_drafting_format is which format that single
    # call drafts for (poster/reel/text_only, same choice normally made
    # per-item at the analytical_review gate) - a reel-focused brand sets
    # this to "reel" so the cheap step drafts reel_script directly instead
    # of wastefully drafting+generating a poster first.
    combined_drafting: Mapped[bool] = mapped_column(Boolean, default=False)
    combined_drafting_format: Mapped[str] = mapped_column(String(20), default="poster")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BrandMember(Base):
    """The access list for a brand - who can see/operate it. The owner
    (brand_kit.owner_user_id) also gets a row here with role="owner", so
    "which brands can this user access" is always a single join on this
    table rather than a UNION with brand_kit.owner_user_id. Permission
    checks that gate sharing/removing members/deleting the brand still go
    through brand_kit.owner_user_id directly, not role=="owner" here."""

    __tablename__ = "brand_members"

    id: Mapped[uuid.UUID] = _uuid_col()
    brand_kit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brand_kit.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="member")  # "owner" | "member"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("brand_kit_id", "user_id", name="uq_brand_members_brand_user"),)


class AgentModelConfig(Base):
    """Which model each agent/task uses. brand_kit_id NULL = the global
    default row (what every brand falls back to); a non-NULL row overrides
    that default for just that brand (llm/provider.py::ChatProvider tries
    the brand-specific row first, then the global row, then
    DEFAULT_MODELS). No DB-level uniqueness on (agent_task, brand_kit_id) -
    Postgres treats every NULL as distinct so a naive constraint wouldn't
    actually enforce "at most one global row per task" anyway; enforced at
    the application level instead (query-then-upsert), same pattern as
    every other UI-editable setting in this app."""

    __tablename__ = "agent_model_config"

    id: Mapped[uuid.UUID] = _uuid_col()
    agent_task: Mapped[str] = mapped_column(String(100), nullable=False)
    brand_kit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brand_kit.id"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # "anthropic"|"openai"|"google"
    model_id: Mapped[str] = mapped_column(String(150), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)  # e.g. {"thinking_effort": "high"}
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LlmCallLog(Base):
    __tablename__ = "llm_call_log"

    id: Mapped[uuid.UUID] = _uuid_col()
    agent_task: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model_id: Mapped[str] = mapped_column(String(150), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    content_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("content_items.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OAuthCredential(Base):
    """Stores an encrypted google.oauth2.credentials.Credentials.to_json()
    payload per provider. "gmail" today; a future "google_login" row (same
    table) backs the eventual GoogleOAuthBackend for site login."""

    __tablename__ = "oauth_credentials"

    id: Mapped[uuid.UUID] = _uuid_col()
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    # Which brand this credential belongs to - nullable because a future
    # "google_login" row (site-login OAuth) isn't brand-scoped at all. Every
    # "gmail" row always has one, since Gmail is a per-brand connection.
    brand_kit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brand_kit.id"), nullable=True
    )
    encrypted_payload: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("provider", "brand_kit_id", name="uq_oauth_credentials_provider_brand"),)


class SyncState(Base):
    """Small key/value table for cursor-like state, e.g. gmail's last
    successful fetch timestamp, without a dedicated table per integration."""

    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    # Part of the PK alongside `key` - every setting this table holds today
    # (auto-mode, reel cost cap/video model, Gmail cursor/search query) is
    # per-brand, so there's no "global" row to allow a NULL here for -
    # Postgres disallows nullable PK columns anyway. Callers now do
    # db.get(SyncState, (key, brand_kit_id)).
    brand_kit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brand_kit.id"), primary_key=True
    )
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IngestedEmail(Base):
    """One fetched email. May contain many articles (a typical newsletter has
    10-30) - the Researcher agent extracts them into one ContentItem each, so
    priority scoring etc. live on ContentItem now, not here."""

    __tablename__ = "ingested_emails"

    id: Mapped[uuid.UUID] = _uuid_col()
    brand_kit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brand_kit.id"), nullable=False, index=True
    )
    # Unique per brand, not globally - two brands can (and, per a live report,
    # do) connect the SAME physical Gmail account, and each needs to see and
    # ingest every message independently. A bare global-unique constraint
    # here meant the second brand's fetch silently found nothing: the dedup
    # check saw the first brand's already-ingested rows and skipped every
    # message, even on that brand's very first-ever fetch.
    gmail_message_id: Mapped[str] = mapped_column(String(100), nullable=False)
    sender: Mapped[str] = mapped_column(String(320), default="")
    subject: Mapped[str] = mapped_column(String(998), default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snippet: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    # Markdown-ish text preserving hyperlinks (plain .get_text() drops every
    # href) - what the Researcher's article-extraction prompt is built from.
    body_with_links: Mapped[str] = mapped_column(Text, default="")

    status: Mapped[str] = mapped_column(String(20), default="new")  # new|processed|failed
    # How many articles the Researcher extracted from this email (0 if none
    # were worth turning into a ContentItem) - shown in the UI so it's clear
    # this email was actually looked at, not silently skipped.
    articles_found: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # No back-reference to content_items here - that would create a mutual FK
    # cycle between the two tables. Look it up via
    # ContentItem.source_email_id == IngestedEmail.id instead.

    __table_args__ = (
        UniqueConstraint("gmail_message_id", "brand_kit_id", name="uq_ingested_emails_message_brand"),
    )


class ContentItem(Base):
    """The shared pipeline record for ONE article/post-in-making - mirrors
    backend/agents/state.py's ContentItemState, which is checkpointed against
    langgraph_thread_id (== str(id)) by the orchestrator's PostgresSaver.
    Many of these can share one source_email_id (one per newsletter article)."""

    __tablename__ = "content_items"

    id: Mapped[uuid.UUID] = _uuid_col()
    # Direct column (not just derived through source_email_id) so every
    # agent function that already holds a ContentItem has brand identity for
    # free, without a join through IngestedEmail.
    brand_kit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brand_kit.id"), nullable=False, index=True
    )
    source_email_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingested_emails.id"), nullable=True
    )
    stage: Mapped[str] = mapped_column(String(30), default="raw")
    format: Mapped[str | None] = mapped_column(String(20), nullable=True)
    language: Mapped[str] = mapped_column(String(20), default="en")

    # Set by the Researcher agent at extraction time
    article_title: Mapped[str] = mapped_column(String(500), default="")
    article_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    article_summary: Mapped[str] = mapped_column(Text, default="")  # blurb as it appeared in the email
    priority_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    priority_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Set by the fetch-article step (Phase 1) - full text from article_url,
    # fetched via a free extraction (httpx + readability), used by Analytical
    # instead of just the newsletter's blurb. Null if there was no URL, the
    # fetch failed, or the site blocked it - Analytical falls back to
    # article_summary in that case.
    article_full_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    brief: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Set by the Content Writer agent (Phase 2)
    copy_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # the social caption
    hashtags: Mapped[list] = mapped_column(JSONB, default=list)
    # Short punchy text for the Graphic Designer to render on a poster -
    # deliberately separate from copy_text (which can be long) so the
    # Graphic Designer has an exact, already-approved string it must render
    # verbatim rather than deriving/summarizing text itself.
    poster_headline: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Which poster layout to use - "quote" | "tribute" | "narrative" |
    # "fact_critique" | "trivia" | "event" (see graphic_designer/templates.py).
    # Inferred by Content Writer from the article, user-overridable before
    # generation. poster_content's shape depends on which template this is -
    # each template's render function in graphic_designer/layout.py owns
    # interpreting its own fields, see templates.py for the exact shapes.
    poster_template: Mapped[str | None] = mapped_column(String(30), nullable=True)
    poster_content: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Set by the Content Writer agent when format="reel" (Phase 3)
    reel_script: Mapped[str | None] = mapped_column(Text, nullable=True)  # overall narrative
    character_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "explainer_influencer" | "faceless" | "animated_contextual" - see
    # reel_editor/templates.py. Inferred by Content Writer, user-overridable
    # before generation, same pattern as poster_template.
    reel_template: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # [{"type": "video", "description": "...", "narration": "..."}, or
    # {"type": "text_card", "text": "...", "label": "..."}] - one per scene,
    # built once by reel_editor's shot-listing step and reused on retry/
    # regenerate. See reel_editor/prompts.py for the exact contract.
    reel_scenes: Mapped[list] = mapped_column(JSONB, default=list)

    # Set when a background task (currently only Reel Editor) fails; cleared
    # on the next successful attempt. Distinguishes "stage=drafted, genuinely
    # paused at the content_review interrupt" from "stage=drafted because a
    # reel generation attempt failed after already consuming that interrupt" -
    # those look identical from stage/is_processing alone, but the former
    # accepts normal approve/regenerate/discard while the latter has no
    # pending interrupt to resume and needs retry_reel/discard instead.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # True while a background task (currently: Reel Editor - Veo generation
    # can take minutes per clip, far too long to hold an HTTP request open)
    # is working on this item. The board shows a "processing" state instead
    # of whatever stage it's stuck at until the task clears this.
    is_processing: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ContentItemVersion(Base):
    """Append-only edit/regeneration log per stage - audit trail and the
    basis for "regenerate with feedback" at every approval gate."""

    __tablename__ = "content_item_versions"

    id: Mapped[uuid.UUID] = _uuid_col()
    content_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("content_items.id"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    model_used: Mapped[str | None] = mapped_column(String(150), nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MediaAsset(Base):
    """A generated image/video for a content item (Phase 2: poster PNGs from
    the Graphic Designer agent; Phase 3 will add reel MP4s the same way)."""

    __tablename__ = "media_assets"

    id: Mapped[uuid.UUID] = _uuid_col()
    content_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("content_items.id"), nullable=False
    )
    asset_type: Mapped[str] = mapped_column(String(30), nullable=False)  # "poster" | "reel" (Phase 3)
    storage_uri: Mapped[str] = mapped_column(String(1000), nullable=False)
    generation_model: Mapped[str | None] = mapped_column(String(150), nullable=True)
    generation_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PostizPost(Base):
    """One row per channel targeted by a "Send to Postiz" click
    (integrations/postiz/send.py::send_content_item batches every configured
    channel into one Postiz API call, but each channel gets its own postId
    back) - lets the board show a "Scheduled" column with channel + time
    without polling Postiz. scheduled_at is None for an immediate ("now")
    send; only rows with it set are surfaced as still-pending."""

    __tablename__ = "postiz_posts"

    id: Mapped[uuid.UUID] = _uuid_col()
    content_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("content_items.id"), nullable=False, index=True
    )
    postiz_post_id: Mapped[str] = mapped_column(String(200), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(200), nullable=False)
    channel_name: Mapped[str] = mapped_column(String(200), default="")
    channel_identifier: Mapped[str] = mapped_column(String(50), default="")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
