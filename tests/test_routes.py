"""Route resolution: fixed paths must not be swallowed by /{id} routes declared earlier."""
import pytest
from starlette.routing import Match

from vibe_flipper import main  # noqa: F401  (registers the routes)
from vibe_flipper.web import router


def resolve(method: str, path: str) -> str:
    scope = {"type": "http", "method": method, "path": path, "root_path": ""}
    for route in router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return route.name
    raise AssertionError(f"no route for {method} {path}")


@pytest.mark.parametrize("method,path,endpoint", [
    ("POST", "/products/active", "products_set_active"),
    ("POST", "/products/new", "product_create"),
    ("GET", "/products/new", "product_new"),
    ("GET", "/products/12", "product_detail"),
    ("POST", "/products/12", "product_update"),
    ("POST", "/products/12/toggle", "product_toggle"),
    ("POST", "/products/12/delete", "product_delete"),
    ("POST", "/products/12/ranges", "product_ranges"),
    ("POST", "/settings", "settings_save"),
    ("POST", "/purge", "purge_now"),
    ("POST", "/scrape/full", "scrape_full"),
    ("POST", "/scrape", "scrape_now"),
    ("GET", "/price-changes", "price_changes_page"),
])
def test_routes_resolve(method, path, endpoint):
    assert resolve(method, path) == endpoint


def test_natural_product_sort():
    from vibe_flipper.web import natural_key
    names = ["iPad 10 (2022)", "HDD 1TB", "iPad 9 (2021)", "HDD 500GB", "HDD 10TB", "RTX 4060", "RTX 3060 Ti",
             "RTX 3060", "airPods Pro 2"]
    assert sorted(names, key=natural_key) == ["airPods Pro 2", "HDD 500GB", "HDD 1TB", "HDD 10TB", "iPad 9 (2021)",
                                              "iPad 10 (2022)", "RTX 3060", "RTX 3060 Ti", "RTX 4060"]


def _request(query: str = "", cookie: str = ""):
    from starlette.requests import Request
    headers = [(b"cookie", cookie.encode())] if cookie else []
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": query.encode(),
                    "headers": headers, "root_path": ""})


def test_filters_are_restored_from_cookie():
    from vibe_flipper.web import _restore_filters
    r = _restore_filters(_request(cookie="f_dash=category%3DGPU%26days%3D30"), "f_dash")
    assert r.status_code == 303 and r.headers["location"] == "/?category=GPU&days=30"
    assert _restore_filters(_request("days=7"), "f_dash") is None  # explicit filters win
    assert _restore_filters(_request(), "f_dash") is None  # nothing saved


def test_filters_are_remembered_without_page():
    from starlette.responses import Response
    from vibe_flipper.web import _remember_filters
    resp = _remember_filters(_request("category=GPU&page=3&deals=true"), Response(), "f_dash")
    assert "f_dash=" in resp.headers["set-cookie"] and "page" not in resp.headers["set-cookie"]
    resp = _remember_filters(_request("reset=1"), Response(), "f_dash")
    assert 'f_dash=""' in resp.headers["set-cookie"] or "Max-Age=0" in resp.headers["set-cookie"]
