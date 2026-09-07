import uuid
from pathlib import Path

from backend.app.config import get_settings

settings = get_settings()


class LocalDiskStorage:
    """Default StorageBackend for dev/personal use. storage_uri is just the
    filename; served back out via the /media/<filename> route in main.py."""

    def __init__(self, base_dir: str | None = None):
        self.base_dir = Path(base_dir or settings.local_storage_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, data: bytes, filename: str) -> str:
        # Namespace with a uuid prefix so two content items' posters never collide.
        unique_name = f"{uuid.uuid4().hex}_{filename}"
        path = self.base_dir / unique_name
        path.write_bytes(data)
        return unique_name

    def url_for(self, storage_uri: str) -> str:
        return f"/media/{storage_uri}"

    def load(self, storage_uri: str) -> bytes:
        return (self.base_dir / storage_uri).read_bytes()


def get_storage_backend() -> LocalDiskStorage:
    # Only backend implemented so far - STORAGE_BACKEND=gcs is a Phase 3+ swap.
    return LocalDiskStorage()
