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
    ("POST", "/settings", "settings_save"),
    ("POST", "/purge", "purge_now"),
    ("POST", "/scrape/full", "scrape_full"),
    ("POST", "/scrape", "scrape_now"),
])
def test_routes_resolve(method, path, endpoint):
    assert resolve(method, path) == endpoint


def test_natural_product_sort():
    from vibe_flipper.web import natural_key
    names = ["iPad 10 (2022)", "HDD 1TB", "iPad 9 (2021)", "HDD 500GB", "HDD 10TB", "RTX 4060", "RTX 3060 Ti",
             "RTX 3060", "airPods Pro 2"]
    assert sorted(names, key=natural_key) == ["airPods Pro 2", "HDD 500GB", "HDD 1TB", "HDD 10TB", "iPad 9 (2021)",
                                              "iPad 10 (2022)", "RTX 3060", "RTX 3060 Ti", "RTX 4060"]
