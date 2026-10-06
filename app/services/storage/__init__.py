"""Storage backends — opaque keys in, opaque keys out.

Domain code never hard-codes a filesystem path or a public URL: a file
asset stores ``storage_key`` (e.g. ``file_assets/<uuid>``), and this
package owns how that key maps onto bytes. Swapping the local filesystem
for S3 (or any other backend) touches only this package and the settings
root — the models, services and API stay unchanged.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.core.config import get_settings


class StorageError(Exception):
    """A storage operation failed (missing key, IO error, ...)."""


class StorageBackend(Protocol):
    """Write/read/delete opaque storage keys. Never exposes public paths."""

    def save(self, storage_key: str, data: bytes, *, content_type: str) -> None:
        """Persist ``data`` under ``storage_key`` (creating parents as needed)."""

    def read(self, storage_key: str) -> bytes:
        """Return the bytes stored under ``storage_key``."""

    def delete(self, storage_key: str) -> None:
        """Remove ``storage_key``. Missing keys are not an error."""

    def exists(self, storage_key: str) -> bool:
        """Whether ``storage_key`` currently holds bytes."""


class LocalStorageBackend:
    """Dev/test backend: bytes under ``settings.STORAGE_LOCAL_ROOT``.

    Keys are relative path segments; ``..`` and absolute keys are refused
    so a malicious key can never escape the root directory.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        settings = get_settings()
        self._root = Path(root or settings.STORAGE_LOCAL_ROOT).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, storage_key: str) -> Path:
        if not storage_key or storage_key.startswith(("/", "\\")) or ".." in Path(storage_key).parts:
            raise StorageError(f"refusing unsafe storage key: {storage_key!r}")
        path = (self._root / storage_key).resolve()
        if not path.is_relative_to(self._root):
            raise StorageError(f"refusing storage key outside root: {storage_key!r}")
        return path

    def save(self, storage_key: str, data: bytes, *, content_type: str) -> None:
        path = self._resolve(storage_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def read(self, storage_key: str) -> bytes:
        path = self._resolve(storage_key)
        if not path.is_file():
            raise StorageError(f"no stored object for key {storage_key!r}")
        return path.read_bytes()

    def delete(self, storage_key: str) -> None:
        path = self._resolve(storage_key)
        if path.is_file():
            path.unlink()

    def exists(self, storage_key: str) -> bool:
        return self._resolve(storage_key).is_file()


def get_storage_backend() -> StorageBackend:
    """The process-wide storage backend (local filesystem for slice 2B)."""
    return LocalStorageBackend()
