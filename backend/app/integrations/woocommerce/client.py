"""Client for a WooCommerce store's public Store API - the grounding
source for "Draft from Website" (routes_board.py). Unlike GitHub/Postiz/
Botsab, this needs NO credentials at all: the Store API
(/wp-json/wc/store/v1/*) is what WooCommerce itself uses to power a
site's own cart/storefront UI, so it's served publicly and
unauthenticated by design - this is reading a public API, not scraping.
"""

import httpx

_TIMEOUT_SECONDS = 30


class WooCommerceError(Exception):
    pass


class WooCommerceClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def list_products(self, per_page: int = 50) -> list[dict]:
        response = httpx.get(
            f"{self.base_url}/wp-json/wc/store/v1/products",
            params={"per_page": per_page},
            timeout=_TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            raise WooCommerceError(f"WooCommerce Store API error {response.status_code}: {response.text}")
        return response.json()
