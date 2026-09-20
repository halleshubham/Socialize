"""Client for a Shopify store's public `/products.json` endpoint - the
Shopify equivalent of WooCommerce's Store API (integrations/woocommerce/
client.py). Most Shopify storefronts expose this unauthenticated by
default (it's what themes use to build client-side product listings), so
same as WooCommerce, this needs no credentials at all - a real merchant
can disable it, which just means this returns nothing for that store
rather than erroring in a confusing way.

Confirmed live against a real store (wbroast.co.uk, "powered-by: Shopify"
response header) - returns title/body_html/vendor/product_type/tags/
variants/images per product, no auth required.
"""

import httpx

_TIMEOUT_SECONDS = 30


class ShopifyError(Exception):
    pass


class ShopifyClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def list_products(self, limit: int = 50) -> list[dict]:
        response = httpx.get(
            f"{self.base_url}/products.json",
            params={"limit": limit},
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        if response.status_code >= 400:
            raise ShopifyError(f"Shopify products.json error {response.status_code}: {response.text}")
        try:
            data = response.json()
        except ValueError as exc:
            # A non-Shopify site (or one with /products.json disabled/
            # redirected to an HTML page) returns 200 with non-JSON body -
            # a real case, not hypothetical, distinct from a clean 4xx.
            raise ShopifyError("Response wasn't valid JSON - is this actually a Shopify store?") from exc
        products = data.get("products")
        if products is None:
            raise ShopifyError("Response had no 'products' field - is this actually a Shopify store?")
        return products
