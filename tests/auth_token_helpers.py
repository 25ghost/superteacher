"""Test helpers for access JWTs bound to persisted authentication sessions."""
from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy.orm import object_session

from app.core import security
from app.models.auth_session import AuthSession


def bound_access_token(user, *, role: str | None = None) -> str:
    session = object_session(user)
    if session is None:
        raise AssertionError("user must be attached to a SQLAlchemy session")
    auth_session = AuthSession(
        user_id=user.id,
        token_hash=f"test-access-{uuid.uuid4().hex}",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    session.add(auth_session)
    session.flush()
    return security.create_access_token(
        user.id, role if role is not None else user.role, auth_session.id
    )
