import logging
import uuid
from pathlib import Path

from backend.app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


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

    def delete(self, storage_uri: str) -> None:
        path = self.base_dir / storage_uri
        if path.exists():
            path.unlink()


def get_storage_backend():
    # Every call site imports get_storage_backend from here regardless of
    # which backend is actually active - keeps this the one place that
    # needs to know about STORAGE_BACKEND, rather than every caller having
    # to pick between local_disk/r2 itself. Lazy import so r2.py's boto3
    # client is only ever constructed when actually selected - local dev
    # never needs R2 credentials, network access, or even boto3 installed
    # correctly to run the app with the (default) local_disk backend.
    if settings.storage_backend == "r2":
        if not (settings.r2_account_id and settings.r2_access_key_id and settings.r2_bucket):
            # A dev/test environment with STORAGE_BACKEND=r2 copied from
            # another .env but no real R2 credentials of its own shouldn't
            # hard-fail the whole app on first save/load - falls back to
            # local_disk instead, same as if STORAGE_BACKEND were unset.
            logger.warning(
                "STORAGE_BACKEND=r2 but R2 credentials are incomplete "
                "(R2_ACCOUNT_ID/R2_ACCESS_KEY_ID/R2_BUCKET) - falling back to local_disk."
            )
            return LocalDiskStorage()
        from backend.app.storage.r2 import R2Storage

        return R2Storage()
    return LocalDiskStorage()
