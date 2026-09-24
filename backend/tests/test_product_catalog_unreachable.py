"""An unreachable/down store (DNS failure, connection refused, timeout)
must degrade to [] the same way a clean HTTP error response already does -
not raise a raw httpx exception past WooCommerceError/ShopifyError and
into the board page (see routes_board.py's unconditional
`list_brand_products(brand)` call, which has no try/except of its own)."""

from types import SimpleNamespace

import httpx
import pytest

from backend.app.integrations.product_catalog import list_brand_products
from backend.app.integrations.shopify.client import ShopifyClient, ShopifyError
from backend.app.integrations.woocommerce.client import WooCommerceClient, WooCommerceError


def _raise_connect_error(*args, **kwargs):
    raise httpx.ConnectError("connection refused")


def test_woocommerce_client_wraps_network_error(monkeypatch):
    monkeypatch.setattr(httpx, "get", _raise_connect_error)
    with pytest.raises(WooCommerceError):
        WooCommerceClient("https://down-store.example").list_products()


def test_shopify_client_wraps_network_error(monkeypatch):
    monkeypatch.setattr(httpx, "get", _raise_connect_error)
    with pytest.raises(ShopifyError):
        ShopifyClient("https://down-store.example").list_products()


def test_list_brand_products_degrades_to_empty_when_store_unreachable(monkeypatch):
    monkeypatch.setattr(httpx, "get", _raise_connect_error)
    brand = SimpleNamespace(product_catalog_url="https://down-store.example")
    assert list_brand_products(brand) == []
