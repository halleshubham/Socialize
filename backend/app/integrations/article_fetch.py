"""Fetches an article's main content from its URL using free, self-hosted
extraction (httpx + readability-lxml) rather than a paid content API. Best
effort: many sites block scrapers, sit behind a paywall, or need JS - on any
failure this returns no text and the caller (Analytical agent) falls back to
the newsletter's own blurb.
"""

import logging
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup
from readability import Document

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (compatible; SocializeBot/0.1; "
    "+https://github.com/) research agent, personal use, low volume"
)
_TIMEOUT_SECONDS = 15
_MAX_CHARS = 20000


@dataclass
class ArticleFetch:
    text: str | None
    # The URL actually reached after following redirects - many newsletter
    # links are click-tracking wrappers (Mailchimp's list-manage.com,
    # Substack, etc.), not the real article, so this is what
    # orchestrator.py's _fetch_article_node writes back onto
    # content_item.article_url: needed for source_attribution.py to credit
    # the real publication instead of a tracking domain, and for the
    # "Source: {article_url}" link Content Writer appends to captions to
    # actually be useful/clickable rather than a wrapper link.
    resolved_url: str | None


def fetch_article_text(url: str) -> ArticleFetch:
    if not url:
        return ArticleFetch(None, None)
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.info("Article fetch failed for %s: %s", url, exc)
        return ArticleFetch(None, None)

    resolved_url = str(response.url)

    try:
        doc = Document(response.text)
        content_html = doc.summary()
    except Exception as exc:  # readability can raise on malformed markup
        logger.info("Article extraction failed for %s: %s", url, exc)
        return ArticleFetch(None, resolved_url)

    text = BeautifulSoup(content_html, "html.parser").get_text(separator="\n").strip()
    return ArticleFetch(text[:_MAX_CHARS] if text else None, resolved_url)
