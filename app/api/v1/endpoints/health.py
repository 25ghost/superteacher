"""Health check endpoint."""
from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
def read_health() -> dict:
    """Liveness probe: no database or external service required."""
    settings = get_settings()
    return {"status": "ok", "service": settings.APP_NAME}
