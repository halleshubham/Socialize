import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import html2text
from bs4 import BeautifulSoup


def _decode_part(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


def _walk_parts(payload: dict) -> list[dict]:
    if "parts" in payload:
        parts = []
        for p in payload["parts"]:
            parts.extend(_walk_parts(p))
        return parts
    return [payload]


def _collect_bodies(message: dict) -> tuple[list[str], list[str]]:
    """Returns (plain_chunks, html_chunks) decoded from the message's MIME parts."""
    payload = message.get("payload", {})
    plain_chunks: list[str] = []
    html_chunks: list[str] = []
    for part in _walk_parts(payload):
        mime_type = part.get("mimeType", "")
        body_data = part.get("body", {}).get("data")
        if not body_data:
            continue
        text = _decode_part(body_data)
        if mime_type == "text/plain":
            plain_chunks.append(text)
        elif mime_type == "text/html":
            html_chunks.append(text)
    return plain_chunks, html_chunks


def extract_body_text(message: dict) -> str:
    """Prefers text/plain; falls back to stripping text/html to plain text.
    For display and as a fallback - loses hyperlinks, unlike
    extract_body_with_links below."""
    plain_chunks, html_chunks = _collect_bodies(message)
    if plain_chunks:
        return "\n".join(plain_chunks).strip()
    if html_chunks:
        soup = BeautifulSoup("\n".join(html_chunks), "html.parser")
        return soup.get_text(separator="\n").strip()
    return ""


def extract_body_with_links(message: dict) -> str:
    """Markdown-ish text ("Article Title [https://...]") that preserves
    hyperlinks. Newsletters are mostly a list of article links with blurbs;
    plain .get_text() silently drops every href, which makes it impossible
    for the Researcher agent to point the Analytical agent at an actual
    article URL. Falls back to plain text (URLs already inline) when there's
    no HTML part to convert."""
    plain_chunks, html_chunks = _collect_bodies(message)
    if not html_chunks:
        return "\n".join(plain_chunks).strip()

    converter = html2text.HTML2Text()
    converter.ignore_images = True
    converter.ignore_emphasis = True
    converter.body_width = 0  # don't hard-wrap lines mid-URL
    return converter.handle("\n".join(html_chunks)).strip()


def header(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def received_at(message: dict) -> datetime | None:
    date_header = header(message, "Date")
    if date_header:
        try:
            return parsedate_to_datetime(date_header)
        except (ValueError, TypeError):
            pass
    internal_date = message.get("internalDate")
    if internal_date:
        return datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)
    return None
