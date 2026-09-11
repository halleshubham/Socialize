# Architecture

Full design rationale lives in the approved plan; this is the quick-reference version plus how to actually run things locally.

## Shape

One LangGraph graph per `content_item` (= one article, not one email - see Researcher below), subgraphs as nodes, `interrupt()` at every human approval gate, checkpointed to Postgres (`PostgresSaver`) so a run can pause indefinitely and resume via `Command(resume=...)` against the same `thread_id`.

```
Gmail fetch (daily cron)
  -> Researcher extracts every article from the email (process_email, outside the graph)
  -> per suitable article: fetch_article -> analytical -> [INTERRUPT: review]
       -> content_writer -> [INTERRUPT: approve copy]
            -> (poster format)    graphic_designer -> [INTERRUPT: approve media] -> END
            -> (reel format)      reel_editor      -> [INTERRUPT: approve media] -> END
            -> (carousel format)  carousel_editor  -> [INTERRUPT: approve media] -> END
            -> (text_only format) END
       -> END (discard / write_myself at either gate)
```

See `backend/agents/orchestrator.py` for the current graph and `backend/agents/state.py` for the shared `ContentItemState`. Social Manager (Phase 4) isn't built yet.

**reel_editor runs in a background thread**, not inline in the graph invocation - Veo generation can take minutes per scene, far too long to hold an HTTP request open. `routes_board.py`'s `decide()` detects (by checking `content_item.stage`/`format`/`decision` - mirrors `orchestrator.py`'s own routing logic, keep both in sync) when a resume will enter `reel_editor` and dispatches it via `app/worker/background.py`'s thread pool instead of calling `resume_content_item` directly; the card shows `is_processing` in the meantime. A failure there can't be retried via the normal `/decide` resume (the `content_review` interrupt was already consumed to get there) - `last_error` gets set instead, and the board shows Retry/Discard, where Retry calls `generate_reel` directly rather than going through the graph.

## Combined drafting

`brand_kit.combined_drafting` (off by default) skips the separate Content Writer LLM call - one cheap Sonnet-5 call in the Analytical step drafts the brief AND the post (copy_text/hashtags/poster-or-reel fields) together, instead of a second, pricier Opus/GPT call. `agents/orchestrator.py`'s `_analytical_node` branches on the brand's flag: non-combined path is untouched (`write_brief`, stage→`ANALYZED`, the two-gate flow below); combined path calls `analytical/graph.py::write_brief_and_copy`, sets `content_item.format` from `brand_kit.combined_drafting_format` (a per-brand default, `"poster"`/`"reel"`/`"text_only"` - **not** hardcoded to poster, since that would mean a reel-focused brand always drafts+generates a wasted poster before "Generate reel instead" - the format-switch button - becomes available, since that button only lives on already-generated Media Review cards) and sets `content_item.language` from `brand_kit.default_language` (no per-item pre-draft override in combined mode, unlike normal mode's analytical_review gate), then lands the item at `Stage.DRAFTED` directly from `Stage.RESEARCHED` - skipping `Stage.ANALYZED` entirely.

That stage-skip is the trick that makes the rest of the codebase not need to know combined mode exists: `Stage.DRAFTED` already has a fully generic UI (board's "Content Review" column, the approve/regenerate/discard modal, `routes_board.py::decide`/`_decide_enters_slow_node`, `auto_advance_content_item`'s `stage == Stage.DRAFTED` branch) that reads purely off `content_item.stage`/`format`/fields, never which LangGraph node produced them - so `content_writer/graph.py`, `board.html`, and `agents/auto_mode.py` needed zero changes. Combined-mode cards simply never appear in "Needs Review" (`Stage.ANALYZED`) - not a bug, they skip straight to "Content Review".

A new `combined_review_gate` node (mirrors `content_review_gate`'s approve/regenerate/discard shape, plus `write_myself` since it's this item's only gate) replaces `analytical_review_gate`+`content_writer`+`content_review_gate` for these items - `_route_after_analytical` picks which gate based on a `combined_drafting` flag threaded through `ContentItemState` (set by `_analytical_node`, read by the router, avoiding a second DB query). "Regenerate" cycles back to `analytical` itself (not `content_writer` - there isn't one on this path), since brief+copy are one call now, redoing both together. Verified live end-to-end for both `combined_drafting_format="poster"` (real poster generated, exactly one `analytical_combined_draft` LLM call in `llm_call_log`, not the usual two) and `"reel"` (reel_script/character_description populated directly, poster fields correctly left empty, no poster ever generated first).

## Multi-brand support

The app moved from single-tenant (one implicit `BrandKit` row) to multi-brand-per-user with sharing. `BrandKit` **is** the brand/tenant entity - no separate `Brand` table, since it already held every identity field including the logo path. Every brand-scoped table (`niche_config`, `ingested_emails`, `content_items`, `sync_state`, `oauth_credentials`) carries a `brand_kit_id` FK; `content_items`/`ingested_emails` have it as a direct column (not just derived through the email relationship) so any agent function already holding a `ContentItem` has brand identity for free.

**Ownership and sharing**: `brand_kit.owner_user_id` is the single source of truth for who can manage sharing/remove members (`backend/app/api/routes_brands.py`'s owner-only routes, gated by `auth/brand_deps.py`'s `get_owned_brand`). `BrandMember` is the full access list (owner included, with `role="owner"`) - "which brands can this user see" is always one join on it (`get_accessible_brands`), never a UNION with `owner_user_id`. Two-tier only: owner manages sharing/removal, member has full operational access otherwise (board, brand-kit, niche, generation).

**Active brand resolution**: `auth/brand_deps.py`'s `get_active_brand` FastAPI dependency reads `request.session["active_brand_id"]`, validated against the user's accessible brands, defaulting to the first accessible one (alphabetical) if unset/no-longer-accessible. Every route touching brand-scoped data adds `brand: BrandKit = Depends(get_active_brand)` alongside `Depends(get_current_user)` - FastAPI's dependency cache means this doesn't double the auth check. A user with zero accessible brands (every admin-created account starts here) hits `NoBrandAccess`, caught in `main.py` and redirected to `/brands/new`.

**Authorization on content items**: every `routes_board.py` action taking a `content_item_id` goes through `_accessible_content_item(db, user, content_item_id)` first - checks the item's `brand_kit_id` is one the current user can access (owned or shared) before touching it, independent of which brand happens to be "active" in the session. Verified live: a user with no access to a brand gets silently no-op'd (redirected, item untouched) on a direct POST to another brand's item.

**User accounts**: no self-serve signup, no email infra - `users.is_admin` gates `/admin/users`, an admin-only "create a user" form (email + password, shared out-of-band). A brand owner then shares a brand with that user by email from the Brand Kit page's Members section. `bootstrap_admin_user()` (main.py) sets `is_admin=True` on the one env-var-configured admin account.

**SyncState became brand-scoped**: `sync_state`'s primary key grew from `key` alone to `(key, brand_kit_id)` (Postgres disallows a nullable PK column, so every one of its 8 settings - auto-mode x4, reel cost cap, reel video model, Gmail last-fetch-epoch, Gmail search query - is now always tied to a real brand, no "global" row). Every getter/setter (`agents/auto_mode.py`, `reel_editor/graph.py`, `integrations/gmail/fetch.py`) takes an explicit `brand_kit_id` and does `db.get(SyncState, (key, brand_kit_id))`.

**Gmail is per-brand**: each brand connects its own Gmail account (`oauth_credentials.brand_kit_id`, unique per `(provider, brand_kit_id)`) via its own fetch cursor/search query. `worker/scheduler.py`'s daily cron job loops over every brand that has a `provider="gmail"` credential row, one try/except per brand so one brand's quota/revoked-consent failure doesn't block the others.

**Connecting Gmail** has two paths that both end up calling the same `integrations/gmail/oauth.py::save_credentials`: the original local script (`python -m backend.scripts.gmail_authorize <brand name>`, needs a browser on the machine running it, so useful when you don't want the web app itself hitting Google) and, now, a real in-app OAuth flow - the "Connect Gmail" button on `/brand-kit` (`routes_gmail.py`, prefix `/brand-kit/gmail`: `GET /connect` builds a `google_auth_oauthlib.flow.Flow` from `GMAIL_CREDENTIALS_PATH` with `redirect_uri` set to this request's own `gmail_oauth_callback` URL, redirects to Google; `GET /callback` completes the token exchange and calls `save_credentials`; `POST /disconnect` removes the brand's row). PKCE's `code_verifier` and the CSRF `state` don't survive across the redirect on their own (a fresh `Flow` object is constructed per request) - both round-trip through `request.session` alongside which brand initiated the connect, set in `/connect` and consumed in `/callback`. Any user with access to the active brand can connect/reconnect/disconnect it (operational, not owner-gated, same as the reel/auto-mode settings).

**The OAuth client itself is still shared, not per-brand** - `GMAIL_CREDENTIALS_PATH` points at one Google Cloud OAuth client used for every brand's consent flow (only the resulting per-brand *token* differs). The existing client (used since Phase 1) is an "installed" (Desktop app) type, whose registered `redirect_uris` field Google actually ignores in favor of its own loopback-IP rule - any `http://localhost:<port>/<path>` redirect is accepted regardless of what's literally in the client JSON, confirmed live: the real Google consent screen came back correctly (no `redirect_uri_mismatch`) for `http://localhost:8000/brand-kit/gmail/callback`. This means local dev works out of the box with the existing client, but a real deployment on an actual domain (not `localhost`) will need a **"Web application"**-type OAuth client in Google Cloud Console with that domain's `/brand-kit/gmail/callback` registered as an authorized redirect URI - `build_authorization_flow` auto-detects "web" vs "installed" from whichever `GMAIL_CREDENTIALS_PATH` points at, so swapping the file is the only change needed. `docker-compose.yml` mounts `./secrets:/srv/secrets:ro` into the app container specifically so this in-app flow (unlike the old script, which always ran on the host) can read that file at runtime.

**Existing single-brand data migrated as "Brand 1"**, owned by the already-bootstrapped admin, via a 3-migration sequence (`da36ce3f1e10` schema → `6a6bb0e0ca70` backfill → `0d40be379bab` constraint-tightening) - split that way so a failure partway (e.g. `ADMIN_EMAIL` unset) leaves the DB nullable/rollback-able rather than half-migrated.

## Language

`backend/agents/languages.py`'s `LANGUAGE_CHOICES` is the fixed, validated set for generated content - English/Hindi/Marathi for now (same "bounded dropdown, not free text" reasoning as `graphic_designer/fonts.py`). Brand kit sets the default (`/brand-kit`); the analytical-review gate lets you override it per post when sending something to the Content Writer (`/board`'s "Needs Review" cards). Only the Content Writer's actual output (`copy_text`/`hashtags`/`poster_headline`/`reel_script`) is generated in the selected language - the Analytical agent's brief stays whatever language the model defaults to, since that's for your own reading, not the post. Verified live: both Hindi and Marathi produce natural, idiomatic copy, not literal translation.

## Hindi/Marathi pre-generation text review

AI-drafted Hindi/Marathi text quality isn't reliable enough yet (user-reported), so for these two
languages, Content Review (`Stage.DRAFTED`) exposes the exact final text that will be spoken/rendered
as directly editable, before Approve spends real image/video generation cost on it. English (or any
other future language) keeps the original behavior untouched for poster/reel - nothing here changes
their cost, latency, or UI. **Carousel is the one exception**: its slide-text edit form is available in
every language, not just hi/mr - carousel wording often benefits from a human tweak regardless of
language (a separate, later user request, not an AI-quality-specific problem like the other two
formats).

**The shot list is built earlier than usual.** Normally reel/carousel shot-listing (turning
`reel_script`/`carousel_script` into per-scene narration or per-slide headline/body_text) only happens
lazily, inside `generate_reel`/`generate_carousel`, at Approve time - there'd be nothing to edit at
Content Review otherwise. `orchestrator.py`'s `_prebuild_shotlist_for_review` runs right after drafting
(wired into `_content_writer_node`, `_analytical_node`'s combined-drafting branch,
`send_to_content_review`, and `routes_board.py`'s `retry_content_writer` - every place an item can land
at its content-review gate) and rebuilds `reel_scenes` (hi/mr only) / `carousel_slides` (every language)
immediately. Best-effort: a failure here is logged and swallowed, not propagated - the item still
reaches Content Review, just without a pre-built list to edit (same as before this existed; generation
still builds one lazily as a fallback).

**Editable fields, by format** (`routes_board.py`'s `edit-poster-content`/`edit-reel-scenes` routes are
gated on `content_item.language in ("hi", "mr")`; `edit-carousel-slides` has no language gate; all three
require `stage == Stage.DRAFTED`; board.html only renders each form under its matching condition):
- **Poster** (hi/mr only): `poster_headline` + every `poster_content` field for the item's current
  `poster_template` (a per-template form - `quote`'s `quote_text`/`attribution`/`citation`,
  `fact_critique`'s up-to-3 `facts` pairs, etc., mirroring `POSTER_TEMPLATE_GUIDE`'s shapes). This is a
  complete fix for posters specifically - nothing else transforms this text before it's handed to the
  image model.
- **Reel** (hi/mr only): only each scene's `narration` (the literal words Veo will speak) - a scene's
  visual `description` is an English generation prompt regardless of the post's language, not
  user-facing content text, so it stays read-only. `reel_script` itself also stays read-only context above the
  editable list.
- **Carousel** (every language): each slide's `headline`/`body_text` (the literal text the image model
  will render) - `carousel_script` stays read-only context above the editable list.

Saving an edit is a plain field update - no LLM call, no stage change, item stays at Content Review so
the user can keep iterating or click Approve when satisfied. Approve does **not** re-run shot-listing
(`_build_shotlist`/`_build_carousel_shotlist` both return the cached list unconditionally once
non-empty) - generation uses the edited text exactly as saved. "Regenerate" still means what it always
has app-wide: a full fresh AI redraft (re-running `_content_writer_node`/`_analytical_node`, which clears
and rebuilds the shot list from the new script), discarding any manual edits - editing and regenerating
are deliberately separate actions, not merged.

**Known scope boundary**: the "add a format to an already-approved text_only post"
bypass path (`generate_media`/`approve_and_generate_media`, see "Adding media to already-approved text
content" below) does not stop at any review gate, so this editing step doesn't apply there - it still
generates directly, same as every other language.

## Model routing

`backend/app/llm/provider.py`'s `ChatProvider` resolves a model per `agent_task` from the `agent_model_config` table (seeded with sane defaults by `backend/scripts/seed_agent_models.py`), then calls it via LiteLLM so provider (Anthropic/OpenAI/Google) is just a config string. Every call is logged to `llm_call_log` with token counts and cost. The Content Writer uses Claude's server-side `web_search` tool for live hashtag relevance (falls back to a plain call if the tool call itself fails).

**Model routing is now per-brand overridable, with a global fallback chain**: `agent_model_config` gained a nullable `brand_kit_id` - a NULL row is the global default (what `seed_agent_models.py` seeds), a non-NULL row overrides just that brand. `ChatProvider._resolve_model` tries the brand-specific row first (only when constructed with a `brand_kit_id`), then the global row, then the hardcoded `DEFAULT_MODELS` dict - no DB-level uniqueness on `(agent_task, brand_kit_id)` since Postgres treats every NULL as distinct anyway, so "at most one row" is enforced at the application level (query-then-upsert) via `llm/provider.py::set_brand_model_override`/`clear_brand_model_override`, same pattern as every other UI-editable setting in this app. Exposed on `/brand-kit`'s "Models" panel (`routes_brand_kit.py::update_model_overrides`) as one dropdown per `CONFIGURABLE_AGENT_TASKS` entry, choices drawn from a curated `MODEL_CHOICES` list (not free text - a typo'd `model_id` would silently break generation with no useful error until the next real content item hits it). Covers every litellm chat-completion step (Researcher triage, Analytical brief, combined drafting, both Content Writer variants, Graphic Designer's creative-direction prompt, Reel Editor's shot-listing) - **not** the actual image-generation model (Gemini, a separate SDK call, not litellm - see Image generation below) or the reel video tier (Veo, already brand-configurable via `reel_editor/graph.py`'s `get_video_model_key`, exposed on the board's settings panel instead, a separate mechanism since it interacts with the per-reel cost cap).

**API keys are per-user, resolved by brand ownership** (`llm/user_keys.py`): `ChatProvider(db, brand_kit_id)` and `get_image_provider(api_key)`/`get_video_provider(api_key)` (`llm/image_provider.py`/`video_provider.py`, both already took an explicit `api_key` at construction) resolve which key to actually use via `resolve_api_key(db, brand_kit_id, provider)` - the BRAND OWNER's own key (`UserApiKey`, encrypted with the same Fernet scheme as Gmail's `OAuthCredential`) if they've set one on `/account`, else the shared `.env`-configured key as a fallback. A member operating a shared brand always bills to the *owner's* key, never their own - keeps billing attached to whoever created the brand regardless of who's clicking buttons on it. Every `ChatProvider(db)` call site was threaded a `brand_kit_id` (from whatever `ContentItem`/`IngestedEmail`/brand it already had in scope); `scripts/check_providers.py` is the one exception (no brand context to give it), so it goes straight to the `.env` fallback via `ChatProvider(db)`'s default `brand_kit_id=None`.

**Citation-tag leakage from `web_search`** (found live, not by inspection): the Claude+web_search path occasionally returns a search-grounded sentence wrapped in leftover citation-tag syntax instead of plain prose, e.g. `(cite index="3-3,3-4">Protected lanes cut cyclist risk by 34%...</cite>` right inside `copy_text` - malformed (a stray `(` instead of `<` on the opening tag), root cause unconfirmed (Claude imitating an internal citation format vs. a content-block-join artifact), but the pattern was consistent enough to catch defensively. `json_utils.py`'s `sanitize_llm_json` recursively strips `strip_citation_artifacts` over every string in the parsed response (not just `copy_text` - `poster_content`'s nested fields, `reel_script`, anything) immediately after every `extract_json` call in `content_writer/graph.py`, keeping the real cited sentence and only removing the tag wrapper around it.

**Content Writer is multi-provider, routed by language** (user-reported: Claude's Marathi writing quality wasn't good enough). `content_writer/graph.py`'s `_agent_task_for_language` picks `"content_writer_localized"` (GPT-5.4, per `DEFAULT_MODELS`) for `language` in `{"hi", "mr"}`, and the normal `"content_writer"` (Claude Opus 5) for everything else - same `agent_task`-keyed DB-override mechanism as everywhere else, so either can be repointed at a different model without a code change. The `web_search` tool is Anthropic-specific (would error against an OpenAI model), so it's only ever attached on the Claude path; the localized path uses a variant system prompt (`SYSTEM_PROMPT_LOCALIZED`) that asks for hashtags from the model's own knowledge instead of referencing a tool call that isn't there. Verified live: Marathi output reads as natural, idiomatic copy (not stilted/machine-translated), confirmed by inspecting real generated text.

## No programmatic content text

**App-wide policy**: Pillow/deterministic code never draws poster or reel CONTENT text - headline, quote, stat, caption. For posters, that content is either generated by the AI model itself or not shown at all. The only things code is still allowed to stamp on, and only as independently-optional per-brand toggles (`BrandKit.show_logo_on_posters`/`show_brand_name_on_posters`/`show_source_attribution` - the last of these now posters-only), are: branding (name + logo) and source attribution. This reversed two earlier, deliberate designs (a Devanagari-specific Pillow headline fallback, Pillow-rendered reel "text_card" scenes) in favor of trusting the image model, with a strict "render this verbatim, don't alter it" instruction wherever it's asked to reproduce exact text, and the Media Review approval gate (regenerate if wrong) as the accepted safety net - including the known, accepted risk that Gemini's image model renders Devanagari unreliably.

**Reels are stricter still**: a third reversal - letting Veo render on-screen text itself - was tried and then reverted (see Video generation below) after a live test came back completely garbled with wrong framing. Reels now carry no on-screen text at all, in any language, and no closing card either (not even source attribution) - every fact/quote reaches the viewer through narration alone, or not at all.

## Image generation

`backend/app/llm/image_provider.py`'s `GeminiImageProvider` generates the poster's background AND headline together via the Gemini Developer API (`gemini-2.5-flash-image`, ~$0.039/image, plain `GOOGLE_API_KEY`, no Vertex AI needed). Before that call, `graphic_designer/graph.py`'s creative-direction step (Sonnet 5, `agent_task="graphic_designer_direction"`) writes a specific, story-grounded prompt from the article's actual content, explicitly instructing the image model to render the exact headline, verbatim and unaltered, as styled poster typography (different type treatment per story - condensed stencil, vintage travel-poster arc, etc.) - a fixed template wrapped around just the headline made every poster look the same regardless of subject, and a plain Pillow-drawn headline looked the same on every poster even once backgrounds varied.

This applies in every script, including Devanagari (Hindi/Marathi) - live testing found the image model renders Devanagari as garbled, wrong conjuncts/matras, but per the no-programmatic-content-text policy above, that's an accepted risk rather than a reason to fall back to a Pillow-drawn headline. The Media Review approval gate (where you see the finished poster before it goes anywhere) is the safety net - Regenerate if the text comes out wrong. If there's no image provider configured, or the AI call fails, `layout.py`'s `render_fallback_poster` produces background (a neutral gradient, since there's no successful AI image to show text from) + brand strip only - no headline text, ever. Fonts are a fixed, vendored set (`graphic_designer/fonts.py`, `.ttf` files under `app/static/fonts/`) selectable from a dropdown, still relevant for the brand strip's own text (name/handles) and the Devanagari-capable subset used there.

## Poster templates

`backend/agents/graphic_designer/templates.py` is the single source of truth for the poster template registry: six templates (`quote`, `tribute`, `narrative`, `fact_critique`, `trivia`, `event`), derived from analyzing a real set of 16 sample Marathi social/history posters the user provided. Each template names its own `poster_content` JSON shape (e.g. `quote` needs `quote_text`/`attribution`/`citation`; `fact_critique` needs a `facts` list of `{stat, source}` plus a `critique_line`). Multi-slide carousels (also present in the samples) are deliberately out of scope for now - these are all single-slide layouts.

The Content Writer infers the best-fitting `poster_template` and produces matching `poster_content` alongside the existing `poster_headline`/`copy_text` (see `content_writer/prompts.py`'s `POSTER_TEMPLATE_GUIDE`, interpolated into both the normal and format-only system prompts). The user can override the inferred template from the "drafted" card's modal (`POST /board/{id}/poster-template`) before generation - `poster_content` itself isn't regenerated, only which layout it's rendered into.

**Templated posters are freely designed by the image model, not Pillow**: `graphic_designer/graph.py`'s `generate_poster`, for any item with a known `poster_template` and non-empty `poster_content`, calls `_craft_full_design_prompt` (Sonnet 5, `agent_task="graphic_designer_direction"`, using `prompts.py`'s `SYSTEM_PROMPT_FULL_DESIGN` + `templates.py`'s `TEMPLATE_MOODS`) to write a prompt handing the image model every text field for that template plus a mood description, then generates the **complete** poster - background, layout, and all typography - via `ImageGenProvider.generate_full_design`. The prompt instructs the model to render each text field verbatim and unaltered. This applies in every language including Devanagari - same accepted-risk reasoning as above. There is no deterministic per-template Pillow fallback any more (the old `poster_render.py` was deleted): if there's no image provider, or the full-design call fails, the poster falls back to background + brand strip only, same as the legacy single-headline path.

Product-photo posters (WooCommerce source) skip the AI call entirely - the real product photo IS the product being sold, and no AI model can reproduce an exact print/design anyway - so `generate_poster` just composites the real photo + brand strip + source line, with no caption overlay at all. Stays free.

`backend/app/llm/image_provider.py` exposes two Gemini models: `IMAGE_MODEL_STANDARD` (`gemini-2.5-flash-image`, $0.039/image) for the legacy single-headline path and Reel Editor character references, and `IMAGE_MODEL_PRO` (`gemini-3-pro-image`, "Nano Banana Pro", ~$0.134/image at 1K/2K per ai.google.dev's pricing page, confirmed Sept 2026) for `generate_full_design` - Google's higher-fidelity, better-text-rendering tier, used specifically because the full-design path needs the model to get real words right, not just compose a scene.

## Carousels (Carousel Editor)

A third post format alongside poster/reel - a set of 4-6 still images (`MediaAsset.asset_type="carousel_slide"`, ordered by `slide_index`) telling one complete story, meant to be swiped through in order. Structured like Reel Editor (script -> shot list -> per-unit generation), not like the six poster templates - each slide is a uniform `{"headline": ..., "body_text": ...}` shape, not tied to `quote`/`tribute`/etc.

**Script and slide count**: Content Writer writes `carousel_script` (a narrative arc, same shape as `reel_script`) alongside the normal copy fields when `format="carousel"` (`content_writer/prompts.py`'s `CAROUSEL_GUIDE`). `carousel_editor/graph.py`'s `_build_carousel_shotlist` (Sonnet 5, `agent_task="carousel_shotlist"`) then breaks that script into an ordered list of slides, **LLM-decided within 4-6** based on how much the story actually needs (not a fixed count) - a defensive cap at 6 guards a misbehaving model, but the prompt's own instruction is the real enforcement.

**Visual consistency across slides comes from generation mechanics, not prompt wording alone**: `generate_carousel` first generates one shared background image (no text at all, cached as `asset_type="carousel_background"` so a retry after a partial failure reuses it rather than regenerating a visually different one), then produces each slide by editing a copy of that same background via `ImageGenProvider.generate_full_design(reference_image=<shared background>)` - the same reference-image-editing mechanism product-photo posters use to preserve a real photo, repurposed here to preserve a shared AI-generated background instead. Each slide's prompt only ever asks for that slide's own headline/body_text to be composed on top, never a new scene - one consistent art style/palette across the whole set is the actual lever, not just asking each slide to "look similar."

**Text placement is pinned to a fixed zone, not left to each slide's own judgment** (found live: early slides put text in different places - some in a clean top band, one spilling onto busy artwork with poor contrast - since each slide's creative-direction prompt is an independent LLM call with no shared layout constraint). `SYSTEM_PROMPT_BACKGROUND` now reserves one deliberate, clearly-bounded calm text zone in the top ~30-35% of the frame; `SYSTEM_PROMPT_SLIDE` instructs every slide to confine its headline/body text to that exact same zone and never repaint/re-crop the shared background. This is what actually produces aligned, template-like consistency across a set, not just a shared background alone.

**Sequence indicator, AI-drawn**: each slide also renders a small page-counter in the text zone's top-right corner - `"{slide_number}/{slide_count}"`, followed by a right-pointing arrow on every slide except the last (which shows just the number, no arrow) - so swiping reads as a connected sequence with a clear end. Drawn by the image model itself in the same call as the headline/body text (not Pillow-stamped) - a deliberate accepted-risk choice (matches the app's general "AI draws content text" policy) in exchange for a page-counter that's visually native to each slide's art style, rather than a bolted-on deterministic badge.

**Same no-programmatic-content-text policy as posters/reels**: each slide's headline/body_text (and the sequence indicator) is rendered by the image model itself, verbatim, or not shown at all - Pillow only ever stamps branding (`add_brand_strip`) and source attribution (`add_source_line`), applied identically to every slide.

**Error-handling policy** (mirrors `generate_poster`'s product-photo branch): the shared background generation failing propagates (nothing to fall back to - a carousel of blank branded cards would silently look finished but isn't); an individual slide's reference-image edit failing after the background succeeded is a soft failure, falling back to just that one slide being the shared background + brand strip rather than discarding the whole carousel over one bad slide.

**Sends**: Botsab has no multi-image/album API at all (confirmed reading its source) - `botsab/send.py` sends a carousel as a plain sequence of separate image messages, the full caption attached to the first slide only. Postiz's `image: [...]` wire format already accepted a list - `postiz/send.py` now uploads every slide into it for a real, native multi-image post, no schema change needed on Postiz's side. Both send paths group `MediaAsset` rows by "newest asset per `slide_index`" (a regenerate inserts a fresh full set of rows) rather than trusting insertion order.

## Video generation (Reel Editor)

`backend/app/llm/video_provider.py`'s `GeminiVideoProvider` generates each ~8s scene via Veo on the same Gemini Developer API key. Three tiers, chosen per generation via a board-level selector (`reel_editor/graph.py`'s `get_video_model_key`/`set_video_model_key`, `SyncState`-backed like the cost cap): `veo-3.1-lite-generate-preview` (lite, $0.08/sec 1080p), `veo-3.1-fast-generate-preview` (fast, **the default** as of Sept 2026 - $0.12/sec 1080p, bumped from lite after a user-reported quality complaint - pricing confirmed directly off ai.google.dev's official pricing page, not a live 400/200 test), and `veo-3.1-generate-preview` (standard, $0.40/sec, 5x fast). Three things about the Lite tier are confirmed via live 400 errors, not documentation - it rejects `reference_images` (Veo's subject-consistency feature), `negative_prompt`, and the whole SDK rejects `generate_audio` on the Gemini Developer API surface entirely (audio is generated unconditionally regardless - confirmed by a clip having an AAC track despite the code never requesting one). The Fast tier is treated the same as Lite for `reference_images`/`negative_prompt` purely out of caution (another preview-tier model, not separately confirmed) - the code only special-cases Standard as the one confirmed to accept `negative_prompt`. Consequences:
- **Character consistency** comes from (a) chaining a starting image through `generate_scene`'s `starting_image` param (the character reference image for scene 1, the previous clip's last frame - extracted via ffmpeg - for scene 2+, only across consecutive scenes) and (b) the shot-listing step repeating the character description verbatim in every scene's prompt.
- **On-screen text is suppressed, permanently** - a brief attempt at letting Veo render on-screen text itself (per the no-programmatic-content-text policy) was reverted after a live test on a real reel (a Tamil Nadu factory-deaths story): the on-screen Devanagari came back completely garbled (not just imperfect - nonsense glyphs), and the scene was composited inside a bordered "poster"-style box instead of full-bleed video. `DEFAULT_NEGATIVE_PROMPT` (`video_provider.py`) again carries `"subtitles, captions, on-screen text, written words, watermark, garbled text"`, the per-scene "(no on-screen text...)" prompt suffix is back, and `reel_editor/prompts.py`'s shot-listing prompt never asks for on-screen text at all - every fact/quote/stat is carried through narration only, in every language.
- **`RESOURCE_EXHAUSTED` (429) errors don't necessarily mean depleted billing** - user-confirmed live that account credits were available when this fired; `veo-3.1-lite-generate-preview` being a *preview* model most likely carries its own per-day/per-minute request cap independent of credit balance. `generate_reel` catches this and points at https://ai.dev/rate-limit rather than assuming billing.
- **A prompt Google's Responsible AI (RAI) safety filter rejects comes back as a "successful" operation** (`operation.error` empty) but with `response.generated_videos` left `None` - this previously crashed as a bare `'NoneType' object is not subscriptable` with zero indication of why, found live on a real caste-discrimination story (exactly the kind of real-world social-conflict subject matter this app's niche content leans on, and safety filters are more likely to flag). `generate_scene` now checks for this explicitly and surfaces `response.rai_media_filtered_reasons` (a real field on the SDK's `GenerateVideosResponse` type) so a rejected prompt is diagnosable instead of looking like an app bug.

**Reel templates** (`reel_editor/templates.py`): three types - `explainer_influencer` (consistent on-camera presenter), `faceless` (voiceover over B-roll, no character), `animated_contextual` (illustrated/motion-graphic style) - confirmed via research as a real, recognized short-form-video framework, not invented. Content Writer infers `reel_template` the same way it infers `poster_template` (user-overridable on the board). Every scene is a Veo-generated clip - there's no Pillow-rendered scene type at all (the old `text_card` scene type and `reel_editor/text_card.py` were removed entirely, including its later-added closing source-attribution card - reels carry zero programmatic content, not even source credit). Where the article has a real, quotable line or striking statistic, the shot-lister folds it into narration only - never on-screen text (see above).

WooCommerce-sourced (product) reels go through this same pipeline, not a shortcut - the real product photo (if any) is passed in as the first scene's `starting_image`, the same continuity mechanism used for the character reference, grounding Veo's generation in the real product visually without hard-requiring it (Veo may still not reproduce the exact print - known, accepted trade-off, same reasoning as product posters above).

**Voiceover via Veo's native audio, every language** (`reel_editor/prompts.py`): the shot-listing step writes a short narration line per scene, composed into the final Veo prompt as `A narrator says: "..."`. Confirmed live at no extra cost (Veo's per-second price already includes audio). hi/mr reels get narration written in Devanagari script (`SYSTEM_PROMPT_LOCALIZED`) and spoken by Veo directly - UNVERIFIED quality end-to-end, accepted per explicit user request rather than falling back to silent scenes or Pillow-rendered cards. The actual spoken-audio quality of the English path itself is also not yet verified end-to-end - live testing kept hitting `RESOURCE_EXHAUSTED` (429) on Veo calls. **Not a depleted-credits issue** (user confirmed live that account credits were available) - `veo-3.1-lite-generate-preview` is a preview model, and this is most likely its own per-day/per-minute request cap rather than billing. `generate_reel` (`reel_editor/graph.py`) now catches this specifically and surfaces a clear message pointing at https://ai.dev/rate-limit instead of Google's raw JSON error, and treats it like the cost cap - stops generating further scenes but keeps whatever already rendered rather than discarding a partial reel. The request format itself is confirmed correct (no API validation errors on a real call), just not the audio output quality.

`backend/agents/reel_editor/graph.py`'s `generate_reel` orchestrates: shot-listing (Sonnet 5, `agent_task="reel_shotlist"`, script + reel_template + real article text -> 2-8 scenes) -> character reference image (Gemini image gen, skipped for faceless posts with no character) -> per-scene Veo generation, **checking the configured cost cap before each scene and stopping generation (not failing) once the next one would exceed it** -> `app/ffmpeg/stitch.py` stitches clips with loudness-normalized audio and a short (0.4s) crossfade between consecutive scenes (`xfade`+`acrossfade`), falling back to a plain hard-cut concat (stream-copy, or a re-encode if codecs don't match) if the crossfade filter_complex build fails. Cap defaults to $8.00/reel (enough for a full 8-scene reel at the fast or lite tier), editable on the board. No closing card of any kind - `BrandKit.show_source_attribution` only affects posters now.

## Per-card cost tracking

`routes_board.py`'s `_cost_by_item_id` sums `llm_call_log.cost_usd` + `media_assets.cost_usd` per `content_item_id` (every LLM call plus every generated poster/reel/character-reference), shown as a small `$X.XX` badge in each card's icon row - the running total of everything spent on that specific post, not just its most visible generation step.

## Fonts

`backend/agents/graphic_designer/fonts.py`'s `FONT_CHOICES` is one flat, unified list covering both scripts, not two separate Latin/Devanagari pickers - Rajdhani, Khand, Teko, Mukta, Poppins, and the rest added on request all ship real glyph coverage for *both* Latin and Devanagari in the same file (verified two ways per font: cmap + virama presence, and actually rendering a real Marathi sentence and reading the result - the same standard as the original Devanagari bug fix, since cmap presence alone doesn't guarantee correct conjunct shaping). `load_font(font_key, ...)` now uses the brand's own chosen font for Devanagari text too, via `DEVANAGARI_CAPABLE`, only falling back to `DEFAULT_DEVANAGARI_FONT_KEY` (Noto Sans Devanagari) for the original four fonts that have no Devanagari glyphs at all. `FONT_GROUPS` drives the brand-kit dropdown's `<optgroup>`s (General / Devanagari - body / Devanagari - display).

Two requested fonts are deliberately **not** vendored: **Mangal** is a proprietary Microsoft font with no legal open-redistribution path, and **Samyak Devanagari** isn't published on Google Fonts' repo or anywhere else with a clearly verifiable open license - vendoring either would be illegal or unverifiable, so they were skipped rather than guessed at.

## Brand strip icons

`add_brand_strip` (in `graphic_designer/layout.py`) draws real platform icon glyphs next to each handle - Instagram, Facebook, X, YouTube, TikTok, and a globe for the website - instead of spelling out platform names. Icons come from Font Awesome 6 Free (`app/static/fonts/fa-brands-400.ttf`, `fa-solid-900.ttf`; icons CC BY 4.0, font SIL OFL). Codepoints were confirmed by inspecting the fonts' cmap with `fontTools` and rendering each one, not guessed from documentation.

**Logo**: `brand_kit.logo_asset_path` (uploaded via `/brand-kit/logo`, stored through the same `StorageBackend` as generated media) is composited into the strip's left edge by `add_brand_strip` - alpha-masked, resized to fit the strip height, with the text-segment centering logic shrinking its available width to leave room for it rather than overlapping. `graphic_designer/graph.py`'s `generate_poster` loads the logo bytes before calling either strip-drawing path (`add_brand_strip` directly, or via `render_fallback_poster`) and passes them through as `logo_bytes`; a brand with no logo renders exactly as before (the param defaults to `None` everywhere in the chain). Logo and brand name are independently toggleable (`show_logo_on_posters`/`show_brand_name_on_posters`) and no longer mutually exclusive - both can render together. Reels have no brand-strip mechanism and no closing card of any kind - nothing programmatic is ever composited onto a reel.

## Adding media to already-approved text content

`routes_board.py`'s `generate_media` (`POST /board/{id}/generate-media`) lets you add a poster or reel to a post that was already approved as text_only (or already has one format and you want another) - the "Generate poster"/"Generate reel" buttons on Approved cards. It bypasses the LangGraph graph entirely (same reasoning as `retry_reel`: that item's run already reached `END`, so there's no interrupt to resume into) and calls `content_writer/graph.py`'s `write_format_fields` - a prompt variant (`SYSTEM_PROMPT_FORMAT_ONLY`) that derives only the new format's field(s) (`poster_headline`, or `reel_script`+`character_description`) from the article/brief, explicitly told not to touch the existing `copy_text`/`hashtags`. Verified live: the caption came back byte-for-byte identical after generating a reel for a text_only post. Always runs in the background (even posters), same `is_processing`/`last_error` handling as the rest of the media-generation paths.

## WhatsApp (Botsab)

`backend/app/integrations/botsab/` sends approved posts to a configured WhatsApp number/group via [Botsab](https://github.com/) (self-hosted, local repo `../Botsab` on this machine), a thin wrapper over WhatsApp's Baileys library with a REST API in front. `client.py`'s `BotsabClient` is a direct HTTP client (`x-api-key` header auth, one `instance_id` = one connected WhatsApp session) - its endpoint/schema details were confirmed by reading Botsab's own backend source (`routes/messages.ts`, `routes/media.ts`), not just its `/api-docs` page, since the docs page doesn't list every message type (video is missing there but present in the actual Zod schema).

`send.py`'s `send_content_item(db, content_item)` (used by `routes_board.py`'s `POST /board/{id}/send-whatsapp`, a button on Approved cards) auto-picks the send shape from whatever the item actually has: text-only if there's no media asset, otherwise the latest poster or reel. **Posters and reels both send the same way** - uploaded directly to Botsab via `POST /media/upload` (multipart, this app's own outbound HTTP call) which returns a `fileId`, then sent by `fileId`. This works regardless of whether this app itself is reachable from the internet, since Botsab is always the one being called, not the one calling back - no tunnel/public exposure needed for this app at all.

Botsab's API originally only supported `fileId` for images - video was `url`-only (Botsab's server would have had to fetch the file *from* this app, requiring this app to be publicly reachable, which it isn't). Since Botsab is our own self-hosted app (local repo `../Botsab`, deployed to `botsab.shackyapps.in` via `./deploy.sh`), that limitation was removed directly at the source instead of worked around: `backend/src/routes/media.ts`'s upload endpoint now accepts video (raised the cap from 16MB to 100MB - a stitched multi-scene reel commonly lands in the 10-50MB range), and `routes/messages.ts`'s video message type now accepts `fileId` exactly like image does. Verified live post-deploy with a real (harmless, never sent) test upload against the production server, not just locally.

Recipient (`whatsapp_recipient`, a JID - `"{phone}@s.whatsapp.net"` for a DM or `"{numeric_id}@g.us"` for a group) lives on `BrandKit`, editable on `/brand-kit` - same "one destination, one settings row" pattern as everything else there.

**The Botsab connection itself is per-brand too** (`brand_kit.botsab_instance_id`/`botsab_api_key_encrypted`, encrypted the same way as everything else brand-scoped) - different brands can send from different WhatsApp numbers/instances. `send.py`'s `get_botsab_client(brand_kit)` falls back to the shared `BOTSAB_API_KEY`/`BOTSAB_INSTANCE_ID` env vars if the brand hasn't set its own. `BOTSAB_BASE_URL` stays a single shared `.env` setting either way (one Botsab deployment endpoint serves every brand's instances).

## Avoiding redundant email processing

Two independent layers, at two different points in the pipeline:

- **Never re-fetch an already-seen Gmail message** (`integrations/gmail/fetch.py`): `fetch_new_emails` only queries Gmail for messages `after:{last_fetch_epoch}` (a `SyncState` cursor advanced on every successful fetch), and additionally skips any `gmail_message_id` already present in `ingested_emails` (which also has a DB-level unique constraint on that column) - belt-and-suspenders against the same message ever getting inserted twice.
- **Never process the same fetched-but-not-yet-processed email twice** (`orchestrator.py`'s `process_email`): found live as a real gap, not hypothetical - `IngestedEmail.status` used to only flip `"new"` -> `"processed"` at the very end, after `extract_articles` (an LLM call) and every `ContentItem` insert had already happened. Two overlapping calls for the same email - the 06:00 scheduled fetch racing a manual "Fetch now" click, or clicking "Fetch now" again before the first click's backgrounded run had gotten far enough to flip the status - would both see `status="new"` and both run the Researcher, creating **duplicate `ContentItem`s and duplicate LLM cost** for the same newsletter. Fixed with an atomic claim: `process_email` now does `UPDATE ingested_emails SET status='processing' WHERE id=... AND status='new'` *before* any work, checks the row was actually claimed (`rowcount`), and bails out immediately (returns `[]`, logs it) if another run got there first - only the winner proceeds to `extract_articles`.
  - **Restart risk, same shape as the reel `is_processing` bug below**: an email stuck at `"processing"` (process killed mid-run - crash, or a `--reload` restart in dev) would never be picked up again, since `fetch_pending_email_ids`'s backlog sweep only queries `status="new"`. `main.py`'s `recover_orphaned_email_claims`, run on every boot alongside `recover_orphaned_processing_items`, resets any `"processing"` email back to `"new"` - safe unconditionally, since nothing survives a process restart, so there's no chance one is actually still running.

## RSS feeds (fourth content source)

Alongside Gmail, GitHub, and WooCommerce - `BrandKit.rss_feeds` is a manually-entered list (no discovery API exists for arbitrary feed URLs the way GitHub/WooCommerce have one). Ingested the same way as Gmail (continuous stream, not an on-demand picker like GitHub/Website), since a feed is fundamentally a stream of new items over time:

- `integrations/rss/client.py`'s `fetch_feed` wraps `feedparser` (handles RSS 2.0 and Atom transparently) into a plain list of `{entry_id, title, link, summary, published_at}` dicts - `entry_id` is the feed's own `<guid>`/`<id>`, falling back to the link for feeds that omit one.
- `integrations/rss/fetch.py`'s `fetch_new_items` dedups against `ingested_rss_items` per `(entry_id, brand_kit_id)` (same per-brand-not-global uniqueness reasoning as `ingested_emails` - `UniqueConstraint` there too), one feed failing (dead URL, malformed XML) doesn't block the others.
- Unlike Gmail's per-email extraction (one email can hold many embedded articles, so each needs its own LLM call to find them), RSS entries are already discrete - `researcher/rss_angles.py`'s `extract_angles` scores a whole batch of newly-fetched entries against the niche in **one** LLM call, cheaper than one call per entry. `orchestrator.py`'s `process_rss_batch` mirrors `process_email`'s atomic claim-before-work race guard (`status: new -> processing`) and hands the scored batch to the same `create_content_items` every other source uses.
- Same orphaned-claim recovery as email (`main.py`'s `recover_orphaned_rss_claims`, run on every boot) and same daily-schedule pattern (`worker/scheduler.py`'s `_daily_rss_fetch_job`, 06:15 - just after Gmail's 06:00), plus a manual "Fetch RSS" button on the board (only shown once a brand has at least one feed configured).

## Format switching and the LangGraph checkpoint

`routes_board.py`'s `generate_media`/`approve_and_generate_media` routes (the "Generate poster instead" / "Generate reel instead" buttons on a Media Review card) change `content_item.format` directly in Postgres, bypassing the graph entirely - this item already reached its `media_review` interrupt or `END`, so there's no pending interrupt to resume into. But the graph's *own* checkpointed state carries its own copy of `format`, last set whenever `content_review_gate` ran, and `_route_after_media_review` (what a **Regenerate** click actually goes through) reads that checkpointed value, not the DB column. Found live: switching a reel to a poster, then clicking Regenerate, silently regenerated *more reels* - burning real Veo cost each time - because the checkpoint never learned about the switch, even though `content_item.format` in the DB correctly said "poster" the whole time. Fixed with `orchestrator.py`'s `sync_format_to_graph_state(content_item_id, format)`, called right after every out-of-graph format change, which does `graph.update_state(config, {"format": format})` to keep the checkpoint in sync - best-effort (logs, doesn't raise, since the DB-side change already succeeded regardless).

A related display bug shared the same root confusion: the board's `media_by_item_id` lookup used to pick whichever `MediaAsset` (poster or reel) was simply *newest* for a content item, not the one matching its *current* format - so a stray leftover asset of the wrong type (from exactly the bug above, or any future format switch) could shadow the real one in the Media Review modal just by having a later timestamp. Fixed by filtering to `asset.asset_type == content_item.format` before picking the newest match.

## Backgrounded board actions

Every board action that runs an LLM/image/video/network call is backgrounded (`app/worker/background.py`'s thread pool) rather than blocking the HTTP request - `routes_board.py`'s `decide()` now backgrounds any resume that `_decide_enters_slow_node` determines will run `content_writer`/`graphic_designer`/`reel_editor` before the graph's next interrupt (previously only reel did), and `send_whatsapp` backgrounds the Botsab HTTP calls (a live `httpx.WriteTimeout` on a slow upload was blocking the request for the full client timeout, badly enough that it read as a server crash). `_start_processing`/`_run_with_processing_state` are the shared helpers every one of these routes (retry-content-writer, retry-poster, retry-reel, generate-media, approve-and-generate-media, send-whatsapp, decide) uses for the is_processing/last_error bookkeeping, so a card always ends up in one of three states: processing (spinner shown; `board.html` polls quietly in the background and surfaces a dismissible "Updated" banner rather than auto-reloading, which used to yank the page back to the top mid-scroll), done, or errored-with-a-working-Retry-button.

**A background thread cannot survive a process restart** - found live: a reel's per-scene generation loop was mid-run when an app restart happened (a code deploy, a crash, or in dev, any `--reload` reload), silently killing that thread and leaving `is_processing=True` forever with `last_error` never set, since the cleanup code that normally clears both never got to run - the card looked like it was still working indefinitely with no way to tell it was actually dead. `main.py`'s `recover_orphaned_processing_items`, run on every boot (`lifespan`, alongside `bootstrap_admin_user`), sweeps every `ContentItem` still marked `is_processing=True` from before this boot - there's no ambiguity about whether one might really still be running, since nothing survives a fresh process start - and marks each with a clear "interrupted by an app restart, click Retry" `last_error` instead of leaving it silently stuck.

**Retrying a failed slow step bypasses the graph, like retry_reel always has** - once a LangGraph node raises mid-flight, its checkpoint isn't cleanly resumable via `Command(resume=...)` anymore (confirmed empirically, not just inferred), so `retry-content-writer`/`retry-poster`/`retry-reel` each call the underlying function (`write_copy`/`generate_poster`/`generate_reel`) directly and set `stage` themselves. `board.html`'s generic error block picks the right one from `content_item.stage`/`format` alone (no separate tracking field needed): `stage=analyzed` failed inside content_writer, `stage=drafted`/`media_generated` failed inside graphic_designer/reel_editor depending on `format`.

## Bulk actions and discarded-data retention

**Discard all** (`routes_board.py`'s `discard_all`) mirrors `approve_all` exactly - same column scope
(wherever "Approve all" appears: `analyzed`/`drafted`/`media_generated`), same per-item dispatch via
`resume_content_item(decision="discard")` so each item's pending graph interrupt is resolved properly
(not a direct `stage=DISCARDED` write, which only applies to `researched`/`approved` cards with no
pending interrupt - see `discard`'s own docstring). Always synchronous - discard never enters a slow
node (`_decide_enters_slow_node`), so unlike `approve_all` there's no background-dispatch branch to
mirror.

**Discarded items are deleted for good after a configurable retention window** (default 2 days,
per-brand, `SyncState`-backed like the reel cost cap - `worker/cleanup.py`'s
`get_retention_days`/`set_retention_days`, editable on the board's Settings panel, 0 opts a brand out
entirely). A daily cron job (`worker/scheduler.py`'s `_daily_discarded_cleanup_job`, 06:30, alongside the
Gmail/RSS fetch jobs) sweeps every brand's `DISCARDED` items against its own window
(`content_item.updated_at`, auto-refreshed on the stage transition into `DISCARDED`, stands in for "when
it was discarded" - good enough without a dedicated timestamp column) and permanently deletes each one:
its stored media files, then every table with a `content_item_id` FK (`MediaAsset`, `LlmCallLog`,
`ContentItemVersion`, `PostizPost`), then the `ContentItem` row itself.

**No ORM `relationship()` exists anywhere in this schema** (every table here uses a plain FK column, not
a mapped relationship) - found live: deleting a child row and its parent `ContentItem` in the same
`session.flush()` via individual `db.delete()` calls hit a real `ForeignKeyViolation` on every single
discarded item that had a `MediaAsset`, because SQLAlchemy has no relationship-based dependency info to
auto-order cross-table deletes within one flush without it, and emitted `DELETE FROM content_items`
before `DELETE FROM media_assets`. `worker/cleanup.py::_delete_content_item` sidesteps this entirely by
using bulk `.filter(...).delete()` calls for every child table (each executes immediately, not deferred
to a later flush) before deleting the parent row - confirmed no such gap remains by testing the exact
FK-violation scenario directly (a discarded item with a real `MediaAsset` row) after the fix.

## Auto-schedule concurrency guard

`routes_board.py`'s `auto_schedule`/`reschedule_all` each run as one sequential background job, paced
at `_AUTO_SCHEDULE_ITEM_DELAY_SECONDS` (240s) per item to stay gentle on Postiz's rate limit - a batch
of 20+ items can legitimately take well over an hour to finish. **Found live on a real brand
(Janata Weekly)**: nothing stopped a second (or third) click of "Auto-schedule" while an earlier click's
job was still slowly working through its list - each click's own redirect message ("Scheduling N
post(s) in the background") doesn't make clear the job could take an hour+, so a second click reads as
a reasonable retry. Each job computes its own snapshot of "which Approved items still need
scheduling" at start time, so a second job started before the first had caught up genuinely
double-scheduled the same items - two items ended up with duplicate `PostizPost` rows (one even with 4
rows - two full channel-pairs at two different times), which would have posted to Instagram/Facebook
twice each once those scheduled times arrived. Cleaned up after the fact by cancelling the redundant
rows via `PostizClient.delete_post` (same call the individual per-post Cancel button uses) - confirmed
every scheduled item ended up with exactly one row per configured channel.

**Fixed with a per-brand lock**: a `SyncState` row (`schedule_job_lock`, brand-scoped) is set before
either background job starts and cleared in a `finally` once it finishes. `_schedule_job_already_running`
gates both routes - a second attempt while the lock is fresh gets a clear "already running, wait for it
to finish" redirect instead of silently starting a duplicate job. The lock has a 2-hour staleness window
(`_SCHEDULE_LOCK_STALE_SECONDS`) for the case where the *process* dies mid-job (a crash, or a
`--reload` restart) - background threads don't survive that, so a lock left behind is guaranteed
orphaned, not genuinely still running; `main.py`'s `recover_orphaned_schedule_locks` also sweeps every
lock unconditionally on every boot, same "nothing survives a restart" reasoning as
`recover_orphaned_processing_items`, so a fresh boot doesn't even need to wait out the staleness window.

## Auto mode

`agents/auto_mode.py` + `orchestrator.py`'s `auto_advance_content_item` - for items whose `priority_score` clears a configurable threshold, auto-resumes the `analytical_review` gate (`send_to_content_writer`, using the brand's `default_language`), once that lands at `content_review` auto-resumes that too (`approve`), and - per an explicit user decision to extend past generation-only - once that lands at `media_review` auto-resumes that too (`approve`), landing at APPROVED with zero clicks. If the separate `auto_send` toggle is also on, landing at APPROVED this way additionally triggers an actual WhatsApp send (`integrations/botsab/send.py`'s `send_content_item`, the same call the manual "Send via WhatsApp" button uses) - a failure there is caught and written to `last_error` rather than raised, so a bad send doesn't look like a silent no-op. `auto_send` defaults off and is independent of `enabled`, specifically so flipping on auto-approve for an existing setup doesn't also start blasting WhatsApp messages without a separate explicit opt-in - **this means nothing is reviewed by a human before it goes out** once both are on, a deliberate change from the original media-review-is-always-manual design (see git history / conversation record for that tradeoff being made knowingly). Settings (`enabled`, `min_score`, `format`, `auto_send`) live in the same `SyncState` key/value table as the reel cost cap and Gmail search query. Called from two places: `process_email`'s per-item loop (so newly-fetched items auto-advance immediately) and `routes_board.py`'s `/board/auto-mode` route, which - only on the enabled:false→true transition - sweeps every item currently sitting in Needs Review/Content Review/**Media Review** in the background (including auto-approving, and auto-sending if that's on, an already-generated poster/reel nobody has looked at yet), so turning it on applies to the existing backlog too, not just future fetches. Verified live end-to-end with an isolated test item (real graph run, real auto-resumes, correctly respected the account's actual niche/brand-language config) rather than just reviewed.

## Storage

`backend/app/storage/` defines a `StorageBackend` protocol (`save`/`url_for`/`load`/`delete`) with two
implementations, selected by `STORAGE_BACKEND` (`local_disk.py`'s `get_storage_backend()` factory - every
call site imports from there regardless of which backend is active, so this is the one place that needs
to know): `local_disk` (default - writes under `LOCAL_STORAGE_DIR`, served via the auth-gated
`/media/<filename>` route in `main.py`, not a public static mount, since generated posters are the
user's own content) and `r2` (Cloudflare R2, S3-compatible - `r2.py`'s `R2Storage`, plain `boto3` S3
client pointed at R2's endpoint).

**R2's bucket stays private** - `url_for()` hands out a short-lived presigned GET URL
(`R2_PRESIGNED_URL_EXPIRY_SECONDS`, default 1 hour) rather than a permanent public link, preserving the
same "must be authenticated to get a working link" property `local_disk`'s auth-gated `/media` route
already has (a public bucket + permanent URLs was the simpler, more commonly-documented R2 setup, but
was explicitly rejected - see git history). Presigned-URL generation is pure local signing (no network
round-trip to R2), so switching backends adds no real latency to a board render.

**Existing local media isn't automatically migrated** when switching `STORAGE_BACKEND` to `r2` -
`MediaAsset.storage_uri`/`BrandKit.logo_asset_path` values are just filenames, and `LocalDiskStorage`
vs. `R2Storage` each resolve them against their own store, so anything saved before the switch would
404 under the new backend until migrated. `backend/scripts/migrate_media_to_r2.py`
(`python -m backend.scripts.migrate_media_to_r2`) uploads every existing file to R2 under its own
unchanged `storage_uri` as the object key - no DB rows need updating, since the same key format works
with both backends. Idempotent (safe to re-run) and leaves local files in place rather than deleting
them, so `local_disk` stays a working fallback. Live-verified on this deployment's real data: 119/142
`MediaAsset` rows + 1 brand logo uploaded (23 skipped - local files already missing before the
migration ran, not caused by it), then confirmed a real migrated asset's presigned URL actually
resolves over HTTP after flipping `STORAGE_BACKEND=r2`.

**Falls back to `local_disk` if R2 credentials are incomplete** (`get_storage_backend()` checks
`R2_ACCOUNT_ID`/`R2_ACCESS_KEY_ID`/`R2_BUCKET` are all set before constructing `R2Storage`, logging a
warning and returning `LocalDiskStorage` otherwise) - so a dev/test environment that copied
`STORAGE_BACKEND=r2` from another `.env` without real R2 credentials of its own doesn't hard-fail on
first save/load; it degrades to the same local-disk behavior as if `STORAGE_BACKEND` were unset.
`.env.example` still defaults to `local_disk` with all R2 fields blank, so a fresh clone needs no R2
setup at all to run.

**Note on `docker compose restart` vs `up -d`**: found live while wiring this up - `restart` reuses a
container's already-baked environment from whenever it was originally created/started; it does NOT
re-read `.env`/`env_file` changes. `docker compose up -d <service>` is what actually recreates the
container against the current compose config when `.env` changes - needed every time a `.env` edit
should take effect, not just a code change (which `--reload` already handles on its own).

## UI

`backend/app/static/css/design-system.css` is the one shared token/component set (colors incl. dark mode via `prefers-color-scheme`, spacing, buttons, forms, cards, badges) - every template (`base.html` and everything extending it, plus the standalone `login.html`) links it rather than styling itself independently. `base.html`'s nav highlights the active page via each route passing `active_nav` in its template context. Board-specific layout/card/modal CSS lives inline in `board.html` but reads from the same CSS custom properties, not hardcoded colors - keep it that way when touching board styles.

## Auth

`backend/app/auth/` defines an `AuthBackend` protocol. `BasicAuthBackend` (bcrypt + session cookie) is the only implementation today; a future `GoogleOAuthBackend` slots in behind the same interface, switched via `AUTH_BACKEND` in `.env` - no route changes required. `auth/brand_deps.py` layers brand-scoping on top (see Multi-brand support above) - `get_active_brand`/`get_accessible_brands`/`get_current_admin_user`/`get_owned_brand`.

## Getting started (local dev)

```bash
cp .env.example .env
# fill in APP_SECRET_KEY, APP_ENCRYPTION_KEY (see comment in .env.example),
# and ADMIN_EMAIL/ADMIN_PASSWORD. LLM provider keys are now optional here -
# see below - .env only needs to hold what's required to actually boot the
# app (DB connection, session/encryption secrets, which storage backend
# exists in this deployment, the bootstrap admin).

docker compose up --build
# runs alembic migrations, then uvicorn with --reload, on http://localhost:8000
```

Log in at `/login` with `ADMIN_EMAIL`/`ADMIN_PASSWORD` (that user is bootstrapped on first startup, `is_admin=True`). You'll land on `/brands/new` the first time (zero brands yet) - create one. Configure its niche and identity at `/brand-kit`, LLM provider keys for anything you own at `/account` (optional - falls back to `.env` if unset), other user accounts at `/admin/users` (admin-only - no self-serve signup). `/board` is the main Kanban view, scoped to whichever brand is active (switcher top-right once you have more than one) - one card per article/post-in-making, click a card for the full detail + actions modal.

To verify LLM provider credentials independently of the web app:

```bash
docker compose exec app python -m backend.scripts.check_providers
```

### Connecting Gmail

```bash
cp your-downloaded-client.json secrets/gmail_credentials.json
```

Then either click **Connect Gmail** on that brand's `/brand-kit` page (real in-app OAuth flow - see Multi-brand support above; works with the docker-compose setup as-is for local dev at `http://localhost:8000`), or, for a one-off/headless setup, run the original local script (needs a browser on the machine running it, so on the host, not in Docker):

```bash
docker compose up -d db   # needs Postgres reachable at DATABASE_URL
source .venv/bin/activate
python -m backend.scripts.gmail_authorize [brand name]
# brand name is optional if you only have one brand
```

Either way stores an encrypted refresh token in Postgres, scoped to that brand; the dockerized app and the daily scheduled job both read from there afterward (each brand with its own connection). Trigger a fetch manually from `/board` ("Fetch now", scoped to whichever brand is active) instead of waiting for the 06:00 daily job (which now loops over every brand that has Gmail connected). By default it fetches all mail (not just Primary - newsletters usually live in Promotions/Updates); narrow it with the Gmail search box on the board if needed (e.g. `category:promotions OR category:updates`, `label:Newsletters`).

To sanity-check the Researcher/Analytical pipeline without Gmail connected yet (needs `ANTHROPIC_API_KEY`):

```bash
docker compose exec app python -m backend.scripts.seed_test_content_item
```

## Migrations

```bash
docker compose exec app alembic -c backend/app/db/migrations/alembic.ini revision --autogenerate -m "..."
docker compose exec app alembic -c backend/app/db/migrations/alembic.ini upgrade head
```

Note: `backend/app/db/migrations/env.py` excludes LangGraph's own `checkpoint*` tables from autogenerate diffing (it manages that schema itself via `checkpointer.setup()`) - don't remove that filter or autogenerate will propose dropping them.

## Phase status

- **Phase 0**: scaffold, auth, niche/brand-kit config, LLM provider adapter, orchestrator skeleton.
- **Phase 1**: Gmail OAuth + fetch (configurable search query), Researcher agent (extracts every article from a newsletter, not just one per email, and scores each independently), Analytical agent (fetches the article's own full text via free extraction, then writes a brief), the niche-evolution suggestion flow.
- **Phase 2**: Content Writer agent (copy + live hashtags via web search + poster headline, Claude Opus 5), Graphic Designer agent (Pillow poster layout, real AI backgrounds via Gemini's `gemini-2.5-flash-image`, brand strip with name/social handles/website, fixed font dropdown), the `/board` Kanban UI (one card per article, click-through modal with all actions).
- **Phase 3**: Reel Editor agent (script/character description from Content Writer, shot-listing, Veo scene generation with cost cap, ffmpeg stitching), background execution for the whole reel-generation path (`app/worker/background.py`), `is_processing`/`last_error` states on the board with retry.
- **Multi-brand** (this state, cross-cutting rather than a numbered phase): `BrandKit` went from a single implicit row to a real multi-tenant entity - per-brand niche/Gmail/settings scoping, `BrandMember`-based ownership + sharing, admin-created user accounts, a brand switcher, per-brand logo compositing, an in-app Gmail-connect OAuth flow, and per-user/per-brand LLM/Botsab credentials replacing what used to be `.env`-only global config (see Multi-brand support above). Brand deletion is the one known gap left for a follow-up pass.
- **Carousel format** (cross-cutting, third post format alongside poster/reel): Carousel Editor agent (script -> LLM-decided 4-6 slide shot list -> shared background -> per-slide reference-image editing for visual consistency), full LangGraph/board/send-flow wiring (see Carousels above). Not yet verified live end-to-end with a real image provider.

Brand kit (`/brand-kit`) is identity/handles for the poster's bottom strip (and, once built, a reel end-card) - not a color scheme; the AI-generated background (or neutral gradient fallback) carries the visual look instead.

- **Postiz publishing**: manual and bulk-scheduled sends to any number of Postiz-connected channels per brand, mirroring the WhatsApp/Botsab integration's brand-override-with-shared-fallback pattern (see WhatsApp section above) - see git history for the full design.
