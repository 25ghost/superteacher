"""SuperTeacher backend — FastAPI application entry point.

This backend serves the SuperTeacher project as a whole; the Student
Registration Portal is its first module. It is fully independent from the
SuperTeacher desktop application and the other applications in this
repository.

CORS (Phase 5E/5F): browser-based API consumers call this API from a
different origin. The allowed origins come from the ``CORS_ORIGINS``
setting (comma-separated; see ``backend/.env.example``) — never a
wildcard. ``Authorization`` is now an allowed header so browser clients
can send Bearer tokens (Phase 5G).

Authentication (Phase 5G): protected endpoints (student profile writes,
registrations, /me) require a Bearer access token issued by
``/api/v1/auth/login`` or ``/api/v1/auth/register``. The Bearer scheme is
declared in OpenAPI; public endpoints (health, catalog, readiness) remain
public. Endpoints are role-split: self-service lives under ``/me/*``
(student-only routes tagged "Student Self-Service"), administration under
``/admin/*`` (admin role, tagged "Administration"), and no route serves
both roles.

Rate limiting: login, register and refresh endpoints are throttled via
slowapi to prevent brute-force and account-spam attacks. The limits are
configurable via RATE_LIMIT_LOGIN, RATE_LIMIT_REGISTER and
RATE_LIMIT_REFRESH environment variables.
"""
import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from fastapi.openapi.utils import get_openapi
from slowapi.errors import RateLimitExceeded

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.rate_limit import limiter
from app.services.auth_service import AuthError
from app.services.registration_service import RegistrationError
from app.services.student_service import ProfileError

logger = logging.getLogger(__name__)

settings = get_settings()

# M9: one dictConfig, applied once here — module import happens exactly once
# per process, so startup (uvicorn, tests, embedding) shares one log shape.
configure_logging(settings.LOG_LEVEL)

# Fail fast (Phase B2): refuse to boot when ENVIRONMENT is unset/blank or the
# JWT secret/CORS configuration is unsafe for the active stage, instead of
# failing later on the first signed token.
settings.validate_startup()

_is_production = settings.ENVIRONMENT.lower() == "production"

app = FastAPI(
    title="SuperTeacher API",
    description=(
        "SuperTeacher backend API. Currently serving the Student "
        "Registration Portal module (authenticated). Independent from the "
        "existing SuperTeacher desktop application."
    ),
    version="0.2.0",
    docs_url="/docs" if not _is_production else None,
    redoc_url="/redoc" if not _is_production else None,
    openapi_url="/openapi.json" if not _is_production else None,
)
app.state.limiter = limiter


def _rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """429 in the API's error shape (``{"detail": ...}``) with ``Retry-After``.

    slowapi's stock handler answers ``{"error": ...}`` and, with
    ``headers_enabled=False`` (our default), sends no ``Retry-After``, so
    clients cannot tell when to retry. The countdown comes from the very
    window that was exceeded (``request.state.view_rate_limit``), read
    straight from the limiter's storage.
    """
    headers: dict[str, str] | None = None
    try:
        item, keys = request.state.view_rate_limit
        reset_time, _remaining = limiter.limiter.get_window_stats(item, *keys)
        headers = {"Retry-After": str(max(1, int(reset_time - time.time())))}
    except Exception:  # noqa: BLE001 — a header must never mask the 429 itself
        logger.debug("could not compute Retry-After for %s", request.url.path)
    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded: {exc.detail}"},
        headers=headers,
    )


app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)



@app.exception_handler(AuthError)
async def _auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
    """Uncaught auth-service failure → its documented status, never a 500.

    Routes wrap their service calls in ``except AuthError`` already; this is
    the safety net for anything they miss (and for dependency-driven flows),
    mirroring the ``ProfileError`` handler below.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": str(exc)},
    )


@app.exception_handler(RegistrationError)
async def _registration_error_handler(
    request: Request, exc: RegistrationError
) -> JSONResponse:
    """Same safety net for the registration service family (400/404/409/422/503)."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": str(exc)},
    )


@app.exception_handler(ProfileError)
async def _profile_error_handler(request: Request, exc: ProfileError) -> JSONResponse:
    """Map the student-service error family onto its documented HTTP status.

    Registered for the ``ProfileError`` base class, so Starlette's MRO
    lookup routes ``ProfileNotFoundError`` (404), ``ProfileConflictError``
    (409) and ``ProfileValidationError`` (422) here too. Without this
    handler an uncaught service error (e.g. ``load_student_row`` on an
    unknown id) fell through to the generic Exception handler as a 500,
    breaking the documented error contract.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": str(exc)},
    )


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if _is_production:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    return response

# Development-stage CORS: explicit origins only (no allow_origins=["*"]).
# Authorization is allowed so authenticated browser clients can send
# Bearer tokens; credentials (cookies) remain unsupported — auth is
# token-based, not cookie-based.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Content-Type", "Accept", "Authorization"],
)


def custom_openapi() -> dict:
    """OpenAPI schema with the Bearer security scheme declared (Step 37).

    Declaring ``HTTPBearer(auto_error=False)`` as a global scheme makes the
    scheme available in /docs for manual "Authorize" use, while individual
    endpoints that depend on ``get_current_user`` advertise their own
    ``security`` requirements; public endpoints (health, catalog,
    readiness) carry none.
    """
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    openapi_schema.setdefault("components", {})["securitySchemes"] = {
        "HTTPBearer": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "JWT access token from /api/v1/auth/login or /api/v1/auth/register",
        }
    }
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi

app.include_router(api_router, prefix="/api/v1")
