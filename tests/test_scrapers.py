from pathlib import Path

import pytest

from vibe_flipper.scrapers import insomnia, vendora, vinted
from vibe_flipper.scrapers.base import parse_price

FX = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("text,expected", [
    ("€ 1.799", 1799.0),
    ("420 €", 420.0),
    ("420 &euro;", 420.0),
    ("12,50 €", 12.5),
    ("1.234,56 €", 1234.56),
    ("57.32", 57.32),
    ("0.01 €", None),
    ("Επικοινωνία", None),
    (None, None),
])
def test_parse_price(text, expected):
    assert parse_price(text) == expected


def test_insomnia_parser():
    items = insomnia.parse_listings(read("insomnia_recent.html"))
    assert len(items) == 16
    assert all(i.external_id.isdigit() and i.url.startswith("https://www.insomnia.gr/classifieds/item/") for i in items)
    assert all(i.title for i in items)
    wanted = [i for i in items if i.is_wanted_ad]
    assert wanted and all(i.price is None or i.price > 0 for i in wanted)
    assert any(i.price == 450.0 and "PlayStation 5" in i.title for i in items)
    assert all(i.image_url is None or i.image_url.startswith("https:") for i in items)


def test_vendora_parser():
    items = vendora.parse_listings(read("vendora_ajax_q.html"))
    assert len(items) == 36
    assert len({i.external_id for i in items}) == 36
    by_title = {i.title: i for i in items}
    assert by_title["NVIDIA GeForce RTX 3070 Founders Edition κάρτα γραφικών 8GB"].price == 277.0
    assert any(i.price == 1799.0 for i in items)  # thousands separator


def test_vinted_parser():
    items = vinted.parse_listings(read("vinted_rtx3070.html"))
    assert len(items) >= 90
    it = items[0]
    assert it.url.startswith("https://www.vinted.gr/items/")
    assert it.price and it.buy_cost and it.buy_cost >= it.price
    assert any(i.raw_condition for i in items)


def test_vinted_catalog_url_has_category():
    assert "catalog%5B%5D=2994" in vinted.catalog_url("ps5")


def test_insomnia_search_parser():
    items = insomnia.parse_search(read("insomnia_search.html"))
    assert len(items) == 25
    assert all(i.external_id.isdigit() and i.title for i in items)
    assert any("3070" in i.title and i.price for i in items)
    assert all(i.posted_at is not None for i in items)
    assert any(i.description for i in items)
    assert {i.raw_condition for i in items} <= {"Καινουργιο", "Μεταχειρισμενο", None}


def test_insomnia_search_url_is_sale_only_newest():
    url = insomnia.search_url("rtx 3070")
    assert "sortby=newest" in url and "filter=classifieds_type_1" in url and "q=rtx+3070" in url


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, data=None, fail=False):
        self.data, self.fail, self.urls = data, fail, []

    def get(self, url, **kw):
        self.urls.append(url)
        if self.fail:
            raise RuntimeError("boom")
        return _FakeResp(self.data)


def test_vinted_lookup_user():
    c = _FakeClient({"user": {"id": 1, "country_iso_code": "gr", "city": "Thessaloniki"}})
    assert vinted.lookup_user(c, "1") == ("GR", "Thessaloniki")
    assert c.urls == ["https://www.vinted.gr/api/v2/users/1"]
    assert vinted.lookup_user(_FakeClient({"user": {"country_code": "RO", "city": ""}}), "2") == ("RO", None)
    assert vinted.lookup_user(_FakeClient(fail=True), "3") is None  # retried on a later run


def test_greek_title_detection():
    from vibe_flipper.jobs import _GREEK
    assert _GREEK.search("PS5 σαν καινούργιο")
    assert _GREEK.search("ΠΩΛΕΙΤΑΙ iPhone")
    assert not _GREEK.search("Ps5 + 7 jocuri")
    assert not _GREEK.search("PS5 Slim 1TB")
