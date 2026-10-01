"""Unit tests: startup configuration fail-fast (Phase B2).

Production must never boot on development defaults. The two silent-default
hazards are covered here:

- ``ENVIRONMENT`` has a class default of ``"development"``, so a deployment
  that forgets to set it would boot with development conveniences (placeholder
  secret accepted, localhost CORS, relaxed guards);
- ``SECRET_KEY`` validation used to live only inside the ``jwt_secret``
  property, i.e. it fired only when a token happened to be signed — a server
  could start, serve health checks, and fail later.

``Settings.validate_startup()`` is invoked at import time by ``app.main``,
so ``uvicorn app.main:app`` refuses to boot instead of serving insecurely.
The subprocess tests exercise that wiring for real (a clean interpreter, no
cached module state).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import Settings

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

DEV_SECRET = "change-me"


def _settings(**overrides) -> Settings:
    base = {"_env_file": None}
    base.update(overrides)
    return Settings(**base)


# --- Settings.validate_startup() ------------------------------------------------


def test_startup_refuses_when_environment_is_not_provided(monkeypatch) -> None:
    # The suite states ENVIRONMENT for itself (tests/conftest.py); a real
    # deployment that forgets it must still be refused, so remove it here.
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    with pytest.raises(RuntimeError, match="ENVIRONMENT is not set"):
        _settings().validate_startup()


def test_startup_refuses_blank_environment() -> None:
    with pytest.raises(RuntimeError, match="ENVIRONMENT"):
        _settings(ENVIRONMENT="   ").validate_startup()


def test_startup_refuses_placeholder_secret_in_production() -> None:
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        _settings(ENVIRONMENT="production", SECRET_KEY=DEV_SECRET).validate_startup()


def test_startup_refuses_empty_secret_in_production() -> None:
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        _settings(ENVIRONMENT="production", SECRET_KEY="  ").validate_startup()


def test_startup_refuses_short_secret_in_production() -> None:
    with pytest.raises(RuntimeError, match="too short"):
        _settings(ENVIRONMENT="production", SECRET_KEY="x" * 20).validate_startup()


def test_startup_refuses_wildcard_cors_in_production() -> None:
    with pytest.raises(RuntimeError, match="wildcard"):
        _settings(
            ENVIRONMENT="production",
            SECRET_KEY="y" * 64,
            CORS_ORIGINS="*",
        ).validate_startup()


def test_startup_accepts_production_with_real_configuration() -> None:
    settings = _settings(
        ENVIRONMENT="production",
        SECRET_KEY="z" * 64,
        CORS_ORIGINS="https://portal.example.rw",
    )
    assert settings.validate_startup() is None


@pytest.mark.parametrize("environment", ["development", "testing"])
def test_startup_accepts_development_stages_with_default_secret(environment: str) -> None:
    settings = _settings(ENVIRONMENT=environment, SECRET_KEY=DEV_SECRET)
    assert settings.validate_startup() is None


def test_startup_treats_staging_as_a_production_grade_stage() -> None:
    """Any stage outside development/testing gets the strict secret rules."""
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        _settings(ENVIRONMENT="staging", SECRET_KEY=DEV_SECRET).validate_startup()

    settings = _settings(ENVIRONMENT="staging", SECRET_KEY="s" * 64)
    assert settings.validate_startup() is None


# --- wiring: app.main refuses to import under an unsafe configuration ----------


def _import_app_main(env: dict, cwd: Path) -> subprocess.CompletedProcess:
    """Run ``import app.main`` in a clean interpreter and report the outcome."""
    command = [
        sys.executable,
        "-c",
        "import app.main; print('IMPORTED')",
    ]
    environment = {**os.environ, **env}
    environment["PYTHONPATH"] = str(BACKEND_DIR)
    return subprocess.run(
        command,
        cwd=str(cwd),
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_app_import_refuses_production_with_placeholder_secret() -> None:
    result = _import_app_main(
        {"ENVIRONMENT": "production", "SECRET_KEY": DEV_SECRET},
        BACKEND_DIR,
    )
    assert result.returncode != 0, result.stdout
    assert "SECRET_KEY" in result.stderr, result.stderr


def test_app_import_refuses_when_environment_is_unset(tmp_path: Path) -> None:
    """No .env, no ENVIRONMENT: the app must refuse to start, not guess."""
    env = {k: v for k, v in os.environ.items() if k != "ENVIRONMENT"}
    command = [sys.executable, "-c", "import app.main; print('IMPORTED')"]
    environment = {**env, "PYTHONPATH": str(BACKEND_DIR)}
    environment.pop("SECRET_KEY", None)
    result = subprocess.run(
        command,
        cwd=str(tmp_path),  # empty dir: backend/.env is not picked up
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0, result.stdout
    assert "ENVIRONMENT" in result.stderr, result.stderr


def test_app_import_succeeds_in_development() -> None:
    """The normal development configuration (backend/.env) still boots."""
    environment = {**os.environ, "ENVIRONMENT": "development"}
    environment["PYTHONPATH"] = str(BACKEND_DIR)
    result = subprocess.run(
        [sys.executable, "-c", "import app.main; print('IMPORTED')"],
        cwd=str(BACKEND_DIR),
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "IMPORTED" in result.stdout
