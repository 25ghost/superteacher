"""Readiness probe integration tests (PostgreSQL, guarded test DB).

``GET /api/v1/health/ready`` against the real application and the real
database:

- 200 ``{"status": "ready"}`` when ``SELECT 1`` succeeds;
- 130 consecutive anonymous probes stay 200 — the route carries no
  ``@limiter`` decorator, so orchestration probes can never be throttled
  (``RATE_LIMIT_HEALTH`` is 120/minute and applies to ``/health`` only);
- a database failure answers 503 with the generic body, regardless of
  what the exception was.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

pytestmark = pytest.mark.integration

PATH = "/api/v1/health/ready"


class _BrokenSession:
    """A session whose query fails the way a dead database does."""

    def execute(self, *_args, **_kwargs):
        raise OperationalError(
            "SELECT 1", {}, Exception("connection refused: no route to host")
        )

    def close(self) -> None:  # pragma: no cover - mirrors Session.close
        pass


@pytest.fixture(scope="module")
def api_client(pg_engine):
    from app.core.database import Base, SessionLocal, get_db
    import app.models  # noqa: F401
    from app.main import app

    Base.metadata.create_all(pg_engine)

    def _override_get_db():
        db = SessionLocal(bind=pg_engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_ready_returns_200(api_client: TestClient) -> None:
    response = api_client.get(PATH)
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ready"}


def test_ready_is_never_rate_limited(api_client: TestClient) -> None:
    """130 probes (the health budget is 120/minute): none may answer 429."""
    for attempt in range(130):
        response = api_client.get(PATH)
        assert response.status_code == 200, (
            f"probe #{attempt + 1} -> {response.status_code}: {response.text}"
        )


def test_ready_answers_503_when_the_database_is_unreachable(
    api_client: TestClient,
) -> None:
    from app.core.database import get_db
    from app.main import app

    original_override = app.dependency_overrides[get_db]
    app.dependency_overrides[get_db] = lambda: _BrokenSession()
    try:
        response = api_client.get(PATH)
    finally:
        app.dependency_overrides[get_db] = original_override

    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "Service unavailable"}
    for leak in ("OperationalError", "connection refused", "Traceback"):
        assert leak not in response.text, f"exception detail leaked: {response.text}"
