"""Orchestrates sending one content_item to Postiz - the board's "Send to
Postiz" action (routes_board.py). Unlike Botsab (one fixed WhatsApp
recipient), a brand can have several connected Postiz channels
(brand_kit.postiz_channels, chosen on the Brand Kit page from a live
GET /integrations list) - every send targets all of them in one batched
POST /posts call.
"""

import mimetypes
from datetime import datetime

from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.db.models import BrandKit, ContentItem, MediaAsset, PostizPost
from backend.app.integrations.postiz.client import PostizClient, PostizError
from backend.app.storage.local_disk import get_storage_backend
from backend.app.util.crypto import decrypt

# instagram/instagram-standalone need post_type or Postiz rejects the
# request (confirmed required in docs.postiz.com's create-post schema);
# everything else gets just {"__type": identifier} and Postiz applies its
# own defaults rather than this app guessing every platform's full schema.
_PLATFORM_SETTINGS_DEFAULTS = {
    "instagram": {"post_type": "post"},
    "instagram-standalone": {"post_type": "post"},
}


class PostizSendError(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def get_postiz_client(brand_kit: BrandKit | None) -> PostizClient | None:
    """Each brand can connect its own Postiz API key (brand_kit.
    postiz_api_key_encrypted, set on the Brand Kit page) - falls back to
    the shared POSTIZ_API_KEY env var when the brand hasn't set its own,
    same pattern as Botsab/LLM keys. postiz_base_url stays a single shared
    setting either way (one Postiz deployment for the whole app)."""
    settings = get_settings()
    api_key = (
        decrypt(brand_kit.postiz_api_key_encrypted) if brand_kit and brand_kit.postiz_api_key_encrypted else None
    ) or settings.postiz_api_key
    if not api_key:
        return None
    return PostizClient(settings.postiz_base_url, api_key)


def list_brand_channels(brand_kit: BrandKit | None) -> list[dict]:
    """Live GET /integrations for the Brand Kit page's channel checkboxes -
    best-effort: returns [] (rather than raising) if there's no client
    configured yet or the call fails, so the page still renders with a
    "not connected" hint instead of a 500."""
    client = get_postiz_client(brand_kit)
    if not client:
        return []
    try:
        return client.list_integrations()
    except PostizError:
        return []


def _build_caption(content_item: ContentItem) -> str:
    caption = content_item.copy_text or ""
    if content_item.hashtags:
        caption = f"{caption}\n\n{' '.join(content_item.hashtags)}"
    return caption


def _latest_media_asset(db: Session, content_item_id) -> MediaAsset | None:
    return (
        db.query(MediaAsset)
        .filter(MediaAsset.content_item_id == content_item_id)
        .filter(MediaAsset.asset_type.in_(["poster", "reel"]))
        .order_by(MediaAsset.created_at.desc())
        .first()
    )


def send_content_item(db: Session, content_item: ContentItem, schedule_at: datetime | None = None) -> list[dict]:
    brand_kit = db.get(BrandKit, content_item.brand_kit_id)
    client = get_postiz_client(brand_kit)
    if not client:
        raise PostizSendError("Postiz isn't configured - set an API key on the Brand Kit page, or POSTIZ_API_KEY in .env.")

    channels = brand_kit.postiz_channels if brand_kit else []
    if not channels:
        raise PostizSendError("No Postiz channels selected - pick at least one on the Brand Kit page first.")

    caption = _build_caption(content_item)
    if not caption:
        raise PostizSendError("Nothing to send - no caption on this post.")

    image = None
    asset = _latest_media_asset(db, content_item.id)
    try:
        if asset is not None:
            storage = get_storage_backend()
            file_bytes = storage.load(asset.storage_uri)
            default_mimetype = "image/png" if asset.asset_type == "poster" else "video/mp4"
            mimetype = mimetypes.guess_type(asset.storage_uri)[0] or default_mimetype
            # Uploaded once, the same {id, path} is reused across every
            # targeted channel's post below - no need to re-upload per channel.
            uploaded = client.upload_file(file_bytes, asset.storage_uri, mimetype)
            image = [{"id": uploaded["id"], "path": uploaded["path"]}]

        entries = []
        for channel in channels:
            identifier = channel.get("identifier", "")
            settings = {"__type": identifier, **_PLATFORM_SETTINGS_DEFAULTS.get(identifier, {})}
            value: dict = {"content": caption}
            if image:
                value["image"] = image
            entries.append({"integration": {"id": channel["id"]}, "value": [value], "settings": settings})

        results = client.create_posts(entries, schedule_at=schedule_at)
    except PostizError as exc:
        raise PostizSendError(str(exc), retry_after=exc.retry_after) from exc

    # One PostizPost row per targeted channel - results is documented as
    # [{postId, integration}] in the same order as `entries`/`channels`, but
    # matched by integration id (falling back to position) rather than
    # trusting order, in case Postiz ever reorders or drops a failed one.
    # integration has been observed as both {"id": ...} and a bare id
    # string in the wild - docs only confirm the former, so both are
    # handled rather than assuming the shape and crashing on the other.
    channels_by_id = {c["id"]: c for c in channels}
    for i, result in enumerate(results):
        raw_integration = result.get("integration") if isinstance(result, dict) else None
        if isinstance(raw_integration, dict):
            integration_id = raw_integration.get("id")
        elif isinstance(raw_integration, str):
            integration_id = raw_integration
        else:
            integration_id = None
        channel = channels_by_id.get(integration_id) or (channels[i] if i < len(channels) else {})
        db.add(
            PostizPost(
                content_item_id=content_item.id,
                postiz_post_id=str(result.get("postId", "")) if isinstance(result, dict) else "",
                channel_id=channel.get("id", ""),
                channel_name=channel.get("name", ""),
                channel_identifier=channel.get("identifier", ""),
                scheduled_at=schedule_at,
            )
        )
    db.commit()
    return results
