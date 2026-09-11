from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from backend.agents.auto_mode import DEFAULT_FORMAT, VALID_FORMATS
from backend.agents.graphic_designer.fonts import FONT_CHOICES, FONT_GROUPS
from backend.agents.languages import DEFAULT_LANGUAGE, LANGUAGE_CHOICES
from backend.agents.niche import get_active_niche_config
from backend.agents.prompt_registry import PROMPT_SLOTS, missing_guardrail_phrases
from backend.app.auth.brand_deps import get_accessible_brands, get_active_brand
from backend.app.auth.deps import get_current_user
from backend.app.db.models import BrandKit, BrandMember, NicheConfig, User
from backend.app.db.session import get_db
from backend.app.integrations.github.source import list_brand_repos
from backend.app.integrations.gmail.oauth import is_connected as gmail_is_connected
from backend.app.integrations.postiz.send import list_brand_channels
from backend.app.integrations.rss.fetch import add_feed as add_rss_feed
from backend.app.integrations.rss.fetch import remove_feed as remove_rss_feed
from backend.app.integrations.woocommerce.source import list_brand_products
from backend.app.llm.prompt_overrides import clear_prompt_override, get_brand_prompt_overrides, set_prompt_override
from backend.app.llm.provider import (
    CONFIGURABLE_AGENT_TASKS,
    DEFAULT_MODELS,
    MODEL_CHOICES,
    clear_brand_model_override,
    get_brand_model_overrides,
    set_brand_model_override,
)
from backend.app.storage.local_disk import get_storage_backend
from backend.app.util.crypto import encrypt

router = APIRouter(prefix="/brand-kit")
templates = Jinja2Templates(directory="backend/app/templates")

SOCIAL_PLATFORMS = ["instagram", "facebook", "youtube", "x", "tiktok"]
CONTENT_VOICE_CHOICES = {
    "newsletter": "Newsletter (default - third-person article summary)",
    "personal": "Personal (first-person builder voice, for GitHub-sourced posts)",
    "product": "Product / e-commerce (persuasive copy, for Website-sourced posts)",
}


@router.get("", response_class=HTMLResponse)
def show_brand_kit(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
    error: str | None = None,
    gmail: str | None = None,
    prompt_warning: str | None = None,
):
    active_niche = get_active_niche_config(db, brand.id)
    niche_history = (
        db.query(NicheConfig)
        .filter(NicheConfig.brand_kit_id == brand.id)
        .order_by(NicheConfig.version.desc())
        .limit(20)
        .all()
    )
    pending_niche_suggestions = [
        cfg for cfg in niche_history if cfg.created_by in ("llm_suggested", "brand_drafted") and not cfg.is_active
    ]
    members = (
        db.query(BrandMember, User)
        .join(User, User.id == BrandMember.user_id)
        .filter(BrandMember.brand_kit_id == brand.id)
        .order_by(BrandMember.role.desc(), User.email)
        .all()
    )
    storage = get_storage_backend()
    overrides = get_brand_model_overrides(db, brand.id)
    model_tasks = []
    for task, label in CONFIGURABLE_AGENT_TASKS.items():
        default_pair = DEFAULT_MODELS.get(task)
        model_tasks.append(
            {
                "agent_task": task,
                "label": label,
                "override": overrides.get(task),
                "default_label": MODEL_CHOICES.get(default_pair, default_pair[1] if default_pair else "?"),
            }
        )
    prompt_overrides = get_brand_prompt_overrides(db, brand.id)
    prompt_groups: dict[str, list[dict]] = {}
    for key, slot in PROMPT_SLOTS.items():
        override_text = prompt_overrides.get(key)
        prompt_groups.setdefault(slot["group"], []).append(
            {
                "key": key,
                "label": slot["label"],
                "default": slot["default"],
                "current_text": override_text or slot["default"],
                "has_override": override_text is not None,
            }
        )
    return templates.TemplateResponse(
        request,
        "brand_kit.html",
        {
            "kit": brand,
            "model_tasks": model_tasks,
            "model_choices": MODEL_CHOICES,
            "prompt_groups": prompt_groups,
            "platforms": SOCIAL_PLATFORMS,
            "font_choices": FONT_CHOICES,
            "font_groups": FONT_GROUPS,
            "language_choices": LANGUAGE_CHOICES,
            "active_niche": active_niche,
            "niche_history": niche_history,
            "pending_niche_suggestions": pending_niche_suggestions,
            "is_owner": brand.owner_user_id == user.id,
            "members": [{"user": u, "role": bm.role} for bm, u in members],
            "logo_url": storage.url_for(brand.logo_asset_path) if brand.logo_asset_path else None,
            "accessible_brands": get_accessible_brands(db, user),
            "active_brand": brand,
            "user": user,
            "error": error,
            "prompt_warning": prompt_warning,
            "gmail_connected": gmail_is_connected(db, brand.id),
            "gmail_just_connected": gmail == "connected",
            "postiz_channels_available": list_brand_channels(brand),
            "github_repos_available": list_brand_repos(brand),
            "website_products_available": list_brand_products(brand),
            "content_voice_choices": CONTENT_VOICE_CHOICES,
            "active_nav": "brand-kit",
        },
    )


@router.post("/identity")
def update_identity(
    brand_name: str = Form(""),
    industry: str = Form(""),
    website_url: str = Form(""),
    font_choice: str = Form("inter"),
    tone_of_voice_prompt: str = Form(""),
    default_language: str = Form(DEFAULT_LANGUAGE),
    social_instagram: str = Form(""),
    social_facebook: str = Form(""),
    social_youtube: str = Form(""),
    social_x: str = Form(""),
    social_tiktok: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """Who this brand is and how its posts look/sound - split out of the old
    single do-everything /brand-kit route (see git history) so each settings
    group on the page saves independently, same as Postiz/GitHub/Models
    already did."""
    brand.brand_name = brand_name
    brand.industry = industry
    brand.website_url = website_url or None
    brand.font_choice = font_choice if font_choice in FONT_CHOICES else "inter"
    brand.tone_of_voice_prompt = tone_of_voice_prompt
    brand.default_language = default_language if default_language in LANGUAGE_CHOICES else DEFAULT_LANGUAGE
    brand.social_handles = {
        platform: handle
        for platform, handle in {
            "instagram": social_instagram,
            "facebook": social_facebook,
            "youtube": social_youtube,
            "x": social_x,
            "tiktok": social_tiktok,
        }.items()
        if handle.strip()
    }
    db.commit()
    return RedirectResponse(url="/brand-kit", status_code=303)


@router.post("/whatsapp")
def update_whatsapp(
    whatsapp_recipient: str = Form(""),
    botsab_instance_id: str = Form(""),
    botsab_api_key: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    brand.whatsapp_recipient = whatsapp_recipient.strip() or None
    brand.botsab_instance_id = botsab_instance_id.strip() or None
    if botsab_api_key.strip():
        # Blank means "leave it as-is" (the field never echoes a set key
        # back, so an unedited blank submit must not wipe it) - clearing it
        # deliberately would need its own explicit action, not built yet.
        brand.botsab_api_key_encrypted = encrypt(botsab_api_key.strip())
    db.commit()
    return RedirectResponse(url="/brand-kit", status_code=303)


@router.post("/ai-behavior")
def update_ai_behavior(
    combined_drafting: str = Form(""),
    combined_drafting_format: str = Form(DEFAULT_FORMAT),
    content_voice: str = Form("newsletter"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    brand.combined_drafting = combined_drafting == "on"
    brand.combined_drafting_format = (
        combined_drafting_format if combined_drafting_format in VALID_FORMATS else DEFAULT_FORMAT
    )
    brand.content_voice = content_voice if content_voice in CONTENT_VOICE_CHOICES else "newsletter"
    db.commit()
    return RedirectResponse(url="/brand-kit", status_code=303)


@router.post("/model-overrides")
async def update_model_overrides(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """One combined form, one field per CONFIGURABLE_AGENT_TASKS entry
    (see brand_kit.html) - each field's value is "" (use the global
    default) or "<provider>|<model_id>" (one of MODEL_CHOICES). Generic
    request.form() parsing rather than a named Form(...) per task so this
    route doesn't need editing every time CONFIGURABLE_AGENT_TASKS changes."""
    form = await request.form()
    for agent_task in CONFIGURABLE_AGENT_TASKS:
        value = (form.get(f"model_{agent_task}") or "").strip()
        if not value:
            clear_brand_model_override(db, brand.id, agent_task)
            continue
        provider, _, model_id = value.partition("|")
        if (provider, model_id) in MODEL_CHOICES:
            set_brand_model_override(db, brand.id, agent_task, provider, model_id)
    return RedirectResponse(url="/brand-kit", status_code=303)


@router.post("/prompts/{prompt_key}")
def update_prompt_override(
    prompt_key: str,
    action: str = Form(...),
    prompt_text: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """Full-text replacement of one stage's system prompt, per brand - see
    agents/prompt_registry.py's PROMPT_SLOTS for the fixed set of valid
    prompt_key values (rejects anything else) and llm/prompt_overrides.py
    for how this gets resolved back at generation time. action="save"
    writes prompt_text as the override; action="reset" deletes it, falling
    back to the hardcoded default - the only real safety net for a bad
    edit. On save, also checks the new text against
    prompt_registry.missing_guardrail_phrases - never blocks the save (full
    override is the deliberate design), just redirects with a warning
    banner when the saved text looks like it dropped a hard requirement the
    default prompt relied on (verbatim rendering, no invented facts/offers,
    no real people's likeness, the JSON-only response contract, etc.)."""
    if prompt_key not in PROMPT_SLOTS:
        return RedirectResponse(url="/brand-kit#prompts", status_code=303)
    warning = None
    if action == "reset":
        clear_prompt_override(db, brand.id, prompt_key)
    elif action == "save" and prompt_text.strip():
        set_prompt_override(db, brand.id, prompt_key, prompt_text)
        missing = missing_guardrail_phrases(prompt_key, prompt_text)
        if missing:
            warning = (
                f"Saved - but this prompt no longer mentions: {'; '.join(missing)}. "
                "Double check that's intentional, or use Reset to restore the default."
            )
    # Query string before the fragment - a fragment can't carry a server-
    # visible query param (everything after "#" is client-side only), so
    # "#prompts?..." would silently never reach show_brand_kit's
    # prompt_warning param.
    url = "/brand-kit#prompts"
    if warning:
        from urllib.parse import quote

        url = f"/brand-kit?prompt_warning={quote(warning)}#prompts"
    return RedirectResponse(url=url, status_code=303)


@router.post("/postiz")
async def update_postiz(
    request: Request,
    postiz_api_key: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """postiz_api_key blank means "leave it as-is" (same convention as the
    Botsab key). Selected channels come as repeated "channel" fields, one
    per checked checkbox, each value "id|identifier|name" (parsed the same
    way update_model_overrides parses its own pipe-delimited values) -
    generic request.form() rather than a fixed list since how many channels
    exist depends on what's actually connected in Postiz."""
    if postiz_api_key.strip():
        brand.postiz_api_key_encrypted = encrypt(postiz_api_key.strip())

    form = await request.form()
    channels = []
    for value in form.getlist("channel"):
        channel_id, _, rest = value.partition("|")
        identifier, _, name = rest.partition("|")
        if channel_id:
            channels.append({"id": channel_id, "identifier": identifier, "name": name})
    brand.postiz_channels = channels

    db.commit()
    return RedirectResponse(url="/brand-kit", status_code=303)


@router.post("/github")
async def update_github(
    request: Request,
    github_token: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """Mirrors update_postiz exactly: github_token blank means "leave it
    as-is"; selected repos come as repeated "repo" fields, one per checked
    checkbox, each value "full_name|private" (private is "1"/"")."""
    if github_token.strip():
        brand.github_token_encrypted = encrypt(github_token.strip())

    form = await request.form()
    repos = []
    for value in form.getlist("repo"):
        full_name, _, private_flag = value.partition("|")
        if full_name:
            repos.append({"full_name": full_name, "private": private_flag == "1"})
    brand.github_repos = repos

    db.commit()
    return RedirectResponse(url="/brand-kit", status_code=303)


@router.post("/website")
def update_website(
    product_catalog_url: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """No credentials to manage - the WooCommerce Store API this reads
    from is public - so this is just the one URL field, unlike Postiz/
    GitHub's key-plus-selection panels."""
    brand.product_catalog_url = product_catalog_url.strip() or None
    db.commit()
    return RedirectResponse(url="/brand-kit", status_code=303)


@router.post("/rss/add")
def add_rss_feed_route(
    url: str = Form(...),
    name: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    if url.strip():
        add_rss_feed(db, brand, url, name)
    return RedirectResponse(url="/brand-kit", status_code=303)


@router.post("/rss/remove")
def remove_rss_feed_route(
    url: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    remove_rss_feed(db, brand, url)
    return RedirectResponse(url="/brand-kit", status_code=303)


@router.post("/logo")
def upload_logo(
    logo: UploadFile | None = File(None),
    show_logo_on_posters: str = Form(""),
    show_brand_name_on_posters: str = Form(""),
    show_source_attribution: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """The file is optional so the checkboxes can be saved on their own,
    without re-uploading - upload only replaces logo_asset_path when a real
    file came through. The three checkboxes are independent toggles over
    what Pillow/deterministic code is allowed to stamp onto a generated
    poster/reel - see docs/architecture.md's "no programmatic content text"
    policy."""
    if logo is not None and logo.filename:
        storage = get_storage_backend()
        storage_uri = storage.save(logo.file.read(), logo.filename)
        brand.logo_asset_path = storage_uri
    brand.show_logo_on_posters = show_logo_on_posters == "on"
    brand.show_brand_name_on_posters = show_brand_name_on_posters == "on"
    brand.show_source_attribution = show_source_attribution == "on"
    db.commit()
    return RedirectResponse(url="/brand-kit", status_code=303)
