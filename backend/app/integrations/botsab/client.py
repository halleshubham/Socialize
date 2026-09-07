"""Client for Botsab - a self-hosted WhatsApp API (local repo ../Botsab,
hosted for this account at settings.botsab_base_url). One instance = one
connected WhatsApp number/session, addressed by botsab_instance_id
("{userIdPrefix}_{slug}", confirmed by reading Botsab's own
backend/src/routes/instances.ts).

Endpoint/schema details below are confirmed by reading Botsab's backend
source directly (routes/messages.ts, routes/media.ts), not just its
/api-docs page. POST /media/upload originally accepted images only (16MB
cap) and only "image" sends supported fileId - video/document were url-only,
meaning Botsab's server had to fetch the file itself, which only works if
this app is publicly reachable. Since reels need to send the same way
posters do (this app calls Botsab, not the other way around), both sides
of Botsab itself were extended: /media/upload now accepts video too (100MB
cap - a stitched multi-scene reel commonly lands in the 10-50MB range), and
the video send type now accepts fileId exactly like image does. Document
sends are still url-only - not needed by anything in this app yet.

Auth is a flat `x-api-key` header (also accepted as `Authorization: Bearer
<key>` or an `x-api-key` query param, but the header is simplest).
"""

import httpx

_TIMEOUT_SECONDS = 60  # video uploads are larger than a poster PNG


class BotsabError(Exception):
    pass


class BotsabClient:
    def __init__(self, base_url: str, api_key: str, instance_id: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.instance_id = instance_id

    def upload_file(self, file_bytes: bytes, filename: str, mimetype: str) -> str:
        """Returns a fileId usable in send_image_bytes/send_video_bytes."""
        response = httpx.post(
            f"{self.base_url}/media/upload",
            headers={"x-api-key": self.api_key},
            files={"file": (filename, file_bytes, mimetype)},
            timeout=_TIMEOUT_SECONDS,
        )
        self._raise_for_status(response)
        return response.json()["fileId"]

    def send_text(self, to: str, text: str) -> dict:
        return self._send(to, {"type": "text", "text": text})

    def send_image_bytes(self, to: str, file_bytes: bytes, filename: str, mimetype: str, caption: str = "") -> dict:
        file_id = self.upload_file(file_bytes, filename, mimetype)
        return self._send(to, {"type": "image", "fileId": file_id, "caption": caption})

    def send_video_bytes(self, to: str, file_bytes: bytes, filename: str, mimetype: str, caption: str = "") -> dict:
        file_id = self.upload_file(file_bytes, filename, mimetype)
        return self._send(to, {"type": "video", "fileId": file_id, "caption": caption})

    def _send(self, to: str, payload: dict) -> dict:
        response = httpx.post(
            f"{self.base_url}/instances/{self.instance_id}/messages/send",
            headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
            json={"to": to, **payload},
            timeout=_TIMEOUT_SECONDS,
        )
        self._raise_for_status(response)
        return response.json()

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", response.text)
            except Exception:
                detail = response.text
            raise BotsabError(f"Botsab API error {response.status_code}: {detail}")
