"""The cross-role endpoint matrix over every real v1 route (Phase B, slice 6).

The suite drives the *actual* routers (``api_router``, not probe routes)
with four principals — anonymous, student, teacher, administrator — and
pins the contract that makes ``require_teacher`` "real":

- anonymous → **401** on every protected route, never 403;
- a wrong-role caller → **403 from the guard before the handler runs**
  (empty bodies are intentional: role guards are dependencies, and
  FastAPI solves dependencies before validating the request body, so a
  missing guard surfaces as a 422/404/200 where a 403 is required);
- role-named 403 details name the namespace that refused the call
  (``administrator role required...`` / ``teacher role required...``);
- the teacher's own set — identity reads, profile read/update, login,
  change-password, logout, deactivate — works end to end, privilege
  escalation through the profile body is 422, and deactivation is
  DB-authoritative (the same bearer token goes from 200 to 401);
- public routes never answer 401/403.

Removing ``require_admin`` / ``require_teacher`` / ``get_current_student``
from any route fails the corresponding test.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.router import api_router
from app.core import security
from app.core.database import Base, get_db
from app.core.rate_limit import limiter
from app.models.enums import UserRole, UserStatus
from app.models.teacher import Teacher
from app.models.user import User
import app.models  # noqa: F401  (registers every table)

PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "a completely different password"

ADMIN_DETAIL = "administrator role required for this operation"
TEACHER_DETAIL = "teacher role required for this operation"

# --- the matrix ---------------------------------------------------------------------


STUDENT_ROUTES: list[tuple[str, str, dict | None]] = [
    ("GET", "/api/v1/me/student", None),
    ("POST", "/api/v1/me/student", {}),
    ("PATCH", "/api/v1/me/student", {}),
    ("POST", "/api/v1/me/registrations", {}),
    ("GET", "/api/v1/me/registrations", None),
    ("GET", "/api/v1/me/registrations/{enrollment_id}", None),
    # Phase 1 marketplace + learning enrollments (student-only).
    ("GET", "/api/v1/marketplace/offerings", None),
    ("POST", "/api/v1/marketplace/offerings/{offering_id}/enroll", None),
    ("GET", "/api/v1/me/learning-enrollments", None),
    ("GET", "/api/v1/me/learning-enrollments/{enrollment_id}", None),
    ("POST", "/api/v1/me/learning-enrollments/{enrollment_id}/leave", None),
    # Phase 2 slice 2C — student content access + basic progress.
    ("GET", "/api/v1/me/learning-content/{enrollment_id}", None),
    ("GET", "/api/v1/me/learning-content/{enrollment_id}/topics", None),
    ("GET", "/api/v1/me/learning-content/{enrollment_id}/lessons", None),
    ("GET", "/api/v1/me/learning-content/{enrollment_id}/materials", None),
    ("GET", "/api/v1/me/learning-content/{enrollment_id}/materials/{material_id}", None),
    ("GET", "/api/v1/me/learning-content/{enrollment_id}/materials/{material_id}/content", None),
    ("GET", "/api/v1/me/learning-content/{enrollment_id}/materials/{material_id}/progress", None),
    ("PUT", "/api/v1/me/learning-content/{enrollment_id}/materials/{material_id}/progress", {"status": "in_progress"}),
    # Phase 3 slice 3A — the student's view of online classes (student-only).
    ("GET", "/api/v1/me/classes", None),
    ("GET", "/api/v1/me/classes/{class_id}", None),
    # Phase 3 slice 3B — own attendance + class transcript (student-only).
    ("GET", "/api/v1/me/classes/{class_id}/attendance", None),
    ("GET", "/api/v1/me/classes/{class_id}/transcript", None),
    # Phase 3 slice 3C — single-use WebSocket ticket (student-only).
    ("POST", "/api/v1/me/classes/{class_id}/ws-ticket", None),
]

TEACHER_ROUTES: list[tuple[str, str, dict | None]] = [
    ("GET", "/api/v1/me/teacher", None),
    ("PATCH", "/api/v1/me/teacher", {}),
    # Phase 1 teaching offerings (teacher-only).
    ("POST", "/api/v1/me/teacher/offerings", {}),
    ("GET", "/api/v1/me/teacher/offerings", None),
    ("GET", "/api/v1/me/teacher/offerings/{offering_id}", None),
    # The offering-scoped roster: the only student visibility a teacher has.
    ("GET", "/api/v1/me/teacher/offerings/{offering_id}/students", None),
    ("PATCH", "/api/v1/me/teacher/offerings/{offering_id}", {}),
    # Phase 2 slice 2A — topics and lessons under an offering (teacher-only).
    ("POST", "/api/v1/me/teacher/offerings/{offering_id}/topics", {}),
    ("GET", "/api/v1/me/teacher/offerings/{offering_id}/topics", None),
    ("GET", "/api/v1/me/teacher/offerings/{offering_id}/topics/{topic_id}", None),
    ("PATCH", "/api/v1/me/teacher/offerings/{offering_id}/topics/{topic_id}", {}),
    ("DELETE", "/api/v1/me/teacher/offerings/{offering_id}/topics/{topic_id}", None),
    ("POST", "/api/v1/me/teacher/offerings/{offering_id}/topics/{topic_id}/lessons", {}),
    ("GET", "/api/v1/me/teacher/offerings/{offering_id}/topics/{topic_id}/lessons", None),
    ("GET", "/api/v1/me/teacher/offerings/{offering_id}/topics/{topic_id}/lessons/{lesson_id}", None),
    ("PATCH", "/api/v1/me/teacher/offerings/{offering_id}/topics/{topic_id}/lessons/{lesson_id}", {}),
    ("DELETE", "/api/v1/me/teacher/offerings/{offering_id}/topics/{topic_id}/lessons/{lesson_id}", None),
    # Phase 2 slice 2B — teaching materials under an offering (teacher-only).
    ("POST", "/api/v1/me/teacher/offerings/{offering_id}/materials", {}),
    ("GET", "/api/v1/me/teacher/offerings/{offering_id}/materials", None),
    ("GET", "/api/v1/me/teacher/offerings/{offering_id}/materials/{material_id}", None),
    ("PATCH", "/api/v1/me/teacher/offerings/{offering_id}/materials/{material_id}", {}),
    ("POST", "/api/v1/me/teacher/offerings/{offering_id}/materials/{material_id}/submit", None),
    ("POST", "/api/v1/me/teacher/offerings/{offering_id}/materials/{material_id}/revise", None),
    ("POST", "/api/v1/me/teacher/offerings/{offering_id}/materials/{material_id}/archive", None),
    ("DELETE", "/api/v1/me/teacher/offerings/{offering_id}/materials/{material_id}", None),
    # Phase 3 slice 3A — online classes under an offering (teacher-only).
    ("POST", "/api/v1/me/teacher/offerings/{offering_id}/classes", {}),
    ("GET", "/api/v1/me/teacher/offerings/{offering_id}/classes", None),
    ("GET", "/api/v1/me/teacher/offerings/{offering_id}/classes/{class_id}", None),
    ("PATCH", "/api/v1/me/teacher/offerings/{offering_id}/classes/{class_id}", {}),
    ("POST", "/api/v1/me/teacher/offerings/{offering_id}/classes/{class_id}/start", None),
    ("POST", "/api/v1/me/teacher/offerings/{offering_id}/classes/{class_id}/end", None),
    ("POST", "/api/v1/me/teacher/offerings/{offering_id}/classes/{class_id}/cancel", None),
    # Phase 3 slice 3B — participant roster + derived attendance +
    # historical transcript of an ENDED class (teacher-only).
    ("GET", "/api/v1/me/teacher/offerings/{offering_id}/classes/{class_id}/participants", None),
    ("GET", "/api/v1/me/teacher/offerings/{offering_id}/classes/{class_id}/attendance", None),
    ("GET", "/api/v1/me/teacher/offerings/{offering_id}/classes/{class_id}/transcript", None),
    # Phase 3 slice 3C — single-use WebSocket ticket (teacher-only).
    ("POST", "/api/v1/me/teacher/offerings/{offering_id}/classes/{class_id}/ws-ticket", None),
]

ADMIN_ROUTES: list[tuple[str, str, dict | None]] = [
    ("GET", "/api/v1/admin/students", None),
    ("POST", "/api/v1/admin/students", {}),
    ("GET", "/api/v1/admin/students/{student_id}", None),
    ("PATCH", "/api/v1/admin/students/{student_id}", {}),
    ("GET", "/api/v1/admin/students/{student_id}/history", None),
    ("POST", "/api/v1/admin/teachers", {}),
    ("GET", "/api/v1/admin/teachers", None),
    ("POST", "/api/v1/admin/teachers/{user_id}/invite", {}),
    ("POST", "/api/v1/admin/teachers/{user_id}/activate", {}),
    ("POST", "/api/v1/admin/teachers/{user_id}/deactivate", {}),
    ("PATCH", "/api/v1/admin/teachers/{user_id}/school", {"school_id": None}),
    # Phase 1 verification axis (admin-only).
    ("PATCH", "/api/v1/admin/teachers/{user_id}/verification", {"status": "approved"}),
    ("GET", "/api/v1/admin/users", None),
    ("GET", "/api/v1/admin/users/{user_id}", None),
    ("POST", "/api/v1/admin/users/{user_id}/deactivate", None),
    ("POST", "/api/v1/admin/users/{user_id}/activate", None),
    ("PATCH", "/api/v1/admin/users/{user_id}/role", {}),
    ("POST", "/api/v1/admin/users/{user_id}/unlock", {}),
    ("GET", "/api/v1/admin/registrations", None),
    ("POST", "/api/v1/admin/registrations", {}),
    ("GET", "/api/v1/admin/registrations/{enrollment_id}", None),
    ("PATCH", "/api/v1/admin/registrations/{enrollment_id}/status", {}),
    ("GET", "/api/v1/admin/students/{student_id}/registrations", None),
    # Phase 2 slice 2B — material moderation (admin-only).
    ("GET", "/api/v1/admin/materials", None),
    ("GET", "/api/v1/admin/materials/{material_id}", None),
    ("POST", "/api/v1/admin/materials/{material_id}/approve", None),
    ("POST", "/api/v1/admin/materials/{material_id}/reject", {"reason": "not allowed"}),
    ("POST", "/api/v1/admin/materials/{material_id}/archive", None),
]

ANY_ROLE_ROUTES: list[tuple[str, str, dict | None]] = [
    ("POST", "/api/v1/auth/logout", {"refresh_token": "not-a-real-refresh"}),
    ("GET", "/api/v1/auth/me", None),
    ("GET", "/api/v1/me", None),
    ("POST", "/api/v1/me/change-password", {}),
    ("POST", "/api/v1/me/deactivate", {}),
]

PUBLIC_ROUTES: list[tuple[str, str, dict | None]] = [
    ("GET", "/api/v1/health", None),
    ("GET", "/api/v1/health/ready", None),
    ("POST", "/api/v1/auth/register", {}),
    ("POST", "/api/v1/auth/register-teacher", {}),
    ("POST", "/api/v1/auth/login", {}),
    ("POST", "/api/v1/auth/refresh", {}),
    ("POST", "/api/v1/auth/forgot-password", {}),
    ("POST", "/api/v1/auth/reset-password", {}),
    ("POST", "/api/v1/auth/accept-invite", {}),
    ("GET", "/api/v1/registrations/readiness", None),
    ("GET", "/api/v1/catalog/academic-years", None),
    ("GET", "/api/v1/catalog/pathways", None),
    ("GET", "/api/v1/catalog/education-levels", None),
    ("GET", "/api/v1/catalog/subjects", None),
    ("GET", "/api/v1/catalog/programs", None),
    ("GET", "/api/v1/catalog/program-versions", None),
    ("GET", "/api/v1/catalog/tvet/sectors", None),
    ("GET", "/api/v1/catalog/tvet/programs", None),
    ("GET", "/api/v1/catalog/schools", None),
    ("GET", "/api/v1/catalog/schools/{school_code}/programs", None),
]

PROTECTED_ROUTES = (
    STUDENT_ROUTES + TEACHER_ROUTES + ADMIN_ROUTES + ANY_ROLE_ROUTES
)


# --- scaffolding ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_rate_limiter():
    """Every test starts with a full slowapi bucket (unit tests share one process)."""
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture()
def session() -> Session:
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
def api_app(session: Session) -> FastAPI:
    """The real, complete v1 router mounted on a probe app."""
    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: session
    app.include_router(api_router, prefix="/api/v1")
    return app


@pytest.fixture()
def api_client(api_app: FastAPI) -> TestClient:
    return TestClient(api_app)


@pytest.fixture()
def principals(session: Session) -> dict[str, User]:
    """One active account per role; the teacher has a profile row."""
    users: dict[str, User] = {}
    for role in (UserRole.STUDENT, UserRole.TEACHER, UserRole.ADMIN):
        user = User(
            email=f"{role.value}-{uuid.uuid4().hex[:8]}@matrix.example.com",
            role=role.value,
            status=UserStatus.ACTIVE.value,
            password_hash=security.hash_password(PASSWORD),
        )
        session.add(user)
        users[role.value] = user
    session.flush()
    session.add(
        Teacher(
            user_id=users[UserRole.TEACHER.value].id,
            full_name="Matrix Teacher",
            subject="Mathematics",
        )
    )
    session.commit()
    return users


def _header(user: User) -> dict:
    return {"Authorization": f"Bearer {security.create_access_token(user.id, user.role)}"}


def _path(template: str) -> str:
    return template.format(
        student_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        enrollment_id=uuid.uuid4(),
        offering_id=uuid.uuid4(),
        topic_id=uuid.uuid4(),
        lesson_id=uuid.uuid4(),
        material_id=uuid.uuid4(),
        class_id=uuid.uuid4(),
        school_code="MISSING",
    )


def _call(
    client: TestClient, route: tuple[str, str, dict | None], headers: dict | None
) -> object:
    method, template, body = route
    kwargs = {"headers": headers} if headers else {}
    if body is not None:
        kwargs["json"] = body
    return client.request(method, _path(template), **kwargs)


# --- anonymous ------------------------------------------------------------------------


@pytest.mark.parametrize("route", PROTECTED_ROUTES, ids=lambda r: f"{r[0]} {r[1]}")
def test_anonymous_is_refused_on_every_protected_route(
    api_client: TestClient, route: tuple[str, str, dict | None]
) -> None:
    response = _call(api_client, route, headers=None)
    assert response.status_code == 401, (
        f"{route[0]} {route[1]} -> {response.status_code}: {response.text}"
    )


@pytest.mark.parametrize("route", PUBLIC_ROUTES, ids=lambda r: f"{r[0]} {r[1]}")
def test_public_routes_never_answer_401_or_403(
    api_client: TestClient, route: tuple[str, str, dict | None]
) -> None:
    response = _call(api_client, route, headers=None)
    assert response.status_code not in (401, 403), (
        f"{route[0]} {route[1]} -> {response.status_code}: {response.text}"
    )


def test_route_inventory_is_exactly_the_matrix() -> None:
    """No v1 endpoint may exist outside the role matrix — and vice versa.

    The count pin (113) makes adding or removing a route a conscious
    decision: a new endpoint must be listed in one of the five groups
    above or this test names it as unlisted.
    """
    actual = {
        (method, f"/api/v1{route.path}")  # the prefix the app mounts it with
        for route in api_router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if method not in ("HEAD", "OPTIONS")
    }
    listed = {
        (method, template)
        for method, template, _ in PROTECTED_ROUTES + PUBLIC_ROUTES
    }
    assert listed == actual, (
        f"unlisted routes: {sorted(actual - listed)}; "
        f"stale matrix entries: {sorted(listed - actual)}"
    )
    assert len(actual) == 113, f"route count changed: {len(actual)} != 113"


# --- wrong-role callers (guards must answer before the handler) ----------------------


@pytest.mark.parametrize("route", STUDENT_ROUTES, ids=lambda r: f"{r[0]} {r[1]}")
def test_teacher_is_refused_on_student_routes(
    api_client: TestClient, principals: dict[str, User], route: tuple[str, str, dict | None]
) -> None:
    response = _call(api_client, route, _header(principals["teacher"]))
    assert response.status_code == 403, (
        f"{route[0]} {route[1]} -> {response.status_code}: {response.text}"
    )
    assert response.json()["detail"]


@pytest.mark.parametrize("route", ADMIN_ROUTES, ids=lambda r: f"{r[0]} {r[1]}")
def test_teacher_is_refused_on_admin_routes(
    api_client: TestClient, principals: dict[str, User], route: tuple[str, str, dict | None]
) -> None:
    response = _call(api_client, route, _header(principals["teacher"]))
    assert response.status_code == 403, (
        f"{route[0]} {route[1]} -> {response.status_code}: {response.text}"
    )
    assert response.json()["detail"] == ADMIN_DETAIL


@pytest.mark.parametrize("route", TEACHER_ROUTES, ids=lambda r: f"{r[0]} {r[1]}")
def test_student_is_refused_on_teacher_routes(
    api_client: TestClient, principals: dict[str, User], route: tuple[str, str, dict | None]
) -> None:
    response = _call(api_client, route, _header(principals["student"]))
    assert response.status_code == 403, (
        f"{route[0]} {route[1]} -> {response.status_code}: {response.text}"
    )
    assert response.json()["detail"] == TEACHER_DETAIL


@pytest.mark.parametrize("route", ADMIN_ROUTES, ids=lambda r: f"{r[0]} {r[1]}")
def test_student_is_refused_on_admin_routes(
    api_client: TestClient, principals: dict[str, User], route: tuple[str, str, dict | None]
) -> None:
    response = _call(api_client, route, _header(principals["student"]))
    assert response.status_code == 403, (
        f"{route[0]} {route[1]} -> {response.status_code}: {response.text}"
    )
    assert response.json()["detail"] == ADMIN_DETAIL


@pytest.mark.parametrize("route", STUDENT_ROUTES, ids=lambda r: f"{r[0]} {r[1]}")
def test_admin_is_refused_on_student_routes(
    api_client: TestClient, principals: dict[str, User], route: tuple[str, str, dict | None]
) -> None:
    response = _call(api_client, route, _header(principals["admin"]))
    assert response.status_code == 403, (
        f"{route[0]} {route[1]} -> {response.status_code}: {response.text}"
    )
    assert response.json()["detail"]


@pytest.mark.parametrize("route", TEACHER_ROUTES, ids=lambda r: f"{r[0]} {r[1]}")
def test_admin_is_refused_on_teacher_routes(
    api_client: TestClient, principals: dict[str, User], route: tuple[str, str, dict | None]
) -> None:
    response = _call(api_client, route, _header(principals["admin"]))
    assert response.status_code == 403, (
        f"{route[0]} {route[1]} -> {response.status_code}: {response.text}"
    )
    assert response.json()["detail"] == TEACHER_DETAIL


# --- the teacher's own namespace (positive, end to end) -------------------------------


def test_teacher_can_read_identity_and_profile(
    api_client: TestClient, principals: dict[str, User]
) -> None:
    header = _header(principals["teacher"])

    for path in ("/api/v1/me", "/api/v1/auth/me"):
        response = api_client.get(path, headers=header)
        assert response.status_code == 200, f"{path} -> {response.text}"
        assert response.json()["role"] == "teacher"

    profile = api_client.get("/api/v1/me/teacher", headers=header)
    assert profile.status_code == 200, profile.text
    assert profile.json()["full_name"] == "Matrix Teacher"
    assert profile.json()["role"] == "teacher"


def test_teacher_can_update_profile_but_not_widen_the_boundary(
    api_client: TestClient, principals: dict[str, User]
) -> None:
    header = _header(principals["teacher"])

    patched = api_client.patch(
        "/api/v1/me/teacher", json={"subject": "Physics"}, headers=header
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["subject"] == "Physics"

    # Privilege escalation through the body is not expressible.
    for attempt in (
        {"role": "admin"},
        {"status": "active"},
        {"school_id": str(uuid.uuid4())},
        {"teacher_id": str(uuid.uuid4())},
    ):
        response = api_client.patch(
            "/api/v1/me/teacher", json=attempt, headers=header
        )
        assert response.status_code == 422, (
            f"body {attempt} must be refused, got {response.status_code}"
        )
        assert api_client.get("/api/v1/me/teacher", headers=header).json()["role"] == "teacher"


def test_teacher_can_login_change_password_and_logout(
    api_client: TestClient, principals: dict[str, User]
) -> None:
    email = principals["teacher"].email

    login = api_client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    tokens = login.json()
    header = {"Authorization": f"Bearer {tokens['access_token']}"}

    changed = api_client.post(
        "/api/v1/me/change-password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=header,
    )
    assert changed.status_code == 204, changed.text

    assert (
        api_client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        ).status_code
        == 401
    )
    relogin = api_client.post(
        "/api/v1/auth/login", json={"email": email, "password": NEW_PASSWORD}
    )
    assert relogin.status_code == 200, relogin.text

    logout = api_client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers=header,
    )
    assert logout.status_code == 204, logout.text
    refresh = api_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refresh.status_code == 401, refresh.text


def test_teacher_deactivation_is_db_authoritative(
    api_client: TestClient, principals: dict[str, User]
) -> None:
    """The token stays cryptographically valid; the DB status gates it."""
    header = _header(principals["teacher"])
    assert api_client.get("/api/v1/me", headers=header).status_code == 200

    deactivated = api_client.post(
        "/api/v1/me/deactivate", json={"password": PASSWORD}, headers=header
    )
    assert deactivated.status_code == 204, deactivated.text

    after = api_client.get("/api/v1/me", headers=header)
    assert after.status_code == 401, after.text
    assert (
        api_client.get("/api/v1/me/teacher", headers=header).status_code == 401
    )
