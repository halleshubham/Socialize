"""Admin user account management. Registration is public self-serve now
(routes_auth.py's /signup, open to anyone, always creates a plain
non-admin account) - this page is for admin-side management afterward:
deactivation, deletion, and granting/revoking admin status. An admin can
also still create an account directly here (e.g. onboarding someone by
hand), same as before self-serve signup existed.

Granting or revoking is_admin on another user is superadmin-only (see
auth/brand_deps.py's get_current_superadmin_user) - every other action on
this page stays available to any admin.
"""

import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from backend.app.auth.account_deletion import delete_user
from backend.app.auth.basic import hash_password
from backend.app.auth.brand_deps import get_current_admin_user, get_current_superadmin_user
from backend.app.db.models import User
from backend.app.db.session import get_db

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="backend/app/templates")


@router.get("/users", response_class=HTMLResponse)
def list_users(
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin_user),
    error: str | None = None,
):
    users = db.query(User).order_by(User.created_at.asc()).all()
    verified_count = sum(1 for u in users if u.email_verified)
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {
            "users": users,
            "user": admin,
            "error": error,
            "active_nav": "admin",
            "verified_count": verified_count,
            "pending_count": len(users) - verified_count,
        },
    )


@router.post("/users")
def create_user(
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(""),
    contact_number: str = Form(""),
    company_name: str = Form(""),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin_user),
):
    """Unlike /signup, the profile fields here are optional - an admin
    creating an account by hand may not have all of it, and is_admin/
    is_superadmin still aren't settable from this form either way (always
    False here, same as self-serve - see toggle_admin below for the only
    way to grant admin)."""
    email = email.strip().lower()
    if not email or not password:
        return RedirectResponse(url="/admin/users?error=Email and password are required", status_code=303)
    if db.query(User).filter(User.email == email).first():
        return RedirectResponse(url="/admin/users?error=A user with that email already exists", status_code=303)
    db.add(
        User(
            email=email,
            hashed_password=hash_password(password),
            auth_provider="basic",
            full_name=full_name.strip() or None,
            contact_number=contact_number.strip() or None,
            company_name=company_name.strip() or None,
        )
    )
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/toggle-admin")
def toggle_admin(
    user_id: str,
    db: Session = Depends(get_db),
    superadmin: User = Depends(get_current_superadmin_user),
):
    """The only way any user ever becomes (or stops being) an admin -
    superadmin-only, per this route's own dependency. Refuses on the
    superadmin's own account (there's exactly one superadmin, and it must
    stay an admin) and on any other superadmin (none exist today since
    is_superadmin is never grantable through the app, but the check costs
    nothing and keeps the invariant explicit)."""
    try:
        target_id = uuid.UUID(user_id)
    except ValueError:
        return RedirectResponse(url="/admin/users", status_code=303)
    if target_id == superadmin.id:
        return RedirectResponse(url="/admin/users?error=Can't change your own admin status", status_code=303)
    target = db.get(User, target_id)
    if not target:
        return RedirectResponse(url="/admin/users", status_code=303)
    if target.is_superadmin:
        return RedirectResponse(url="/admin/users?error=Can't change a superadmin's admin status", status_code=303)
    target.is_admin = not target.is_admin
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/toggle-active")
def toggle_active(
    user_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin_user),
):
    """Abuse-response lever short of deletion - reversible, doesn't touch
    any data. A deactivated user is immediately signed out of any existing
    session too (BasicAuthBackend.get_current_user checks is_active on
    every request, not just at login)."""
    try:
        target_id = uuid.UUID(user_id)
    except ValueError:
        return RedirectResponse(url="/admin/users", status_code=303)
    if target_id == admin.id:
        return RedirectResponse(url="/admin/users?error=Can't deactivate your own account", status_code=303)
    target = db.get(User, target_id)
    if target:
        target.is_active = not target.is_active
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/delete")
def delete_user_route(
    user_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin_user),
):
    """See auth/account_deletion.py's delete_user - refuses (with a clear
    reason shown back on the page) if the target still owns any brand."""
    try:
        target_id = uuid.UUID(user_id)
    except ValueError:
        return RedirectResponse(url="/admin/users", status_code=303)
    if target_id == admin.id:
        return RedirectResponse(
            url="/admin/users?error=Can't delete your own account from here - use Account settings",
            status_code=303,
        )
    ok, reason = delete_user(db, target_id)
    if not ok:
        return RedirectResponse(url=f"/admin/users?error={reason}", status_code=303)
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)
