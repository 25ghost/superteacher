"""Data access for the administrator book library (``books`` family).

Insert/lookup helpers only — validation, storage writes and lifecycle
decisions are **service** concerns. All inserts are ``flush``ed, never
committed (the endpoint owns the transaction, as everywhere else).
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.book import Book
from app.models.book_revision import BookRevision
from app.models.book_revision_context import BookRevisionContext


def create_book(
    session: Session,
    *,
    title: str,
    description: str | None,
    created_by_user_id: uuid.UUID,
) -> Book:
    """Insert one book row (flushed, not committed)."""
    book = Book(
        title=title,
        description=description,
        created_by_user_id=created_by_user_id,
    )
    session.add(book)
    session.flush()
    return book


def next_revision_number(session: Session, book_id: uuid.UUID) -> int:
    """The edition number a new revision of this book must carry."""
    current = session.scalar(
        select(func.max(BookRevision.revision_number)).where(
            BookRevision.book_id == book_id
        )
    )
    return (current or 0) + 1


def create_revision(
    session: Session,
    *,
    book_id: uuid.UUID,
    revision_number: int,
    file_asset_id: uuid.UUID,
    uploaded_by_user_id: uuid.UUID,
    status: str,
) -> BookRevision:
    """Insert one book-edition row (flushed, not committed)."""
    revision = BookRevision(
        book_id=book_id,
        revision_number=revision_number,
        file_asset_id=file_asset_id,
        uploaded_by_user_id=uploaded_by_user_id,
        status=status,
    )
    session.add(revision)
    session.flush()
    return revision


def create_revision_context(
    session: Session,
    *,
    book_revision_id: uuid.UUID,
    learning_context_id: uuid.UUID,
) -> BookRevisionContext:
    """Attach one canonical learning context to one revision."""
    link = BookRevisionContext(
        book_revision_id=book_revision_id,
        learning_context_id=learning_context_id,
    )
    session.add(link)
    session.flush()
    return link


def _book_with_details():
    return (
        selectinload(Book.revisions).joinedload(BookRevision.file_asset),
        selectinload(Book.revisions).selectinload(BookRevision.contexts),
    )


def get_book_by_id(session: Session, book_id: uuid.UUID) -> Book | None:
    """One book with its revisions, assets and context links (or None)."""
    stmt = (
        select(Book)
        .where(Book.id == book_id)
        .options(*_book_with_details())
    )
    return session.scalar(stmt)


def list_books(
    session: Session, *, limit: int, offset: int
) -> list[Book]:
    """Newest-first page of books, each with revisions and contexts."""
    stmt = (
        select(Book)
        .options(*_book_with_details())
        .order_by(Book.created_at.desc(), Book.id)
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt))
