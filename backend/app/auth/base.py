from typing import Protocol

from fastapi import Request
from sqlalchemy.orm import Session

from backend.app.db.models import User


class AuthBackend(Protocol):
    """Swappable auth strategy. Routes depend only on `get_current_user`
    (see auth/deps.py), never on a concrete backend, so switching
    AUTH_BACKEND=basic -> google_oauth later touches no route code."""

    def get_current_user(self, request: Request, db: Session) -> User | None: ...

    def login_url(self) -> str: ...
