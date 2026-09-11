"""Resolves a brand's WooCommerce client and assembles grounding text for
the product-angle-extraction step (agents/researcher/product_angles.py) -
the product-catalog equivalent of a fetched newsletter email or a GitHub
repo's README.
"""

from bs4 import BeautifulSoup

from backend.app.db.models import BrandKit
from backend.app.integrations.woocommerce.client import WooCommerceClient, WooCommerceError


def get_woocommerce_client(brand_kit: BrandKit | None) -> WooCommerceClient | None:
    if not brand_kit or not brand_kit.product_catalog_url:
        return None
    return WooCommerceClient(brand_kit.product_catalog_url)


def list_brand_products(brand_kit: BrandKit | None) -> list[dict]:
    """Live GET /products for the Brand Kit page's preview and the board's
    "Draft from Website" picker - best-effort: [] if the URL isn't set or
    the call fails (not WooCommerce, wrong URL, site down)."""
    client = get_woocommerce_client(brand_kit)
    if not client:
        return []
    try:
        return client.list_products()
    except WooCommerceError:
        return []


def _strip_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n").strip()


def build_grounding_text(product: dict) -> str:
    """name + categories + price + description, HTML stripped - real
    merchant-written marketing copy, richer grounding than a README."""
    sections = [f"## {product.get('name', '')}"]

    categories = ", ".join(c.get("name", "") for c in product.get("categories", []))
    if categories:
        sections.append(f"Categories: {categories}")

    prices = product.get("prices") or {}
    minor_unit = int(prices.get("currency_minor_unit", 2))
    price_str = prices.get("price")
    if price_str is not None:
        amount = int(price_str) / (10**minor_unit)
        symbol = prices.get("currency_symbol", "")
        sections.append(f"Price: {symbol}{amount:.2f}")

    description = _strip_html(product.get("description", ""))
    if description:
        sections.append(f"## Description\n\n{description}")

    short_description = _strip_html(product.get("short_description", ""))
    if short_description and short_description not in description:
        sections.append(f"## Short description\n\n{short_description}")

    return "\n\n".join(sections)


def primary_image_url(product: dict) -> str | None:
    images = product.get("images") or []
    return images[0]["src"] if images else None
