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
TAG_MARKETPLACE = "Marketplace"
TAG_ADMIN = "Admin"
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
            "Student-only self-service under /me/student, "
            "/me/registrations and /me/learning-enrollments: maintain your "
            "own profile, enroll yourself in an academic year, read your "
            "own registrations, and read or leave your own marketplace "
            "learning enrollments."
        ),
    },
    {
        "name": TAG_TEACHER,
        "description": (
            "Teacher-only self-service under /me/teacher: read and update "
            "your own teacher profile, publish, list, read and amend your "
            "own teaching offerings (Phase 1), maintain the topics and "
            "lessons beneath one of those offerings (Phase 2, slice 2A), "
            "and author teaching materials — upload a validated file, "
            "submit for review, revise rejections and archive published "
            "works (Phase 2, slice 2B). Curriculum and material rows stay "
            "inside the offering's educational context; only the owning "
            "teacher may touch them. Publication itself is administrator-"
            "only."
        ),
    },
    {
        "name": TAG_MARKETPLACE,
        "description": (
            "Student-only marketplace under /marketplace: discover live "
            "teaching offerings (active offers by approved teachers) and "
            "enroll yourself into one. Enrolling twice in the same learning "
            "context requires leaving the first enrollment first."
        ),
    },
    {
        "name": TAG_ADMIN,
        "description": (
            "Administrator-only maintenance under /admin/*: student "
            "profiles and their field-level change history "
            "(/admin/students), enrollment administration "
            "(/admin/registrations), the teacher account lifecycle "
            "(/admin/teachers) — create, invite, activate, deactivate, "
            "school assignment, verification status (Phase 1) — user "
            "account administration (/admin/users): list, retrieve, "
            "activate, deactivate, change an account's role and clear a "
            "failed-login lockout, and teaching-material moderation "
            "(/admin/materials): review the pending queue, approve "
            "(publish) or reject with a recorded reason, and archive "
            "published works (Phase 2, slice 2B)."
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
