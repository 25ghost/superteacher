"""Unit tests: auth session cleanup (in-memory SQLite, no HTTP).

Covers:

- cleanup_expired removes expired sessions,
- cleanup_expired removes old-revoked sessions,
- cleanup_expired preserves active sessions,
- cleanup_expired preserves recently-revoked sessions (within retention),
- cleanup_expired returns correct count.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
import app.models  # noqa: F401  (registers every table)
from app.models.auth_session import AuthSession
from app.repositories import auth_session_repository as repo


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _make_session(
    session: Session,
    *,
    expires_at: datetime,
    revoked_at: datetime | None = None,
) -> AuthSession:
    auth_session = AuthSession(
        user_id=uuid.uuid4(),
        token_hash=f"hash-{uuid.uuid4().hex[:12]}",
        expires_at=expires_at,
        revoked_at=revoked_at,
    )
    session.add(auth_session)
    session.flush()
    return auth_session


def test_cleanup_removes_expired_sessions(session: Session) -> None:
    now = datetime.now(timezone.utc)
    _make_session(session, expires_at=now - timedelta(days=10))
    _make_session(session, expires_at=now + timedelta(days=10))
    session.commit()

    deleted = repo.cleanup_expired(session, cutoff=now)
    session.commit()
    assert deleted == 1


def test_cleanup_removes_old_revoked_sessions(session: Session) -> None:
    now = datetime.now(timezone.utc)
    _make_session(
        session,
        expires_at=now + timedelta(days=30),
        revoked_at=now - timedelta(days=30),
    )
    _make_session(
        session,
        expires_at=now + timedelta(days=30),
        revoked_at=now - timedelta(days=2),
    )
    session.commit()

    deleted = repo.cleanup_expired(session, cutoff=now - timedelta(days=7))
    session.commit()
    assert deleted == 1  # only the old-revoked one


def test_cleanup_preserves_active_sessions(session: Session) -> None:
    now = datetime.now(timezone.utc)
    _make_session(session, expires_at=now + timedelta(days=10))
    _make_session(session, expires_at=now + timedelta(days=20))
    session.commit()

    deleted = repo.cleanup_expired(session, cutoff=now)
    session.commit()
    assert deleted == 0


def test_cleanup_returns_zero_on_empty_table(session: Session) -> None:
    now = datetime.now(timezone.utc)
    deleted = repo.cleanup_expired(session, cutoff=now)
    session.commit()
    assert deleted == 0


def test_cleanup_removes_both_expired_and_revoked(session: Session) -> None:
    now = datetime.now(timezone.utc)
    _make_session(session, expires_at=now - timedelta(days=5))
    _make_session(session, expires_at=now - timedelta(days=3))
    _make_session(
        session,
        expires_at=now + timedelta(days=30),
        revoked_at=now - timedelta(days=10),
    )
    _make_session(session, expires_at=now + timedelta(days=10))
    session.commit()

    deleted = repo.cleanup_expired(session, cutoff=now)
    session.commit()
    assert deleted == 3  # 2 expired + 1 old-revoked, 1 active kept
