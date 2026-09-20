from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from backend.app.auth.basic import BasicAuthBackend, hash_password, verify_password
from backend.app.auth.email_verification import can_resend, is_configured, send_verification_email, verify_token
from backend.app.auth.login_throttle import is_locked_out, record_failure, record_success
from backend.app.db.models import User
from backend.app.db.session import get_db
from backend.app.integrations.resend.client import ResendError
from backend.app.util.csrf import generate_csrf_token, verify_csrf_token

router = APIRouter()
templates = Jinja2Templates(directory="backend/app/templates")
backend_auth = BasicAuthBackend()

_MIN_PASSWORD_LENGTH = 8


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
        # A correct password against an unverified account gets a distinct,
        # actionable message (not a security leak - this person already
        # knows the account exists, they just created it) rather than the
        # generic "invalid email or password", which would otherwise read
        # as a wrong-password error with no indication of what to actually
        # do next.
        unverified = (
            db.query(User)
            .filter(User.email == email.strip().lower(), User.email_verified.is_(False))
            .first()
        )
        if unverified and unverified.hashed_password and verify_password(password, unverified.hashed_password):
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "error": "Please verify your email first - check your inbox for the link, "
                    "or use the resend option on the verification page.",
                    "csrf_token": generate_csrf_token(request),
                    "unverified_email": unverified.email,
                },
                status_code=401,
            )
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


def _signup_form_values(full_name: str, email: str, contact_number: str, company_name: str) -> dict:
    """Echoes back what was typed (never the passwords) so a validation
    error doesn't force retyping the whole form."""
    return {
        "full_name": full_name,
        "email": email,
        "contact_number": contact_number,
        "company_name": company_name,
    }


@router.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request):
    return templates.TemplateResponse(
        request,
        "signup.html",
        {"error": None, "csrf_token": generate_csrf_token(request), "values": {}},
    )


@router.post("/signup")
def signup(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    contact_number: str = Form(...),
    company_name: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    """Public, unauthenticated self-serve registration - open to anyone, no
    admin gate, no approval step, no billing gate (payments are handled
    outside this app entirely - see Account settings' BYOK model).
    is_admin/is_superadmin are never read from this form - every account
    created here is a plain non-admin user by construction; the only way to
    become an admin is a superadmin granting it afterward
    (routes_admin.py's toggle_admin, get_current_superadmin_user-gated)."""
    full_name = full_name.strip()
    email = email.strip().lower()
    contact_number = contact_number.strip()
    company_name = company_name.strip()
    values = _signup_form_values(full_name, email, contact_number, company_name)

    if not verify_csrf_token(request, csrf_token):
        return templates.TemplateResponse(
            request,
            "signup.html",
            {
                "error": "Your session expired - please try again.",
                "csrf_token": generate_csrf_token(request),
                "values": values,
            },
            status_code=400,
        )

    if not all([full_name, email, contact_number, company_name, password]):
        return templates.TemplateResponse(
            request,
            "signup.html",
            {
                "error": "Name, email, contact number, company name, and password are all required.",
                "csrf_token": generate_csrf_token(request),
                "values": values,
            },
            status_code=400,
        )
    # Matches the users table's actual column lengths (models.py) - without
    # this, an overlong value doesn't fail cleanly here, it reaches the
    # INSERT and raises a raw DB error (StringDataRightTruncation) instead
    # of a normal validation message.
    _field_limits = [
        ("email", email, 320),
        ("full name", full_name, 255),
        ("contact number", contact_number, 30),
        ("company name", company_name, 255),
    ]
    for label, value, limit in _field_limits:
        if len(value) > limit:
            return templates.TemplateResponse(
                request,
                "signup.html",
                {
                    "error": f"{label.capitalize()} is too long (max {limit} characters).",
                    "csrf_token": generate_csrf_token(request),
                    "values": values,
                },
                status_code=400,
            )
    if password != confirm_password:
        return templates.TemplateResponse(
            request,
            "signup.html",
            {"error": "Passwords don't match.", "csrf_token": generate_csrf_token(request), "values": values},
            status_code=400,
        )
    if len(password) < _MIN_PASSWORD_LENGTH:
        return templates.TemplateResponse(
            request,
            "signup.html",
            {
                "error": f"Password must be at least {_MIN_PASSWORD_LENGTH} characters.",
                "csrf_token": generate_csrf_token(request),
                "values": values,
            },
            status_code=400,
        )
    if db.query(User).filter(User.email == email).first():
        return templates.TemplateResponse(
            request,
            "signup.html",
            {
                "error": "An account with that email already exists - log in instead.",
                "csrf_token": generate_csrf_token(request),
                "values": values,
            },
            status_code=400,
        )

    resend_configured = is_configured()
    user = User(
        email=email,
        hashed_password=hash_password(password),
        auth_provider="basic",
        full_name=full_name,
        contact_number=contact_number,
        company_name=company_name,
        # Deliberately hardcoded, not derived from any request input -
        # self-serve signup can never mint an admin or superadmin account.
        is_admin=False,
        is_superadmin=False,
        # Auto-verified when Resend isn't configured - see
        # email_verification.py's module docstring for why this fails
        # open rather than stranding new accounts with no way to verify.
        email_verified=not resend_configured,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    if resend_configured:
        verify_url_base = str(request.url_for("verify_email"))
        try:
            send_verification_email(db, user, verify_url_base)
        except ResendError:
            # The account still exists and can resend from the "check your
            # email" page - a Resend outage shouldn't be a 500 on signup.
            pass
        return RedirectResponse(url=f"/verify-email/pending?email={email}", status_code=303)

    request.session["user_id"] = str(user.id)
    return RedirectResponse(url="/board", status_code=303)


@router.get("/verify-email/pending", response_class=HTMLResponse)
def verify_email_pending(request: Request, email: str = "", sent: str = ""):
    """Landed on right after signup when Resend is configured - the token
    itself was already generated and emailed by send_verification_email in
    the signup route above; this is just "check your inbox," with a resend
    option once the cooldown in email_verification.py's can_resend allows it."""
    return templates.TemplateResponse(
        request,
        "verify_email_pending.html",
        {"email": email, "just_resent": bool(sent), "csrf_token": generate_csrf_token(request)},
    )


@router.post("/verify-email/resend")
def verify_email_resend(
    request: Request,
    email: str = Form(...),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    if not verify_csrf_token(request, csrf_token):
        return RedirectResponse(url=f"/verify-email/pending?email={email}", status_code=303)
    user = db.query(User).filter(User.email == email, User.email_verified.is_(False)).first()
    # Same redirect either way - doesn't confirm/deny whether an unverified
    # account exists for this email; the "sent" banner shows regardless,
    # and the cooldown/Resend-outage cases fail silently on purpose (same
    # reasoning as the signup route above).
    if user and can_resend(user):
        try:
            verify_url_base = str(request.url_for("verify_email"))
            send_verification_email(db, user, verify_url_base)
        except ResendError:
            pass
    return RedirectResponse(url=f"/verify-email/pending?email={email}&sent=1", status_code=303)


@router.get("/verify-email", response_class=HTMLResponse, name="verify_email")
def verify_email(request: Request, token: str = "", db: Session = Depends(get_db)):
    user = verify_token(db, token)
    if not user:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "That verification link is invalid or already used - log in, or request a new one.",
                "csrf_token": generate_csrf_token(request),
            },
            status_code=400,
        )
    request.session["user_id"] = str(user.id)
    return RedirectResponse(url="/board", status_code=303)
