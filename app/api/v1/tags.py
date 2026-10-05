"""OpenAPI tag vocabulary — single source of truth for Swagger grouping.

Every API operation carries exactly one tag from this module: the ``TAG_*``
constants are referenced by the endpoint routers, and ``OPENAPI_TAGS`` is
published as ``openapi_tags`` on the FastAPI app so every group shows a
description and keeps this display order.

The access-note helpers describe *who may call* an operation. The role is
derived from the route's actual dependency — never from its path or summary —
so the documentation stays honest if a route ever moves.
"""
import re

from fastapi.routing import APIRoute

from app.core.auth_dependencies import (
    get_current_student,
    get_current_user,
    require_admin,
    require_student,
    require_teacher,
)

TAG_AUTHENTICATION = "Authentication"
TAG_ACCOUNT = "Account"
TAG_STUDENT = "Student"
TAG_TEACHER = "Teacher"
TAG_ADMIN_STUDENTS = "Admin - Students"
TAG_ADMIN_REGISTRATIONS = "Admin - Registrations"
TAG_ADMIN_TEACHERS = "Admin - Teachers"
TAG_ADMIN_USERS = "Admin - Users"
TAG_CATALOG = "Catalog"
TAG_HEALTH = "Health"

#: Display order for Swagger UI — one entry per tag, every tag used at least once.
OPENAPI_TAGS: list[dict[str, str]] = [
    {
        "name": TAG_AUTHENTICATION,
        "description": (
            "Account lifecycle: register, log in, refresh tokens, password "
            "reset and invitation acceptance are public; reading your own "
            "identity (GET /auth/me) and logging out need a Bearer token."
        ),
    },
    {
        "name": TAG_ACCOUNT,
        "description": (
            "Any authenticated user — student, teacher or admin: read your "
            "identity, change your password, deactivate your own account."
        ),
    },
    {
        "name": TAG_STUDENT,
        "description": (
            "Student-only self-service under /me/student and "
            "/me/registrations: maintain your own profile, enroll yourself, "
            "and read your own registrations."
        ),
    },
    {
        "name": TAG_TEACHER,
        "description": (
            "Teacher-only self-service under /me/teacher: read and update "
            "your own teacher profile."
        ),
    },
    {
        "name": TAG_ADMIN_STUDENTS,
        "description": (
            "Administrator-only maintenance of student profiles under "
            "/admin/students: create, list, retrieve and patch profiles, and "
            "read their field-level change history."
        ),
    },
    {
        "name": TAG_ADMIN_REGISTRATIONS,
        "description": (
            "Administrator-only enrollment administration under "
            "/admin/registrations: register a student on their behalf, list "
            "and retrieve registrations, change a registration's status and "
            "list any student's registrations."
        ),
    },
    {
        "name": TAG_ADMIN_TEACHERS,
        "description": (
            "Administrator-only teacher account lifecycle under "
            "/admin/teachers: create an account, list them, re-send "
            "invitations, activate and deactivate."
        ),
    },
    {
        "name": TAG_ADMIN_USERS,
        "description": (
            "Administrator-only account administration under /admin/users: "
            "change an account's role and clear a failed-login lockout."
        ),
    },
    {
        "name": TAG_CATALOG,
        "description": (
            "Public, read-only reference data the portal needs during "
            "registration: pathways, education levels, subjects, programs, "
            "TVET and school catalogs, the open academic year, and the "
            "registration readiness report."
        ),
    },
    {
        "name": TAG_HEALTH,
        "description": (
            "Liveness and readiness probes for orchestration and monitoring. "
            "Both are public; only readiness touches the database."
        ),
    },
]

#: Top-level route dependency → the role the access note reports.
_GUARD_ROLES: tuple[tuple[object, str], ...] = (
    (require_admin, "admin"),
    (require_student, "student"),
    (get_current_student, "student"),
    (require_teacher, "teacher"),
    (get_current_user, "any authenticated user"),
)


def access_note(role: str) -> str:
    """First line of an operation's description: who may call it."""
    return f"Access: {role}"


def access_note_for(route: APIRoute) -> str:
    """Derive ``access_note`` from a route's own dependency.

    Only the route's *top-level* dependencies are inspected: a nested
    ``get_current_user`` inside a role guard would otherwise mislabel every
    admin route as merely "authenticated". Routes with no identity dependency
    are public.
    """
    for dependency in route.dependant.dependencies:
        for guard, role in _GUARD_ROLES:
            if dependency.call is guard:
                return access_note(role)
    return access_note("public")


def _tag_slug(tag: str) -> str:
    """Lowercase the tag and collapse every non-alphanumeric run to ``-``."""
    return re.sub(r"[^a-z0-9]+", "-", tag.lower()).strip("-")


def generate_operation_id(route: APIRoute) -> str:
    """Stable, human-readable ``operationId``: ``<tag-slug>_<function name>``.

    Replaces FastAPI's default (path-based) ids, which churn whenever a path
    is refactored and leak the full URL into generated clients. The tag makes
    the id sortable by API group; the function name makes it recognisable.

    Assigned to ``route.operation_id`` by the app's OpenAPI hook rather than
    through ``generate_unique_id_function``: FastAPI reuses that result as
    ``route.unique_id``, which also names the response schema field
    (``"Response_" + unique_id``) — changing it would rename schema titles.
    ``operation_id`` alone feeds the spec's ``operationId``.
    """
    if len(route.tags) != 1:
        raise ValueError(
            f"route {route.name!r} must carry exactly one tag for its "
            f"operationId, got {route.tags!r}"
        )
    return f"{_tag_slug(route.tags[0])}_{route.name}"
