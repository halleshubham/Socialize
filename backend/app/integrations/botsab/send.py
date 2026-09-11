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


def _latest_carousel_slides(db: Session, content_item_id) -> list[MediaAsset]:
    """All slide_index positions for the item's most recent carousel
    generation - a regenerate inserts a fresh full set of "carousel_slide"
    rows (see carousel_editor/graph.py), so this takes the newest asset per
    slide_index rather than every row ever generated, same grouping
    routes_board.py's board() view uses for the media gallery."""
    assets = (
        db.query(MediaAsset)
        .filter(MediaAsset.content_item_id == content_item_id, MediaAsset.asset_type == "carousel_slide")
        .order_by(MediaAsset.created_at.desc())
        .all()
    )
    by_index: dict[int, MediaAsset] = {}
    for asset in assets:
        by_index.setdefault(asset.slide_index, asset)
    return [by_index[i] for i in sorted(by_index)]


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
    storage = get_storage_backend()

    # Botsab's API has no multi-image/album send at all (confirmed reading
    # its own source, see client.py's module docstring) - a carousel is
    # sent as a plain sequence of separate image messages instead, each its
    # own send_image_bytes call. The full caption goes on the first slide
    # only (mirroring how a native album attaches one caption to the whole
    # set); later slides go captionless rather than repeating it N times.
    if content_item.format == "carousel":
        slides = _latest_carousel_slides(db, content_item.id)
        if not slides:
            if not caption:
                raise WhatsAppSendError("Nothing to send - no caption or media on this post.")
            return client.send_text(recipient, caption)
        try:
            last_result: dict = {}
            for i, slide in enumerate(slides):
                file_bytes = storage.load(slide.storage_uri)
                mimetype = mimetypes.guess_type(slide.storage_uri)[0] or "image/png"
                last_result = client.send_image_bytes(
                    recipient, file_bytes, slide.storage_uri, mimetype, caption=caption if i == 0 else ""
                )
            return last_result
        except BotsabError as exc:
            raise WhatsAppSendError(str(exc)) from exc

    asset = _latest_media_asset(db, content_item.id)

    try:
        if asset is None:
            if not caption:
                raise WhatsAppSendError("Nothing to send - no caption or media on this post.")
            return client.send_text(recipient, caption)

        file_bytes = storage.load(asset.storage_uri)
        default_mimetype = "image/png" if asset.asset_type == "poster" else "video/mp4"
        mimetype = mimetypes.guess_type(asset.storage_uri)[0] or default_mimetype

        if asset.asset_type == "poster":
            return client.send_image_bytes(recipient, file_bytes, asset.storage_uri, mimetype, caption=caption)
        return client.send_video_bytes(recipient, file_bytes, asset.storage_uri, mimetype, caption=caption)
    except BotsabError as exc:
        raise WhatsAppSendError(str(exc)) from exc
