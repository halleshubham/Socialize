import uuid

import bcrypt
from fastapi import Request
from sqlalchemy.orm import Session

from backend.app.db.models import User

# Using bcrypt directly rather than passlib: passlib 1.7.4 (last release,
# effectively unmaintained) breaks against bcrypt>=4.1's removed __about__
# attribute, so the extra abstraction layer buys nothing here.


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


class BasicAuthBackend:
    """Session-cookie auth backed by a bcrypt password hash. Single-user in
    practice today, but the user table/session shape already supports more."""

    def get_current_user(self, request: Request, db: Session) -> User | None:
        user_id = request.session.get("user_id")
        if not user_id:
            return None
        return db.get(User, uuid.UUID(user_id))

    def login_url(self) -> str:
        return "/login"

    def authenticate(self, db: Session, email: str, password: str) -> User | None:
        user = db.query(User).filter(User.email == email).first()
        if not user or not user.hashed_password:
            return None
        if not verify_password(password, user.hashed_password):
            return None
        return user
