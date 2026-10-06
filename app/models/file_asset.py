"""file_assets table — one uploaded file backing a teaching material.

A ``file_assets`` row is the storage-adjacent record of a single uploaded
file: where it lives in the storage backend (``storage_key`` — an opaque
reference, never a public URL or absolute filesystem path), what the client
sent (original filename, content type), how big it is, its integrity digest
(SHA-256) and how far it got through the validation pipeline.

Validation is a domain state machine, not an HTTP concern:

    upload → validating → (invalid | valid) → stored

``invalid`` is terminal (size, content type or magic-bytes refusal); the
teacher uploads a different file. ``stored`` means the bytes are in the
storage backend and the asset may back a material. No malware-scanning
infrastructure is bundled — this pipeline is the abstraction point for one.
"""
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.time_mixin import TimestampMixin
from app.models.enums import FileAssetStatus, sql_in_list


class FileAsset(TimestampMixin, Base):
    """One uploaded file, with its validation state and storage reference."""

    __tablename__ = "file_assets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    uploaded_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    validation_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=FileAssetStatus.UPLOAD.value
    )
    validation_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"validation_status IN ({sql_in_list(FileAssetStatus)})",
            name="file_assets_validation_status_check",
        ),
        CheckConstraint(
            "size_bytes >= 0",
            name="file_assets_size_bytes_check",
        ),
        Index("file_assets_uploaded_by_user_id_idx", "uploaded_by_user_id"),
    )

    uploaded_by = relationship("User", back_populates="file_assets")
    materials = relationship("Material", back_populates="file_asset")
