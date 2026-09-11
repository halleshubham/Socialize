from typing import Protocol


class StorageBackend(Protocol):
    """Where generated media (posters, reels, carousel slides) lives -
    local_disk.py's LocalDiskStorage (dev/personal default) or r2.py's
    R2Storage (STORAGE_BACKEND=r2, Cloudflare R2), selected by
    local_disk.py's get_storage_backend() factory. Callers never construct
    a backend directly or branch on which one is active."""

    def save(self, data: bytes, filename: str) -> str:
        """Persists data, returns a storage_uri to keep on the MediaAsset row."""
        ...

    def url_for(self, storage_uri: str) -> str:
        """Turns a stored storage_uri into a URL the browser can load."""
        ...

    def load(self, storage_uri: str) -> bytes:
        """Reads back previously-saved data - reference images, retries, sends."""
        ...

    def delete(self, storage_uri: str) -> None:
        """Permanently removes previously-saved data (discarded-item cleanup)."""
        ...
