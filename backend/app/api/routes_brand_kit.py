from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from backend.agents.auto_mode import DEFAULT_FORMAT, VALID_FORMATS
from backend.agents.graphic_designer.fonts import FONT_CHOICES, FONT_GROUPS
from backend.agents.languages import DEFAULT_LANGUAGE, LANGUAGE_CHOICES
from backend.agents.niche import get_active_niche_config
from backend.app.auth.brand_deps import get_accessible_brands, get_active_brand
from backend.app.auth.deps import get_current_user
from backend.app.db.models import BrandKit, BrandMember, NicheConfig, User
from backend.app.db.session import get_db
from backend.app.integrations.gmail.oauth import is_connected as gmail_is_connected
from backend.app.integrations.postiz.send import list_brand_channels
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


@router.get("", response_class=HTMLResponse)
def show_brand_kit(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
    error: str | None = None,
    gmail: str | None = None,
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
    return templates.TemplateResponse(
        request,
        "brand_kit.html",
        {
            "kit": brand,
            "model_tasks": model_tasks,
            "model_choices": MODEL_CHOICES,
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
            "gmail_connected": gmail_is_connected(db, brand.id),
            "gmail_just_connected": gmail == "connected",
            "postiz_channels_available": list_brand_channels(brand),
            "active_nav": "brand-kit",
        },
    )


@router.post("")
def update_brand_kit(
    brand_name: str = Form(""),
    industry: str = Form(""),
    website_url: str = Form(""),
    whatsapp_recipient: str = Form(""),
    botsab_instance_id: str = Form(""),
    botsab_api_key: str = Form(""),
    font_choice: str = Form("inter"),
    tone_of_voice_prompt: str = Form(""),
    default_language: str = Form(DEFAULT_LANGUAGE),
    social_instagram: str = Form(""),
    social_facebook: str = Form(""),
    social_youtube: str = Form(""),
    social_x: str = Form(""),
    social_tiktok: str = Form(""),
    combined_drafting: str = Form(""),
    combined_drafting_format: str = Form(DEFAULT_FORMAT),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    brand.brand_name = brand_name
    brand.industry = industry
    brand.website_url = website_url or None
    brand.whatsapp_recipient = whatsapp_recipient.strip() or None
    brand.botsab_instance_id = botsab_instance_id.strip() or None
    if botsab_api_key.strip():
        # Blank means "leave it as-is" (the field never echoes a set key
        # back, so an unedited blank submit must not wipe it) - clearing it
        # deliberately would need its own explicit action, not built yet.
        brand.botsab_api_key_encrypted = encrypt(botsab_api_key.strip())
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
    brand.combined_drafting = combined_drafting == "on"
    brand.combined_drafting_format = (
        combined_drafting_format if combined_drafting_format in VALID_FORMATS else DEFAULT_FORMAT
    )
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


@router.post("/logo")
def upload_logo(
    logo: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    if not logo.filename:
        return RedirectResponse(url="/brand-kit?error=No file selected", status_code=303)
    storage = get_storage_backend()
    storage_uri = storage.save(logo.file.read(), logo.filename)
    brand.logo_asset_path = storage_uri
    db.commit()
    return RedirectResponse(url="/brand-kit", status_code=303)
