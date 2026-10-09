"""Data access for ``class_ws_tickets`` (Phase 3, slice 3C).

Ticket issuance and — decisively — ticket consumption. The row is the
authority: one user, one class, one short expiry, single use.

The one rule that matters here is :func:`consume`: redemption is a
SINGLE atomic ``UPDATE ... WHERE ... RETURNING`` statement, never a
check-then-write. Two handshakes racing over the same ticket therefore
have exactly one winner (§10); the loser's ``WHERE`` no longer matches
because ``used_at`` is set. The statement also enforces, in the same
instant, that the ticket belongs to the class being entered
(``class_session_id``) and that it has not expired — a mismatch simply
matches no row and the caller treats that as "invalid".

Only the SHA-256 digest of the raw ticket is ever stored (``token_hash``);
the raw value exists solely in the HTTP response that minted it and in
the one handshake that redeemed it — it is never logged, audited or
returned again.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.class_ws_ticket import ClassWsTicket


def create(
    session: Session,
    *,
    class_session_id: uuid.UUID,
    user_id: uuid.UUID,
    token_hash: str,
    expires_at: datetime,
) -> ClassWsTicket:
    """Mint one ticket row (flushed, not committed — the caller commits)."""
    ticket = ClassWsTicket(
        class_session_id=class_session_id,
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    session.add(ticket)
    session.flush()
    return ticket


def consume(
    session: Session,
    *,
    token_hash: str,
    class_session_id: uuid.UUID,
    now: datetime,
) -> ClassWsTicket | None:
    """Atomically redeem one ticket for one class, or ``None``.

    One statement marks ``used_at`` if and only if the ticket is for
    this class, still unused and not yet expired — the row is returned
    only when this call actually won the redemption (§10). ``None`` means
    "invalid" without distinguishing unknown/expired/used/wrong-class to
    the caller: the handshake answers with one close code either way, so
    probing learns nothing (§40).
    """
    stmt = (
        update(ClassWsTicket)
        .where(
            ClassWsTicket.token_hash == token_hash,
            ClassWsTicket.class_session_id == class_session_id,
            ClassWsTicket.used_at.is_(None),
            ClassWsTicket.expires_at > now,
        )
        .values(used_at=now)
        .returning(ClassWsTicket)
        .execution_options(synchronize_session=False)
    )
    return session.execute(stmt).scalar_one_or_none()
