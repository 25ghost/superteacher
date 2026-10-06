"""Data access for ``file_assets``.

Insert/lookup helpers only — validation, storage writes and lifecycle
decisions are **service** concerns (the same Option-A split as the rest of
the marketplace). All inserts are ``flush``ed, never committed.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.file_asset import FileAsset


def get_by_id(session: Session, file_asset_id: uuid.UUID) -> FileAsset | None:
    """One file-asset row (or None)."""
    stmt = select(FileAsset).where(FileAsset.id == file_asset_id)
    return session.scalar(stmt)


def create(
    session: Session,
    *,
    uploaded_by_user_id: uuid.UUID,
    original_filename: str,
    content_type: str,
    size_bytes: int,
    checksum_sha256: str,
    storage_key: str,
    validation_status: str,
    validation_error: str | None = None,
) -> FileAsset:
    """Insert one file-asset row (flushed, not committed)."""
    asset = FileAsset(
        uploaded_by_user_id=uploaded_by_user_id,
        original_filename=original_filename,
        content_type=content_type,
        size_bytes=size_bytes,
        checksum_sha256=checksum_sha256,
        storage_key=storage_key,
        validation_status=validation_status,
        validation_error=validation_error,
    )
    session.add(asset)
    session.flush()  # assign the PK so the storage key / material can name it
    return asset
