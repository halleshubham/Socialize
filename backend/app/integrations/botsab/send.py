"""Orchestrates sending one content_item to WhatsApp via Botsab - the
board's "Send via WhatsApp" action (routes_board.py). Picks image vs video
vs text-only automatically from whatever media the item actually has.
"""

import mimetypes

from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.db.models import BrandKit, ContentItem, MediaAsset
from backend.app.integrations.botsab.client import BotsabClient, BotsabError
from backend.app.storage.local_disk import get_storage_backend
from backend.app.util.crypto import decrypt

_CAPTION_MAX_CHARS = 4000  # WhatsApp truncates far earlier than this in practice, but Botsab/Baileys don't enforce a hard limit themselves


class WhatsAppSendError(Exception):
    pass


def get_botsab_client(brand_kit: BrandKit | None) -> BotsabClient | None:
    """Each brand can send from its own WhatsApp number/instance
    (brand_kit.botsab_instance_id/botsab_api_key_encrypted, set on the Brand
    Kit page) - falls back to the shared BOTSAB_API_KEY/INSTANCE_ID env vars
    when the brand hasn't set its own, same pattern as llm/user_keys.py.
    botsab_base_url stays a single shared setting either way (one Botsab
    deployment endpoint for the whole app)."""
    settings = get_settings()
    api_key = (decrypt(brand_kit.botsab_api_key_encrypted) if brand_kit and brand_kit.botsab_api_key_encrypted else None) or settings.botsab_api_key
    instance_id = (brand_kit.botsab_instance_id if brand_kit else None) or settings.botsab_instance_id
    if not (api_key and instance_id):
        return None
    return BotsabClient(settings.botsab_base_url, api_key, instance_id)


def _build_caption(content_item: ContentItem) -> str:
    caption = content_item.copy_text or ""
    if content_item.hashtags:
        caption = f"{caption}\n\n{' '.join(content_item.hashtags)}"
    return caption[:_CAPTION_MAX_CHARS]


def _latest_media_asset(db: Session, content_item_id) -> MediaAsset | None:
    return (
        db.query(MediaAsset)
        .filter(MediaAsset.content_item_id == content_item_id)
        .filter(MediaAsset.asset_type.in_(["poster", "reel"]))
        .order_by(MediaAsset.created_at.desc())
        .first()
    )


def send_content_item(db: Session, content_item: ContentItem) -> dict:
    brand_kit = db.get(BrandKit, content_item.brand_kit_id)
    client = get_botsab_client(brand_kit)
    if not client:
        raise WhatsAppSendError(
            "WhatsApp isn't configured - set it on the Brand Kit page, or BOTSAB_API_KEY/INSTANCE_ID in .env."
        )

    recipient = brand_kit.whatsapp_recipient if brand_kit else None
    if not recipient:
        raise WhatsAppSendError("Set a WhatsApp recipient on the Brand Kit page first.")

    caption = _build_caption(content_item)
    asset = _latest_media_asset(db, content_item.id)

    try:
        if asset is None:
            if not caption:
                raise WhatsAppSendError("Nothing to send - no caption or media on this post.")
            return client.send_text(recipient, caption)

        storage = get_storage_backend()
        file_bytes = storage.load(asset.storage_uri)
        default_mimetype = "image/png" if asset.asset_type == "poster" else "video/mp4"
        mimetype = mimetypes.guess_type(asset.storage_uri)[0] or default_mimetype

        if asset.asset_type == "poster":
            return client.send_image_bytes(recipient, file_bytes, asset.storage_uri, mimetype, caption=caption)
        return client.send_video_bytes(recipient, file_bytes, asset.storage_uri, mimetype, caption=caption)
    except BotsabError as exc:
        raise WhatsAppSendError(str(exc)) from exc
