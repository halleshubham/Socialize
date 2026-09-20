"""Per-user account settings - currently just LLM provider API keys (see
llm/user_keys.py). When a brand generates content, the brand OWNER's keys
here are used, falling back to the shared .env-configured keys if the
owner hasn't set their own.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from backend.app.auth.account_deletion import delete_user
from backend.app.auth.brand_deps import get_accessible_brands
from backend.app.auth.deps import get_current_user
from backend.app.db.models import BrandKit, ContentItem, MediaAsset, User
from backend.app.db.session import get_db
from backend.app.llm.user_keys import PROVIDERS, clear_user_api_key, get_user_api_keys, set_user_api_key

router = APIRouter(prefix="/account")
templates = Jinja2Templates(directory="backend/app/templates")

PROVIDER_LABELS = {"anthropic": "Anthropic (Claude)", "openai": "OpenAI", "google": "Google (Gemini/Veo)"}

# Short "how to get this key" pointers shown next to each field - BYOK is
# now required (llm/user_keys.py), so a self-serve user who never got
# out-of-band context from an admin needs to know where to actually go.
PROVIDER_GUIDES = {
    "anthropic": (
        '<ol class="guide-steps">'
        '<li>Open <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener">'
        "console.anthropic.com → API Keys</a></li>"
        "<li>Click <strong>Create Key</strong></li>"
        "<li>Paste it below</li>"
        "</ol>"
        '<p class="faint" style="margin:.3rem 0 0">Used for content drafting/analysis.</p>'
    ),
    "openai": (
        '<ol class="guide-steps">'
        '<li>Open <a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener">'
        "platform.openai.com → API keys</a></li>"
        "<li>Click <strong>Create new secret key</strong></li>"
        "<li>Paste it below</li>"
        "</ol>"
        '<p class="faint" style="margin:.3rem 0 0">Used for Hindi/Marathi content specifically.</p>'
    ),
    "google": (
        '<ol class="guide-steps">'
        '<li>Open <a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener">'
        "aistudio.google.com → Get API key</a></li>"
        "<li>Click <strong>Create API key</strong></li>"
        "<li>Paste it below</li>"
        "</ol>"
        '<p class="faint" style="margin:.3rem 0 0">Used for poster images and Veo reel video. '
        "<strong>Veo specifically requires a billed Google Cloud project</strong> - a free AI Studio "
        "key alone will save here but fail at video generation time.</p>"
    ),
}


@router.get("", response_class=HTMLResponse)
def show_account(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    keys = get_user_api_keys(db, user.id)
    return templates.TemplateResponse(
        request,
        "account.html",
        {
            "providers": PROVIDERS,
            "provider_labels": PROVIDER_LABELS,
            "provider_guides": PROVIDER_GUIDES,
            "keys_set": {p: bool(v) for p, v in keys.items()},
            "user": user,
            "active_nav": "account",
        },
    )


@router.post("/api-keys")
def update_api_key(
    provider: str = Form(...),
    api_key: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if provider not in PROVIDERS:
        return RedirectResponse(url="/account", status_code=303)
    api_key = api_key.strip()
    if api_key:
        set_user_api_key(db, user.id, provider, api_key)
    return RedirectResponse(url="/account", status_code=303)


@router.post("/api-keys/{provider}/clear")
def clear_api_key(
    provider: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if provider in PROVIDERS:
        clear_user_api_key(db, user.id, provider)
    return RedirectResponse(url="/account", status_code=303)


@router.get("/export")
def export_my_data(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """GDPR-style "right to portability" - a JSON download of everything
    tied to this account. Scoped to brands you OWN for the full content
    dump (a brand you're just a member of has other people's contributions
    mixed in, so only your membership itself is listed, not a full export
    of someone else's brand). Never includes actual API key VALUES -
    get_user_api_keys is used elsewhere for masked display; this only says
    which providers are configured, not the secret itself, consistent with
    never echoing a raw key back anywhere in the app."""
    owned_brands = db.query(BrandKit).filter(BrandKit.owner_user_id == user.id).all()
    member_brands = get_accessible_brands(db, user)

    def _export_brand(brand: BrandKit) -> dict:
        items = db.query(ContentItem).filter(ContentItem.brand_kit_id == brand.id).all()
        return {
            "brand_name": brand.brand_name or brand.name,
            "industry": brand.industry,
            "website_url": brand.website_url,
            "default_language": brand.default_language,
            "created_at": brand.created_at.isoformat(),
            "content_items": [
                {
                    "id": str(item.id),
                    "stage": item.stage,
                    "format": item.format,
                    "article_title": item.article_title,
                    "article_url": item.article_url,
                    "copy_text": item.copy_text,
                    "hashtags": item.hashtags,
                    "poster_headline": item.poster_headline,
                    "poster_template": item.poster_template,
                    "poster_content": item.poster_content,
                    "reel_script": item.reel_script,
                    "carousel_script": item.carousel_script,
                    "media_assets": [
                        {"asset_type": a.asset_type, "storage_uri": a.storage_uri, "created_at": a.created_at.isoformat()}
                        for a in db.query(MediaAsset).filter(MediaAsset.content_item_id == item.id).all()
                    ],
                    "created_at": item.created_at.isoformat(),
                }
                for item in items
            ],
        }

    data = {
        "account": {
            "email": user.email,
            "is_admin": user.is_admin,
            "created_at": user.created_at.isoformat(),
        },
        "api_keys_configured": [p for p, v in get_user_api_keys(db, user.id).items() if v],
        "brand_memberships": [
            {"brand_name": b.brand_name or b.name, "role": "owner" if b.owner_user_id == user.id else "member"}
            for b in member_brands
        ],
        "owned_brands": [_export_brand(b) for b in owned_brands],
    }
    return JSONResponse(
        content=data,
        headers={"Content-Disposition": "attachment; filename=socialize-data-export.json"},
    )


@router.post("/delete")
def delete_own_account(
    request: Request,
    confirm_email: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Self-service account deletion - see auth/account_deletion.py's
    delete_user. Requires retyping your own email as confirmation, same
    reasoning as brand deletion's retype-the-name confirmation. Refuses
    (with a clear reason) if you still own any brand - see delete_user's
    own docstring for why that's not something to cascade through
    silently."""
    if confirm_email.strip().lower() != user.email.strip().lower():
        return RedirectResponse(url="/account?error=Email didn't match - nothing was deleted", status_code=303)
    ok, reason = delete_user(db, user.id)
    if not ok:
        return RedirectResponse(url=f"/account?error={reason}", status_code=303)
    db.commit()
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
