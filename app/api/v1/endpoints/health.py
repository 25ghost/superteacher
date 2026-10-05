"""Health check endpoints (liveness and readiness)."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.v1.tags import TAG_HEALTH
from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=[TAG_HEALTH])

_settings = get_settings()


@router.get("/health", summary="Report service liveness")
@limiter.limit(_settings.RATE_LIMIT_HEALTH)
def read_health(request: Request) -> dict:
    """Liveness probe: no database or external service required.

    Rate-limited (``RATE_LIMIT_HEALTH``) so a probe storm or a client
    in a retry loop cannot pin the event loop — beyond the budget the
    answer is 429, which monitors treat as "throttled", not as "down".
    """
    settings = get_settings()
    return {"status": "ok", "service": settings.APP_NAME}


@router.get("/health/ready", summary="Report database reachability")
def read_readiness(session: Session = Depends(get_db)) -> dict:
    """Readiness probe: the process can reach the database.

    Runs ``SELECT 1`` through the same session pool every request uses.
    200 keeps the instance in the load balancer; any database failure is
    logged server-side and answered with a generic 503 so orchestrators
    route around the instance while the error never leaks to the client.

    Deliberately not rate-limited: orchestration probes come from the
    infrastructure and must never be throttled, and they are bounded by
    the orchestrator's own probe interval.
    """
    try:
        session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("readiness check failed: database unreachable")
        raise HTTPException(status_code=503, detail="Service unavailable")
    return {"status": "ready"}
