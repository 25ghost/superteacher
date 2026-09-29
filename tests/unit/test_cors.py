"""Unit tests: CORS configuration (Phase 5E).

The backend must allow browser-based API consumers (e.g. a Student Portal
dev server) from explicitly configured origins only — never a wildcard —
while keeping API behaviour unchanged for non-browser callers.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.core.config import get_settings


def _client(origin: str | None = None) -> TestClient:
    headers = {"Origin": origin} if origin else {}
    return TestClient(app, headers=headers)


def test_allowed_origin_gets_cors_headers() -> None:
    origin = get_settings().cors_origins[0]
    response = _client(origin).get("/api/v1/health")
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == origin


def test_unknown_origin_gets_no_cors_headers() -> None:
    response = _client("https://evil.example.com").get("/api/v1/health")
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_preflight_allows_configured_origin_and_methods() -> None:
    origin = get_settings().cors_origins[0]
    response = _client(origin).options(
        "/api/v1/me/registrations",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == origin
    assert "POST" in response.headers.get("access-control-allow-methods", "")


def test_preflight_rejects_unknown_method_origin_combination() -> None:
    origin = get_settings().cors_origins[0]
    response = _client(origin).options(
        "/api/v1/me/registrations",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "DELETE",
        },
    )
    # DELETE is not in the allowed method list -> preflight is not approved.
    assert response.status_code == 400


def test_no_wildcard_origin_is_configured() -> None:
    settings = get_settings()
    assert "*" not in settings.cors_origins
    assert len(settings.cors_origins) > 0
