from fastapi import Depends, Request
from sqlalchemy.orm import Session

from backend.app.auth.basic import BasicAuthBackend
from backend.app.config import get_settings
from backend.app.db.models import User
from backend.app.db.session import get_db

settings = get_settings()

_BACKENDS = {
    "basic": BasicAuthBackend(),
    # "google_oauth": GoogleOAuthBackend(),  # wired up when AUTH_BACKEND=google_oauth lands
}


class NotAuthenticated(Exception):
    """Raised by get_current_user; translated to a /login redirect by the
    exception handler registered in main.py."""


def get_auth_backend():
    return _BACKENDS[settings.auth_backend]


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    backend=Depends(get_auth_backend),
) -> User:
    user = backend.get_current_user(request, db)
    if user is None:
        raise NotAuthenticated()
    return user


def get_current_user_optional(
    request: Request,
    db: Session = Depends(get_db),
    backend=Depends(get_auth_backend),
) -> User | None:
    return backend.get_current_user(request, db)
