"""Create the first administrator account (offline bootstrap).

There is deliberately no HTTP endpoint that creates an administrator: the
only way to mint one is this script, run by hand on the server (or a
workstation) with database access. That keeps ``role=admin`` out of every
request path.

Safety properties:

- an email that already exists (any role) is NEVER overwritten — the script
  refuses and exits non-zero, so re-running it cannot demote or reset an
  existing account;
- the password is read from a TTY (``getpass``, confirmed twice) or, for
  non-interactive use, from the ``ADMIN_PASSWORD`` environment variable. It
  is never a command-line argument (argv is world-readable in ``ps``), never
  echoed to stdout/stderr and never embedded in a message;
- the password is validated against the shared policy and stored only as an
  Argon2id PHC hash;
- the account is created active, so the CLI-created admin can log in
  immediately, and an ``admin_created`` audit row is written alongside it;
- in ``ENVIRONMENT=production`` the script refuses to run unless
  ``--allow-production`` is passed, mirroring the guard on
  ``scripts/seed_reference_data.py``.

Transaction rule (explicit exception): this script owns its own
``commit()``/``rollback()`` — same as ``scripts/cleanup_auth_sessions.py``.
There is no endpoint and no request scope to own the transaction, so the
flush-only rule for services/repositories applies and the commit happens
here, in ``main()``.

Usage (from ``backend/``):

    .venv/Scripts/python scripts/create_admin.py --email admin@school.rw
    ADMIN_PASSWORD=... .venv/Scripts/python scripts/create_admin.py --email a@b.c
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core import security  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.repositories import auth_event_repository as auth_event_repo  # noqa: E402
from app.repositories import user_repository as user_repo  # noqa: E402
from app.models.user import User  # noqa: E402

#: Non-interactive password source. Presence means "do not prompt".
ADMIN_PASSWORD_ENV = "ADMIN_PASSWORD"


class CreateAdminError(Exception):
    """The admin bootstrap request cannot be fulfilled as given."""


def create_admin(session, email: str, password: str) -> User:
    """Create an active administrator account, or raise. Never overwrites.

    Flushes the ``users`` row and its ``admin_created`` audit event; the
    caller (``main``) owns the commit — see the module docstring.

    ``auth_events.event_type`` carries no CHECK constraint, so the
    ``admin_created`` vocabulary is accepted by the schema (the deviation
    from the prompt's expectation is reported in the final report).
    """
    normalized = (email or "").strip().lower()
    if not normalized:
        raise CreateAdminError("email is required")

    # Raises PasswordPolicyError before anything is written.
    candidate = security.validate_password_policy(password)

    existing = user_repo.get_by_email(session, normalized)
    if existing is not None:
        raise CreateAdminError(
            f"refusing: an account already exists for {normalized} "
            "(this script never overwrites an existing account)"
        )

    user = user_repo.create_admin_user(
        session,
        email=normalized,
        password_hash=security.hash_password(candidate),
    )
    auth_event_repo.log_event(
        session, user_id=user.id, event_type="admin_created"
    )
    session.flush()
    return user


def _read_new_password() -> str:
    """The password, from ``ADMIN_PASSWORD`` or from the terminal.

    Never echoed, never returned inside an exception message: a mismatch or
    a missing TTY is reported as a generic error instead.
    """
    from_env = os.environ.get(ADMIN_PASSWORD_ENV)
    if from_env:
        return from_env

    try:
        first = getpass.getpass("New admin password: ")
        second = getpass.getpass("Confirm password: ")
    except (EOFError, KeyboardInterrupt) as exc:
        raise CreateAdminError(
            f"no password supplied (interactive input unavailable; set "
            f"{ADMIN_PASSWORD_ENV} for non-interactive use)"
        ) from exc
    if first != second:
        raise CreateAdminError("passwords do not match")
    return first


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create the first administrator account. The password is read "
            f"from the terminal or from {ADMIN_PASSWORD_ENV}; it is never "
            "accepted as an argument."
        ),
    )
    parser.add_argument(
        "--email", required=True, help="email address for the admin account"
    )
    parser.add_argument(
        "--allow-production",
        action="store_true",
        help=(
            "Required to run when ENVIRONMENT=production (the guard "
            "refuses otherwise)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    settings = get_settings()
    if (
        settings.ENVIRONMENT.strip().lower() == "production"
        and not args.allow_production
    ):
        print(
            "ERROR: refusing to create an admin while "
            f"ENVIRONMENT={settings.ENVIRONMENT}; re-run with "
            "--allow-production if this is intended.",
            file=sys.stderr,
        )
        return 1

    session = SessionLocal()
    try:
        password = _read_new_password()
        user = create_admin(session, args.email, password)
        # Explicit exception to the flush-only rule (module docstring):
        # this script owns its transaction, exactly like
        # scripts/cleanup_auth_sessions.py.
        session.commit()
        # The email is the operator's own input; the password/hash never appear.
        print(f"Administrator account created: {user.email}")
        return 0
    except (CreateAdminError, security.PasswordPolicyError) as exc:
        session.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
