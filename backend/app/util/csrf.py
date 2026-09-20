"""Lightweight synchronizer-token CSRF protection for unauthenticated
state-changing endpoints - currently just /login (routes_auth.py), the
only unauthenticated POST route in the app today. Not a general CSRF
middleware for every authenticated route in the app (those are a separate,
broader concern) - scoped to this one endpoint per the SaaS-readiness
Phase 0 item that asked for it specifically. A session-based synchronizer
token works fine pre-login: Starlette's SessionMiddleware doesn't require
authentication to have a session, it's just a signed cookie either way.
"""

import secrets

from starlette.requests import Request

_SESSION_KEY = "csrf_token"


def generate_csrf_token(request: Request) -> str:
    """Call when rendering a form - stores a fresh token in the session and
    returns it for the hidden form field. Reuses an existing token already
    in the session rather than rotating on every render, so a user with two
    login tabs open doesn't have submitting one invalidate the other."""
    token = request.session.get(_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[_SESSION_KEY] = token
    return token


def verify_csrf_token(request: Request, submitted: str | None) -> bool:
    expected = request.session.get(_SESSION_KEY)
    return bool(expected) and secrets.compare_digest(expected, submitted or "")
