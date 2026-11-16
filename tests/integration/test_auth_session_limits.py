"""AUTH-REF-001: real PostgreSQL capacity, rotation, locks and rollback."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import threading
import time
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core import security
from app.models.auth_session import AuthSession
from app.models.user import User
from app.repositories import auth_session_repository as repo
from app.schemas.auth import LoginRequest
from app.services import auth_service as service

pytestmark = pytest.mark.integration
PASSWORD = 'session-test-password'


@pytest.fixture
def account(pg_engine):
    # Only account data is created here. A single cascading truncate avoids
    # repeatedly traversing the entire application's dependency graph.
    with pg_engine.begin() as connection:
        assert connection.scalar(text('SELECT current_database()')).endswith('_test')
        connection.execute(text('TRUNCATE TABLE users CASCADE'))
    with Session(pg_engine) as db:
        user = User(email='sessions@example.com', role='student', status='active',
                    password_hash=security.hash_password(PASSWORD))
        db.add(user)
        db.commit()
        user_id = user.id
    yield pg_engine, user_id
    with pg_engine.begin() as connection:
        connection.execute(text('TRUNCATE TABLE users CASCADE'))


def seed(engine, user_id, count, *, expired=0, revoked=0, tied=False):
    now = datetime.now(timezone.utc)
    tokens = []
    # Reverse insertion order deliberately tests the UUID tie-breaker.
    with Session(engine) as db:
        for index in reversed(range(count + expired + revoked)):
            sid = uuid.UUID(int=index + 1)
            token = security.create_refresh_token(user_id, sid)
            row = AuthSession(id=sid, user_id=user_id,
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                created_at=now - timedelta(days=2) + timedelta(seconds=0 if tied else index),
                expires_at=now + timedelta(days=2), revoked_at=None)
            if count <= index < count + expired:
                row.expires_at = now - timedelta(seconds=1)
            if index >= count + expired:
                row.revoked_at = now - timedelta(days=1)
            db.add(row)
            tokens.append((sid, token))
        db.commit()
    return dict(tokens)


def login(db):
    return service.login(db, LoginRequest(email='sessions@example.com', password=PASSWORD))


def count_valid(engine, user_id):
    with Session(engine) as db:
        return repo.count_active_for_user(db, user_id, now=datetime.now(timezone.utc))


@pytest.mark.parametrize('existing', [0, 3, 4, 5, 8])
def test_issuance_capacity_and_oldest_eviction(account, existing):
    engine, uid = account
    seed(engine, uid, existing)
    with Session(engine) as db:
        tokens = login(db)
        db.commit()
    assert count_valid(engine, uid) == min(existing + 1, 5)
    with Session(engine) as db:
        rows = db.scalars(select(AuthSession).where(AuthSession.user_id == uid)
                          .order_by(AuthSession.created_at, AuthSession.id)).all()
        assert len(rows) == existing + 1  # preserve historical records
        excess = max(existing + 1 - 5, 0)
        assert all(row.revoked_at is not None for row in rows[:excess])
        assert all(row.revoked_at is None for row in rows[excess:])
        new_id = uuid.UUID(security.decode_token(tokens.refresh_token, expected_type='refresh')['sid'])
        assert db.get(AuthSession, new_id).revoked_at is None


def test_expired_and_revoked_sessions_do_not_consume_capacity(account):
    engine, uid = account
    seed(engine, uid, 4, expired=3, revoked=2)
    with Session(engine) as db:
        before = {r.id: r.revoked_at for r in db.scalars(select(AuthSession))}
        login(db)
        db.commit()
    assert count_valid(engine, uid) == 5
    with Session(engine) as db:
        assert all(db.get(AuthSession, sid).revoked_at == value for sid, value in before.items())


def test_expiry_boundary_and_deterministic_eviction(account):
    engine, uid = account
    seed(engine, uid, 7, tied=True)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        boundary = db.get(AuthSession, uuid.UUID(int=1))
        boundary.expires_at = now
        db.flush()
        assert repo.enforce_session_limit(db, uid, 5, now) == 1
        db.commit()
    with Session(engine) as db:
        assert db.get(AuthSession, uuid.UUID(int=1)).revoked_at is None
        assert db.get(AuthSession, uuid.UUID(int=2)).revoked_at == now
        assert db.get(AuthSession, uuid.UUID(int=3)).revoked_at is None


@pytest.mark.parametrize('operation', ['login', 'refresh'])
def test_failure_rolls_back_creation_eviction_and_rotation(account, monkeypatch, operation):
    engine, uid = account
    old = seed(engine, uid, 5)
    with Session(engine) as db:
        before = {r.id: (r.token_hash, r.revoked_at) for r in db.scalars(select(AuthSession))}
    def fail(*args, **kwargs):
        raise RuntimeError('injected signing failure')
    with monkeypatch.context() as patch:
        patch.setattr(security, 'create_access_token', fail)
        with Session(engine) as db:
            with pytest.raises(RuntimeError, match='injected'):
                if operation == 'login':
                    login(db)
                else:
                    service.refresh(db, old[uuid.UUID(int=1)])
            # Changes must have happened inside the still-open transaction.
            assert db.scalar(select(AuthSession).where(AuthSession.revoked_at.is_not(None))) is not None
            db.rollback()
    with Session(engine) as db:
        assert {r.id: (r.token_hash, r.revoked_at) for r in db.scalars(select(AuthSession))} == before
        service.refresh(db, old[uuid.UUID(int=1)])
        db.commit()
    assert count_valid(engine, uid) == 5


@pytest.mark.parametrize('operations', [('login', 'login'), ('login', 'refresh'),
                                       ('refresh', 'refresh'), ('same', 'same')])
def test_concurrent_issuance_and_replay(account, operations):
    engine, uid = account
    tokens = seed(engine, uid, 5)
    barrier = threading.Barrier(2)
    def worker(index, operation):
        with Session(engine) as db:
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            db.execute(text("SET LOCAL statement_timeout = '10s'"))
            assert db.scalar(text('SHOW transaction_isolation')) == 'read committed'
            barrier.wait(timeout=10)
            try:
                result = login(db) if operation == 'login' else service.refresh(
                    db, tokens[uuid.UUID(int=1 if operation == 'same' else index + 1)])
                db.commit()
                assert count_valid(engine, uid) <= 5
                return result
            except service.AuthCredentialsError:
                db.rollback()
                return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, index, op) for index, op in enumerate(operations)]
        results = [future.result(timeout=20) for future in futures]
    assert count_valid(engine, uid) == 5
    assert sum(r is not None for r in results) == (1 if operations[0] == 'same' else 2)
    for index, op in enumerate(operations):
        if op in ('refresh', 'same'):
            with Session(engine) as db, pytest.raises(service.AuthCredentialsError):
                service.refresh(db, tokens[uuid.UUID(int=1 if op == 'same' else index + 1)])


def test_refresh_rechecks_preloaded_session_after_rotation(account):
    engine, uid = account
    tokens = seed(engine, uid, 1)
    with Session(engine, expire_on_commit=False) as stale:
        row = stale.get(AuthSession, uuid.UUID(int=1))
        assert row.revoked_at is None
        with Session(engine) as winner:
            service.refresh(winner, tokens[row.id])
            winner.commit()
        with pytest.raises(service.AuthCredentialsError):
            service.refresh(stale, tokens[row.id])


def test_eight_concurrent_logins_never_exceed_capacity(account):
    engine, uid = account
    barrier = threading.Barrier(8)
    def worker():
        with Session(engine, autoflush=False) as db:
            db.execute(text("SET LOCAL lock_timeout = '10s'"))
            db.execute(text("SET LOCAL statement_timeout = '15s'"))
            barrier.wait(timeout=10)
            login(db)
            db.commit()
            assert count_valid(engine, uid) <= 5
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker) for _ in range(8)]
        for future in futures:
            future.result(timeout=25)
    assert count_valid(engine, uid) == 5
    with Session(engine) as db:
        rows = db.scalars(select(AuthSession).where(AuthSession.user_id == uid)).all()
        assert len(rows) == 8
        assert sum(row.revoked_at is not None for row in rows) == 3


def test_old_transaction_does_not_evict_its_newly_issued_session(account):
    engine, uid = account
    with Session(engine, autoflush=False) as delayed:
        # PostgreSQL's now()/server default is fixed at transaction start.
        delayed.execute(text('SELECT transaction_timestamp()'))
        with Session(engine) as fast:
            for _ in range(5):
                login(fast)
                fast.commit()
        tokens = login(delayed)
        sid = uuid.UUID(security.decode_token(tokens.refresh_token, expected_type='refresh')['sid'])
        assert delayed.get(AuthSession, sid).revoked_at is None
        delayed.commit()
    assert count_valid(engine, uid) == 5


@pytest.mark.parametrize('revocation', ['logout', 'revoke_all'])
def test_concurrent_refresh_and_revocation_have_no_lock_cycle(account, revocation):
    engine, uid = account
    token = seed(engine, uid, 1)[uuid.UUID(int=1)]
    barrier = threading.Barrier(2)
    def worker(refreshing):
        with Session(engine, autoflush=False) as db:
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            barrier.wait(timeout=10)
            try:
                if refreshing:
                    service.refresh(db, token)
                elif revocation == 'logout':
                    service.logout(db, token, expected_user_id=uid)
                else:
                    service.revoke_all_sessions(db, uid)
                db.commit()
                return True
            except service.AuthCredentialsError:
                db.rollback()
                return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, refreshing) for refreshing in (True, False)]
        results = [future.result(timeout=15) for future in futures]
    if revocation == 'revoke_all':
        assert results[1]
        assert count_valid(engine, uid) == 0
    else:
        assert sum(results) == 1
        assert count_valid(engine, uid) == int(results[0])


@pytest.mark.parametrize('field,value', [('status', 'disabled'), ('role', 'teacher'),
                                        ('password_hash', 'replacement-hash')])
def test_bulk_revocation_preserves_pending_account_changes(account, field, value):
    engine, uid = account
    seed(engine, uid, 2)
    with Session(engine, autoflush=False) as db:
        user = db.get(User, uid)
        setattr(user, field, value)
        assert service.revoke_all_sessions(db, uid) == 2
        db.commit()
    with Session(engine) as db:
        assert getattr(db.get(User, uid), field) == value
    assert count_valid(engine, uid) == 0


@pytest.mark.parametrize('operation', ['login', 'refresh', 'logout', 'revoke_all'])
def test_account_lock_precedes_session_writes_and_is_account_scoped(account, operation):
    engine, uid = account
    tokens = seed(engine, uid, 5)
    with Session(engine) as db:
        other = User(email='other@example.com', role='student', status='active',
                     password_hash=security.hash_password(PASSWORD))
        db.add(other)
        db.commit()
        other_id = other.id
    started = threading.Event()
    pid = []
    def worker():
        with Session(engine) as db:
            db.execute(text("SET LOCAL lock_timeout = '8s'"))
            pid.append(db.scalar(text('SELECT pg_backend_pid()')))
            started.set()
            if operation == 'login':
                login(db)
            elif operation == 'refresh':
                service.refresh(db, tokens[uuid.UUID(int=1)])
            elif operation == 'logout':
                service.logout(db, tokens[uuid.UUID(int=1)], expected_user_id=uid)
            else:
                service.revoke_all_sessions(db, uid)
            db.commit()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(engine) as holder:
            holder.execute(select(User.id).where(User.id == uid).with_for_update(key_share=True))
            future = pool.submit(worker)
            assert started.wait(5)
            deadline = time.monotonic() + 5
            waiting = False
            while time.monotonic() < deadline and not future.done():
                with engine.connect() as observer:
                    waiting = observer.execute(text(
                        "SELECT wait_event_type = 'Lock' AND query ILIKE '%users%' "
                        "FROM pg_stat_activity WHERE pid=:pid"), {'pid': pid[0]}).scalar()
                if waiting:
                    break
                time.sleep(0.02)
            assert waiting, 'operation did not wait for the owning user before session writes'
            # Another account can issue tokens while this account is blocked.
            with Session(engine) as independent:
                independent.execute(text("SET LOCAL lock_timeout = '1s'"))
                service._issue_session(independent, independent.get(User, other_id), datetime.now(timezone.utc))
                independent.commit()
            holder.commit()
        future.result(timeout=12)
    assert count_valid(engine, other_id) == 1
    assert count_valid(engine, uid) == (0 if operation == 'revoke_all' else 4 if operation == 'logout' else 5)
