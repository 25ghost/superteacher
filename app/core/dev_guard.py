"""Development-stage guard for the remaining unauthenticated surfaces.

Phase 5G status: authentication is implemented and all student profile and
registration operations are authenticated. Ordinary student operations are
no longer protected by "development stage" warnings — they are protected by
real credentials (``get_current_user`` / ``get_current_student``) in every
environment.

What still uses this guard:

- nothing, today. The dependency is retained as reusable infrastructure
  for any future surface that is deliberately unauthenticated in
  development stages only (e.g. a diagnostics probe). Guarded endpoints
  refuse with 503 outside development/testing.
"""
from __future__ import annotations

from fastapi import HTTPException, status

from app.core.config import _DEVELOPMENT_STAGE_ENVIRONMENTS, get_settings

_GUARD_DETAIL = (
    "This endpoint is a development-stage surface and is disabled because "
    "ENVIRONMENT is not a development stage."
)


def require_development_stage() -> None:
    """FastAPI dependency: refuse guarded endpoints outside development stages.

    Raises 503 (Service Unavailable) — the surface exists, but the service
    is deliberately not offering it in this environment.
    """
    environment = get_settings().ENVIRONMENT.lower()
    if environment not in _DEVELOPMENT_STAGE_ENVIRONMENTS:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_GUARD_DETAIL)
