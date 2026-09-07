"""Client for a self-hosted Postiz instance (this account's is at
social.shackyapps.in) - Postiz's own public REST API
(https://docs.postiz.com/public-api), confirmed against the docs directly:
self-hosted base path is "{domain}/api/public/v1", auth is the raw API key
in the Authorization header (no "Bearer " prefix), and POST /posts can
batch multiple channels' posts into one request (useful given the
documented 30 requests/hour rate limit on self-hosted instances).
"""

from datetime import datetime, timezone

import httpx

_TIMEOUT_SECONDS = 60  # video uploads are larger than a poster PNG, same reasoning as BotsabClient


class PostizError(Exception):
    pass


class PostizClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _headers(self, **extra) -> dict:
        return {"Authorization": self.api_key, **extra}

    def list_integrations(self) -> list[dict]:
        response = httpx.get(
            f"{self.base_url}/api/public/v1/integrations",
            headers=self._headers(),
            timeout=_TIMEOUT_SECONDS,
        )
        self._raise_for_status(response)
        return response.json()

    def upload_file(self, file_bytes: bytes, filename: str, mimetype: str) -> dict:
        """Returns the full upload object ({id, name, path, ...}) - both id
        and path are needed for the image entry in create_posts."""
        response = httpx.post(
            f"{self.base_url}/api/public/v1/upload",
            headers=self._headers(),
            files={"file": (filename, file_bytes, mimetype)},
            timeout=_TIMEOUT_SECONDS,
        )
        self._raise_for_status(response)
        return response.json()

    def create_posts(self, entries: list[dict], schedule_at: datetime | None = None) -> list[dict]:
        """entries: one dict per targeted channel, each already shaped as
        {"integration": {"id": ...}, "value": [...], "settings": {...}} -
        batched into a single /posts call. schedule_at=None means publish
        now; Postiz's schema requires `date` regardless (documented as
        ignored for type="now"), so the current time is sent either way."""
        payload = {
            "type": "schedule" if schedule_at else "now",
            "date": (schedule_at or datetime.now(timezone.utc)).isoformat(),
            "shortLink": False,
            "tags": [],
            "posts": entries,
        }
        response = httpx.post(
            f"{self.base_url}/api/public/v1/posts",
            headers=self._headers(**{"Content-Type": "application/json"}),
            json=payload,
            timeout=_TIMEOUT_SECONDS,
        )
        self._raise_for_status(response)
        return response.json()

    def delete_post(self, post_id: str) -> dict:
        response = httpx.delete(
            f"{self.base_url}/api/public/v1/posts/{post_id}",
            headers=self._headers(),
            timeout=_TIMEOUT_SECONDS,
        )
        self._raise_for_status(response)
        return response.json()

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise PostizError(f"Postiz API error {response.status_code}: {detail}")
