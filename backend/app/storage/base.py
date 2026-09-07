from typing import Protocol


class StorageBackend(Protocol):
    """Where generated media (poster PNGs now, reel MP4s in Phase 3) lives.
    local_disk is the only implementation today; a gcs.py implementing the
    same protocol is a Phase 3+ swap-in (STORAGE_BACKEND=gcs), not a
    rearchitecture - callers only ever see save()/url_for()."""

    def save(self, data: bytes, filename: str) -> str:
        """Persists data, returns a storage_uri to keep on the MediaAsset row."""
        ...

    def url_for(self, storage_uri: str) -> str:
        """Turns a stored storage_uri into a URL the browser can load."""
        ...
