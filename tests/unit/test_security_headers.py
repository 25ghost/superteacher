"""Security headers and the global 500 contract (no database required).

Both live in ``app.main``: the ``_security_headers`` middleware and the
catch-all ``Exception`` handler. They are asserted here against throw-away
probe routes so the default (unit) suite proves them without PostgreSQL and
without leaving routes behind for other tests.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main_module

_PROBE_PATH = "/__security_probe"
_BOOM_PATH = "/__security_boom"


@pytest.fixture()
def probe_client():
    app = main_module.app

    def _probe() -> dict:
        return {"ok": True}

    def _boom() -> None:
        raise RuntimeError("internal detail that must never leak")

    app.add_api_route(_PROBE_PATH, _probe, methods=["GET"])
    app.add_api_route(_BOOM_PATH, _boom, methods=["GET"])
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client
    finally:
        app.router.routes[:] = [
            route
            for route in app.router.routes
            if getattr(route, "path", "") not in (_PROBE_PATH, _BOOM_PATH)
        ]


def test_security_headers_present_on_every_response(probe_client) -> None:
    response = probe_client.get(_PROBE_PATH)
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in response.headers["permissions-policy"]
    assert "microphone=()" in response.headers["permissions-policy"]


def test_hsts_is_sent_only_in_production(probe_client, monkeypatch) -> None:
    outside = probe_client.get(_PROBE_PATH)
    assert "strict-transport-security" not in outside.headers

    monkeypatch.setattr(main_module, "_is_production", True)
    production = probe_client.get(_PROBE_PATH)
    assert (
        production.headers["strict-transport-security"]
        == "max-age=31536000; includeSubDomains"
    )

    monkeypatch.setattr(main_module, "_is_production", False)
    again = probe_client.get(_PROBE_PATH)
    assert "strict-transport-security" not in again.headers


def test_unhandled_exception_returns_a_generic_500(probe_client) -> None:
    """The catch-all handler answers with the documented envelope only.

    Note: Starlette's ``ServerErrorMiddleware`` sits outside the HTTP
    middleware stack, so a 500 built there never passes back through
    ``_security_headers`` — the header contract above is asserted on
    responses produced inside the app (2xx/4xx), which is where the task's
    "security headers present" requirement applies.
    """
    response = probe_client.get(_BOOM_PATH)
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "internal detail that must never leak" not in response.text
    assert "Traceback" not in response.text
