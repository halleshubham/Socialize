"""Single entry point for the "Website / Products" content source -
callers (routes_board.py, routes_brand_kit.py) import from here, not
directly from integrations/woocommerce or integrations/shopify, so the
platform each brand's store_url actually is stays an implementation
detail. One URL field on Brand Kit, not a platform picker: tries
WooCommerce's Store API first (this app's original platform, so an
existing brand's behavior is completely unchanged - it succeeds before
Shopify is ever tried), falls back to Shopify's /products.json if that
comes back empty. A store that's neither just returns [], same as
before this existed for a WooCommerce-only, wrong, or unreachable URL.

build_grounding_text/primary_image_url stay defined once, in
woocommerce/source.py, and operate on the one shared normalized product
shape both platforms produce - re-exported here so callers only need this
one import.
"""

from backend.app.db.models import BrandKit
from backend.app.integrations.shopify.source import list_shopify_products
from backend.app.integrations.woocommerce.source import build_grounding_text, primary_image_url
from backend.app.integrations.woocommerce.source import list_brand_products as _list_woocommerce_products

__all__ = ["list_brand_products", "build_grounding_text", "primary_image_url"]


def list_brand_products(brand_kit: BrandKit | None) -> list[dict]:
    products = _list_woocommerce_products(brand_kit)
    if products:
        return products
    return list_shopify_products(brand_kit)
