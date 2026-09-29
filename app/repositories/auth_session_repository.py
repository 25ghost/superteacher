"""Data access for ``auth_sessions`` (revocable refresh sessions).

Insert/lookup/update helpers only — no HTTP, no token construction, no
business rules. The auth service decides *when* sessions are created,
rotated or revoked; this module decides *how* rows are written and found.

All writes are ``flush``ed, never committed: the request-scoped session
remains the single transaction owner (same convention as every other
repository in this backend).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.auth_session import AuthSession


def create(
    session: Session,
    *,
    user_id: uuid.UUID,
    token_hash: str,
    expires_at: datetime,
) -> AuthSession:
    """Insert one refresh session row (flushed, not committed)."""
    auth_session = AuthSession(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    session.add(auth_session)
    session.flush()
    return auth_session


def get_by_token_hash(session: Session, token_hash: str) -> AuthSession | None:
    """The session matching one refresh-token digest (or None)."""
    return session.scalar(
        select(AuthSession).where(AuthSession.token_hash == token_hash)
    )


def get_by_token_hash_for_update(session: Session, token_hash: str) -> AuthSession | None:
    """The session matching one refresh-token digest, locked with SELECT FOR UPDATE.

    Prevents the concurrent-refresh race: two requests presenting the
    same refresh token will serialize on this row lock. The second
    request blocks until the first commits or rolls back, at which point
    its own check finds the session already revoked.
    """
    return session.scalar(
        select(AuthSession)
        .where(AuthSession.token_hash == token_hash)
        .with_for_update()
    )


def get_by_id(session: Session, auth_session_id: uuid.UUID) -> AuthSession | None:
    return session.get(AuthSession, auth_session_id)


def mark_used(session: Session, auth_session: AuthSession, used_at: datetime) -> AuthSession:
    """Update ``last_used_at`` (flushed, not committed)."""
    auth_session.last_used_at = used_at
    session.flush()
    return auth_session


def revoke(session: Session, auth_session: AuthSession, revoked_at: datetime) -> AuthSession:
    """Revoke one session (idempotent; flushed, not committed).

    ``revoked_at`` is only set when still empty, so revoking an already
    revoked session never overwrites the original revocation time.
    """
    if auth_session.revoked_at is None:
        auth_session.revoked_at = revoked_at
        session.flush()
    return auth_session


def revoke_all_for_user(session: Session, user_id: uuid.UUID, revoked_at: datetime) -> int:
    """Revoke every active session of one user atomically; return the count revoked.

    Used when an account's status changes (suspension/disabling) so no
    outstanding refresh session survives a status change (Step 30).

    Uses an atomic UPDATE rather than read-then-write to prevent a race
    where a concurrent refresh creates a new session between the SELECT
    and the UPDATE, leaving the new session unrevoked.
    """
    stmt = (
        update(AuthSession)
        .where(
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=revoked_at)
    )
    result = session.execute(stmt)
    return result.rowcount


def enforce_session_limit(session: Session, user_id: uuid.UUID, max_sessions: int, revoked_at: datetime) -> int:
    """Revoke oldest sessions when the user exceeds ``max_sessions``.

    Counts active (non-revoked) sessions and revokes the excess oldest
    ones so that at most ``max_sessions`` remain. Returns the number
    revoked (may be 0).
    """
    active = session.scalars(
        select(AuthSession)
        .where(
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
        )
        .order_by(AuthSession.created_at.asc())
    ).all()
    excess = len(active) - max_sessions
    if excess <= 0:
        return 0
    for auth_session in active[:excess]:
        auth_session.revoked_at = revoked_at
    session.flush()
    return excess


def cleanup_expired(session: Session, cutoff: datetime) -> int:
    """Delete expired and long-revoked auth sessions older than the cutoff.

    Purges two categories:
    - sessions whose ``expires_at`` is before the cutoff (expired),
    - sessions whose ``revoked_at`` is set and before the cutoff (revoked
      long enough to be safe to discard).

    Active (non-expired, non-revoked) sessions are never touched.
    Returns the number of rows deleted.
    """
    from sqlalchemy import delete

    stmt = delete(AuthSession).where(
        (AuthSession.expires_at < cutoff)
        | (
            AuthSession.revoked_at.isnot(None)
            & (AuthSession.revoked_at < cutoff)
        )
    )
    result = session.execute(stmt)
    session.flush()
    return result.rowcount
