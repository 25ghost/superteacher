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

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from fastapi.openapi.utils import get_openapi
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.services.student_service import ProfileError

logger = logging.getLogger(__name__)

settings = get_settings()

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
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


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
