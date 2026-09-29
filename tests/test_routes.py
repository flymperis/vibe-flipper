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


def test_category_tiles_toggle():
    from vibe_flipper.web import _tile_href
    qs = "sort=new&days=7"
    assert _tile_href(qs, [], "GPU") == "/?sort=new&days=7&category=GPU"                 # select
    assert _tile_href(qs, ["GPU"], "CPU") == "/?sort=new&days=7&category=GPU&category=CPU"  # add
    assert _tile_href(qs, ["GPU", "CPU"], "GPU") == "/?sort=new&days=7&category=CPU"      # deselect
    assert _tile_href(qs, ["GPU"], "GPU") == "/?sort=new&days=7"                          # back to all


def _sort_row(id, price, profit=None, margin=None, market=None, product=None, broken=False, age=0):
    from datetime import datetime, timedelta
    from types import SimpleNamespace as NS
    l = NS(id=id, price=price, is_broken=broken, is_wanted_ad=False,
           first_seen=datetime(2026, 9, 1) - timedelta(hours=age))
    pr = None if profit is None else NS(profit=profit, margin_pct=margin, market=market)
    return {"l": l, "p": NS(name=product) if product else None, "profit": pr}


def test_listing_sorts():
    from vibe_flipper.web import SORTS, sort_rows
    rows = [_sort_row(1, 300, 50, 16, 400, "RTX 3070", age=3),
            _sort_row(2, 100, 80, 80, 200, "RTX 3060", age=1),
            _sort_row(3, 200, age=2),                                       # no market price
            _sort_row(4, 50, 500, 900, 600, "RTX 3060 Ti", broken=True, age=0)]
    ids = lambda sort: [r["l"].id for r in sort_rows(rows, sort)]
    assert ids("new") == [4, 2, 3, 1] and ids("old") == [1, 3, 2, 4]
    assert ids("price") == [4, 2, 3, 1] and ids("price_desc") == [1, 3, 2, 4]
    # without a market price or broken: always last, whatever the direction
    assert ids("margin") == [2, 1, 3, 4] and ids("margin_asc") == [1, 2, 3, 4]
    assert ids("profit") == [2, 1, 3, 4] and ids("market") == [1, 2, 3, 4] and ids("market_asc") == [2, 1, 3, 4]
    assert ids("product") == [2, 4, 1, 3]                          # natural order, unmatched last
    assert ids("bogus") == ids("new")
    for sort in SORTS:
        assert sorted(ids(sort)) == [1, 2, 3, 4]


def test_sort_headers_keep_filters():
    from vibe_flipper.web import with_query
    req = _request("category=GPU&category=CPU&sort=price&page=3")
    assert with_query(req, sort="price_desc", page=None) == "?category=GPU&category=CPU&sort=price_desc"
    assert with_query(req, page=4) == "?category=GPU&category=CPU&sort=price&page=4"
