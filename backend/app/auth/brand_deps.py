"""Brand-scoping dependencies, layered on top of get_current_user
(auth/deps.py) - a logged-in user can belong to several brands (own +
shared), and most routes need to know which one is "active" for this
request. Session-based, same mechanism as user auth itself.
"""

import uuid

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.app.auth.deps import get_current_user
from backend.app.db.models import BrandKit, BrandMember, User
from backend.app.db.session import get_db

ACTIVE_BRAND_SESSION_KEY = "active_brand_id"


class NoBrandAccess(Exception):
    """Raised by get_active_brand when the user belongs to zero brands -
    translated to a /brands/new redirect by the exception handler
    registered in main.py. Every admin-created user starts here until they
    create their own brand or an owner shares one with them."""


def get_accessible_brands(db: Session, user: User) -> list[BrandKit]:
    """Every brand this user can see - owned or shared with them. The owner
    also has a BrandMember row (role="owner"), so this is always a single
    join, not a UNION with brand_kit.owner_user_id."""
    return (
        db.query(BrandKit)
        .join(BrandMember, BrandMember.brand_kit_id == BrandKit.id)
        .filter(BrandMember.user_id == user.id)
        .order_by(BrandKit.name)
        .all()
    )


def get_active_brand(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BrandKit:
    """Resolves which brand this request is operating on, from
    request.session[ACTIVE_BRAND_SESSION_KEY] - falls back to the user's
    first accessible brand (alphabetically) if unset or no longer
    accessible (e.g. the owner removed them from it mid-session)."""
    brands = get_accessible_brands(db, user)
    if not brands:
        raise NoBrandAccess()

    active_id = request.session.get(ACTIVE_BRAND_SESSION_KEY)
    match = next((b for b in brands if str(b.id) == active_id), None) if active_id else None
    if match:
        return match

    request.session[ACTIVE_BRAND_SESSION_KEY] = str(brands[0].id)
    return brands[0]


def get_current_admin_user(user: User = Depends(get_current_user)) -> User:
    """Gates the /admin/users account-creation flow - distinct from being a
    brand owner."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def get_owned_brand(brand_kit_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> BrandKit:
    """For routes_brands.py's member-management routes, which name a brand
    in the URL path rather than acting on the session's active brand - an
    owner should be able to manage a brand's membership without first
    switching to it. 404s if the brand doesn't exist, 403s if the current
    user isn't its owner."""
    try:
        brand_uuid = uuid.UUID(brand_kit_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Brand not found")
    brand = db.get(BrandKit, brand_uuid)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    if brand.owner_user_id != user.id:
        raise HTTPException(status_code=403, detail="Only the brand owner can do this")
    return brand
