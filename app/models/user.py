"""users table — system identity with password authentication (Phase 5G)."""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import UserRole, UserStatus, sql_in_list


class User(TimestampMixin, Base):
    """An application user account.

    Roles and statuses are stored as VARCHAR guarded by CHECK constraints.
    Phase 5G adds password authentication: ``password_hash`` holds a PHC
    Argon2id string (never plaintext, never reversible). It is nullable so
    the migration can add it to existing rows; accounts without one simply
    cannot authenticate until a password is set.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    phone: Mapped[str | None] = mapped_column(String(32), unique=True)
    role: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=UserRole.STUDENT.value,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=UserStatus.ACTIVE.value,
    )
    # PHC-format Argon2id hash (argon2-cffi). Nullable: see migration 0002.
    password_hash: Mapped[str | None] = mapped_column(String(255))

    # Login lockout (Phase B, slice 7): consecutive refused attempts and
    # the instant until which the account may not authenticate. Both are
    # cleared by a successful login, by an expired window, or by an
    # administrator (POST /admin/users/{id}/unlock).
    failed_login_count: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default="0"
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(f"role IN ({sql_in_list(UserRole)})", name="users_role_check"),
        CheckConstraint(f"status IN ({sql_in_list(UserStatus)})", name="users_status_check"),
        # Case-insensitive email uniqueness (migration 0004): even a row
        # written outside the API cannot register the same address in a
        # different case. NULLs stay unrestricted (login/reset are email-
        # based; phone-only identities are refused at the API boundary).
        Index("uq_users_email_ci_key", func.lower(email), unique=True),
    )

    student = relationship("Student", back_populates="user", uselist=False)
    auth_sessions = relationship(
        "AuthSession", back_populates="user", cascade="all, delete-orphan"
    )
