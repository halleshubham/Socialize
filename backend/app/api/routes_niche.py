import logging

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.agents.niche import get_active_niche_config
from backend.agents.researcher.niche_evolution import draft_niche_from_brand, suggest_niche_update
from backend.app.auth.brand_deps import get_active_brand
from backend.app.auth.deps import get_current_user
from backend.app.db.models import BrandKit, NicheConfig, User
from backend.app.db.session import get_db

logger = logging.getLogger(__name__)

# Niche config lives on the /brand-kit page now (niche and brand are two
# facets of one thing: what this account posts about, and how) - these
# routes are just the write-side actions; show_brand_kit (routes_brand_kit.py)
# renders the niche history/pending-suggestions state alongside brand fields.
router = APIRouter(prefix="/niche")


@router.get("")
def show_niche_redirect(user: User = Depends(get_current_user)):
    return RedirectResponse(url="/brand-kit", status_code=303)


@router.post("")
def update_niche(
    prompt_text: str = Form(...),
    keywords: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """Editing the niche creates a new version rather than mutating the
    active one, so the config keeps a real history/diff over time."""
    active = get_active_niche_config(db, brand.id)
    if active:
        active.is_active = False
    max_version = db.query(func.max(NicheConfig.version)).filter(NicheConfig.brand_kit_id == brand.id).scalar() or 0
    keyword_list = [k.strip() for k in keywords.split(",") if k.strip()]
    db.add(
        NicheConfig(
            version=max_version + 1,
            prompt_text=prompt_text,
            keywords=keyword_list,
            parent_version_id=active.id if active else None,
            brand_kit_id=brand.id,
            created_by="user",
            is_active=True,
        )
    )
    db.commit()
    return RedirectResponse(url="/brand-kit", status_code=303)


@router.post("/suggest")
def trigger_suggestion(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """Asks the niche-evolution agent for a revised prompt based on recent
    triage outcomes. Saved inactive - review it on /brand-kit before activating."""
    try:
        suggest_niche_update(db, brand.id)
    except ValueError as exc:
        return RedirectResponse(url=f"/brand-kit?error={exc}", status_code=303)
    except Exception:
        logger.exception("Niche suggestion failed")
        return RedirectResponse(
            url="/brand-kit?error=Suggestion failed - check ANTHROPIC_API_KEY and server logs.",
            status_code=303,
        )
    return RedirectResponse(url="/brand-kit", status_code=303)


@router.post("/draft-from-brand")
def draft_from_brand(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    """Grounds a first-draft niche prompt in the brand kit's industry/
    website/handles instead of triage history - for a brand-new or still-
    vague niche. Saved inactive, same review-then-activate flow as /suggest."""
    try:
        draft_niche_from_brand(db, brand.id)
    except ValueError as exc:
        return RedirectResponse(url=f"/brand-kit?error={exc}", status_code=303)
    except Exception:
        logger.exception("Niche draft-from-brand failed")
        return RedirectResponse(
            url="/brand-kit?error=Draft failed - check ANTHROPIC_API_KEY and server logs.",
            status_code=303,
        )
    return RedirectResponse(url="/brand-kit", status_code=303)


@router.post("/activate/{config_id}")
def activate_suggestion(
    config_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    brand: BrandKit = Depends(get_active_brand),
):
    suggestion = db.get(NicheConfig, config_id)
    if suggestion and suggestion.brand_kit_id != brand.id:
        suggestion = None
    if suggestion:
        active = get_active_niche_config(db, brand.id)
        if active:
            active.is_active = False
        suggestion.is_active = True
        db.commit()
    return RedirectResponse(url="/brand-kit", status_code=303)
