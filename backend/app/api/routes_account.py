"""Per-user account settings - currently just LLM provider API keys (see
llm/user_keys.py). When a brand generates content, the brand OWNER's keys
here are used, falling back to the shared .env-configured keys if the
owner hasn't set their own.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from backend.app.auth.deps import get_current_user
from backend.app.db.models import User
from backend.app.db.session import get_db
from backend.app.llm.user_keys import PROVIDERS, clear_user_api_key, get_user_api_keys, set_user_api_key

router = APIRouter(prefix="/account")
templates = Jinja2Templates(directory="backend/app/templates")

PROVIDER_LABELS = {"anthropic": "Anthropic (Claude)", "openai": "OpenAI", "google": "Google (Gemini/Veo)"}


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
