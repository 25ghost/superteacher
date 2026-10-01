"""Health check endpoint."""
from fastapi import APIRouter, Request

from app.core.config import get_settings
from app.core.rate_limit import limiter

router = APIRouter(tags=["health"])

_settings = get_settings()


@router.get("/health")
@limiter.limit(_settings.RATE_LIMIT_HEALTH)
def read_health(request: Request) -> dict:
    """Liveness probe: no database or external service required.

    Rate-limited (``RATE_LIMIT_HEALTH``) so a probe storm or a client
    in a retry loop cannot pin the event loop — beyond the budget the
    answer is 429, which monitors treat as "throttled", not as "down".
    """
    settings = get_settings()
    return {"status": "ok", "service": settings.APP_NAME}
