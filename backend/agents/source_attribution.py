"""Shared by graphic_designer/graph.py (poster "Source: X" line) and
reel_editor/graph.py (text_card "Source: X" line) - both attribute
generated media back to the original publication."""

import re
from urllib.parse import urlparse


def platform_name(sender: str) -> str | None:
    """Extracts a clean publication name from an email "From" header, e.g.
    '"The Wire" <noreply@thewire.in>' or 'The Wire <noreply@thewire.in>' ->
    "The Wire". Falls back to the sending domain (thewire.in -> "Thewire")
    if there's no display name, since that's still more identifiable on a
    poster/reel than the raw address.

    Only used as a fallback now (see resolve_source_name below) - the
    "From" header is the newsletter/digest that delivered the article, not
    necessarily who published it (a roundup email like "Janata Weekly"
    linking to a thewire.in piece should credit The Wire, not the digest)."""
    if not sender:
        return None
    match = re.match(r'^\s*"?([^"<]+?)"?\s*<', sender)
    if match and match.group(1).strip():
        return match.group(1).strip()
    domain_match = re.search(r"@([^\s>]+)", sender)
    if domain_match:
        return domain_match.group(1).split(".")[0].capitalize()
    return sender.strip() or None


def _domain_display_name(url: str) -> str | None:
    """Best-effort human-readable name from a URL's domain, e.g.
    "https://thewire.in/rights/some-article" -> "Thewire". Same modest
    quality bar as platform_name's own domain fallback above - not a real
    publication-name lookup, just more identifiable on a poster/reel than a
    bare domain or nothing at all."""
    try:
        netloc = urlparse(url).netloc
    except ValueError:
        return None
    netloc = netloc.split("@")[-1].split(":")[0]  # strip userinfo/port if present
    if netloc.startswith("www."):
        netloc = netloc[4:]
    root = netloc.split(".")[0]
    return root.capitalize() if root else None


def resolve_source_name(article_url: str | None, sender: str | None) -> str | None:
    """The name to stamp on a poster/reel's "Source: X" line - the actual
    publication the article was published on, not the newsletter/digest
    email that happened to deliver it. Prefers article_url's domain (set by
    the Researcher's link extraction, and what analytical's article-fetch
    step itself downloads from, so it's the real destination, not a
    tracking redirect); falls back to the sending email's "From" header
    only when there's no article_url to go on (a manually-written post, or
    the article-fetch step never resolved one)."""
    if article_url:
        name = _domain_display_name(article_url)
        if name:
            return name
    return platform_name(sender) if sender else None
