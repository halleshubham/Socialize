from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from backend.app.auth.basic import BasicAuthBackend
from backend.app.auth.login_throttle import is_locked_out, record_failure, record_success
from backend.app.db.session import get_db
from backend.app.util.csrf import generate_csrf_token, verify_csrf_token

router = APIRouter()
templates = Jinja2Templates(directory="backend/app/templates")
backend_auth = BasicAuthBackend()


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "csrf_token": generate_csrf_token(request)}
    )


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    if not verify_csrf_token(request, csrf_token):
        # Expired/missing session token (e.g. a stale form left open across
        # a server restart) rather than "wrong password" - a distinct
        # message so this doesn't read as a real credential failure.
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Your session expired - please try again.", "csrf_token": generate_csrf_token(request)},
            status_code=400,
        )
    if is_locked_out(email):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Too many failed attempts for this account - please wait 15 minutes and try again.",
                "csrf_token": generate_csrf_token(request),
            },
            status_code=429,
        )
    user = backend_auth.authenticate(db, email, password)
    if not user:
        record_failure(email)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid email or password", "csrf_token": generate_csrf_token(request)},
            status_code=401,
        )
    record_success(email)
    request.session["user_id"] = str(user.id)
    return RedirectResponse(url="/board", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
