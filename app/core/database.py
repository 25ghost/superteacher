"""SQLAlchemy database layer for the SuperTeacher backend.

Provides:
- a declarative Base whose metadata carries a project-wide constraint
  naming convention (e.g. ``students_user_id_fkey``),
- the SQLAlchemy engine (URL comes from environment configuration only),
- the session factory,
- a FastAPI-compatible session dependency.

No business logic lives here. Models register their tables on
``Base.metadata``, which Alembic consumes via ``alembic/env.py``.
"""
from collections.abc import Iterator

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings

# Deterministic constraint names so migrations and diagnostics are stable.
# FK  -> students_user_id_fkey
# UQ  -> uq_users_email_key
# PK  -> users_pkey
# CK  -> uses the explicit name passed to CheckConstraint
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s_key",
    "ck": "%(constraint_name)s",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    "pk": "%(table_name)s_pkey",
}


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    pool_size=_settings.DB_POOL_SIZE,
    max_overflow=_settings.DB_MAX_OVERFLOW,
    pool_recycle=_settings.DB_POOL_RECYCLE,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yield a database session, always closing it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
