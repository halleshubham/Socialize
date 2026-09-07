"""Brand creation, switching, and sharing. A brand is a BrandKit row -
create/switch act on the current user's session; member management acts on
a brand named in the URL path (see auth/brand_deps.py::get_owned_brand),
so an owner can manage a shared brand's membership without first switching
their active brand to it.
"""

import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from backend.app.auth.brand_deps import (
    ACTIVE_BRAND_SESSION_KEY,
    get_accessible_brands,
    get_owned_brand,
)
from backend.app.auth.deps import get_current_user
from backend.app.db.models import BrandKit, BrandMember, User
from backend.app.db.session import get_db

router = APIRouter(prefix="/brands")
templates = Jinja2Templates(directory="backend/app/templates")


@router.get("/new", response_class=HTMLResponse)
def new_brand_form(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    error: str | None = None,
):
    return templates.TemplateResponse(
        request,
        "brand_new.html",
        {
            "accessible_brands": get_accessible_brands(db, user),
            "user": user,
            "error": error,
            "active_nav": "brands",
        },
    )


@router.post("")
def create_brand(
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    name = name.strip()
    if not name:
        return RedirectResponse(url="/brands/new?error=Name is required", status_code=303)
    # brand_name seeded the same as name so the switcher/lists (which prefer
    # brand_name once it's set on the Brand Kit page) show something sane
    # immediately, not blank, before anyone's visited /brand-kit yet.
    brand = BrandKit(name=name, brand_name=name, owner_user_id=user.id)
    db.add(brand)
    db.flush()
    db.add(BrandMember(brand_kit_id=brand.id, user_id=user.id, role="owner"))
    db.commit()
    request.session[ACTIVE_BRAND_SESSION_KEY] = str(brand.id)
    return RedirectResponse(url="/brand-kit", status_code=303)


@router.post("/switch")
def switch_brand(
    request: Request,
    brand_kit_id: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accessible_ids = {str(b.id) for b in get_accessible_brands(db, user)}
    if brand_kit_id in accessible_ids:
        request.session[ACTIVE_BRAND_SESSION_KEY] = brand_kit_id
    referer = request.headers.get("referer", "/board")
    return RedirectResponse(url=referer, status_code=303)


@router.post("/{brand_kit_id}/members")
def add_member(
    email: str = Form(...),
    db: Session = Depends(get_db),
    brand: BrandKit = Depends(get_owned_brand),
):
    email = email.strip().lower()
    target = db.query(User).filter(User.email == email).first()
    if not target:
        return RedirectResponse(
            url="/brand-kit?error=No user with that email exists yet - ask an admin to create one first",
            status_code=303,
        )
    existing = (
        db.query(BrandMember)
        .filter(BrandMember.brand_kit_id == brand.id, BrandMember.user_id == target.id)
        .first()
    )
    if not existing:
        db.add(BrandMember(brand_kit_id=brand.id, user_id=target.id, role="member"))
        db.commit()
    return RedirectResponse(url="/brand-kit", status_code=303)


@router.post("/{brand_kit_id}/members/{user_id}/remove")
def remove_member(
    user_id: str,
    db: Session = Depends(get_db),
    brand: BrandKit = Depends(get_owned_brand),
):
    try:
        target_id = uuid.UUID(user_id)
    except ValueError:
        return RedirectResponse(url="/brand-kit", status_code=303)
    if target_id == brand.owner_user_id:
        return RedirectResponse(url="/brand-kit?error=Can't remove the brand owner", status_code=303)
    db.query(BrandMember).filter(
        BrandMember.brand_kit_id == brand.id, BrandMember.user_id == target_id
    ).delete()
    db.commit()
    return RedirectResponse(url="/brand-kit", status_code=303)
