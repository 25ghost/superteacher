"""Readiness probe contract on the real v1 router (unit, no PostgreSQL).

``GET /api/v1/health/ready`` runs ``SELECT 1`` through the request-scoped
session and pins both outcomes of the contract:

- a healthy database session → 200 ``{"status": "ready"}`` for an
  anonymous caller (no auth, no rate limit decorator);
- a failing database session → 503 with the generic
  ``{"detail": "Service unavailable"}`` body, the exception logged with
  traceback server-side and never leaked to the client.
"""
from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.router import api_router
from app.core.database import Base, get_db

PATH = "/api/v1/health/ready"


class _BrokenSession:
    """A session whose query fails the way a dead database does."""

    def execute(self, *_args, **_kwargs):
        raise OperationalError(
            "SELECT 1", {}, Exception("connection refused: no route to host")
        )

    def close(self) -> None:  # pragma: no cover - mirrors Session.close
        pass


@pytest.fixture()
def api_app() -> FastAPI:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)()
    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: session
    app.include_router(api_router, prefix="/api/v1")
    try:
        yield app
    finally:
        session.close()
        engine.dispose()
        app.dependency_overrides.clear()


@pytest.fixture()
def api_client(api_app: FastAPI) -> TestClient:
    return TestClient(api_app)


def test_ready_returns_200_for_an_anonymous_caller(api_client: TestClient) -> None:
    response = api_client.get(PATH)  # deliberately no Authorization header
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ready"}


def test_ready_answers_503_and_logs_the_failure_when_the_db_is_down(
    api_app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    api_app.dependency_overrides[get_db] = lambda: _BrokenSession()
    with caplog.at_level(logging.ERROR, logger="app.api.v1.endpoints.health"):
        response = TestClient(api_app).get(PATH)

    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "Service unavailable"}

    # The exception is logged with its traceback, server-side only.
    health_errors = [
        r for r in caplog.records
        if r.name == "app.api.v1.endpoints.health"
        and r.levelno == logging.ERROR
        and r.exc_info is not None
    ]
    assert health_errors, "readiness failure must be logged with an exception"

    # ...and never reaches the client body.
    body = response.text
    for leak in ("OperationalError", "connection refused", "Traceback"):
        assert leak not in body, f"exception detail leaked to client: {body}"
