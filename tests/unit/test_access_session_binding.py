"""AUTH-REF-002 access JWT/session binding regressions."""
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core import security
from app.core.database import Base
import app.models  # noqa: F401
from app.core.auth_dependencies import get_current_user
from app.models.auth_session import AuthSession
from app.models.user import User


@pytest.fixture
def session():
    engine = create_engine('sqlite+pysqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def invoke_current_user(token, session):
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    return get_current_user(request, credentials, session)


def test_bound_access_token_accepts_its_live_session(session):
    user = User(email='access@example.com', role='student', status='active', password_hash='x')
    session.add(user)
    session.flush()
    row = AuthSession(user_id=user.id, token_hash='access-hash', expires_at=datetime.now(timezone.utc)+timedelta(days=1))
    session.add(row)
    session.flush()
    token = security.create_access_token(user.id, user.role, session_id=row.id)
    assert invoke_current_user(token, session).id == user.id


@pytest.mark.parametrize('case', ['missing', 'malformed', 'foreign', 'missing_row', 'revoked', 'expired'])
def test_invalid_access_session_is_generic_401(session, case):
    user = User(email='access@example.com', role='student', status='active', password_hash='x')
    other = User(email='other@example.com', role='student', status='active', password_hash='x')
    session.add_all([user, other])
    session.flush()
    sid = uuid.uuid4()
    if case in {'revoked', 'expired', 'foreign'}:
        sid_user = other.id if case == 'foreign' else user.id
        row = AuthSession(id=sid, user_id=sid_user, token_hash=case,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            revoked_at=datetime.now(timezone.utc) if case == 'revoked' else None)
        if case == 'expired':
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(row)
        session.flush()
    token = security.create_access_token(user.id, user.role, session_id=sid)
    if case == 'missing':
        import jwt
        token = jwt.encode({'sub': str(user.id), 'role': user.role, 'typ': 'access',
            'jti': str(uuid.uuid4()), 'iat': datetime.now(timezone.utc),
            'exp': datetime.now(timezone.utc) + timedelta(minutes=2)}, security.get_settings().jwt_secret, algorithm='HS256')
    if case == 'malformed':
        import jwt
        claims = security.decode_token(token, expected_type='access')
        claims['sid'] = 'not-a-uuid'
        token = jwt.encode(claims, security.get_settings().jwt_secret, algorithm='HS256')
    with pytest.raises(HTTPException) as error:
        invoke_current_user(token, session)
    assert error.value.status_code == 401
    assert error.value.detail == 'Not authenticated'
