"""Admin-only user account management. There's no self-serve signup and no
email-sending infra in this app - an admin creates an account (email +
password, shared with that person out-of-band) and then a brand owner
shares a brand with them (see routes_brands.py). New users start with zero
brand memberships.
"""

import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from backend.app.auth.account_deletion import delete_user
from backend.app.auth.basic import hash_password
from backend.app.auth.brand_deps import get_current_admin_user
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
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {"users": users, "user": admin, "error": error, "active_nav": "admin"},
    )


@router.post("/users")
def create_user(
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin_user),
):
    email = email.strip().lower()
    if not email or not password:
        return RedirectResponse(url="/admin/users?error=Email and password are required", status_code=303)
    if db.query(User).filter(User.email == email).first():
        return RedirectResponse(url="/admin/users?error=A user with that email already exists", status_code=303)
    db.add(User(email=email, hashed_password=hash_password(password), auth_provider="basic"))
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
