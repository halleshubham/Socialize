"""Admin-only user account management. There's no self-serve signup and no
email-sending infra in this app - an admin creates an account (email +
password, shared with that person out-of-band) and then a brand owner
shares a brand with them (see routes_brands.py). New users start with zero
brand memberships.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

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
