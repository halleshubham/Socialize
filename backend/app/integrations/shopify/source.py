"""Resolves a brand's Shopify client and normalizes its raw `/products.json`
shape into the exact same dict shape integrations/woocommerce/source.py's
build_grounding_text/primary_image_url already expect - lets both
platforms share that one grounding-text/image-picking logic unchanged,
rather than duplicating it per-platform. See integrations/product_catalog.py
for the dispatcher that decides which platform a given store URL is.
"""

from backend.app.db.models import BrandKit
from backend.app.integrations.shopify.client import ShopifyClient, ShopifyError


def get_shopify_client(brand_kit: BrandKit | None) -> ShopifyClient | None:
    if not brand_kit or not brand_kit.product_catalog_url:
        return None
    return ShopifyClient(brand_kit.product_catalog_url)


def _normalize_product(raw: dict) -> dict:
    """Maps Shopify's product shape onto WooCommerce Store API's shape:
    name/categories/prices(minor-unit integer + symbol)/description/
    short_description/images[].src. Shopify's /products.json has no
    currency code or symbol on the product itself (that's a store-level
    setting, not returned here) - rather than guess and risk showing the
    wrong currency symbol, price is included with an empty symbol; still
    shows the real number, just unprefixed."""
    categories = []
    if raw.get("product_type"):
        categories.append({"name": raw["product_type"]})
    categories.extend({"name": tag} for tag in (raw.get("tags") or []))

    variants = raw.get("variants") or []
    prices = {}
    if variants and variants[0].get("price") is not None:
        try:
            minor_units = round(float(variants[0]["price"]) * 100)
            prices = {"price": str(minor_units), "currency_minor_unit": 2, "currency_symbol": ""}
        except (TypeError, ValueError):
            pass

    return {
        "name": raw.get("title", ""),
        "categories": categories,
        "prices": prices,
        "description": raw.get("body_html", ""),
        "short_description": "",
        "images": [{"src": img["src"]} for img in (raw.get("images") or []) if img.get("src")],
    }


def list_shopify_products(brand_kit: BrandKit | None) -> list[dict]:
    """Live GET /products.json, normalized - best-effort: [] if the URL
    isn't set, isn't reachable, or isn't actually a Shopify store."""
    client = get_shopify_client(brand_kit)
    if not client:
        return []
    try:
        return [_normalize_product(p) for p in client.list_products()]
    except ShopifyError:
        return []
