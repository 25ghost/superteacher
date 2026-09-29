"""Unit tests: role authorization (the three-role vocabulary, in-memory SQLite).

Covers the whole authorization contract without touching PostgreSQL:

- the vocabulary is exactly ``student`` / ``teacher`` / ``admin``
  (enum, model CHECK, guard set),
- a protected route can require one or more roles
  (student-only, teacher-only, admin-only, teacher/admin),
- missing, malformed, expired, wrongly-typed, forged or unusable
  credentials are rejected with **401**,
- an authenticated caller without one of the required roles is rejected
  with **403**,
- authorization always reads the role from the database — the ``role``
  claim inside the JWT and any client-supplied id are never trusted.

The probe routes below stand in for future endpoints so the guarantee can
be asserted without shipping placeholder API surface.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import security
from app.core.auth_dependencies import (
    ALLOWED_ROLES,
    get_current_user,
    require_admin,
    require_role,
    require_student,
    require_teacher,
)
from app.core.database import Base, get_db
from app.models.enums import UserRole, UserStatus
from app.models.user import User
import app.models  # noqa: F401  (registers every table)


PASSWORD_HASH = security.hash_password("correct horse battery staple")


# --- scaffolding ---------------------------------------------------------------------


@pytest.fixture()
def session() -> Session:
    # StaticPool + check_same_thread=False: TestClient runs the app in a
    # worker thread, and an in-memory SQLite database must be the SAME
    # database in both threads.
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture()
def probe_app(session: Session) -> FastAPI:
    """A tiny app whose routes exercise every guard combination."""
    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: session

    @app.get("/any")
    def any_authenticated(user: User = Depends(get_current_user)) -> dict:
        return _as_dict(user)

    @app.get("/student-only")
    def student_only(user: User = Depends(require_student)) -> dict:
        return _as_dict(user)

    @app.get("/teacher-only")
    def teacher_only(user: User = Depends(require_teacher)) -> dict:
        return _as_dict(user)

    @app.get("/admin-only")
    def admin_only(user: User = Depends(require_admin)) -> dict:
        return _as_dict(user)

    @app.get("/teacher-or-admin")
    def teacher_or_admin(
        user: User = Depends(require_role(UserRole.TEACHER, UserRole.ADMIN)),
    ) -> dict:
        return _as_dict(user)

    return app


def _as_dict(user: User) -> dict:
    return {"user_id": str(user.id), "role": user.role}


@pytest.fixture()
def client(probe_app: FastAPI) -> TestClient:
    return TestClient(probe_app)


def _make_user(
    session: Session,
    *,
    role: str,
    status: str = UserStatus.ACTIVE.value,
    email: str | None = None,
) -> User:
    user = User(
        email=email or f"{uuid.uuid4().hex[:10]}@example.com",
        role=role,
        status=status,
        password_hash=PASSWORD_HASH,
    )
    session.add(user)
    session.commit()
    return user


def _header(user: User, *, role: str | None = None) -> dict:
    """A Bearer header for ``user``; ``role`` overrides the JWT claim only."""
    token = security.create_access_token(user.id, role if role is not None else user.role)
    return {"Authorization": f"Bearer {token}"}


# --- vocabulary ----------------------------------------------------------------------


def test_role_vocabulary_is_exactly_three() -> None:
    assert [member.value for member in UserRole] == ["student", "teacher", "admin"]
    assert ALLOWED_ROLES == {"student", "teacher", "admin"}


def test_users_role_check_matches_the_three_role_vocabulary() -> None:
    checks = [
        constraint
        for constraint in User.__table__.constraints
        if constraint.name == "users_role_check"
    ]
    assert len(checks) == 1
    text = str(checks[0].sqltext)
    for role in ("student", "teacher", "admin"):
        assert f"'{role}'" in text
    for legacy in ("parent", "school_admin", "rahura_admin"):
        assert legacy not in text


def test_guards_only_accept_allowed_roles() -> None:
    assert require_student is not None
    assert require_teacher is not None
    assert require_admin is not None
    with pytest.raises(ValueError, match="at least one role"):
        require_role()
    with pytest.raises(ValueError, match="UserRole member"):
        require_role("admin")  # type: ignore[arg-type]  (raw string, not an enum)


# --- happy path: each guard admits exactly its roles ----------------------------------


@pytest.mark.parametrize(
    ("route", "allowed", "refused"),
    [
        ("/student-only", {"student"}, {"teacher", "admin"}),
        ("/teacher-only", {"teacher"}, {"student", "admin"}),
        ("/admin-only", {"admin"}, {"student", "teacher"}),
        ("/teacher-or-admin", {"teacher", "admin"}, {"student"}),
        ("/any", {"student", "teacher", "admin"}, set()),
    ],
)
def test_role_matrix(session: Session, client: TestClient, route, allowed, refused) -> None:
    for role in sorted(allowed):
        user = _make_user(session, role=role)
        response = client.get(route, headers=_header(user))
        assert response.status_code == 200, response.text
        assert response.json() == {"user_id": str(user.id), "role": role}
        session.delete(user)
        session.commit()

    for role in sorted(refused):
        user = _make_user(session, role=role)
        response = client.get(route, headers=_header(user))
        assert response.status_code == 403, (route, role, response.text)
        session.delete(user)
        session.commit()


def test_admin_guard_names_the_required_role(session: Session, client: TestClient) -> None:
    user = _make_user(session, role=UserRole.TEACHER.value)
    response = client.get("/admin-only", headers=_header(user))
    assert response.status_code == 403
    assert response.json()["detail"] == "administrator role required for this operation"


# --- 401: authentication failures -----------------------------------------------------


def test_missing_credentials_are_401(client: TestClient) -> None:
    for route in ("/any", "/student-only", "/teacher-only", "/admin-only", "/teacher-or-admin"):
        response = client.get(route)
        assert response.status_code == 401, route
        assert response.json()["detail"] == "Not authenticated"
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header_value",
    [
        "Bearer",
        "Bearer not-a-jwt",
        "Bearer a.b.c",
        "Basic dXNlcjpwYXNz",
    ],
)
def test_malformed_credentials_are_401(client: TestClient, header_value: str) -> None:
    response = client.get("/any", headers={"Authorization": header_value})
    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated"


def test_expired_token_is_401(session: Session, client: TestClient) -> None:
    user = _make_user(session, role=UserRole.STUDENT.value)
    now = datetime.now(timezone.utc)
    expired = pyjwt.encode(
        {
            "sub": str(user.id),
            "role": user.role,
            "typ": "access",
            "jti": str(uuid.uuid4()),
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(minutes=1),
        },
        security.get_settings().jwt_secret,
        algorithm="HS256",
    )
    response = client.get("/any", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated"


def test_forged_signature_is_401(session: Session, client: TestClient) -> None:
    user = _make_user(session, role=UserRole.ADMIN.value)
    forged = pyjwt.encode(
        {
            "sub": str(user.id),
            "role": "admin",
            "typ": "access",
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        "attacker-secret",
        algorithm="HS256",
    )
    response = client.get("/admin-only", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_refresh_token_cannot_call_protected_routes(session: Session, client: TestClient) -> None:
    user = _make_user(session, role=UserRole.ADMIN.value)
    refresh = pyjwt.encode(
        {
            "sub": str(user.id),
            "sid": str(uuid.uuid4()),
            "typ": "refresh",
            "jti": str(uuid.uuid4()),
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(days=1),
        },
        security.get_settings().jwt_secret,
        algorithm="HS256",
    )
    response = client.get("/admin-only", headers={"Authorization": f"Bearer {refresh}"})
    assert response.status_code == 401


def test_token_for_unknown_user_is_401(session: Session, client: TestClient) -> None:
    ghost_id = uuid.uuid4()
    token = security.create_access_token(ghost_id, UserRole.ADMIN.value)
    response = client.get("/admin-only", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


@pytest.mark.parametrize("status_value", ["suspended", "disabled"])
def test_non_active_accounts_are_401(
    session: Session, client: TestClient, status_value: str
) -> None:
    user = _make_user(session, role=UserRole.TEACHER.value, status=status_value)
    response = client.get("/teacher-only", headers=_header(user))
    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated"


# --- the token's role claim is never trusted ------------------------------------------


def test_role_claim_in_token_cannot_escalate(session: Session, client: TestClient) -> None:
    """The DB says student — a forged ``role`` claim grants nothing."""
    user = _make_user(session, role=UserRole.STUDENT.value)

    as_admin = client.get("/admin-only", headers=_header(user, role="admin"))
    assert as_admin.status_code == 403

    as_teacher = client.get("/teacher-only", headers=_header(user, role="teacher"))
    assert as_teacher.status_code == 403

    # The genuine role still works.
    assert client.get("/student-only", headers=_header(user)).status_code == 200


def test_demoted_user_loses_access_on_next_request(session: Session, client: TestClient) -> None:
    """A role change in the database takes effect immediately."""
    user = _make_user(session, role=UserRole.ADMIN.value)
    assert client.get("/admin-only", headers=_header(user)).status_code == 200

    user.role = UserRole.STUDENT.value
    session.commit()

    # The old token still decodes, but the database now says student.
    assert client.get("/admin-only", headers=_header(user, role="admin")).status_code == 403
    assert client.get("/student-only", headers=_header(user, role="admin")).status_code == 200
