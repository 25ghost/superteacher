"""Unit tests: shared production-APPLY guard (no PostgreSQL required).

Proves the predicate/wording live in one module, that both data-loading
CLIs see the same refusal text, and that the guard stays case-insensitive.
"""
from __future__ import annotations

import pytest

from scripts.apply_guard import PRODUCTION_APPLY_REFUSAL, production_apply_refusal


def test_production_returns_shared_refusal() -> None:
    assert production_apply_refusal("production") == PRODUCTION_APPLY_REFUSAL


def test_guard_is_case_insensitive() -> None:
    assert production_apply_refusal("PRODUCTION") == PRODUCTION_APPLY_REFUSAL
    assert production_apply_refusal("Production") == PRODUCTION_APPLY_REFUSAL


@pytest.mark.parametrize(
    "environment",
    ["development", "testing", "staging", "", "not-production"],
)
def test_non_production_environments_are_not_refused(environment: str) -> None:
    assert production_apply_refusal(environment) is None


def test_seeder_prints_the_shared_refusal_wording(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The seeder's production refusal IS the shared wording, verbatim."""
    from scripts.seed_reference_data import main

    class _FakeSettings:
        ENVIRONMENT = "production"
        DB_NAME = "super_teacher_db"
        DB_HOST = "127.0.0.1"
        DB_PORT = 5432

    monkeypatch.setattr(
        "scripts.seed_reference_data.get_settings", lambda: _FakeSettings()
    )
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert PRODUCTION_APPLY_REFUSAL in out
