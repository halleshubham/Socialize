"""Thin wrapper over feedparser - handles both RSS 2.0 and Atom transparently
(feedparser's own job), returning a plain list of dicts rather than its
FeedParserDict entries so the rest of the app doesn't need to know
feedparser's API surface.
"""

import calendar
from datetime import datetime, timezone

import feedparser
from bs4 import BeautifulSoup


class RssError(Exception):
    pass


def _strip_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator=" ").strip()


def _published_at(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


def fetch_feed(url: str, timeout: int = 20) -> list[dict]:
    """One dict per entry: {entry_id, title, link, summary, published_at}.
    entry_id falls back to link when a feed omits <guid>/<id> - some
    minimal feeds do. Raises RssError on a malformed/unreachable feed
    (feedparser itself rarely raises - bozo=True + no entries is its way of
    signaling a real parse failure rather than just a quirky-but-usable feed)."""
    parsed = feedparser.parse(url, request_headers={"User-Agent": "Socialize/1.0"})
    if parsed.bozo and not parsed.entries:
        raise RssError(f"Could not parse feed: {parsed.get('bozo_exception', 'unknown error')}")

    entries = []
    for entry in parsed.entries:
        link = entry.get("link", "")
        entry_id = entry.get("id") or link
        if not entry_id:
            continue
        entries.append(
            {
                "entry_id": entry_id,
                "title": entry.get("title", "").strip(),
                "link": link,
                "summary": _strip_html(entry.get("summary", ""))[:2000],
                "published_at": _published_at(entry),
            }
        )
    return entries
