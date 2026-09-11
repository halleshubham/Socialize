"""Cloudflare R2 storage backend - S3-compatible, so this is just boto3's
S3 client pointed at R2's endpoint (https://<account_id>.r2.cloudflarestorage.com).
"auto" is Cloudflare's documented region_name for R2 (it doesn't have real
regions, but boto3 requires the parameter).

The bucket stays PRIVATE - url_for() hands out a short-lived presigned GET
URL rather than a permanent public link, so viewing generated media still
requires having gone through this app's own auth-gated /media route first
(same property LocalDiskStorage already has), not just knowing a URL.
Presigned-URL generation is pure local signing (no network round-trip to
R2), so this adds no real latency to a page render.
"""

import uuid

import boto3

from backend.app.config import get_settings

settings = get_settings()


class R2Storage:
    def __init__(self):
        self._bucket = settings.r2_bucket
        self._expiry = settings.r2_presigned_url_expiry_seconds
        self._client = boto3.client(
            "s3",
            endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name="auto",
        )

    def save(self, data: bytes, filename: str) -> str:
        # Same uuid-prefix collision-avoidance convention as LocalDiskStorage.
        unique_name = f"{uuid.uuid4().hex}_{filename}"
        self._client.put_object(Bucket=self._bucket, Key=unique_name, Body=data)
        return unique_name

    def url_for(self, storage_uri: str) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": storage_uri},
            ExpiresIn=self._expiry,
        )

    def load(self, storage_uri: str) -> bytes:
        return self._client.get_object(Bucket=self._bucket, Key=storage_uri)["Body"].read()

    def delete(self, storage_uri: str) -> None:
        # delete_object is idempotent on R2/S3 (no error if the key is
        # already gone), so no existence check needed first.
        self._client.delete_object(Bucket=self._bucket, Key=storage_uri)
