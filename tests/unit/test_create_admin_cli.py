"""Unit tests: the admin bootstrap CLI (in-memory SQLite, no PostgreSQL).

Covers the Slice 1 contract:

- an admin account is created with role=admin / status=active;
- the password is stored only as an Argon2id PHC hash (never plaintext);
- an existing email is NEVER overwritten (the CLI refuses, exit code 1);
- a policy-violating password creates no row;
- the password is never echoed to stdout/stderr, in any form.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core import security
from app.core.database import Base
import app.models  # noqa: F401  (registers every table)
from app.models.enums import UserRole, UserStatus
from app.models.user import User

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

_spec = importlib.util.spec_from_file_location(
    "create_admin", BACKEND_DIR / "scripts" / "create_admin.py"
)
create_admin_cli = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("create_admin", create_admin_cli)
_spec.loader.exec_module(create_admin_cli)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _find(session: Session, email: str) -> User | None:
    return session.scalar(select(User).where(func.lower(User.email) == email.lower()))


def test_creates_active_admin_with_argon2_hash(session: Session) -> None:
    user = create_admin_cli.create_admin(session, "Root@Example.COM", "correct horse battery")
    session.commit()

    assert user.role == UserRole.ADMIN.value
    assert user.status == UserStatus.ACTIVE.value
    assert user.email == "root@example.com"  # normalized to lowercase
    # Argon2id PHC string, verifiable, and plaintext nowhere in the row.
    assert user.password_hash.startswith("$argon2id$")
    assert security.verify_password("correct horse battery", user.password_hash)
    assert "correct horse battery" not in user.password_hash


def test_refuses_to_overwrite_existing_email(session: Session) -> None:
    existing = create_admin_cli.create_admin(session, "admin@example.com", "first password here")
    session.commit()
    original_hash = existing.password_hash

    with pytest.raises(create_admin_cli.CreateAdminError) as excinfo:
        create_admin_cli.create_admin(session, "ADMIN@example.com", "second password here")
    session.rollback()

    assert "refusing" in str(excinfo.value).lower()
    rows = session.scalars(select(User)).all()
    assert len(rows) == 1
    # The original password still works; the new one was never stored.
    assert rows[0].password_hash == original_hash
    assert not security.verify_password("second password here", original_hash)


def test_policy_violation_creates_no_row(session: Session) -> None:
    with pytest.raises(security.PasswordPolicyError):
        create_admin_cli.create_admin(session, "admin@example.com", "short")
    session.rollback()

    assert _find(session, "admin@example.com") is None


def test_blank_email_rejected(session: Session) -> None:
    with pytest.raises(create_admin_cli.CreateAdminError):
        create_admin_cli.create_admin(session, "   ", "a long enough password")
    session.rollback()

    assert session.scalars(select(User)).all() == []


def test_password_is_never_echoed(monkeypatch, capsys) -> None:
    """Neither the password nor its hash may reach stdout/stderr."""
    captured_passwords = ["s3cret-passphrase-value", "s3cret-passphrase-value"]

    def fake_getpass(prompt: str = "") -> str:
        return captured_passwords.pop(0)

    class _Result:
        email = "cli@example.com"
        allow_production = False

    monkeypatch.setattr(create_admin_cli.getpass, "getpass", fake_getpass)
    monkeypatch.setattr(
        create_admin_cli.argparse.ArgumentParser, "parse_args", lambda self, argv=None: _Result()
    )

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []

        def scalar(self, stmt):  # noqa: D401 - test double (no existing account)
            return None

        def add(self, obj):  # noqa: D401 - test double
            self.added.append(obj)

        def flush(self):  # noqa: D401 - test double
            pass

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    fake = FakeSession()
    monkeypatch.setattr(create_admin_cli, "SessionLocal", lambda: fake)

    exit_code = create_admin_cli.main([])

    out = capsys.readouterr()
    assert exit_code == 0
    assert "s3cret-passphrase-value" not in out.out
    assert "s3cret-passphrase-value" not in out.err
    # The script now also writes an audit row — assert against the user row.
    user_rows = [obj for obj in fake.added if isinstance(obj, User)]
    assert len(user_rows) == 1
    assert user_rows[0].password_hash not in out.out
    assert user_rows[0].password_hash not in out.err


def test_admin_created_audit_row_is_written(session: Session) -> None:
    """Creating an admin records an ``admin_created`` auth event."""
    from app.models.auth_event import AuthEvent

    user = create_admin_cli.create_admin(
        session, "audit@example.com", "a long enough password"
    )
    session.commit()

    events = session.scalars(
        select(AuthEvent).where(AuthEvent.user_id == user.id)
    ).all()
    assert [event.event_type for event in events] == ["admin_created"]
    # Audit rows store identifiers only — never password material.
    assert all(
        "a long enough password" not in (event.metadata_json or "") for event in events
    )


def test_password_preferred_from_env_without_prompting(monkeypatch) -> None:
    """ADMIN_PASSWORD is used verbatim and the TTY is never touched."""
    monkeypatch.setenv("ADMIN_PASSWORD", "env supplied passphrase")
    monkeypatch.setattr(
        create_admin_cli.getpass,
        "getpass",
        lambda prompt="": pytest.fail("getpass must not be called"),
    )
    assert (
        create_admin_cli._read_new_password() == "env supplied passphrase"
    )


def test_password_is_not_a_cli_option(capsys) -> None:
    """The password must never be accepted as an argument (argv is public)."""
    with pytest.raises(SystemExit) as excinfo:
        create_admin_cli.parse_args(
            ["--email", "cli@example.com", "--password", "secret-on-argv"]
        )
    # argparse rejects the unknown option outright — nothing is executed.
    assert excinfo.value.code != 0
    assert "--password" in capsys.readouterr().err


def test_production_refuses_without_allow_flag(monkeypatch, capsys) -> None:
    """ENVIRONMENT=production aborts before any session or prompt."""

    class _Settings:
        ENVIRONMENT = "production"

    def _no_session():  # pragma: no cover - guard must run first
        raise AssertionError("SessionLocal must not be opened in production")

    monkeypatch.setattr(create_admin_cli, "get_settings", lambda: _Settings())
    monkeypatch.setattr(create_admin_cli, "SessionLocal", _no_session)

    exit_code = create_admin_cli.main(["--email", "root@example.com"])

    out = capsys.readouterr()
    assert exit_code == 1
    assert "--allow-production" in out.err
    assert "root@example.com" not in out.out
