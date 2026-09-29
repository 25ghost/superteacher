"""Auth session cleanup for the SuperTeacher backend.

Purges expired and long-revoked auth_sessions rows to prevent unbounded
table growth. Safe to run periodically (e.g. via cron or as a one-off
admin command).

Usage (from backend/):

    python scripts/cleanup_auth_sessions.py --dry-run    # report only, no deletes
    python scripts/cleanup_auth_sessions.py              # delete old rows

Deletions are committed in a single transaction. Active (non-expired,
non-revoked) sessions are never touched.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.repositories import auth_session_repository as session_repo  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SuperTeacher auth session cleanup (expired + revoked).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be deleted without making any changes.",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=7,
        help=(
            "Number of days after expiry/revocation before a row is safe "
            "to delete. Default: 7 (revoked sessions older than 7 days "
            "and expired sessions older than 7 days are purged)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()

    print("SuperTeacher auth session cleanup")
    print(f"Mode:      {'DRY RUN' if args.dry_run else 'LIVE'}")
    print(f"Database:  {settings.DB_NAME} on {settings.DB_HOST}:{settings.DB_PORT}")
    print(f"Retention: {args.retention_days} days after expiry/revocation")
    print()

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.retention_days)

    if args.dry_run:
        from sqlalchemy import func, select

        from app.models.auth_session import AuthSession

        session = SessionLocal()
        try:
            total = session.scalar(select(func.count()).select_from(AuthSession)) or 0
            expired = session.scalar(
                select(func.count()).select_from(AuthSession).where(
                    AuthSession.expires_at < cutoff
                )
            ) or 0
            revoked = session.scalar(
                select(func.count()).select_from(AuthSession).where(
                    AuthSession.revoked_at.isnot(None),
                    AuthSession.revoked_at < cutoff,
                )
            ) or 0
            active = total - expired - revoked
        finally:
            session.close()

        print(f"Total sessions:       {total}")
        print(f"Would delete:         {expired + revoked}")
        print(f"  Expired (old):      {expired}")
        print(f"  Revoked (old):      {revoked}")
        print(f"  Active (kept):      {active}")
        print()
        print("Dry run complete — the database was not modified.")
        return 0

    session = SessionLocal()
    try:
        deleted = session_repo.cleanup_expired(session, cutoff)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    print(f"Deleted {deleted} expired/revoked session(s) older than {cutoff.isoformat()}.")
    print("Cleanup complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
