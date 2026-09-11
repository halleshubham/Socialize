"""One-time migration: uploads every existing local-disk media file to R2,
under its EXACT current storage_uri as the object key - so no MediaAsset/
BrandKit rows need updating, just STORAGE_BACKEND flipped to "r2"
afterward. Covers both MediaAsset.storage_uri and BrandKit.logo_asset_path
(the two places local_disk.py's get_storage_backend() gets used to store
something with a persistent DB reference).

Safe to re-run - each upload overwrites its own key idempotently, and a
missing/already-migrated local file is logged and skipped rather than
failing the whole run. Local files are left in place (not deleted) so
local_disk stays a working fallback until you're confident in the R2 setup.

Usage: python -m backend.scripts.migrate_media_to_r2
"""

from backend.app.config import get_settings
from backend.app.db.models import BrandKit, MediaAsset
from backend.app.db.session import SessionLocal
from backend.app.storage.local_disk import LocalDiskStorage
from backend.app.storage.r2 import R2Storage


def _upload(r2: R2Storage, local: LocalDiskStorage, storage_uri: str) -> bool:
    try:
        data = local.load(storage_uri)
    except FileNotFoundError:
        print(f"  SKIP (local file missing): {storage_uri}")
        return False
    r2._client.put_object(Bucket=r2._bucket, Key=storage_uri, Body=data)
    return True


def main() -> None:
    settings = get_settings()
    if not (settings.r2_account_id and settings.r2_access_key_id and settings.r2_bucket):
        print("R2 credentials not configured (R2_ACCOUNT_ID/R2_ACCESS_KEY_ID/R2_BUCKET) - aborting.")
        return

    r2 = R2Storage()
    local = LocalDiskStorage()

    db = SessionLocal()
    try:
        assets = db.query(MediaAsset).all()
        uploaded = skipped = 0
        for asset in assets:
            ok = _upload(r2, local, asset.storage_uri)
            uploaded += ok
            skipped += not ok
        print(f"MediaAsset: {uploaded} uploaded, {skipped} skipped (of {len(assets)})")

        brands = db.query(BrandKit).filter(BrandKit.logo_asset_path.isnot(None)).all()
        logo_uploaded = logo_skipped = 0
        for brand in brands:
            ok = _upload(r2, local, brand.logo_asset_path)
            logo_uploaded += ok
            logo_skipped += not ok
        print(f"Brand logos: {logo_uploaded} uploaded, {logo_skipped} skipped (of {len(brands)})")
    finally:
        db.close()

    print("\nDone. Local files were left in place. Once you've verified R2 is serving these "
          "correctly, set STORAGE_BACKEND=r2 and restart the app.")


if __name__ == "__main__":
    main()
